#!/usr/bin/env python3
"""
PostgreSQL Performance Monitoring Script

Captures real-time database metrics during wiki dump ingestion to identify bottlenecks.
Monitors connections, wait events, cache performance, WAL generation, and more.

Usage:
    python scripts/monitor_postgres.py [--interval=5] [--output=monitor.log]
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import psycopg
from django.conf import settings

logger = logging.getLogger(__name__)


class PostgreSQLMonitor:
    """Monitor PostgreSQL performance metrics during bulk operations."""
    
    def __init__(self, interval: int = 5, output_file: Optional[str] = None):
        self.interval = interval
        self.output_file = output_file
        self.connection_params = {
            'host': settings.DATABASES['default']['HOST'],
            'port': settings.DATABASES['default']['PORT'],
            'dbname': settings.DATABASES['default']['NAME'],
            'user': settings.DATABASES['default']['USER'],
            'password': settings.DATABASES['default']['PASSWORD'],
        }
        self.running = False
        
        # Setup logging
        if output_file:
            logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s %(levelname)s: %(message)s',
                handlers=[
                    logging.FileHandler(output_file),
                    logging.StreamHandler()
                ]
            )
        else:
            logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s %(levelname)s: %(message)s'
            )
    
    def get_connection(self) -> psycopg.Connection:
        """Get a new database connection."""
        return psycopg.connect(**self.connection_params)
    
    def get_active_connections(self) -> List[Dict]:
        """Get active connections and their wait events."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        pid, 
                        state, 
                        wait_event_type, 
                        wait_event, 
                        query_start,
                        state_change,
                        LEFT(query, 100) as query_preview
                    FROM pg_stat_activity 
                    WHERE datname = %s AND state != 'idle'
                    ORDER BY query_start DESC
                """, (self.connection_params['dbname'],))
                
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    
    def get_connection_summary(self) -> Dict:
        """Get connection count summary by state."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        state,
                        wait_event_type,
                        COUNT(*) as count
                    FROM pg_stat_activity 
                    WHERE datname = %s
                    GROUP BY state, wait_event_type
                    ORDER BY count DESC
                """, (self.connection_params['dbname'],))
                
                return {f"{row[0]}_{row[1] or 'none'}": row[2] for row in cur.fetchall()}
    
    def get_table_stats(self) -> List[Dict]:
        """Get table statistics for search_engine schema."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        relname,
                        n_tup_ins,
                        n_tup_upd,
                        n_tup_del,
                        n_live_tup,
                        n_dead_tup,
                        n_mod_since_analyze,
                        last_vacuum,
                        last_autovacuum,
                        last_analyze,
                        last_autoanalyze
                    FROM pg_stat_user_tables 
                    WHERE schemaname = 'search_engine'
                    ORDER BY n_tup_ins DESC
                """)
                
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    
    def get_index_stats(self) -> List[Dict]:
        """Get index usage statistics."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        schemaname,
                        tablename,
                        indexname,
                        idx_scan,
                        idx_tup_read,
                        idx_tup_fetch,
                        idx_blks_read,
                        idx_blks_hit
                    FROM pg_stat_user_indexes 
                    WHERE schemaname = 'search_engine'
                    ORDER BY idx_scan ASC
                """)
                
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    
    def get_cache_stats(self) -> Dict:
        """Get buffer cache hit ratios."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        'buffer_cache' as cache_type,
                        sum(blks_hit) as hits,
                        sum(blks_read) as reads,
                        CASE 
                            WHEN sum(blks_hit + blks_read) > 0 
                            THEN round(100.0 * sum(blks_hit) / sum(blks_hit + blks_read), 2)
                            ELSE 0 
                        END as hit_ratio
                    FROM pg_stat_database 
                    WHERE datname = %s
                """, (self.connection_params['dbname'],))
                
                result = cur.fetchone()
                if result:
                    return {
                        'cache_type': result[0],
                        'hits': result[1],
                        'reads': result[2],
                        'hit_ratio': result[3]
                    }
                return {}
    
    def get_wal_stats(self) -> Dict:
        """Get WAL generation statistics."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM pg_stat_wal")
                columns = [desc[0] for desc in cur.description]
                result = cur.fetchone()
                if result:
                    return dict(zip(columns, result))
                return {}
    
    def get_checkpoint_stats(self) -> Dict:
        """Get checkpoint and background writer statistics."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM pg_stat_bgwriter")
                columns = [desc[0] for desc in cur.description]
                result = cur.fetchone()
                if result:
                    return dict(zip(columns, result))
                return {}
    
    def get_lock_contention(self) -> List[Dict]:
        """Get current lock contention."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        blocked_locks.pid AS blocked_pid,
                        blocking_locks.pid AS blocking_pid,
                        blocked_activity.state AS blocked_state,
                        blocking_activity.state AS blocking_state,
                        blocked_activity.wait_event_type AS blocked_wait_type,
                        blocked_activity.wait_event AS blocked_wait_event,
                        LEFT(blocked_activity.query, 100) AS blocked_query,
                        LEFT(blocking_activity.query, 100) AS blocking_query
                    FROM pg_locks blocked_locks
                    JOIN pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
                    JOIN pg_locks blocking_locks ON blocking_locks.locktype = blocked_locks.locktype
                        AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
                        AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
                        AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
                        AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
                        AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
                        AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
                        AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
                        AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
                        AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
                        AND blocking_locks.pid != blocked_locks.pid
                    JOIN pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
                    WHERE NOT blocked_locks.granted
                """)
                
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    
    def get_transaction_stats(self) -> Dict:
        """Get transaction statistics."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        xact_commit,
                        xact_rollback,
                        blks_read,
                        blks_hit,
                        tup_returned,
                        tup_fetched,
                        tup_inserted,
                        tup_updated,
                        tup_deleted,
                        temp_files,
                        temp_bytes
                    FROM pg_stat_database 
                    WHERE datname = %s
                """, (self.connection_params['dbname'],))
                
                result = cur.fetchone()
                if result:
                    return {
                        'xact_commit': result[0],
                        'xact_rollback': result[1],
                        'blks_read': result[2],
                        'blks_hit': result[3],
                        'tup_returned': result[4],
                        'tup_fetched': result[5],
                        'tup_inserted': result[6],
                        'tup_updated': result[7],
                        'tup_deleted': result[8],
                        'temp_files': result[9],
                        'temp_bytes': result[10]
                    }
                return {}
    
    def log_metrics(self, timestamp: datetime):
        """Log all metrics at current timestamp."""
        try:
            # Connection summary
            conn_summary = self.get_connection_summary()
            logger.info(f"CONNECTIONS: {conn_summary}")
            
            # Active connections with wait events
            active_conns = self.get_active_connections()
            if active_conns:
                logger.info(f"ACTIVE_CONNECTIONS: {len(active_conns)}")
                for conn in active_conns[:5]:  # Log top 5
                    logger.info(f"  PID {conn['pid']}: {conn['state']} - {conn['wait_event_type']}/{conn['wait_event']} - {conn['query_preview']}")
            
            # Cache performance
            cache_stats = self.get_cache_stats()
            if cache_stats:
                logger.info(f"CACHE_HIT_RATIO: {cache_stats.get('hit_ratio', 0)}%")
            
            # WAL stats
            wal_stats = self.get_wal_stats()
            if wal_stats:
                logger.info(f"WAL_STATS: generated={wal_stats.get('wal_records', 0)}, fpi={wal_stats.get('wal_fpi', 0)}")
            
            # Transaction stats
            tx_stats = self.get_transaction_stats()
            if tx_stats:
                logger.info(f"TX_STATS: commits={tx_stats.get('xact_commit', 0)}, rollbacks={tx_stats.get('xact_rollback', 0)}, inserts={tx_stats.get('tup_inserted', 0)}")
            
            # Lock contention
            locks = self.get_lock_contention()
            if locks:
                logger.warning(f"LOCK_CONTENTION: {len(locks)} blocked processes")
                for lock in locks:
                    logger.warning(f"  Blocked PID {lock['blocked_pid']} by PID {lock['blocking_pid']}: {lock['blocked_wait_type']}/{lock['blocked_wait_event']}")
            
            # Table stats (every 5th iteration to avoid spam)
            if int(timestamp.timestamp()) % (self.interval * 5) == 0:
                table_stats = self.get_table_stats()
                for table in table_stats:
                    logger.info(f"TABLE {table['relname']}: live={table['n_live_tup']}, dead={table['n_dead_tup']}, inserts={table['n_tup_ins']}")
            
        except Exception as e:
            logger.error(f"Error collecting metrics: {e}")
    
    def run(self):
        """Run monitoring loop."""
        logger.info(f"Starting PostgreSQL monitoring (interval={self.interval}s)")
        logger.info(f"Database: {self.connection_params['dbname']} on {self.connection_params['host']}:{self.connection_params['port']}")
        
        self.running = True
        try:
            while self.running:
                timestamp = datetime.now()
                self.log_metrics(timestamp)
                time.sleep(self.interval)
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
        finally:
            self.running = False
    
    def stop(self):
        """Stop monitoring."""
        self.running = False


def main():
    parser = argparse.ArgumentParser(description="Monitor PostgreSQL performance during bulk operations")
    parser.add_argument("--interval", type=int, default=5, help="Monitoring interval in seconds (default: 5)")
    parser.add_argument("--output", help="Output log file (default: stdout only)")
    parser.add_argument("--once", action="store_true", help="Run once and exit (for testing)")
    
    args = parser.parse_args()
    
    # Setup Django
    import django
    from django.conf import settings
    import os
    sys.path.insert(0, str(Path(__file__).parent.parent / "wiki_search"))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wiki_search.settings')
    django.setup()
    
    monitor = PostgreSQLMonitor(interval=args.interval, output_file=args.output)
    
    if args.once:
        monitor.log_metrics(datetime.now())
    else:
        monitor.run()


if __name__ == "__main__":
    main()

