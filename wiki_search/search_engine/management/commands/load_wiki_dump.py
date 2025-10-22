from __future__ import annotations

import bz2
import cProfile
import logging
import os
import pstats
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from search_engine.ingest.parser import extract_plain_paragraphs, extract_internal_links
from search_engine.models import Article, InternalLink

from tqdm import tqdm

logger = logging.getLogger(__name__)

try:  # Prefer orjson if available
    import orjson as _json  # type: ignore
except Exception:  # pragma: no cover
    import json as _json  # type: ignore


def _default_processed_dir() -> Path:
    base = settings.BASE_DIR.parent
    return base / "data" / "processed" / "enwiki-20171001-pages-meta-current-withlinks-processed"


def find_bz2_files(root: Path) -> List[Path]:
    results: List[Path] = []
    if not root.exists():
        return results
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        results.extend(sorted(sub.glob("wiki_*.bz2")))
    return results


def iter_jsonl_bz2_raw(file_path: Path) -> Iterator[str]:
    """Yield raw JSON lines from bz2 file without parsing."""
    with bz2.open(file_path, mode="rt", encoding="utf-8", errors="strict") as f:
        for line in f:
            if line.strip():
                yield line


def iter_jsonl_bz2(file_path: Path) -> Iterator[dict]:
    # Use larger buffer for better I/O performance
    with bz2.open(file_path, mode="rt", encoding="utf-8", errors="strict", compresslevel=9) as f:
        # Read in larger chunks to reduce I/O overhead
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                yield _json.loads(line)
            except Exception as exc:
                logger.error("Invalid JSON at %s:%d: %s", file_path, line_number, exc)
                raise


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


def save_profile_stats(profiler: cProfile.Profile, phase_name: str) -> Path:
    """Save cProfile statistics to file and log top functions."""
    base_dir = settings.BASE_DIR.parent / "data" / "profiles"
    base_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    profile_path = base_dir / f"{phase_name}_{timestamp}.prof"
    
    profiler.dump_stats(str(profile_path))
    
    # Log top functions by cumulative time
    stats = pstats.Stats(profiler)
    stats.sort_stats(pstats.SortKey.CUMULATIVE)
    
    logger.info("Top 20 functions by cumulative time for %s:", phase_name)
    stats.print_stats(20)
    
    return profile_path


