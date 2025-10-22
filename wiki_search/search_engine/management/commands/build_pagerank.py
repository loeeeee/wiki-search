from __future__ import annotations

import logging
import time
from typing import Dict

from django.core.management.base import BaseCommand
from django.db import transaction
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

    def handle(self, *args, **options):
        # Setup logging
        if options["verbose"]:
            logging.basicConfig(level=logging.INFO, 
                             format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        
        damping = options["damping"]
        max_iter = options["max_iterations"]
        tolerance = options["tolerance"]
        rebuild = options["rebuild"]
        
        start_time = time.perf_counter()
        
        # Clear existing PageRank scores if requested
        if rebuild:
            self.stdout.write("Clearing existing PageRank scores...")
            PageRank.objects.all().delete()
        
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
        
        # Store results in database
        self.stdout.write("Storing PageRank scores in database...")
        
        # Get all articles that have PageRank scores
        article_ids = list(pagerank_scores.keys())
        articles = {a.id: a for a in Article.objects.filter(id__in=article_ids)}
        
        # Create PageRank objects in batches
        pagerank_objects = []
        batch_size = 1000
        
        with tqdm(total=len(pagerank_scores), desc="Storing PageRank scores") as pbar:
            for article_id, score in pagerank_scores.items():
                if article_id in articles:
                    pagerank_objects.append(
                        PageRank(
                            article=articles[article_id],
                            score=score,
                            iteration_count=iterations
                        )
                    )
                
                if len(pagerank_objects) >= batch_size:
                    with transaction.atomic():
                        PageRank.objects.bulk_create(pagerank_objects, ignore_conflicts=True)
                    pbar.update(len(pagerank_objects))
                    pagerank_objects.clear()
            
            # Final batch
            if pagerank_objects:
                with transaction.atomic():
                    PageRank.objects.bulk_create(pagerank_objects, ignore_conflicts=True)
                pbar.update(len(pagerank_objects))
        
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
