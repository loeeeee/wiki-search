from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Min, Max, Count

from search_engine.models import InternalLink

from tqdm import tqdm

logger = logging.getLogger(__name__)


@contextmanager
def phase_timer(phase_name: str):
    """Context manager for timing execution phases."""
    start = time.perf_counter()
    logger.info("Starting phase: %s", phase_name)
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        logger.info("Completed phase: %s in %.2f seconds", phase_name, elapsed)


class Command(BaseCommand):
    help = (
        "Resolve internal link foreign keys by matching from_page_id to articles "
        "and to_title to articles. Can be run independently after article loading."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--batch-size", type=int, default=5000, help="Batch size for processing (default: 5000)")
        parser.add_argument("--db-workers", type=int, default=96, help="Number of database worker threads (default: 96)")

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        
        batch_size: int = int(opts["batch_size"])
        db_workers: int = max(1, int(opts["db_workers"]))

        logger.info("Starting link resolution with batch_size=%d, db_workers=%d", batch_size, db_workers)

        # Phase 1: Resolve from_article links
        with phase_timer("Resolve from_article Links"):
            updated_from = self._resolve_from_article(batch_size, db_workers)

        # Phase 2: Resolve to_article links
        with phase_timer("Resolve to_article Links"):
            updated_to = self._resolve_to_article(batch_size, db_workers)

        logger.info("=" * 60)
        logger.info("LINK RESOLUTION COMPLETE")
        logger.info("Resolved from_article: %d links", updated_from)
        logger.info("Resolved to_article: %d links", updated_to)
        logger.info("=" * 60)

        self.stdout.write(
            self.style.SUCCESS(
                f"Link resolution complete: from_article={updated_from}, to_article={updated_to}"
            )
        )

    def _resolve_from_article(self, batch_size: int, db_workers: int = 6) -> int:
        """Resolve from_article foreign keys using from_page_id."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        # Get ID range for unresolved from_article links
        result = InternalLink.objects.filter(
            from_article__isnull=True, 
            from_page_id__isnull=False
        ).aggregate(min_id=Min('id'), max_id=Max('id'), total=Count('id'))
        
        if not result['total'] or result['min_id'] is None or result['max_id'] is None:
            logger.info("No from_article links to resolve")
            return 0

        min_id = result['min_id']
        max_id = result['max_id']
        unresolved_count = result['total']
        
        logger.info("Resolving from_article for %d links (ID range: %d-%d) using %d workers", 
                    unresolved_count, min_id, max_id, db_workers)

        # Create ID range batches based on batch_size
        batches = []
        for start in range(min_id, max_id + 1, batch_size):
            end = min(start + batch_size, max_id + 1)
            batches.append((start, end))

        logger.info("Processing %d ID range batches for from_article resolution (batch_size=%d)", 
                    len(batches), batch_size)

        # Add progress bar for from_article resolution
        pbar = tqdm(total=len(batches), desc="Resolving from_article", unit="batch", dynamic_ncols=True)

        def update_range_from_article(id_start: int, id_end: int) -> int:
            """Update from_article_id for a range of links."""
            with connection.cursor() as cursor:
                sql = """
                    UPDATE search_engine_internallink AS link
                    SET from_article_id = article.id
                    FROM search_engine_article AS article
                    WHERE link.id >= %s AND link.id < %s
                      AND link.from_article_id IS NULL
                      AND link.from_page_id = article.page_id
                """
                cursor.execute(sql, [id_start, id_end])
                return cursor.rowcount

        # Process batches in parallel
        updated_total = 0
        try:
            with ThreadPoolExecutor(max_workers=db_workers) as executor:
                futures = [executor.submit(update_range_from_article, start, end) for start, end in batches]
                for future in as_completed(futures):
                    try:
                        batch_updated = future.result()
                        updated_total += batch_updated
                        pbar.update(1)
                    except Exception as exc:
                        logger.error("Error in from_article resolution batch update: %s", exc)
                        raise
        finally:
            pbar.close()

        logger.info("Resolved from_article for %d links", updated_total)
        return updated_total

    def _resolve_to_article(self, batch_size: int, db_workers: int = 6) -> int:
        """Resolve to_article foreign keys using to_title."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        # Get ID range for unresolved to_article links
        result = InternalLink.objects.filter(
            to_article__isnull=True
        ).aggregate(min_id=Min('id'), max_id=Max('id'), total=Count('id'))
        
        if not result['total'] or result['min_id'] is None or result['max_id'] is None:
            logger.info("No to_article links to resolve")
            return 0

        min_id = result['min_id']
        max_id = result['max_id']
        unresolved_count = result['total']
        
        logger.info("Resolving to_article for %d links (ID range: %d-%d) using %d workers", 
                    unresolved_count, min_id, max_id, db_workers)

        # Create ID range batches based on batch_size
        batches = []
        for start in range(min_id, max_id + 1, batch_size):
            end = min(start + batch_size, max_id + 1)
            batches.append((start, end))

        logger.info("Processing %d ID range batches for to_article resolution (batch_size=%d)", 
                    len(batches), batch_size)

        # Add progress bar for to_article resolution
        pbar = tqdm(total=len(batches), desc="Resolving to_article", unit="batch", dynamic_ncols=True)

        def update_range_to_article(id_start: int, id_end: int) -> int:
            """Update to_article_id for a range of links."""
            with connection.cursor() as cursor:
                sql = """
                    UPDATE search_engine_internallink AS link
                    SET to_article_id = article.id
                    FROM search_engine_article AS article
                    WHERE link.id >= %s AND link.id < %s
                      AND link.to_article_id IS NULL
                      AND link.to_title = article.title
                """
                cursor.execute(sql, [id_start, id_end])
                return cursor.rowcount

        # Process batches in parallel
        updated_total = 0
        try:
            with ThreadPoolExecutor(max_workers=db_workers) as executor:
                futures = [executor.submit(update_range_to_article, start, end) for start, end in batches]
                for future in as_completed(futures):
                    try:
                        batch_updated = future.result()
                        updated_total += batch_updated
                        pbar.update(1)
                    except Exception as exc:
                        logger.error("Error in to_article resolution batch update: %s", exc)
                        raise
        finally:
            pbar.close()

        logger.info("Resolved to_article for %d links", updated_total)
        return updated_total