def _process_shard_batch(shard_paths: List[Path], record_batch_size: int = 2048) -> Tuple[
    List[Tuple[int, Optional[str], List[str]]],  # (page_id, title, paragraphs)
    List[Tuple[int, str, str]],  # (from_page_id, to_title, anchor_text)
    int  # records_emitted
]:
    """Process multiple shard files with concurrent I/O and parsing.

    This function is at module level so it can be pickled for ProcessPoolExecutor.
    Uses producer-consumer threading pattern to overlap bz2 decompression (I/O) 
    with JSON parsing and text extraction (CPU), maximizing resource utilization.
    Returns simple tuples to minimize serialization overhead between processes.
    """
    import queue
    from threading import Thread
    
    articles: List[Tuple[int, Optional[str], List[str]]] = []
    links: List[Tuple[int, str, str]] = []
    
    # Use 2-3 parser threads per shard for optimal CPU utilization
    NUM_PARSER_THREADS = 3
    QUEUE_SIZE = 1000  # Buffer between I/O and parsing
    
    for shard_path in shard_paths:
        shard_str = str(shard_path)
        raw_queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        result_queue: queue.Queue = queue.Queue()
        parsing_errors: List[Exception] = []
        
        # Producer: read and decompress
        def producer():
            try:
                for line in iter_jsonl_bz2_raw(shard_path):
                    raw_queue.put(line)
            except Exception as exc:
                logger.error("Error reading shard %s: %s", shard_str, exc)
                result_queue.put((None, exc))
            finally:
                # Sentinel values to signal end
                for _ in range(NUM_PARSER_THREADS):
                    raw_queue.put(None)
        
        # Consumer: parse and extract
        def consumer():
            while True:
                line = raw_queue.get()
                if line is None:
                    # Signal completion
                    result_queue.put(("COMPLETED", None, None))
                    break
                try:
                    rec = _json.loads(line)
                    
                    page_id_raw = rec.get("id")
                    if page_id_raw is None:
                        logger.error("Record missing 'id' in %s", shard_str)
                        result_queue.put((None, None, ValueError(f"Missing id in record from {shard_str}")))
                        continue
                    
                    try:
                        page_id_int = int(page_id_raw)
                    except Exception as exc:
                        logger.error("Non-integer id in %s: %r (%s)", shard_str, page_id_raw, exc)
                        result_queue.put((None, None, exc))
                        continue

                    title = rec.get("title")
                    text = rec.get("text") or []
                    paragraphs = extract_plain_paragraphs(text)
                    shard_links = extract_internal_links(text)

                    # Create result tuple
                    article_tuple = (page_id_int, title, paragraphs)
                    link_tuples = [(page_id_int, target_title, anchor_text) for target_title, anchor_text in shard_links]
                    
                    result_queue.put((article_tuple, link_tuples, None))
                    
                except Exception as exc:
                    logger.error("Error parsing record in %s: %s", shard_str, exc)
                    result_queue.put((None, None, exc))
                finally:
                    raw_queue.task_done()
        
        # Start threads
        producer_thread = Thread(target=producer, daemon=True)
        consumer_threads = [Thread(target=consumer, daemon=True) for _ in range(NUM_PARSER_THREADS)]
        
        producer_thread.start()
        for t in consumer_threads:
            t.start()
        
        # Collect results
        completed_consumers = 0
        while completed_consumers < NUM_PARSER_THREADS:
            try:
                result = result_queue.get(timeout=30)  # 30 second timeout
                if len(result) == 3:
                    article_tuple, link_tuples, error = result
                    if result[0] == "COMPLETED":  # Consumer completed
                        completed_consumers += 1
                    elif error is not None:  # Error case
                        parsing_errors.append(error)
                    elif article_tuple is not None:  # Success case
                        articles.append(article_tuple)
                        links.extend(link_tuples)
                elif len(result) == 2 and result[1] is not None:  # Producer error
                    parsing_errors.append(result[1])
            except queue.Empty:
                logger.error("Timeout waiting for results from shard %s", shard_str)
                break
        
        # Wait for all threads to complete
        producer_thread.join(timeout=5)
        for t in consumer_threads:
            t.join(timeout=5)
        
        # Check for errors
        if parsing_errors:
            from django.core.management.base import CommandError
            raise CommandError(f"Errors processing shard {shard_str}: {parsing_errors[0]}")

    return articles, links, len(articles)


