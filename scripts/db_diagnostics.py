#!/usr/bin/env python3
"""
Database Diagnostic Script

Runs comprehensive PostgreSQL diagnostic queries to identify bottlenecks
during wiki dump ingestion.

Usage:
    python scripts/db_diagnostics.py [--output=diagnostics.txt]
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List

import psycopg
from django.conf import settings

logger = logging.getLogger(__name__)


class DatabaseDiagnostics:
    """Run comprehensive database diagnostics."""
    
    def __init__(self, output_file: str = None):
        self.output_file = output_file
        self.connection_params = {
            'host': settings.DATABASES['default']['HOST'],
            'port': settings.DATABASES['default']['PORT'],
            'dbname': settings.DATABASES['default']['NAME'],
            'user': settings.DATABASES['default']['USER'],
            'password': settings.DATABASES['default']['PASSWORD'],
        }
    
    def get_connection(self) -> psycopg.Connection:
        """Get a new database connection."""
        return psycopg.connect(**self.connection_params)
    
    def run_query(self, query: str, description: str) -> List[Dict]:
        """Run a query and return results as list of dicts."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    
    def get_active_connections(self) -> List[Dict]:
        """Get active connections and wait events."""
        query = """
            SELECT 
                pid, 
                state, 
                wait_event_type, 
                wait_event, 
                query_start,
                state_change,
                LEFT(query, 100) as query_preview
            FROM pg_stat_activity 
            WHERE datname = current_database() AND state != 'idle'
            ORDER BY query_start DESC
        """
        return self.run_query(query, "Active connections")
    
    def get_connection_summary(self) -> List[Dict]:
        """Get connection count summary by state."""
        query = """
            SELECT 
                state,
                wait_event_type,
                COUNT(*) as count
            FROM pg_stat_activity 
            WHERE datname = current_database()
            GROUP BY state, wait_event_type
            ORDER BY count DESC
        """
        return self.run_query(query, "Connection summary")
    
    def get_table_stats(self) -> List[Dict]:
        """Get table statistics for search_engine schema."""
        query = """
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
        """
        return self.run_query(query, "Table statistics")
    
    def get_index_stats(self) -> List[Dict]:
        """Get index usage statistics."""
        query = """
            SELECT 
                schemaname,
                relname as tablename,
                indexrelname as indexname,
                idx_scan,
                idx_tup_read,
                idx_tup_fetch
            FROM pg_stat_user_indexes 
            WHERE schemaname = 'search_engine'
            ORDER BY idx_scan ASC
        """
        return self.run_query(query, "Index statistics")
    
    def get_lock_contention(self) -> List[Dict]:
        """Get current lock contention."""
        query = """
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
        """
        return self.run_query(query, "Lock contention")
    
    def get_wal_stats(self) -> List[Dict]:
        """Get WAL generation statistics."""
        query = "SELECT * FROM pg_stat_wal"
        return self.run_query(query, "WAL statistics")
    
    def get_checkpoint_stats(self) -> List[Dict]:
        """Get checkpoint and background writer statistics."""
        query = "SELECT * FROM pg_stat_bgwriter"
        return self.run_query(query, "Checkpoint statistics")
    
    def get_transaction_stats(self) -> List[Dict]:
        """Get transaction statistics."""
        query = """
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
            WHERE datname = current_database()
        """
        return self.run_query(query, "Transaction statistics")
    
    def get_cache_stats(self) -> List[Dict]:
        """Get buffer cache hit ratios."""
        query = """
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
            WHERE datname = current_database()
        """
        return self.run_query(query, "Cache statistics")
    
    def get_current_config(self) -> List[Dict]:
        """Get current PostgreSQL configuration."""
        query = """
            SELECT 
                name,
                setting,
                unit,
                context,
                short_desc
            FROM pg_settings 
            WHERE name IN (
                'shared_buffers', 'work_mem', 'maintenance_work_mem', 'effective_cache_size',
                'checkpoint_timeout', 'max_wal_size', 'min_wal_size', 'checkpoint_completion_target',
                'wal_buffers', 'synchronous_commit', 'fsync', 'full_page_writes',
                'max_connections', 'max_parallel_workers', 'max_worker_processes',
                'autovacuum', 'autovacuum_max_workers', 'autovacuum_naptime'
            )
            ORDER BY name
        """
        return self.run_query(query, "Current configuration")
    
    def format_results(self, results: List[Dict], title: str) -> str:
        """Format query results as a readable string."""
        if not results:
            return f"\n{title}:\n  No data found\n"
        
        lines = [f"\n{title}:"]
        
        # Get column widths
        if results:
            columns = list(results[0].keys())
            widths = {col: len(col) for col in columns}
            for row in results:
                for col in columns:
                    widths[col] = max(widths[col], len(str(row[col])))
            
            # Create header
            header = " | ".join(col.ljust(widths[col]) for col in columns)
            lines.append(header)
            lines.append("-" * len(header))
            
            # Add rows
            for row in results:
                line = " | ".join(str(row[col]).ljust(widths[col]) for col in columns)
                lines.append(line)
        
        return "\n".join(lines)
    
    def run_all_diagnostics(self) -> str:
        """Run all diagnostic queries and return formatted report."""
        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append("PostgreSQL Database Diagnostics")
        report_lines.append("=" * 80)
        report_lines.append(f"Database: {self.connection_params['dbname']} on {self.connection_params['host']}:{self.connection_params['port']}")
        report_lines.append("")
        
        try:
            # Active connections
            active_conns = self.get_active_connections()
            report_lines.append(self.format_results(active_conns, "ACTIVE CONNECTIONS"))
            
            # Connection summary
            conn_summary = self.get_connection_summary()
            report_lines.append(self.format_results(conn_summary, "CONNECTION SUMMARY"))
            
            # Table statistics
            table_stats = self.get_table_stats()
            report_lines.append(self.format_results(table_stats, "TABLE STATISTICS"))
            
            # Index statistics
            index_stats = self.get_index_stats()
            report_lines.append(self.format_results(index_stats, "INDEX STATISTICS"))
            
            # Lock contention
            locks = self.get_lock_contention()
            if locks:
                report_lines.append(self.format_results(locks, "LOCK CONTENTION (WARNING)"))
            else:
                report_lines.append("\nLOCK CONTENTION:\n  No lock contention detected\n")
            
            # WAL statistics
            wal_stats = self.get_wal_stats()
            report_lines.append(self.format_results(wal_stats, "WAL STATISTICS"))
            
            # Checkpoint statistics
            checkpoint_stats = self.get_checkpoint_stats()
            report_lines.append(self.format_results(checkpoint_stats, "CHECKPOINT STATISTICS"))
            
            # Transaction statistics
            tx_stats = self.get_transaction_stats()
            report_lines.append(self.format_results(tx_stats, "TRANSACTION STATISTICS"))
            
            # Cache statistics
            cache_stats = self.get_cache_stats()
            report_lines.append(self.format_results(cache_stats, "CACHE STATISTICS"))
            
            # Current configuration
            config = self.get_current_config()
            report_lines.append(self.format_results(config, "CURRENT CONFIGURATION"))
            
        except Exception as e:
            report_lines.append(f"\nERROR: {e}\n")
        
        return "\n".join(report_lines)
    
    def run(self):
        """Run all diagnostics and output results."""
        try:
            report = self.run_all_diagnostics()
            
            if self.output_file:
                with open(self.output_file, 'w') as f:
                    f.write(report)
                logger.info(f"Diagnostics saved to: {self.output_file}")
            else:
                print(report)
                
        except Exception as e:
            logger.error(f"Error running diagnostics: {e}")
            raise


def main():
    parser = argparse.ArgumentParser(description="Run PostgreSQL database diagnostics")
    parser.add_argument("--output", help="Output file for diagnostics report")
    
    args = parser.parse_args()
    
    # Setup Django
    import django
    from django.conf import settings
    import os
    sys.path.insert(0, str(Path(__file__).parent.parent / "wiki_search"))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wiki_search.settings')
    django.setup()
    
    diagnostics = DatabaseDiagnostics(output_file=args.output)
    diagnostics.run()


if __name__ == "__main__":
    main()
