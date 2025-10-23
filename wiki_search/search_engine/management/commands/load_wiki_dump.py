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
from django.db import transaction, connection
from django.db.models import Min, Max, Count

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


def _process_shard_batch(
    shard_paths: List[Path], 
    record_batch_size: int = 2048,
    producer_threads: int = 3
) -> Tuple[
    List[Tuple[int, Optional[str], List[str]]],  # (page_id, title, paragraphs)
    List[Tuple[int, str, str]],  # (from_page_id, to_title, anchor_text)
    int  # records_emitted
]:
    """Process multiple shard files with concurrent I/O and parsing.

    This function is at module level so it can be pickled for ProcessPoolExecutor.
    Uses multiple producer threads (I/O-bound bz2 decompression) and 1 consumer thread 
    (CPU-bound parsing) to maximize I/O throughput while minimizing CPU overhead.
    
    Args:
        shard_paths: List of shard files to process
        record_batch_size: Batch size for processing (unused, kept for compatibility)
        producer_threads: Number of producer threads for concurrent I/O (default: 3)
    
    Returns simple tuples to minimize serialization overhead between processes.
    """
    import queue
    from threading import Thread, Lock
    
    articles: List[Tuple[int, Optional[str], List[str]]] = []
    links: List[Tuple[int, str, str]] = []
    
    # I/O-optimized: configurable producer threads, 1 consumer thread
    NUM_PRODUCER_THREADS = max(1, min(producer_threads, len(shard_paths)))  # Don't exceed shard count
    NUM_CONSUMER_THREADS = 1
    QUEUE_SIZE = 500 * NUM_PRODUCER_THREADS  # Scale queue with producer count
    
    # Shared queues for all shards
    raw_queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
    result_queue: queue.Queue = queue.Queue()
    parsing_errors: List[Exception] = []
    errors_lock = Lock()
    
    # Track active producers
    active_producers_lock = Lock()
    active_producers = len(shard_paths)
    
    # Producer: read and decompress (one per shard, executed by thread pool)
    def producer(shard_path: Path):
        nonlocal active_producers
        shard_str = str(shard_path)
        try:
            for line in iter_jsonl_bz2_raw(shard_path):
                # Tag each line with source shard for error reporting
                raw_queue.put((line, shard_str))
        except Exception as exc:
            logger.error("Error reading shard %s: %s", shard_str, exc)
            with errors_lock:
                parsing_errors.append(exc)
        finally:
            # Decrement active producers
            with active_producers_lock:
                active_producers -= 1
                if active_producers == 0:
                    # Signal consumer to stop when all producers done
                    for _ in range(NUM_CONSUMER_THREADS):
                        raw_queue.put(None)
    
    # Consumer: parse and extract
    def consumer():
        while True:
            item = raw_queue.get()
            if item is None:
                # Signal completion
                result_queue.put(("COMPLETED", None, None))
                break
            
            line, shard_str = item
            try:
                rec = _json.loads(line)
                
                page_id_raw = rec.get("id")
                if page_id_raw is None:
                    logger.error("Record missing 'id' in %s", shard_str)
                    with errors_lock:
                        parsing_errors.append(ValueError(f"Missing id in record from {shard_str}"))
                    continue
                
                try:
                    page_id_int = int(page_id_raw)
                except Exception as exc:
                    logger.error("Non-integer id in %s: %r (%s)", shard_str, page_id_raw, exc)
                    with errors_lock:
                        parsing_errors.append(exc)
                    continue

                title = rec.get("title")
                text = rec.get("text") or []
                paragraphs = extract_plain_paragraphs(text)
                shard_links = extract_internal_links(text)

                # Create result tuple
                article_tuple = (page_id_int, title, paragraphs)
                # Truncate link titles to fit database constraints (512 chars)
                link_tuples = [(page_id_int, target_title[:512], anchor_text[:512]) for target_title, anchor_text in shard_links]
                
                result_queue.put((article_tuple, link_tuples, None))
                
            except Exception as exc:
                logger.error("Error parsing record in %s: %s", shard_str, exc)
                with errors_lock:
                    parsing_errors.append(exc)
            finally:
                raw_queue.task_done()
    
    # Create producer threads (one per shard, up to NUM_PRODUCER_THREADS)
    producer_thread_list = [Thread(target=producer, args=(shard_path,), daemon=True) 
                           for shard_path in shard_paths]
    consumer_thread = Thread(target=consumer, daemon=True)
    
    # Start all threads
    for t in producer_thread_list:
        t.start()
    consumer_thread.start()
    
    # Collect results
    completed_consumers = 0
    while completed_consumers < NUM_CONSUMER_THREADS:
        try:
            result = result_queue.get(timeout=60)  # Longer timeout for I/O operations
            if len(result) == 3:
                article_tuple, link_tuples, error = result
                if result[0] == "COMPLETED":  # Consumer completed
                    completed_consumers += 1
                elif error is not None:  # Error case
                    with errors_lock:
                        parsing_errors.append(error)
                elif article_tuple is not None:  # Success case
                    articles.append(article_tuple)
                    links.extend(link_tuples)
        except queue.Empty:
            logger.error("Timeout waiting for results from shards")
            break
    
    # Wait for all threads to complete
    for t in producer_thread_list:
        t.join(timeout=10)
    consumer_thread.join(timeout=10)
    
    # Check for errors
    if parsing_errors:
        from django.core.management.base import CommandError
        raise CommandError(f"Errors processing shards: {parsing_errors[0]}")

    return articles, links, len(articles)


