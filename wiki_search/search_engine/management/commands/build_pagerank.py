from __future__ import annotations

import logging
import time
from typing import Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from django.core.management.base import BaseCommand
from django.db import transaction, connection
from tqdm import tqdm

from search_engine.models import Article, PageRank
from search_engine.pagerank import compute_pagerank, get_pagerank_stats

logger = logging.getLogger(__name__)


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
        parser.add_argument("--batch-size", type=int, default=1000,
                          help="Batch size for database operations (default: 1000)")

    def _store_pagerank_batch(self, batch_data: List[Tuple[int, float, int]], 
                            articles_dict: Dict[int, Article]) -> int:
        """Store a batch of PageRank scores in the database.
        
        Args:
            batch_data: List of (article_id, score, iteration_count) tuples
            articles_dict: Dictionary mapping article_id to Article objects
            
        Returns:
            Number of objects created
        """
        pagerank_objects = []
        for article_id, score, iteration_count in batch_data:
            if article_id in articles_dict:
                pagerank_objects.append(
                    PageRank(
                        article=articles_dict[article_id],
                        score=score,
                        iteration_count=iteration_count
                    )
                )
        
        if pagerank_objects:
            with transaction.atomic():
                PageRank.objects.bulk_create(pagerank_objects, ignore_conflicts=True)
        
        return len(pagerank_objects)

    def _parallel_store_pagerank_scores(self, pagerank_scores: Dict[int, float], 
                                      articles_dict: Dict[int, Article], 
                                      iteration_count: int, 
                                      num_threads: int, 
                                      batch_size: int) -> None:
        """Store PageRank scores using parallel threads.
        
        Args:
            pagerank_scores: Dictionary mapping article_id to PageRank score
            articles_dict: Dictionary mapping article_id to Article objects
            iteration_count: Number of iterations used for computation
            num_threads: Number of threads to use
            batch_size: Size of each batch
        """
        # Prepare batch data
        batch_data = []
        for article_id, score in pagerank_scores.items():
            batch_data.append((article_id, score, iteration_count))
        
        # Split into batches
        batches = []
        for i in range(0, len(batch_data), batch_size):
            batches.append(batch_data[i:i + batch_size])
        
        self.stdout.write(f"Storing {len(pagerank_scores)} PageRank scores using {num_threads} threads...")
        
        # Use ThreadPoolExecutor for parallel processing
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            # Submit all batch tasks
            future_to_batch = {
                executor.submit(self._store_pagerank_batch, batch, articles_dict): batch
                for batch in batches
            }
            
            # Process completed tasks with progress bar
            total_created = 0
            with tqdm(total=len(pagerank_scores), desc="Storing PageRank scores") as pbar:
                for future in as_completed(future_to_batch):
                    try:
                        created_count = future.result()
                        total_created += created_count
                        pbar.update(created_count)
                    except Exception as e:
                        self.stdout.write(
                            self.style.ERROR(f"Error storing batch: {e}")
                        )
        
        self.stdout.write(f"Successfully stored {total_created} PageRank scores")

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
        batch_size = options["batch_size"]
        
        start_time = time.perf_counter()
        
        # Clear existing PageRank scores if requested
        if rebuild:
            self.stdout.write("Clearing existing PageRank scores...")
            
            # Get count for progress bar
            total_count = PageRank.objects.count()
            if total_count > 0:
                self.stdout.write(f"Found {total_count} existing PageRank scores to delete")
                
                # Delete in batches with progress bar
                batch_size = 10000
                deleted_count = 0
                
                with tqdm(total=total_count, desc="Deleting PageRank scores") as pbar:
                    while True:
                        # Delete a batch
                        batch_ids = list(PageRank.objects.values_list('id', flat=True)[:batch_size])
                        if not batch_ids:
                            break
                        
                        deleted = PageRank.objects.filter(id__in=batch_ids).delete()[0]
                        deleted_count += deleted
                        pbar.update(deleted)
                        
                        # Break if we've deleted everything
                        if deleted < batch_size:
                            break
                
                self.stdout.write(f"Deleted {deleted_count} PageRank scores")
            else:
                self.stdout.write("No existing PageRank scores found")
        
        # Check if we have articles with links
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(DISTINCT a.id)
                FROM search_engine_article a
                WHERE EXISTS (
                    SELECT 1 FROM search_engine_internallink l 
                    WHERE l.from_article_id = a.id OR l.to_article_id = a.id
                )
            """)
            articles_with_links = cursor.fetchone()[0]
        
        if articles_with_links == 0:
            self.stdout.write(
                self.style.WARNING("No articles with links found. PageRank requires a link graph.")
            )
            return
        
        self.stdout.write(f"Found {articles_with_links} articles with links")
        
        # Compute PageRank scores
        self.stdout.write("Computing PageRank scores...")
        pagerank_scores, iterations, residual = compute_pagerank(
            damping=damping,
            max_iter=max_iter,
            tol=tolerance,
            verbose=options["verbose"]
        )
        
        if not pagerank_scores:
            self.stdout.write(
                self.style.ERROR("PageRank computation failed - no scores generated")
            )
            return
        
        # Store results in database using parallel processing
        self.stdout.write("Preparing to store PageRank scores...")
        
        # Get all articles that have PageRank scores
        article_ids = list(pagerank_scores.keys())
        articles = {a.id: a for a in Article.objects.filter(id__in=article_ids)}
        
        # Use parallel storage
        self._parallel_store_pagerank_scores(
            pagerank_scores=pagerank_scores,
            articles_dict=articles,
            iteration_count=iterations,
            num_threads=num_threads,
            batch_size=batch_size
        )
        
        # Display results
        elapsed_time = time.perf_counter() - start_time
        
        self.stdout.write(self.style.SUCCESS(
            f"PageRank computation complete in {elapsed_time:.2f} seconds"
        ))
        self.stdout.write(f"  - Articles processed: {len(pagerank_scores)}")
        self.stdout.write(f"  - Iterations to convergence: {iterations}")
        self.stdout.write(f"  - Final residual: {residual:.2e}")
        
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
        
        self.stdout.write(
            self.style.SUCCESS(
                f"PageRank build complete. Processed {len(pagerank_scores)} articles "
                f"in {elapsed_time:.2f}s using {iterations} iterations."
            )
        )
