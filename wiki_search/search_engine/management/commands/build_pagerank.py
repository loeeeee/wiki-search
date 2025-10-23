from __future__ import annotations

import logging
import time
import cProfile
import pstats
import io
import psutil
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from django.core.management.base import BaseCommand
from django.db import transaction, connection
from tqdm import tqdm

from search_engine.models import Article, PageRank
from search_engine.pagerank import compute_pagerank, get_pagerank_stats

logger = logging.getLogger(__name__)


def phase_timer(phase_name: str):
    """Context manager for timing phases of PageRank build."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            start_time = time.perf_counter()
            logger.info("Starting phase: %s", phase_name)
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start_time
            logger.info("Completed phase: %s in %.2f seconds", phase_name, elapsed)
            return result
        return wrapper
    return decorator


def save_profile_stats(profiler: cProfile.Profile, phase_name: str) -> Path:
    """Save cProfile statistics to file."""
    profile_dir = Path("data/profiles")
    profile_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = int(time.time())
    profile_path = profile_dir / f"pagerank_{phase_name}_{timestamp}.prof"
    
    profiler.dump_stats(str(profile_path))
    logger.info("Profile saved to: %s", profile_path)
    
    # Also save human-readable summary
    summary_path = profile_path.with_suffix('.txt')
    with open(summary_path, 'w') as f:
        stats = pstats.Stats(profiler, stream=f)
        stats.sort_stats('cumulative')
        stats.print_stats(20)  # Top 20 functions
    
    return profile_path


def get_memory_usage() -> float:
    """Get current memory usage in MB."""
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / 1024 / 1024


class Command(BaseCommand):
    help = "Build PageRank scores for articles using the InternalLink graph"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--damping", type=float, default=0.85, 
                          help="PageRank damping factor (default: 0.85)")
        parser.add_argument("--max-iterations", type=int, default=100,
                          help="Maximum number of iterations (default: 100)")
        parser.add_argument("--tolerance", type=float, default=1e-6,
                          help="Convergence tolerance (default: 1e-6)")
        parser.add_argument("--rebuild", action="store_true",
                          help="Clear existing PageRank scores before building")
        parser.add_argument("--verbose", action="store_true",
                          help="Enable verbose logging")
        parser.add_argument("--threads", type=int, default=4,
                          help="Number of threads for parallel database operations (default: 4)")
        parser.add_argument("--db-read-workers", type=int, default=4,
                          help="Number of parallel workers for reading links (default: 4)")
        parser.add_argument("--db-write-workers", type=int, default=4,
                          help="Number of parallel workers for writing scores (default: 4)")
        parser.add_argument("--batch-size", type=int, default=1000,
                          help="Batch size for database operations (default: 1000)")
        parser.add_argument("--profile", action="store_true",
                          help="Enable detailed profiling with cProfile")
        parser.add_argument("--limit", type=int, default=None,
                          help="Limit number of links to process (for testing)")

    def _drop_pagerank_indexes(self):
        """Drop PageRank indexes before bulk insert for faster writes."""
        with connection.cursor() as cursor:
            # Drop unique constraint on article_id (OneToOneField)
            cursor.execute("""
                ALTER TABLE search_engine_pagerank 
                DROP CONSTRAINT IF EXISTS search_engine_pagerank_article_id_key
            """)
            logger.info("Dropped article_id unique constraint")
            
            # Get other index names from pg_indexes
            cursor.execute("""
                SELECT indexname FROM pg_indexes 
                WHERE tablename = 'search_engine_pagerank'
                AND indexname != 'search_engine_pagerank_pkey'
                AND indexname != 'search_engine_pagerank_article_id_key'
            """)
            indexes = [row[0] for row in cursor.fetchall()]
            
            for index_name in indexes:
                cursor.execute(f"DROP INDEX IF EXISTS {index_name}")
                logger.info(f"Dropped index: {index_name}")
    
    def _rebuild_pagerank_indexes(self):
        """Rebuild PageRank indexes after bulk insert."""
        with connection.cursor() as cursor:
            # Rebuild article_id unique constraint (OneToOneField)
            cursor.execute("""
                ALTER TABLE search_engine_pagerank 
                ADD CONSTRAINT search_engine_pagerank_article_id_key 
                UNIQUE (article_id)
            """)
            logger.info("Rebuilt article_id unique constraint")
            
            # Rebuild score index
            cursor.execute("""
                CREATE INDEX search_engine_pagerank_score_idx 
                ON search_engine_pagerank (score)
            """)
            
            # Rebuild -score index (for ordering DESC)
            cursor.execute("""
                CREATE INDEX search_engi_score_293842_idx 
                ON search_engine_pagerank (score DESC)
            """)
            
            logger.info("Rebuilt all PageRank indexes")

    def _store_pagerank_copy(self, pagerank_scores: Dict[int, float], 
                           iteration_count: int,
                           batch_size: int = 50000) -> int:
        """Store PageRank scores using PostgreSQL COPY with batch streaming.
        
        Args:
            pagerank_scores: Dictionary mapping article_id to PageRank score
            iteration_count: Number of iterations used for computation
            batch_size: Number of records to process per batch
            
        Returns:
            Number of objects created
        """
        if not pagerank_scores:
            return 0
        
        # Drop indexes before bulk insert for faster writes
        self._drop_pagerank_indexes()
        
        total = len(pagerank_scores)
        created = 0
        
        # Stream in batches
        items = list(pagerank_scores.items())
        for i in range(0, total, batch_size):
            batch = items[i:i + batch_size]
            
            with transaction.atomic():
                with connection.cursor() as cursor:
                    with cursor.copy(
                        "COPY search_engine_pagerank (article_id, score, iteration_count, last_computed) FROM STDIN"
                    ) as copy:
                        for article_id, score in batch:
                            copy.write_row((article_id, float(score), iteration_count, datetime.now()))
            
            created += len(batch)
            
        # Rebuild indexes after bulk insert
        self._rebuild_pagerank_indexes()
        
        return created

    def _store_pagerank_parallel(self, pagerank_scores: Dict[int, float],
                                iteration_count: int,
                                batch_size: int = 50000,
                                db_workers: int = 4) -> int:
        """Store PageRank scores using parallel database writes.
        
        Args:
            pagerank_scores: Dictionary mapping article_id to PageRank score
            iteration_count: Number of iterations used for computation
            batch_size: Number of records to process per batch
            db_workers: Number of parallel database workers
            
        Returns:
            Number of objects created
        """
        if not pagerank_scores:
            return 0
        
        # Drop indexes ONCE before all writes
        self._drop_pagerank_indexes()
        
        # Split data into worker chunks
        items = list(pagerank_scores.items())
        total = len(items)
        worker_chunk_size = max(1, total // db_workers)
        chunks = [items[i:i + worker_chunk_size] 
                  for i in range(0, total, worker_chunk_size)]
        
        logger.info(f"Storing {total} PageRank scores using {db_workers} parallel workers")
        
        # Parallel COPY operations
        def write_chunk(chunk: List[Tuple[int, float]]) -> int:
            """Each thread writes its chunk using COPY."""
            written = 0
            # Further subdivide into batches for memory efficiency
            for i in range(0, len(chunk), batch_size):
                batch = chunk[i:i + batch_size]
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        with cursor.copy(
                            "COPY search_engine_pagerank (article_id, score, iteration_count, last_computed) FROM STDIN"
                        ) as copy:
                            for article_id, score in batch:
                                copy.write_row((article_id, float(score), iteration_count, datetime.now()))
                written += len(batch)
            return written
        
        # Use ThreadPoolExecutor for database writes
        created = 0
        with ThreadPoolExecutor(max_workers=db_workers) as executor:
            futures = [executor.submit(write_chunk, chunk) for chunk in chunks]
            with tqdm(total=total, desc="Storing PageRank scores") as pbar:
                for future in as_completed(futures):
                    try:
                        count = future.result()
                        created += count
                        pbar.update(count)
                    except Exception as e:
                        logger.error(f"Error in parallel storage: {e}")
                        raise
        
        # Rebuild indexes ONCE after all writes
        self._rebuild_pagerank_indexes()
        
        return created


    def handle(self, *args, **options):
        # Setup logging
        if options["verbose"]:
            logging.basicConfig(level=logging.INFO, 
                             format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        
        damping = options["damping"]
        max_iter = options["max_iterations"]
        tolerance = options["tolerance"]
        rebuild = options["rebuild"]
        num_threads = options["threads"]
        db_read_workers = options["db_read_workers"]
        db_write_workers = options["db_write_workers"]
        batch_size = options["batch_size"]
        profile = options["profile"]
        
        start_time = time.perf_counter()
        initial_memory = get_memory_usage()
        
        # Initialize profiler if requested
        profiler = None
        if profile:
            profiler = cProfile.Profile()
            profiler.enable()
            logger.info("Profiling enabled")
        
        logger.info("Starting PageRank build with damping=%.2f, max_iter=%d, tolerance=%.2e", 
                   damping, max_iter, tolerance)
        logger.info("Initial memory usage: %.2f MB", initial_memory)
        
        # Clear existing PageRank scores if requested
        if rebuild:
            delete_start = time.perf_counter()
            self.stdout.write("Clearing existing PageRank scores...")
            logger.info("Starting delete phase")
            
            # Get count for progress bar
            total_count = PageRank.objects.count()
            if total_count > 0:
                self.stdout.write(f"Found {total_count} existing PageRank scores to delete")
                
                # Use single raw SQL DELETE for maximum performance
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM search_engine_pagerank")
                    deleted_count = cursor.rowcount
                
                delete_elapsed = time.perf_counter() - delete_start
                self.stdout.write(f"Deleted {deleted_count} PageRank scores in {delete_elapsed:.2f}s")
                logger.info("Delete phase completed in %.2f seconds", delete_elapsed)
            else:
                self.stdout.write("No existing PageRank scores found")
                logger.info("No existing PageRank scores to delete")
        
        # Skip the expensive check - we know we have links from the database summary
        # The build_adjacency_matrix function will handle the case of no links gracefully
        self.stdout.write("Proceeding with PageRank computation...")
        
        # Compute PageRank scores using parallel database reads
        self.stdout.write(f"Computing PageRank scores using {db_read_workers} parallel database workers...")
        logger.info("Starting computation phase with parallel graph loading")
        computation_start = time.perf_counter()
        
        from search_engine.pagerank import compute_pagerank_parallel
        pagerank_scores, iterations, residual = compute_pagerank_parallel(
            damping=damping,
            max_iter=max_iter,
            tol=tolerance,
            verbose=options["verbose"],
            limit=options["limit"],
            db_read_workers=db_read_workers
        )
        
        computation_elapsed = time.perf_counter() - computation_start
        logger.info("Computation phase completed in %.2f seconds", computation_elapsed)
        
        if not pagerank_scores:
            self.stdout.write(
                self.style.ERROR("PageRank computation failed - no scores generated")
            )
            return
        
        # Store results in database using parallel PostgreSQL COPY
        self.stdout.write(f"Preparing to store PageRank scores using {db_write_workers} parallel workers...")
        logger.info("Starting storage phase with parallel writes")
        storage_start = time.perf_counter()
        
        # Use parallel COPY for high-throughput storage
        self.stdout.write(f"Storing {len(pagerank_scores)} PageRank scores using parallel PostgreSQL COPY...")
        created_count = self._store_pagerank_parallel(
            pagerank_scores=pagerank_scores,
            iteration_count=iterations,
            batch_size=batch_size,
            db_workers=db_write_workers
        )
        
        storage_elapsed = time.perf_counter() - storage_start
        logger.info("Storage phase completed in %.2f seconds", storage_elapsed)
        self.stdout.write(f"Successfully stored {created_count} PageRank scores")
        
        # Display results
        elapsed_time = time.perf_counter() - start_time
        final_memory = get_memory_usage()
        memory_delta = final_memory - initial_memory
        
        self.stdout.write(self.style.SUCCESS(
            f"PageRank computation complete in {elapsed_time:.2f} seconds"
        ))
        self.stdout.write(f"  - Articles processed: {len(pagerank_scores)}")
        self.stdout.write(f"  - Iterations to convergence: {iterations}")
        self.stdout.write(f"  - Final residual: {residual:.2e}")
        self.stdout.write(f"  - Memory usage: {final_memory:.2f} MB (delta: {memory_delta:+.2f} MB)")
        
        # Show statistics
        stats = get_pagerank_stats()
        self.stdout.write(f"  - Average score: {stats['avg_score']:.6f}")
        self.stdout.write(f"  - Score range: [{stats['min_score']:.6f}, {stats['max_score']:.6f}]")
        self.stdout.write(f"  - Sum of scores: {stats['sum_scores']:.6f}")
        
        # Show top articles
        from search_engine.pagerank import get_top_pagerank_articles
        top_articles = get_top_pagerank_articles(5)
        if top_articles:
            self.stdout.write("\nTop 5 articles by PageRank:")
            for i, (article_id, title, score) in enumerate(top_articles, 1):
                self.stdout.write(f"  {i}. {title} (score: {score:.6f})")
        
        # Save profiling results if enabled
        if profiler:
            profiler.disable()
            save_profile_stats(profiler, "full_build")
            logger.info("Profiling results saved")
        
        self.stdout.write(
            self.style.SUCCESS(
                f"PageRank build complete. Processed {len(pagerank_scores)} articles "
                f"in {elapsed_time:.2f}s using {iterations} iterations."
            )
        )
