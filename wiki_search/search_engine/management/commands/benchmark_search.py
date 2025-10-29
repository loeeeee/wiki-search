"""
Django management command to benchmark search retrieval performance.
"""

from __future__ import annotations

import cProfile
import io
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pstats
from django.core.management.base import BaseCommand
from django.db import connection

from search_engine.models import Article
from search_engine.search import search_hybrid


@dataclass
class BenchmarkMetrics:
    """Metrics collected during benchmark execution."""
    
    total_searches: int
    total_time: float
    throughput: float
    avg_latency: float
    example_results: List[Dict[str, any]]


class Command(BaseCommand):
    help = "Benchmark search retrieval performance with profiling and example results"

    def add_arguments(self, parser):
        parser.add_argument(
            '--num-searches',
            type=int,
            default=1000,
            help='Number of searches to execute (default: 1000)'
        )
        parser.add_argument(
            '--profile-output',
            type=str,
            default='search_benchmark_profile.txt',
            help='Output file for cProfile results (default: search_benchmark_profile.txt)'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Enable verbose logging'
        )
        parser.add_argument(
            '--show-examples',
            action='store_true',
            default=True,
            help='Display example search results (default: True)'
        )
        parser.add_argument(
            '--no-show-examples',
            action='store_false',
            dest='show_examples',
            help='Disable example search results display'
        )

    def handle(self, *args, **options):
        # Configure logging
        log_level = logging.DEBUG if options['verbose'] else logging.INFO
        log_file = Path('benchmark_search.log')
        
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(self.stdout),
                logging.FileHandler(log_file)
            ]
        )
        
        logger = logging.getLogger(__name__)
        logger.info(f"Starting search benchmark with {options['num_searches']} searches")
        
        # Generate test queries
        logger.info("Generating test queries from article titles...")
        queries = self.generate_test_queries(options['num_searches'], logger)
        
        if not queries:
            self.stdout.write(self.style.ERROR("No queries generated. Ensure database has articles."))
            return
        
        logger.info(f"Generated {len(queries)} test queries")
        
        # Initialize profiling
        profiler = cProfile.Profile()
        start_time = time.perf_counter()
        
        # Run benchmark with profiling
        try:
            profiler.enable()
            metrics = self.run_search_benchmark(queries, options['show_examples'], logger)
            profiler.disable()
            end_time = time.perf_counter()
            
            # Calculate final metrics
            metrics.total_time = end_time - start_time
            metrics.throughput = metrics.total_searches / metrics.total_time if metrics.total_time > 0 else 0.0
            
            # Save profiling results and get summary
            profile_summary = self.save_profiling_results(profiler, options['profile_output'], logger)
            
            # Display results
            self.display_results(metrics, options['num_searches'], profile_summary, logger)
            
            self.stdout.write(self.style.SUCCESS("Benchmark completed!"))
            
        except Exception as e:
            profiler.disable()
            logger.error(f"Benchmark failed: {e}", exc_info=True)
            raise

    def generate_test_queries(self, count: int, logger: logging.Logger) -> List[str]:
        """Generate test queries by sampling random article titles."""
        total_articles = Article.objects.count()
        
        if total_articles == 0:
            logger.error("No articles found in database")
            return []
        
        # Sample random article titles
        sample_size = min(count, total_articles)
        article_ids = list(Article.objects.values_list('id', flat=True))
        
        if len(article_ids) < sample_size:
            sample_size = len(article_ids)
        
        # Random sampling without replacement
        sampled_ids = random.sample(article_ids, sample_size)
        queries = list(
            Article.objects.filter(id__in=sampled_ids)
            .values_list('title', flat=True)
        )
        
        # If we need more queries than available articles, duplicate randomly
        if len(queries) < count:
            additional_needed = count - len(queries)
            additional = random.choices(queries, k=additional_needed)
            queries.extend(additional)
        
        return queries[:count]

    def run_search_benchmark(
        self,
        queries: List[str],
        show_examples: bool,
        logger: logging.Logger
    ) -> BenchmarkMetrics:
        """Execute searches and collect metrics."""
        total_searches = len(queries)
        example_results = []
        search_times = []
        
        from tqdm import tqdm
        
        # Progress bar
        with tqdm(total=total_searches, desc="Running searches", unit="search") as pbar:
            for idx, query in enumerate(queries):
                search_start = time.perf_counter()
                
                # Execute search (returns top 20 results)
                results = search_hybrid(query, limit=20)
                search_time = time.perf_counter() - search_start
                search_times.append(search_time)
                
                # Validate result count
                if len(results) > 20:
                    logger.warning(f"Search returned {len(results)} results, expected 20")
                elif len(results) < 20 and len(results) > 0:
                    logger.debug(f"Search returned {len(results)} results for query: {query[:50]}")
                
                # Collect example results (first 5 queries, top 3 results each)
                if show_examples and idx < 5:
                    example_entry = {
                        'query': query,
                        'results': []
                    }
                    for article, score in results[:3]:
                        example_entry['results'].append({
                            'title': article.title,
                            'score': score
                        })
                    example_results.append(example_entry)
                
                pbar.update(1)
                pbar.set_postfix({'latency_ms': f'{search_time * 1000:.1f}'})
        
        # Calculate average latency from collected measurements
        avg_latency = sum(search_times) / len(search_times) if search_times else 0.0
        
        metrics = BenchmarkMetrics(
            total_searches=total_searches,
            total_time=0.0,  # Set after profiling
            throughput=0.0,  # Calculated after
            avg_latency=avg_latency,
            example_results=example_results
        )
        
        return metrics

    def save_profiling_results(
        self,
        profiler: cProfile.Profile,
        output_file: str,
        logger: logging.Logger
    ) -> str:
        """Save cProfile results to file and return summary."""
        try:
            output_path = Path(output_file)
            with open(output_path, 'w') as f:
                stats = pstats.Stats(profiler, stream=f)
                stats.sort_stats('cumulative')
                stats.print_stats(50)  # Top 50 functions
            
            # Generate summary of top bottlenecks
            summary_buf = io.StringIO()
            stats_summary = pstats.Stats(profiler, stream=summary_buf)
            stats_summary.sort_stats('cumulative')
            stats_summary.print_stats(20)  # Top 20 for summary
            summary_text = summary_buf.getvalue()
            
            logger.info(f"Profile results saved to: {output_path}")
            self.stdout.write(f"Profile results saved to: {output_path}")
            
            return summary_text
            
        except Exception as e:
            logger.error(f"Failed to save profiling results: {e}")
            self.stdout.write(self.style.ERROR(f"Failed to save profiling results: {e}"))
            return ""

    def display_results(
        self,
        metrics: BenchmarkMetrics,
        num_searches: int,
        profile_summary: str,
        logger: logging.Logger
    ):
        """Display benchmark results and metrics."""
        target_throughput = 20.0
        
        # Summary
        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("SEARCH BENCHMARK RESULTS")
        self.stdout.write("=" * 70)
        
        self.stdout.write(f"\nTotal searches executed: {metrics.total_searches}")
        self.stdout.write(f"Total time: {metrics.total_time:.2f} seconds")
        self.stdout.write(f"Average latency per search: {metrics.avg_latency * 1000:.2f} ms")
        self.stdout.write(f"\nThroughput: {metrics.throughput:.2f} searches/second")
        self.stdout.write(f"Target: {target_throughput:.2f} searches/second")
        
        if metrics.throughput >= target_throughput:
            self.stdout.write(self.style.SUCCESS(f"✓ Target achieved ({metrics.throughput:.2f} >= {target_throughput:.2f})"))
        else:
            percent_of_target = (metrics.throughput / target_throughput) * 100
            self.stdout.write(
                self.style.WARNING(
                    f"✗ Target not met ({metrics.throughput:.2f} < {target_throughput:.2f}, {percent_of_target:.1f}% of target)"
                )
            )
            self.stdout.write("  Investigate profile output for bottlenecks")
        
        # Log summary
        logger.info("=== BENCHMARK SUMMARY ===")
        logger.info(f"Total searches: {metrics.total_searches}")
        logger.info(f"Total time: {metrics.total_time:.2f}s")
        logger.info(f"Throughput: {metrics.throughput:.2f} searches/sec (target: {target_throughput:.2f})")
        logger.info(f"Average latency: {metrics.avg_latency * 1000:.2f} ms")
        
        # Example results
        if metrics.example_results:
            self.stdout.write("\n" + "-" * 70)
            self.stdout.write("EXAMPLE SEARCH RESULTS")
            self.stdout.write("-" * 70)
            
            for example in metrics.example_results:
                self.stdout.write(f"\nQuery: {example['query']}")
                if example['results']:
                    self.stdout.write("  Top results:")
                    for result in example['results']:
                        self.stdout.write(f"    - {result['title']} (score: {result['score']:.4f})")
                else:
                    self.stdout.write("  No results found")
        
        # Profile summary
        if profile_summary:
            self.stdout.write("\n" + "-" * 70)
            self.stdout.write("TOP BOTTLENECKS (Top 20 functions by cumulative time)")
            self.stdout.write("-" * 70)
            # Show first 30 lines of summary (top functions)
            summary_lines = profile_summary.split('\n')[:30]
            for line in summary_lines:
                self.stdout.write(line)
            self.stdout.write("\n... (see profile output file for complete statistics)")
        
        self.stdout.write("\n")

