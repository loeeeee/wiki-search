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
import csv
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
            '--seed',
            type=int,
            default=42,
            help='Deterministic seed (default: 42). Ignored if --randomize is set.'
        )
        parser.add_argument(
            '--randomize',
            action='store_true',
            help='Opt out of deterministic mode and use non-seeded randomness'
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
        parser.add_argument(
            '--queries-file',
            type=str,
            default=None,
            help='Load queries from file (one per line) instead of sampling from DB'
        )
        parser.add_argument(
            '--save-queries',
            type=str,
            default=None,
            help='Save the queries used for this benchmark to the given path'
        )
        parser.add_argument(
            '--export-results',
            type=str,
            default=None,
            help='Export per-query top results to CSV at the given path'
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

        # Deterministic by default unless --randomize
        if not options.get('randomize', False):
            self._initialize_seeds(options['seed'], logger)
            logger.info(f"Deterministic mode enabled with seed {options['seed']}")
        else:
            logger.info("Randomized mode enabled (no fixed seed)")
        
        # Generate test queries
        logger.info("Preparing test queries...")
        queries: List[str]
        if options.get('queries_file'):
            queries = self.load_queries(Path(options['queries_file']), options['num_searches'], logger)
            logger.info(f"Loaded {len(queries)} queries from file: {options['queries_file']}")
        else:
            queries = self.generate_test_queries(
                options['num_searches'],
                logger,
                deterministic=(not options.get('randomize', False)),
                seed=options.get('seed', 42),
            )
            logger.info("Generated queries from article titles")
            if options.get('save_queries'):
                self.save_queries(Path(options['save_queries']), queries, logger)
        
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
            # Store per-query results for optional export
            self._all_results: List[List[Tuple[Article, float]]] = []
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

            # Export results if requested
            if options.get('export_results'):
                try:
                    self.export_results_csv(Path(options['export_results']), queries, logger)
                    self.stdout.write(self.style.SUCCESS(f"Results exported to: {options['export_results']}"))
                except Exception as e:
                    logger.error(f"Failed to export results: {e}")
                    self.stdout.write(self.style.ERROR(f"Failed to export results: {e}"))
            
            self.stdout.write(self.style.SUCCESS("Benchmark completed!"))
            
        except Exception as e:
            profiler.disable()
            logger.error(f"Benchmark failed: {e}", exc_info=True)
            raise

    def generate_test_queries(self, count: int, logger: logging.Logger, deterministic: bool = True, seed: Optional[int] = 42) -> List[str]:
        """Generate test queries by sampling article titles.
        If deterministic is True, uses a local RNG with the provided seed and
        samples from a stable, ordered list of article ids.
        """
        total_articles = Article.objects.count()
        
        if total_articles == 0:
            logger.error("No articles found in database")
            return []
        
        # Sample article titles
        sample_size = min(count, total_articles)
        # Stable order list of article ids to ensure repeatability
        article_ids = list(Article.objects.order_by('id').values_list('id', flat=True))
        
        if len(article_ids) < sample_size:
            sample_size = len(article_ids)
        
        # Random sampling without replacement using local RNG when deterministic
        rng = random.Random(seed) if deterministic else random
        sampled_ids = rng.sample(article_ids, sample_size)
        queries = list(
            Article.objects.filter(id__in=sampled_ids)
            .values_list('title', flat=True)
        )
        
        # If we need more queries than available articles, duplicate deterministically if enabled
        if len(queries) < count:
            additional_needed = count - len(queries)
            additional = rng.choices(queries, k=additional_needed)
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
                # keep all results for export
                if hasattr(self, '_all_results'):
                    self._all_results.append(results)
                
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

    # Helpers for deterministic mode and I/O
    def _initialize_seeds(self, seed: int, logger: logging.Logger) -> None:
        try:
            random.seed(seed)
            # Optional numpy
            try:
                import numpy as np  # type: ignore
                np.random.seed(seed)
                logger.debug("Seeded numpy RNG")
            except Exception:
                logger.debug("Numpy not available; skipping seeding")
            # Optional torch
            try:
                import torch  # type: ignore
                torch.manual_seed(seed)
                if torch.cuda.is_available():  # type: ignore[attr-defined]
                    torch.cuda.manual_seed_all(seed)  # type: ignore[attr-defined]
                try:
                    torch.use_deterministic_algorithms(True)  # type: ignore[attr-defined]
                except Exception:
                    pass
                logger.debug("Seeded torch RNG")
            except Exception:
                logger.debug("Torch not available; skipping seeding")
        except Exception as e:
            logger.warning(f"Failed to set deterministic seeds: {e}")

    def load_queries(self, path: Path, count: int, logger: logging.Logger) -> List[str]:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines()]
            queries = [q for q in lines if q]
            if not queries:
                logger.warning("Queries file is empty; falling back to DB sampling")
                return self.generate_test_queries(count, logger)
            if len(queries) >= count:
                return queries[:count]
            # If fewer than needed, cycle deterministically
            result = []
            idx = 0
            while len(result) < count:
                result.append(queries[idx % len(queries)])
                idx += 1
            return result
        except Exception as e:
            logger.error(f"Failed to load queries from {path}: {e}")
            return self.generate_test_queries(count, logger)

    def save_queries(self, path: Path, queries: List[str], logger: logging.Logger) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                for q in queries:
                    f.write(q + "\n")
            logger.info(f"Saved queries to {path}")
        except Exception as e:
            logger.error(f"Failed to save queries to {path}: {e}")

    def export_results_csv(self, path: Path, queries: List[str], logger: logging.Logger) -> None:
        if not hasattr(self, '_all_results'):
            raise RuntimeError("No results captured to export")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["query", "rank", "article_id", "title", "score"])
            for q, results in zip(queries, getattr(self, '_all_results')):
                for rank, (article, score) in enumerate(results, start=1):
                    writer.writerow([q, rank, getattr(article, 'id', ''), getattr(article, 'title', ''), f"{score:.6f}"])
        logger.info(f"Exported results CSV to {path}")

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