class Command(BaseCommand):
    help = (
        "Load Wikipedia dump into database (assumes pre-decompressed shards). "
        "This command wipes the DB, ingests articles and internal links, and resolves link FKs."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--processed-dir", default=str(_default_processed_dir()))
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
        parser.add_argument("--db-workers", type=int, default=96, help="Number of database writer threads (default: 96)")
        parser.add_argument("--producer-threads", type=int, default=2, help="Number of I/O producer threads per worker for concurrent bz2 decompression (default: 2)")
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
        producer_threads: int = max(1, int(opts.get("producer_threads", 3)))
        limit: Optional[int] = opts.get("limit")

        logger.info("Found %d shards; starting %d workers, %d db workers, %d producer threads per worker", 
                    len(shards), workers, db_workers, producer_threads)
        
        # Phase 1: Ingest articles and links
        profiler = None
        if enable_profiling:
            profiler = cProfile.Profile()
            profiler.enable()
        
        with phase_timer("Article and Link Ingestion"):
            created_total, skipped_total, links_total = self._run_pipeline(shards, batch_size, limit, workers, db_workers, producer_threads)
        
        if enable_profiling and profiler is not None:
            profiler.disable()
            save_profile_stats(profiler, "ingestion_phase")

        logger.info(
            "Ingest complete: articles created=%d, duplicates skipped(in-batch)=%d, links created=%d",
            created_total,
            skipped_total,
            links_total,
        )

        # Phase 2: Resolve link foreign keys (merged from_article and to_article)
        if enable_profiling:
            profiler = cProfile.Profile()
            profiler.enable()
        
        with phase_timer("Resolve Link Foreign Keys"):
            call_command('resolve_links', batch_size=batch_size, db_workers=db_workers)
        
        if enable_profiling:
            profiler.disable()
            save_profile_stats(profiler, "resolve_links_merged")

        overall_elapsed = time.perf_counter() - overall_start
        logger.info("=" * 60)
        logger.info("OVERALL EXECUTION TIME: %.2f seconds", overall_elapsed)
        logger.info("Throughput: %.2f articles/second", created_total / overall_elapsed if overall_elapsed > 0 else 0)
        logger.info("=" * 60)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created_total} new articles, skipped {skipped_total} dups, created {links_total} links "
                f"in {overall_elapsed:.2f}s"
            )
        )

    def _run_pipeline(
        self,
        shards: List[Path],
        batch_size: int,
        limit: Optional[int],
        workers: int,
        db_workers: int,
        producer_threads: int = 3,
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
        estimated_shards = min(len(shards), (limit // 50) + 5) if limit else len(shards)
        pbar = tqdm(total=estimated_shards, desc="Processing shards", unit="shard", dynamic_ncols=True, position=0)
        
        # Add progress bars for database write operations
        pbar_articles = tqdm(desc="Article writes", unit="batch", dynamic_ncols=True, position=1)
        pbar_links = tqdm(desc="Link writes", unit="batch", dynamic_ncols=True, position=2)

        # Use set for O(1) deduplication instead of O(n) list iteration
        seen_article_ids: Set[int] = set()

        def flush_articles_sync(tuples_to_flush: List[Tuple[int, Optional[str], List[str]]]) -> Tuple[int, int]:
            """Synchronous article flush using COPY for speed; dedup by page_id."""
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

            created = 0
            if unique_tuples:
                from psycopg.types.json import Json  # type: ignore
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        # COPY columns explicitly; id is auto
                        with cursor.copy(
                            "COPY search_engine_article (page_id, title, plain_text_paragraphs, is_disambiguation) FROM STDIN"
                        ) as copy:
                            for page_id, title, paragraphs in unique_tuples:
                                copy.write_row([page_id, title, Json(paragraphs), False])
                created = len(unique_tuples)
                seen_article_ids.update(local_seen)

            return created, skipped

        def flush_links_sync(tuples_to_flush: List[Tuple[int, str, str]]) -> int:
            """Synchronous link flush using PostgreSQL COPY for high throughput (psycopg3)."""
            if not tuples_to_flush:
                return 0

            with transaction.atomic():
                with connection.cursor() as cursor:
                    # Use TEXT format (tab-delimited) where None -> \N automatically
                    with cursor.copy(
                        "COPY search_engine_internallink (from_article_id, from_page_id, to_article_id, to_title, anchor_text) FROM STDIN"
                    ) as copy:
                        for from_id, to_title, anchor in tuples_to_flush:
                            copy.write_row([None, from_id, None, to_title, anchor])
            return len(tuples_to_flush)

        try:
            # Use separate thread executor for database writes to avoid blocking main process
            db_write_futures: List[Any] = []
            
            with ThreadPoolExecutor(max_workers=db_workers) as db_executor, \
                 ProcessPoolExecutor(max_workers=workers) as process_executor:
                
                # For I/O-bound work (bz2 decompression), we need MANY more pending tasks
                # than workers to keep them all busy. No batching = maximum parallelism.
                # Each worker can handle multiple I/O operations concurrently.
                MAX_PENDING_FUTURES = min(workers * 4, len(shards))  # Aggressive queuing
                
                shard_iter = iter(shards)
                futures: Dict[Any, Path] = {}
                
                # Submit large initial batch to saturate all workers
                initial_submit = min(MAX_PENDING_FUTURES, len(shards))
                logger.info("Submitting initial %d futures for %d workers (I/O bound work)", initial_submit, workers)
                for _ in range(initial_submit):
                    try:
                        shard = next(shard_iter)
                        future = process_executor.submit(_process_shard_batch, [shard], record_batch_size, producer_threads)
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
                                    new_future = process_executor.submit(_process_shard_batch, [next_shard], record_batch_size, producer_threads)
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
                            pbar_articles.update(1)
                        else:  # links
                            links_total += result
                            pbar_links.update(1)
                    except Exception as exc:
                        logger.error("Error in background database write: %s", exc)
                        raise

        finally:
            pbar.close()
            pbar_articles.close()
            pbar_links.close()

        # Final flush of remaining data
        if article_tuples:
            created, skipped = flush_articles_sync(article_tuples)
            created_total += created
            skipped_total += skipped
        if link_tuples:
            links_total += flush_links_sync(link_tuples)

        return created_total, skipped_total, links_total




