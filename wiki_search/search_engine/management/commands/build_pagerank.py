"""Management command to build PageRank scores from InternalLink graph."""

from __future__ import annotations

import logging
import time
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from tqdm import tqdm

from search_engine.models import PageRank
from search_engine.pagerank import compute_pagerank
from search_engine.utils.profiler import ProfileManager, get_memory_usage, phase_timer

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Build PageRank scores from InternalLink graph.
    
    This command implements single-threaded PageRank computation with comprehensive
    profiling to identify bottlenecks and evaluate GPU acceleration benefits.
    """
    
    help = "Build PageRank scores from InternalLink graph"
    
    def add_arguments(self, parser) -> None:
        """Add command line arguments."""
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Clear existing PageRank scores before building"
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limit number of links to process (for testing)"
        )
        parser.add_argument(
            "--profile",
            action="store_true",
            help="Enable detailed cProfile profiling"
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose logging"
        )
        parser.add_argument(
            "--damping",
            type=float,
            default=0.85,
            help="PageRank damping factor (default: 0.85)"
        )
        parser.add_argument(
            "--max-iter",
            type=int,
            default=100,
            help="Maximum number of PageRank iterations (default: 100)"
        )
        parser.add_argument(
            "--tolerance",
            type=float,
            default=1e-6,
            help="Convergence tolerance (default: 1e-6)"
        )
    
    def handle(self, *args: Any, **options: Any) -> None:
        """Execute the command."""
        # Setup logging
        log_level = logging.DEBUG if options["verbose"] else logging.INFO
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        
        # Initialize profiler
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        profiler = ProfileManager("pagerank", enabled=options["profile"])
        
        # Track overall stats
        overall_start = time.perf_counter()
        phase_stats = []
        
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS("PageRank Build Command"))
        self.stdout.write("=" * 60)
        
        # Display configuration
        self.stdout.write("\nConfiguration:")
        self.stdout.write(f"  Rebuild: {options['rebuild']}")
        self.stdout.write(f"  Limit: {options['limit'] or 'None (process all)'}")
        self.stdout.write(f"  Profile: {options['profile']}")
        self.stdout.write(f"  Damping: {options['damping']}")
        self.stdout.write(f"  Max iterations: {options['max_iter']}")
        self.stdout.write(f"  Tolerance: {options['tolerance']}")
        self.stdout.write(f"  Initial memory: {get_memory_usage():.2f} MB\n")
        
        # Start profiling
        profiler.start()
        
        try:
            # Phase 1: Delete existing PageRank scores
            if options["rebuild"]:
                with phase_timer("Delete existing PageRank scores", options["verbose"]) as stats:
                    deleted_count = self._delete_pagerank_fast()
                    stats["records_deleted"] = deleted_count
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Deleted {deleted_count} existing PageRank scores in {stats['duration']:.2f}s"
                        )
                    )
                phase_stats.append(stats)
            
            # Phase 2: Compute PageRank
            with phase_timer("Compute PageRank", options["verbose"]) as stats:
                pagerank_scores, iterations, residual = compute_pagerank(
                    damping=options["damping"],
                    max_iter=options["max_iter"],
                    tol=options["tolerance"],
                    verbose=options["verbose"],
                    limit=options["limit"]
                )
                
                stats["articles_ranked"] = len(pagerank_scores)
                stats["iterations"] = iterations
                stats["residual"] = residual
                
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Computed PageRank for {len(pagerank_scores)} articles "
                        f"in {stats['duration']:.2f}s ({iterations} iterations, "
                        f"residual: {residual:.2e})"
                    )
                )
            phase_stats.append(stats)
            
            if not pagerank_scores:
                self.stdout.write(self.style.WARNING("No articles to rank. Exiting."))
                return
            
            # Phase 3: Store PageRank scores
            with phase_timer("Store PageRank scores", options["verbose"]) as stats:
                stored_count = self._store_pagerank_copy(pagerank_scores)
                stats["records_stored"] = stored_count
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Stored {stored_count} PageRank scores in {stats['duration']:.2f}s"
                    )
                )
            phase_stats.append(stats)
            
        finally:
            # Stop profiling and save results
            profiler.stop()
            if options["profile"]:
                profile_file, summary_file = profiler.save(timestamp)
                self.stdout.write(f"\nProfile saved to: {profile_file}")
                self.stdout.write(f"Summary saved to: {summary_file}")
        
        # Final statistics
        overall_duration = time.perf_counter() - overall_start
        final_memory = get_memory_usage()
        
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("Final Statistics"))
        self.stdout.write("=" * 60)
        
        # Phase breakdown
        self.stdout.write("\nPhase Breakdown:")
        for stats in phase_stats:
            phase_name = stats["phase"]
            duration = stats["duration"]
            percentage = (duration / overall_duration * 100) if overall_duration > 0 else 0
            self.stdout.write(f"  {phase_name}:")
            self.stdout.write(f"    Time: {duration:.2f}s ({percentage:.1f}%)")
            self.stdout.write(f"    Memory delta: {stats['memory_delta_mb']:+.2f} MB")
            
            # Additional metrics
            if "records_deleted" in stats:
                self.stdout.write(f"    Records deleted: {stats['records_deleted']}")
            if "articles_ranked" in stats:
                self.stdout.write(f"    Articles ranked: {stats['articles_ranked']}")
                self.stdout.write(f"    Iterations: {stats['iterations']}")
                self.stdout.write(f"    Residual: {stats['residual']:.2e}")
            if "records_stored" in stats:
                self.stdout.write(f"    Records stored: {stats['records_stored']}")
        
        # Overall metrics
        self.stdout.write("\nOverall Metrics:")
        self.stdout.write(f"  Total time: {overall_duration:.2f}s")
        self.stdout.write(f"  Final memory: {final_memory:.2f} MB")
        
        if len(pagerank_scores) > 0:
            articles_per_second = len(pagerank_scores) / overall_duration
            self.stdout.write(f"  Throughput: {articles_per_second:.2f} articles/second")
            
            # Scaling projection
            total_articles = 5_486_212
            projected_time = total_articles / articles_per_second
            self.stdout.write(f"\nScaling Projection:")
            self.stdout.write(f"  Full dataset: {total_articles:,} articles")
            self.stdout.write(f"  Projected time: {projected_time:.2f}s ({projected_time/60:.2f} minutes)")
            self.stdout.write(f"  Target time: 15s")
            speedup_needed = projected_time / 15
            self.stdout.write(f"  Speedup needed: {speedup_needed:.2f}x")
        
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("PageRank build completed successfully"))
        self.stdout.write("=" * 60)
    
    def _delete_pagerank_fast(self) -> int:
        """Delete existing PageRank scores using fast TRUNCATE.
        
        Returns:
            Number of records deleted
        """
        with connection.cursor() as cursor:
            # Get count before deletion
            cursor.execute(f"SELECT COUNT(*) FROM {PageRank._meta.db_table}")
            count = cursor.fetchone()[0]
            
            if count > 0:
                # Use TRUNCATE for fast deletion
                cursor.execute(f"TRUNCATE TABLE {PageRank._meta.db_table} RESTART IDENTITY CASCADE")
                logger.info(f"Truncated {count} PageRank records")
            
            return count
    
    def _store_pagerank_copy(self, pagerank_scores: dict[int, float]) -> int:
        """Store PageRank scores using PostgreSQL COPY for high throughput.
        
        Args:
            pagerank_scores: Dict mapping article_id -> PageRank score
            
        Returns:
            Number of records stored
        """
        if not pagerank_scores:
            return 0
        
        # Prepare data for COPY
        records = [(article_id, float(score)) for article_id, score in pagerank_scores.items()]
        
        # Use COPY for bulk insert
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Use COPY for efficient bulk insert
                with cursor.copy(
                    f"COPY {PageRank._meta.db_table} (article_id, score) FROM STDIN"
                ) as copy:
                    with tqdm(total=len(records), desc="Storing PageRank scores", unit="records") as pbar:
                        for article_id, score in records:
                            copy.write_row((article_id, score))
                            pbar.update(1)
        
        logger.info(f"Stored {len(records)} PageRank scores using COPY")
        return len(records)

