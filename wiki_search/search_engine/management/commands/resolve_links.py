from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional, Tuple

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Min, Max, Count, Q

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

        # Phase 1: Resolve both from_article and to_article links in single pass
        with phase_timer("Resolve Link Foreign Keys"):
            updated_from, updated_to = self._resolve_links_merged(batch_size, db_workers)

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

    def _resolve_links_merged(self, batch_size: int, db_workers: int = 6) -> Tuple[int, int]:
        """Resolve both from_article and to_article foreign keys in a single pass."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        # Get ID range for unresolved links (either from_article or to_article is NULL)
        result = InternalLink.objects.filter(
            Q(from_article__isnull=True, from_page_id__isnull=False) |
            Q(to_article__isnull=True)
        ).aggregate(min_id=Min('id'), max_id=Max('id'), total=Count('id'))
        
        if not result['total'] or result['min_id'] is None or result['max_id'] is None:
            logger.info("No links to resolve")
            return 0, 0

        min_id = result['min_id']
        max_id = result['max_id']
        unresolved_count = result['total']
        
        logger.info("Resolving both from_article and to_article for %d links (ID range: %d-%d) using %d workers", 
                    unresolved_count, min_id, max_id, db_workers)

        # Create ID range batches based on batch_size
        batches = []
        for start in range(min_id, max_id + 1, batch_size):
            end = min(start + batch_size, max_id + 1)
            batches.append((start, end))

        logger.info("Processing %d ID range batches of links (batch_size=%d)", 
                    len(batches), batch_size)

        # Add progress bar for link resolution
        pbar = tqdm(total=len(batches), desc="Resolving links", unit="batch", dynamic_ncols=True)

        def update_range_both(id_start: int, id_end: int) -> Tuple[int, int]:
            """Update both from_article and to_article in single query."""
            with connection.cursor() as cursor:
                # Merged UPDATE with dual JOIN
                sql = """
                    UPDATE search_engine_internallink AS link
                    SET 
                        from_article_id = COALESCE(link.from_article_id, from_art.id),
                        to_article_id = COALESCE(link.to_article_id, to_art.id)
                    FROM 
                        search_engine_article AS from_art,
                        search_engine_article AS to_art
                    WHERE link.id >= %s AND link.id < %s
                      AND link.from_page_id = from_art.page_id
                      AND link.to_title = to_art.title
                """
                cursor.execute(sql, [id_start, id_end])
                
                # Count separate updates for reporting
                cursor.execute("""
                    SELECT 
                        COUNT(*) FILTER (WHERE from_article_id IS NOT NULL),
                        COUNT(*) FILTER (WHERE to_article_id IS NOT NULL)
                    FROM search_engine_internallink
                    WHERE id >= %s AND id < %s
                """, [id_start, id_end])
                return cursor.fetchone()

        # Process batches in parallel
        updated_from, updated_to = 0, 0
        try:
            with ThreadPoolExecutor(max_workers=db_workers) as executor:
                futures = [executor.submit(update_range_both, start, end) for start, end in batches]
                for future in as_completed(futures):
                    try:
                        batch_from, batch_to = future.result()
                        updated_from += batch_from
                        updated_to += batch_to
                        pbar.update(1)
                    except Exception as exc:
                        logger.error("Error in link resolution batch update: %s", exc)
                        raise
        finally:
            pbar.close()

        logger.info("Resolved from_article for %d links, to_article for %d links", updated_from, updated_to)
        return updated_from, updated_to