class Command(BaseCommand):
    help = (
        "Load Wikipedia dump into database (assumes pre-decompressed shards). "
        "This command wipes the DB, ingests articles and internal links, and resolves link FKs."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--processed-dir", default=str(_default_processed_dir()))
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
        parser.add_argument("--db-workers", type=int, default=12, help="Number of database writer threads (default: 6)")
        parser.add_argument("--limit", type=int)
        parser.add_argument("--profile", action="store_true", help="Enable detailed profiling with cProfile")

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        
        enable_profiling = opts.get("profile", False)
        overall_start = time.perf_counter()

        # Always clean the DB first
        with phase_timer("DB Cleanup"):
            logger.info("Cleaning database tables before ingestion")
            call_command("clean_db", yes=True, no_progress=True)

        processed_dir = Path(opts["processed_dir"]).expanduser()
        if not processed_dir.exists():
            raise CommandError(f"Processed directory not found: {processed_dir}")

        shards = find_bz2_files(processed_dir)
        if not shards:
            raise CommandError(f"No wiki_*.bz2 files found under {processed_dir}")

        batch_size: int = int(opts["batch_size"]) or 5000
        workers: int = max(1, int(opts["workers"]))
        db_workers: int = max(1, int(opts["db_workers"]))
        limit: Optional[int] = opts.get("limit")

        logger.info("Found %d shards; starting %d workers, %d db workers", len(shards), workers, db_workers)
        
        # Phase 1: Ingest articles and links
        if enable_profiling:
            profiler = cProfile.Profile()
            profiler.enable()
        
        with phase_timer("Article and Link Ingestion"):
            created_total, skipped_total, links_total = self._run_pipeline(shards, batch_size, limit, workers, db_workers)
        
        if enable_profiling:
            profiler.disable()
            save_profile_stats(profiler, "ingestion_phase")

        logger.info(
            "Ingest complete: articles created=%d, duplicates skipped(in-batch)=%d, links created=%d",
            created_total,
            skipped_total,
            links_total,
        )

        # Phase 2: Resolve from_article links
        if enable_profiling:
            profiler = cProfile.Profile()
            profiler.enable()
        
        with phase_timer("Resolve from_article Links"):
            updated_from = self._resolve_from_article(batch_size, db_workers)
        
        if enable_profiling:
            profiler.disable()
            save_profile_stats(profiler, "resolve_from_article")

        # Phase 3: Resolve to_article links
        if enable_profiling:
            profiler = cProfile.Profile()
            profiler.enable()
        
        with phase_timer("Resolve to_article Links"):
            updated_to = self._resolve_to_article(batch_size, db_workers)
        
        if enable_profiling:
            profiler.disable()
            save_profile_stats(profiler, "resolve_to_article")

        overall_elapsed = time.perf_counter() - overall_start
        logger.info("=" * 60)
        logger.info("OVERALL EXECUTION TIME: %.2f seconds", overall_elapsed)
        logger.info("Throughput: %.2f articles/second", created_total / overall_elapsed if overall_elapsed > 0 else 0)
        logger.info("=" * 60)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created_total} new articles, skipped {skipped_total} dups, created {links_total} links; "
                f"resolved from_article={updated_from}, to_article={updated_to} in {overall_elapsed:.2f}s"
            )
        )

    def _run_pipeline(
        self,
        shards: List[Path],
        batch_size: int,
        limit: Optional[int],
        workers: int,
        db_workers: int,
    ) -> Tuple[int, int, int]:
        """Process shards using ProcessPoolExecutor and store results in database."""
        record_batch_size = max(1, min(batch_size, 2048))

        created_total = 0
        skipped_total = 0
        links_total = 0
        processed_records = 0

        article_tuples: List[Tuple[int, Optional[str], List[str]]] = []
        link_tuples: List[Tuple[int, str, str]] = []
        
        # Larger batches to reduce flush frequency
        ARTICLE_FLUSH_THRESHOLD = batch_size * 4  # 4x larger batches
        LINK_FLUSH_THRESHOLD = max(100_000, batch_size * 40)  # 2x larger link batches

        # Estimate progress bar total based on limit
        estimated_shards = min(len(shards), (limit // 500) + 5) if limit else len(shards)
        pbar = tqdm(total=estimated_shards, desc="Processing shards", unit="shard", dynamic_ncols=True)

        # Use set for O(1) deduplication instead of O(n) list iteration
        seen_article_ids: Set[int] = set()

        def flush_articles_sync(tuples_to_flush: List[Tuple[int, Optional[str], List[str]]]) -> Tuple[int, int]:
            """Synchronous article flush - to be run in background thread."""
            if not tuples_to_flush:
                return 0, 0
            
            # Deduplicate using set for O(1) lookup
            unique_tuples: List[Tuple[int, Optional[str], List[str]]] = []
            local_seen: Set[int] = set()
            skipped = 0
            
            for tup in tuples_to_flush:
                page_id = tup[0]
                if page_id in local_seen or page_id in seen_article_ids:
                    skipped += 1
                    continue
                local_seen.add(page_id)
                unique_tuples.append(tup)
            
            # Create Django objects only here, right before bulk_create
            created = 0
            if unique_tuples:
                articles_to_insert = [
                    Article(page_id=page_id, title=title, plain_text_paragraphs=paragraphs)
                    for page_id, title, paragraphs in unique_tuples
                ]
                with transaction.atomic():
                    Article.objects.bulk_create(
                        articles_to_insert,
                        batch_size=batch_size,
                        ignore_conflicts=True
                    )
                created = len(articles_to_insert)
                # Update global seen set
                seen_article_ids.update(local_seen)
            
            return created, skipped

        def flush_links_sync(tuples_to_flush: List[Tuple[int, str, str]]) -> int:
            """Synchronous link flush - to be run in background thread."""
            if not tuples_to_flush:
                return 0
            
            # Create Django objects only here, right before bulk_create
            links_to_insert = [
                InternalLink(from_page_id=from_id, to_title=to_title, anchor_text=anchor)
                for from_id, to_title, anchor in tuples_to_flush
            ]
            with transaction.atomic():
                InternalLink.objects.bulk_create(
                    links_to_insert,
                    batch_size=batch_size,
                    ignore_conflicts=True
                )
            return len(links_to_insert)

        try:
            # Use separate thread executor for database writes to avoid blocking main process
            db_write_futures: List[Any] = []
            
            with ThreadPoolExecutor(max_workers=db_workers) as db_executor, \
                 ProcessPoolExecutor(max_workers=workers) as process_executor:
                
                # For I/O-bound work (bz2 decompression), we need MANY more pending tasks
                # than workers to keep them all busy. No batching = maximum parallelism.
                # Each worker can handle multiple I/O operations concurrently.
                MAX_PENDING_FUTURES = min(workers * 128, len(shards))  # Aggressive queuing
                
                shard_iter = iter(shards)
                futures: Dict[Any, Path] = {}
                
                # Submit large initial batch to saturate all workers
                initial_submit = min(MAX_PENDING_FUTURES, len(shards))
                logger.info("Submitting initial %d futures for %d workers (I/O bound work)", initial_submit, workers)
                for _ in range(initial_submit):
                    try:
                        shard = next(shard_iter)
                        future = process_executor.submit(_process_shard_batch, [shard], record_batch_size)
                        futures[future] = shard
                    except StopIteration:
                        break

                # Process results as they complete and submit new work
                while futures:
                    done_futures = []
                    for future in as_completed(futures):
                        shard = futures[future]
                        done_futures.append(future)
                        
                        try:
                            articles, links, records_emitted = future.result()

                            # Process articles - work with tuples
                            if limit is not None:
                                remaining = limit - processed_records
                                articles = articles[:remaining]
                            
                            article_tuples.extend(articles)
                            processed_records += len(articles)
                            
                            # Submit database write in background thread if threshold reached
                            if len(article_tuples) >= ARTICLE_FLUSH_THRESHOLD:
                                tuples_copy = article_tuples[:]
                                article_tuples.clear()
                                db_future = db_executor.submit(flush_articles_sync, tuples_copy)
                                db_write_futures.append(('articles', db_future))

                            # Process links - work with tuples
                            if limit is None or processed_records < limit:
                                if limit is not None:
                                    # Only keep links for articles we're storing
                                    article_ids = {tup[0] for tup in articles}
                                    links = [l for l in links if l[0] in article_ids]
                                
                                link_tuples.extend(links)
                                # Submit database write in background thread if threshold reached
                                if len(link_tuples) >= LINK_FLUSH_THRESHOLD:
                                    tuples_copy = link_tuples[:]
                                    link_tuples.clear()
                                    db_future = db_executor.submit(flush_links_sync, tuples_copy)
                                    db_write_futures.append(('links', db_future))

                            # Update progress
                            pbar.update(1)

                            # Submit new work to maintain the window
                            if limit is None or processed_records < limit:
                                try:
                                    next_shard = next(shard_iter)
                                    new_future = process_executor.submit(_process_shard_batch, [next_shard], record_batch_size)
                                    futures[new_future] = next_shard
                                except StopIteration:
                                    pass
                            
                            # Check if limit reached
                            if limit is not None and processed_records >= limit:
                                # Cancel all pending futures
                                for f in futures:
                                    if f not in done_futures and not f.done():
                                        f.cancel()
                                break

                        except Exception as exc:
                            logger.error("Error processing shard %s: %s", shard, exc)
                            raise CommandError(f"Failed to process shard {shard}: {exc}")
                    
                    # Remove completed futures
                    for f in done_futures:
                        del futures[f]
                    
                    # Break if limit reached
                    if limit is not None and processed_records >= limit:
                        break
                
                # Wait for all pending database writes to complete
                logger.info("Waiting for %d pending database writes to complete", len(db_write_futures))
                for write_type, db_future in db_write_futures:
                    try:
                        result = db_future.result()
                        if write_type == 'articles':
                            created, skipped = result
                            created_total += created
                            skipped_total += skipped
                        else:  # links
                            links_total += result
                    except Exception as exc:
                        logger.error("Error in background database write: %s", exc)
                        raise

        finally:
            pbar.close()

        # Final flush of remaining data
        if article_tuples:
            created, skipped = flush_articles_sync(article_tuples)
            created_total += created
            skipped_total += skipped
        if link_tuples:
            links_total += flush_links_sync(link_tuples)

        return created_total, skipped_total, links_total

    def _resolve_from_article(self, batch_size: int, db_workers: int = 6) -> int:
        """Resolve from_article foreign keys using parallel batched SQL UPDATE with JOIN."""
        from django.db import connection
        
        unresolved_count = InternalLink.objects.filter(
            from_article__isnull=True, from_page_id__isnull=False
        ).count()
        
        if unresolved_count == 0:
            logger.info("No from_article links to resolve")
            return 0

        logger.info("Resolving from_article for %d links using %d workers", unresolved_count, db_workers)

        # Get all unresolved link IDs to split into batches
        unresolved_ids = list(InternalLink.objects.filter(
            from_article__isnull=True, from_page_id__isnull=False
        ).values_list('id', flat=True))
        
        if not unresolved_ids:
            return 0

        # Split into batches for parallel processing
        batch_size_actual = max(1, len(unresolved_ids) // db_workers)
        batches = []
        for i in range(0, len(unresolved_ids), batch_size_actual):
            batch_ids = unresolved_ids[i:i + batch_size_actual]
            if batch_ids:
                batches.append(batch_ids)

        logger.info("Processing %d batches of from_article links", len(batches))

        def update_batch_from_article(batch_ids: List[int]) -> int:
            """Update a batch of from_article links."""
            from django.db import connection
            with connection.cursor() as cursor:
                sql = """
                    UPDATE search_engine_internallink AS link
                    SET from_article_id = article.id
                    FROM search_engine_article AS article
                    WHERE link.from_page_id = article.page_id
                      AND link.from_article_id IS NULL
                      AND link.from_page_id IS NOT NULL
                      AND link.id = ANY(%s)
                """
                cursor.execute(sql, [batch_ids])
                return cursor.rowcount

        # Process batches in parallel
        updated_total = 0
        with ThreadPoolExecutor(max_workers=db_workers) as executor:
            futures = [executor.submit(update_batch_from_article, batch_ids) for batch_ids in batches]
            for future in as_completed(futures):
                try:
                    batch_updated = future.result()
                    updated_total += batch_updated
                except Exception as exc:
                    logger.error("Error in from_article batch update: %s", exc)
                    raise

        logger.info("Resolved from_article for %d links", updated_total)
        return updated_total

    def _resolve_to_article(self, batch_size: int, db_workers: int = 6) -> int:
        """Resolve to_article foreign keys using parallel batched SQL UPDATE with JOIN."""
        from django.db import connection
        
        unresolved_count = InternalLink.objects.filter(to_article__isnull=True).count()
        
        if unresolved_count == 0:
            logger.info("No to_article links to resolve")
            return 0

        logger.info("Resolving to_article for %d links using %d workers", unresolved_count, db_workers)

        # Get all unresolved link IDs to split into batches
        unresolved_ids = list(InternalLink.objects.filter(
            to_article__isnull=True
        ).values_list('id', flat=True))
        
        if not unresolved_ids:
            return 0

        # Split into batches for parallel processing
        batch_size_actual = max(1, len(unresolved_ids) // db_workers)
        batches = []
        for i in range(0, len(unresolved_ids), batch_size_actual):
            batch_ids = unresolved_ids[i:i + batch_size_actual]
            if batch_ids:
                batches.append(batch_ids)

        logger.info("Processing %d batches of to_article links", len(batches))

        def update_batch_to_article(batch_ids: List[int]) -> int:
            """Update a batch of to_article links."""
            from django.db import connection
            with connection.cursor() as cursor:
                sql = """
                    UPDATE search_engine_internallink AS link
                    SET to_article_id = article.id
                    FROM search_engine_article AS article
                    WHERE link.to_title = article.title
                      AND link.to_article_id IS NULL
                      AND link.id = ANY(%s)
                """
                cursor.execute(sql, [batch_ids])
                return cursor.rowcount

        # Process batches in parallel
        updated_total = 0
        with ThreadPoolExecutor(max_workers=db_workers) as executor:
            futures = [executor.submit(update_batch_to_article, batch_ids) for batch_ids in batches]
            for future in as_completed(futures):
                try:
                    batch_updated = future.result()
                    updated_total += batch_updated
                except Exception as exc:
                    logger.error("Error in to_article batch update: %s", exc)
                    raise

        logger.info("Resolved to_article for %d links", updated_total)
        return updated_total


