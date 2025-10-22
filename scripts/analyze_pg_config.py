#!/usr/bin/env python3
"""
PostgreSQL Configuration Analyzer

Analyzes current PostgreSQL configuration and compares against recommended settings
for bulk loading operations. Identifies suboptimal parameters that may cause bottlenecks.

Usage:
    python scripts/analyze_pg_config.py [--output=config_analysis.txt]
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import psycopg
from django.conf import settings

logger = logging.getLogger(__name__)


class PostgreSQLConfigAnalyzer:
    """Analyze PostgreSQL configuration for bulk loading optimization."""
    
    def __init__(self, output_file: str = None):
        self.output_file = output_file
        self.connection_params = {
            'host': settings.DATABASES['default']['HOST'],
            'port': settings.DATABASES['default']['PORT'],
            'dbname': settings.DATABASES['default']['NAME'],
            'user': settings.DATABASES['default']['USER'],
            'password': settings.DATABASES['default']['PASSWORD'],
        }
        
        # Recommended settings for bulk loading
        self.recommended_settings = {
            # Memory settings
            'shared_buffers': {'recommended': '25% of RAM', 'min_gb': 1, 'max_gb': 8},
            'work_mem': {'recommended': '256MB', 'min_mb': 64, 'max_mb': 1024},
            'maintenance_work_mem': {'recommended': '1GB', 'min_mb': 256, 'max_mb': 4096},
            'effective_cache_size': {'recommended': '75% of RAM', 'min_gb': 2},
            
            # Checkpoint settings
            'checkpoint_timeout': {'recommended': '15min', 'min_minutes': 5, 'max_minutes': 60},
            'max_wal_size': {'recommended': '4GB', 'min_gb': 1, 'max_gb': 16},
            'min_wal_size': {'recommended': '1GB', 'min_mb': 256, 'max_mb': 2048},
            'checkpoint_completion_target': {'recommended': '0.9', 'min': 0.5, 'max': 1.0},
            
            # WAL settings
            'wal_buffers': {'recommended': '16MB', 'min_mb': 1, 'max_mb': 64},
            'synchronous_commit': {'recommended': 'off', 'options': ['on', 'off', 'local']},
            'fsync': {'recommended': 'on', 'options': ['on', 'off']},
            'full_page_writes': {'recommended': 'off', 'options': ['on', 'off']},
            
            # Connection settings
            'max_connections': {'recommended': '200', 'min': 50, 'max': 500},
            'shared_preload_libraries': {'recommended': 'pg_stat_statements', 'should_contain': ['pg_stat_statements']},
            
            # Parallel settings
            'max_parallel_workers': {'recommended': '8', 'min': 2, 'max': 16},
            'max_worker_processes': {'recommended': '8', 'min': 2, 'max': 16},
            'max_parallel_workers_per_gather': {'recommended': '4', 'min': 1, 'max': 8},
            
            # Autovacuum settings
            'autovacuum': {'recommended': 'on', 'options': ['on', 'off']},
            'autovacuum_max_workers': {'recommended': '3', 'min': 1, 'max': 6},
            'autovacuum_naptime': {'recommended': '20s', 'max_seconds': 60},
        }
    
    def get_connection(self) -> psycopg.Connection:
        """Get a new database connection."""
        return psycopg.connect(**self.connection_params)
    
    def get_current_config(self) -> Dict[str, str]:
        """Get current PostgreSQL configuration."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW ALL")
                return {row[0]: row[1] for row in cur.fetchall()}
    
    def get_system_info(self) -> Dict[str, str]:
        """Get system information."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                # Get PostgreSQL version
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                
                # Get database size
                cur.execute("""
                    SELECT pg_size_pretty(pg_database_size(current_database()))
                """)
                db_size = cur.fetchone()[0]
                
                # Get shared memory info
                cur.execute("SHOW shared_memory_type")
                shared_mem_type = cur.fetchone()[0]
                
                return {
                    'version': version,
                    'database_size': db_size,
                    'shared_memory_type': shared_mem_type
                }
    
    def parse_size_to_mb(self, size_str: str) -> int:
        """Parse PostgreSQL size string to MB."""
        if not size_str:
            return 0
        
        size_str = size_str.strip().lower()
        if size_str.endswith('kb'):
            return int(float(size_str[:-2]) / 1024)
        elif size_str.endswith('mb'):
            return int(float(size_str[:-2]))
        elif size_str.endswith('gb'):
            return int(float(size_str[:-2]) * 1024)
        elif size_str.endswith('tb'):
            return int(float(size_str[:-2]) * 1024 * 1024)
        else:
            # Assume bytes
            return int(float(size_str) / (1024 * 1024))
    
    def parse_time_to_seconds(self, time_str: str) -> int:
        """Parse PostgreSQL time string to seconds."""
        if not time_str:
            return 0
        
        time_str = time_str.strip().lower()
        if time_str.endswith('s'):
            return int(float(time_str[:-1]))
        elif time_str.endswith('min'):
            return int(float(time_str[:-3]) * 60)
        elif time_str.endswith('h'):
            return int(float(time_str[:-1]) * 3600)
        elif time_str.endswith('d'):
            return int(float(time_str[:-1]) * 86400)
        else:
            # Assume seconds
            return int(float(time_str))
    
    def analyze_setting(self, setting_name: str, current_value: str) -> Dict:
        """Analyze a single configuration setting."""
        if setting_name not in self.recommended_settings:
            return {'status': 'unknown', 'message': 'No recommendation available'}
        
        recommendation = self.recommended_settings[setting_name]
        analysis = {
            'setting': setting_name,
            'current': current_value,
            'recommended': recommendation.get('recommended', 'N/A'),
            'status': 'ok',
            'message': '',
            'suggestion': ''
        }
        
        # Analyze based on setting type
        if 'min_mb' in recommendation and 'max_mb' in recommendation:
            # Size-based setting
            current_mb = self.parse_size_to_mb(current_value)
            min_mb = recommendation['min_mb']
            max_mb = recommendation['max_mb']
            
            if current_mb < min_mb:
                analysis['status'] = 'warning'
                analysis['message'] = f'Too small: {current_mb}MB < {min_mb}MB minimum'
                analysis['suggestion'] = f'Increase to at least {min_mb}MB'
            elif current_mb > max_mb:
                analysis['status'] = 'warning'
                analysis['message'] = f'Too large: {current_mb}MB > {max_mb}MB maximum'
                analysis['suggestion'] = f'Consider reducing to {max_mb}MB or less'
            else:
                analysis['status'] = 'ok'
                analysis['message'] = f'Within recommended range: {min_mb}-{max_mb}MB'
        
        elif 'min_gb' in recommendation and 'max_gb' in recommendation:
            # GB-based setting
            current_mb = self.parse_size_to_mb(current_value)
            current_gb = current_mb / 1024
            min_gb = recommendation['min_gb']
            max_gb = recommendation['max_gb']
            
            if current_gb < min_gb:
                analysis['status'] = 'warning'
                analysis['message'] = f'Too small: {current_gb:.1f}GB < {min_gb}GB minimum'
                analysis['suggestion'] = f'Increase to at least {min_gb}GB'
            elif current_gb > max_gb:
                analysis['status'] = 'warning'
                analysis['message'] = f'Too large: {current_gb:.1f}GB > {max_gb}GB maximum'
                analysis['suggestion'] = f'Consider reducing to {max_gb}GB or less'
            else:
                analysis['status'] = 'ok'
                analysis['message'] = f'Within recommended range: {min_gb}-{max_gb}GB'
        
        elif 'min_minutes' in recommendation and 'max_minutes' in recommendation:
            # Time-based setting
            current_seconds = self.parse_time_to_seconds(current_value)
            current_minutes = current_seconds / 60
            min_minutes = recommendation['min_minutes']
            max_minutes = recommendation['max_minutes']
            
            if current_minutes < min_minutes:
                analysis['status'] = 'warning'
                analysis['message'] = f'Too short: {current_minutes:.1f}min < {min_minutes}min minimum'
                analysis['suggestion'] = f'Increase to at least {min_minutes}min'
            elif current_minutes > max_minutes:
                analysis['status'] = 'warning'
                analysis['message'] = f'Too long: {current_minutes:.1f}min > {max_minutes}min maximum'
                analysis['suggestion'] = f'Consider reducing to {max_minutes}min or less'
            else:
                analysis['status'] = 'ok'
                analysis['message'] = f'Within recommended range: {min_minutes}-{max_minutes}min'
        
        elif 'options' in recommendation:
            # Enum-based setting
            if current_value.lower() not in [opt.lower() for opt in recommendation['options']]:
                analysis['status'] = 'error'
                analysis['message'] = f'Invalid value: {current_value}'
                analysis['suggestion'] = f'Use one of: {", ".join(recommendation["options"])}'
            elif current_value.lower() != recommendation['recommended'].lower():
                analysis['status'] = 'warning'
                analysis['message'] = f'Not optimal: {current_value} != {recommendation["recommended"]}'
                analysis['suggestion'] = f'Consider changing to {recommendation["recommended"]}'
            else:
                analysis['status'] = 'ok'
                analysis['message'] = f'Optimal value: {current_value}'
        
        elif 'should_contain' in recommendation:
            # String contains check
            should_contain = recommendation['should_contain']
            if not any(item in current_value for item in should_contain):
                analysis['status'] = 'warning'
                analysis['message'] = f'Missing recommended modules: {", ".join(should_contain)}'
                analysis['suggestion'] = f'Add to shared_preload_libraries: {", ".join(should_contain)}'
            else:
                analysis['status'] = 'ok'
                analysis['message'] = f'Contains recommended modules'
        
        return analysis
    
    def generate_report(self) -> str:
        """Generate configuration analysis report."""
        current_config = self.get_current_config()
        system_info = self.get_system_info()
        
        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append("PostgreSQL Configuration Analysis for Bulk Loading")
        report_lines.append("=" * 80)
        report_lines.append("")
        
        # System information
        report_lines.append("SYSTEM INFORMATION:")
        report_lines.append(f"  PostgreSQL Version: {system_info['version']}")
        report_lines.append(f"  Database Size: {system_info['database_size']}")
        report_lines.append(f"  Shared Memory Type: {system_info['shared_memory_type']}")
        report_lines.append("")
        
        # Analyze critical settings
        critical_settings = [
            'shared_buffers', 'work_mem', 'maintenance_work_mem', 'effective_cache_size',
            'checkpoint_timeout', 'max_wal_size', 'min_wal_size', 'checkpoint_completion_target',
            'wal_buffers', 'synchronous_commit', 'fsync', 'full_page_writes',
            'max_connections', 'max_parallel_workers', 'max_worker_processes',
            'autovacuum', 'autovacuum_max_workers', 'autovacuum_naptime'
        ]
        
        analyses = []
        for setting in critical_settings:
            if setting in current_config:
                analysis = self.analyze_setting(setting, current_config[setting])
                analyses.append(analysis)
        
        # Group by status
        status_groups = {'error': [], 'warning': [], 'ok': []}
        for analysis in analyses:
            status_groups[analysis['status']].append(analysis)
        
        # Report errors
        if status_groups['error']:
            report_lines.append("❌ ERRORS (Must Fix):")
            for analysis in status_groups['error']:
                report_lines.append(f"  {analysis['setting']}: {analysis['message']}")
                report_lines.append(f"    Current: {analysis['current']}")
                report_lines.append(f"    Suggestion: {analysis['suggestion']}")
                report_lines.append("")
        
        # Report warnings
        if status_groups['warning']:
            report_lines.append("⚠️  WARNINGS (Should Fix):")
            for analysis in status_groups['warning']:
                report_lines.append(f"  {analysis['setting']}: {analysis['message']}")
                report_lines.append(f"    Current: {analysis['current']}")
                report_lines.append(f"    Recommended: {analysis['recommended']}")
                report_lines.append(f"    Suggestion: {analysis['suggestion']}")
                report_lines.append("")
        
        # Report OK settings
        if status_groups['ok']:
            report_lines.append("✅ OK SETTINGS:")
            for analysis in status_groups['ok']:
                report_lines.append(f"  {analysis['setting']}: {analysis['current']} ({analysis['message']})")
            report_lines.append("")
        
        # Summary
        total_settings = len(analyses)
        error_count = len(status_groups['error'])
        warning_count = len(status_groups['warning'])
        ok_count = len(status_groups['ok'])
        
        report_lines.append("SUMMARY:")
        report_lines.append(f"  Total settings analyzed: {total_settings}")
        report_lines.append(f"  Errors: {error_count}")
        report_lines.append(f"  Warnings: {warning_count}")
        report_lines.append(f"  OK: {ok_count}")
        report_lines.append("")
        
        # Recommendations
        report_lines.append("RECOMMENDATIONS FOR BULK LOADING:")
        report_lines.append("")
        report_lines.append("1. Memory Settings:")
        report_lines.append("   - shared_buffers: 25% of RAM (1-8GB)")
        report_lines.append("   - work_mem: 256MB (for sorting/hashing)")
        report_lines.append("   - maintenance_work_mem: 1GB (for VACUUM, CREATE INDEX)")
        report_lines.append("")
        report_lines.append("2. Checkpoint Settings:")
        report_lines.append("   - checkpoint_timeout: 15min (reduce checkpoint frequency)")
        report_lines.append("   - max_wal_size: 4GB (allow more WAL before checkpoint)")
        report_lines.append("   - checkpoint_completion_target: 0.9 (spread checkpoint I/O)")
        report_lines.append("")
        report_lines.append("3. WAL Settings (RISKY - only for bulk loading):")
        report_lines.append("   - synchronous_commit: off (faster commits, risk of data loss)")
        report_lines.append("   - fsync: on (keep for safety)")
        report_lines.append("   - full_page_writes: off (faster, but requires clean shutdown)")
        report_lines.append("")
        report_lines.append("4. Connection Settings:")
        report_lines.append("   - max_connections: 200 (enough for concurrent workers)")
        report_lines.append("   - shared_preload_libraries: pg_stat_statements (for monitoring)")
        report_lines.append("")
        report_lines.append("5. Autovacuum Settings:")
        report_lines.append("   - autovacuum: on (but may slow down during bulk load)")
        report_lines.append("   - autovacuum_max_workers: 3 (reduce during bulk load)")
        report_lines.append("")
        
        return "\n".join(report_lines)
    
    def run(self):
        """Run configuration analysis."""
        try:
            report = self.generate_report()
            
            if self.output_file:
                with open(self.output_file, 'w') as f:
                    f.write(report)
                logger.info(f"Configuration analysis saved to: {self.output_file}")
            else:
                print(report)
                
        except Exception as e:
            logger.error(f"Error analyzing configuration: {e}")
            raise


def main():
    parser = argparse.ArgumentParser(description="Analyze PostgreSQL configuration for bulk loading")
    parser.add_argument("--output", help="Output file for analysis report")
    
    args = parser.parse_args()
    
    # Setup Django
    import django
    from django.conf import settings
    import os
    sys.path.insert(0, str(Path(__file__).parent.parent / "wiki_search"))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wiki_search.settings')
    django.setup()
    
    analyzer = PostgreSQLConfigAnalyzer(output_file=args.output)
    analyzer.run()


if __name__ == "__main__":
    main()

