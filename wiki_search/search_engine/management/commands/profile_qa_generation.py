"""
Django management command to profile QA dataset generation performance.
"""

from __future__ import annotations

import cProfile
import json
import logging
import os
import pstats
import time
from pathlib import Path
from typing import Dict, List

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test.utils import override_settings

from search_engine.management.commands.generate_qa_dataset import Command as GenerateQACommand


class Command(BaseCommand):
    help = "Profile QA dataset generation performance with detailed timing and database query analysis"

    def add_arguments(self, parser):
        parser.add_argument(
            '--input',
            type=str,
            default='data/raw/hotpot_dev_fullwiki_v1.json',
            help='Path to input HotpotQA JSON file'
        )
        parser.add_argument(
            '--output-dir',
            type=str,
            default='data/processed',
            help='Directory to save output JSON files'
        )
        parser.add_argument(
            '--context-sizes',
            nargs='+',
            type=int,
            default=[8000, 32000, 128000],
            help='Context size limits in tokens (default: 8000 32000 128000)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=100,
            help='Limit number of QA entries to process (default: 100)'
        )
        parser.add_argument(
            '--workers',
            type=int,
            help='Number of worker processes (default: auto-detect)'
        )
        parser.add_argument(
            '--profile-output',
            type=str,
            default='qa_generation_profile.txt',
            help='Output file for cProfile results'
        )
        parser.add_argument(
            '--enable-profiling',
            action='store_true',
            help='Enable cProfile profiling (always enabled in this command)'
        )
        parser.add_argument(
            '--profile-db',
            action='store_true',
            help='Enable database query profiling'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Enable verbose logging'
        )

    def handle(self, *args, **options):
        # Configure logging
        log_level = logging.DEBUG if options['verbose'] else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(self.stdout),
                logging.FileHandler('profile_qa_generation.log')
            ]
        )

        logger = logging.getLogger(__name__)
        
        # Initialize profiling
        profiler = cProfile.Profile()
        start_time = time.perf_counter()
        
        # Enable database query profiling if requested
        if options['profile_db']:
            connection.queries_log.clear()
            logger.info("Database query profiling enabled")
        
        # Start profiling
        profiler.enable()
        
        try:
            # Run the actual QA generation with profiling
            self.run_qa_generation_with_profiling(options, logger)
            
        finally:
            # Stop profiling
            profiler.disable()
            end_time = time.perf_counter()
            
            # Save profiling results
            self.save_profiling_results(profiler, options['profile_output'])
            
            # Analyze database queries if enabled
            if options['profile_db']:
                self.analyze_database_queries(logger)
            
            # Log timing summary
            total_time = end_time - start_time
            logger.info(f"Total execution time: {total_time:.2f} seconds")
            
            self.stdout.write(self.style.SUCCESS("Profiling completed!"))

    def run_qa_generation_with_profiling(self, options: Dict, logger: logging.Logger):
        """Run QA generation with detailed timing breakdown."""
        
        # Import the original worker function
        from search_engine.management.commands.generate_qa_dataset import process_qa_entry_worker
        
        # Set up timing tracking
        timing_data = {
            'article_lookup_time': 0.0,
            'tokenization_time': 0.0,
            'search_time': 0.0,
            'context_calculation_time': 0.0,
            'worker_overhead_time': 0.0
        }
        
        # Create a timed wrapper for the worker function
        def timed_worker(entry_data):
            worker_start = time.perf_counter()
            
            # Track article lookup time
            lookup_start = time.perf_counter()
            result = process_qa_entry_worker(entry_data)
            lookup_end = time.perf_counter()
            
            timing_data['article_lookup_time'] += (lookup_end - lookup_start)
            
            worker_end = time.perf_counter()
            timing_data['worker_overhead_time'] += (worker_end - worker_start)
            
            return result
        
        # Monkey patch the worker function
        import search_engine.management.commands.generate_qa_dataset as qa_module
        original_worker = qa_module.process_qa_entry_worker
        qa_module.process_qa_entry_worker = timed_worker
        
        try:
            # Run the generation using the original command
            qa_command = GenerateQACommand()
            qa_command.handle(
                input=options['input'],
                output_dir=options['output_dir'],
                context_sizes=options['context_sizes'],
                limit=options['limit'],
                workers=options['workers'],
                verbose=options['verbose']
            )
        except Exception as e:
            logger.error(f"Error during QA generation: {e}")
            raise CommandError(f"QA generation failed: {e}")
        finally:
            # Restore original worker function
            qa_module.process_qa_entry_worker = original_worker
        
        # Log timing breakdown
        logger.info("=== TIMING BREAKDOWN ===")
        for phase, time_taken in timing_data.items():
            logger.info(f"{phase}: {time_taken:.2f}s")

    def save_profiling_results(self, profiler: cProfile.Profile, output_file: str):
        """Save cProfile results to file."""
        try:
            with open(output_file, 'w') as f:
                stats = pstats.Stats(profiler, stream=f)
                stats.sort_stats('cumulative')
                stats.print_stats(50)  # Top 50 functions
            
            self.stdout.write(f"Profiling results saved to: {output_file}")
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to save profiling results: {e}"))

    def analyze_database_queries(self, logger: logging.Logger):
        """Analyze database query patterns and performance."""
        queries = connection.queries
        
        if not queries:
            logger.info("No database queries recorded")
            return
        
        # Analyze query patterns
        query_counts = {}
        total_query_time = 0.0
        
        for query in queries:
            sql = query['sql']
            time_taken = float(query['time'])
            
            # Categorize queries
            if 'SELECT' in sql.upper():
                if 'Article' in sql:
                    query_type = 'Article lookup'
                elif 'TFIDFIndex' in sql:
                    query_type = 'TFIDF search'
                elif 'InvertedIndex' in sql:
                    query_type = 'Inverted index'
                else:
                    query_type = 'Other SELECT'
            else:
                query_type = 'Non-SELECT'
            
            query_counts[query_type] = query_counts.get(query_type, 0) + 1
            total_query_time += time_taken
        
        # Log analysis
        logger.info("=== DATABASE QUERY ANALYSIS ===")
        logger.info(f"Total queries: {len(queries)}")
        logger.info(f"Total query time: {total_query_time:.2f}s")
        logger.info("Query breakdown:")
        
        for query_type, count in sorted(query_counts.items()):
            logger.info(f"  {query_type}: {count} queries")
        
        # Identify potential N+1 queries
        article_queries = [q for q in queries if 'Article' in q['sql']]
        if len(article_queries) > 10:  # Arbitrary threshold
            logger.warning(f"Potential N+1 query issue: {len(article_queries)} Article queries")
