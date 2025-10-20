from __future__ import annotations

import bz2
import logging
import os
from pathlib import Path
from queue import Empty
from typing import Dict, Iterator, List, Optional, Set, Tuple

from multiprocessing import JoinableQueue, Process, Queue

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


def iter_jsonl_bz2(file_path: Path) -> Iterator[dict]:
    with bz2.open(file_path, mode="rt", encoding="utf-8", errors="strict") as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                yield _json.loads(line)
            except Exception as exc:
                logger.error("Invalid JSON at %s:%d: %s", file_path, line_number, exc)
                raise


class Command(BaseCommand):
    help = (
        "Load Wikipedia dump into database (assumes pre-decompressed shards). "
        "This command wipes the DB, ingests articles and internal links, and resolves link FKs."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--processed-dir", default=str(_default_processed_dir()))
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
        parser.add_argument("--limit", type=int)

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

        # Always clean the DB first
        logger.info("Cleaning database tables before ingestion")
        call_command("clean_db", yes=True, no_progress=True, drop_recreate=True)

        processed_dir = Path(opts["processed_dir"]).expanduser()
        if not processed_dir.exists():
            raise CommandError(f"Processed directory not found: {processed_dir}")

        shards = find_bz2_files(processed_dir)
        if not shards:
            raise CommandError(f"No wiki_*.bz2 files found under {processed_dir}")

        batch_size: int = int(opts["batch_size"]) or 5000
        workers: int = max(1, int(opts["workers"]))
        limit: Optional[int] = opts.get("limit")

        logger.info("Found %d shards; starting %d workers", len(shards), workers)
        created_total, skipped_total, links_total = self._run_pipeline(shards, batch_size, limit, workers)

        logger.info(
            "Ingest complete: articles created=%d, duplicates skipped(in-batch)=%d, links created=%d",
            created_total,
            skipped_total,
            links_total,
        )

        updated_from = self._resolve_from_article(batch_size)
        updated_to = self._resolve_to_article(batch_size)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created_total} new articles, skipped {skipped_total} dups, created {links_total} links; "
                f"resolved from_article={updated_from}, to_article={updated_to}"
            )
        )

    def _run_pipeline(
        self,
        shards: List[Path],
        batch_size: int,
        limit: Optional[int],
        workers: int,
    ) -> Tuple[int, int, int]:
        shard_queue: JoinableQueue = JoinableQueue()
        result_queue: Queue = Queue(maxsize=0)
        record_batch_size = max(1, min(batch_size, 2048))

        for shard in shards:
            shard_queue.put(shard)
        for _ in range(workers):
            shard_queue.put(None)

        def worker_loop(worker_id: int) -> None:
            while True:
                shard = shard_queue.get()
                if shard is None:
                    shard_queue.task_done()
                    break

                shard_str = str(shard)
                records_emitted = 0
                batch_buffer: List[Tuple[int, Optional[str], List[str]]] = []
                link_buffer: List[Tuple[int, str, str]] = []
                link_batch_size = max(100, record_batch_size * 10)

                for rec in iter_jsonl_bz2(shard):
                    page_id_raw = rec.get("id")
                    if page_id_raw is None:
                        logger.error("Record missing 'id' in %s", shard_str)
                        raise ValueError(f"Missing id in record from {shard_str}")
                    try:
                        page_id_int = int(page_id_raw)
                    except Exception as exc:
                        logger.error("Non-integer id in %s: %r (%s)", shard_str, page_id_raw, exc)
                        raise

                    title = rec.get("title")
                    text = rec.get("text") or []
                    paragraphs = extract_plain_paragraphs(text)
                    links = extract_internal_links(text)

                    batch_buffer.append((page_id_int, title, paragraphs))
                    for target_title, anchor_text in links:
                        link_buffer.append((page_id_int, target_title, anchor_text))

                    if len(batch_buffer) >= record_batch_size:
                        result_queue.put(("record_batch", shard_str, batch_buffer))
                        records_emitted += len(batch_buffer)
                        batch_buffer = []

                    if len(link_buffer) >= link_batch_size:
                        result_queue.put(("link_batch", shard_str, link_buffer))
                        link_buffer = []

                if batch_buffer:
                    result_queue.put(("record_batch", shard_str, batch_buffer))
                    records_emitted += len(batch_buffer)
                if link_buffer:
                    result_queue.put(("link_batch", shard_str, link_buffer))

                result_queue.put(("shard_done", shard_str, records_emitted))
                shard_queue.task_done()

            result_queue.put(("worker_done", worker_id))

        procs = [Process(target=worker_loop, args=(idx,)) for idx in range(workers)]
        for proc in procs:
            proc.start()

        created_total = 0
        skipped_total = 0
        links_total = 0
        processed_records = 0

        article_batch: List[Article] = []
        link_accumulator: List[InternalLink] = []
        LINK_FLUSH_THRESHOLD = max(50_000, batch_size * 20)

        remaining_workers = workers
        pbar = tqdm(total=len(shards), desc="Processing shards", unit="shard", dynamic_ncols=True)

        def flush_articles() -> None:
            nonlocal created_total, skipped_total
            if not article_batch:
                return
            to_insert: List[Article] = []
            seen_new_ids: Set[int] = set()
            for a in article_batch:
                if a.page_id in seen_new_ids:
                    skipped_total += 1
                    continue
                seen_new_ids.add(a.page_id)
                to_insert.append(a)
            with transaction.atomic():
                Article.objects.bulk_create(to_insert, batch_size=batch_size, ignore_conflicts=True)
            created_total += len(to_insert)
            article_batch.clear()

        def flush_links() -> None:
            nonlocal links_total
            if not link_accumulator:
                return
            with transaction.atomic():
                InternalLink.objects.bulk_create(link_accumulator, batch_size=batch_size, ignore_conflicts=True)
            links_total += len(link_accumulator)
            link_accumulator.clear()

        try:
            while remaining_workers > 0:
                for proc in procs:
                    if proc.exitcode is not None and proc.exitcode != 0:
                        raise CommandError(f"Worker process {proc.pid} exited with code {proc.exitcode}")

                try:
                    message = result_queue.get(timeout=1.0)
                except Empty:
                    continue

                kind = message[0]
                if kind == "record_batch":
                    if limit is not None and processed_records >= limit:
                        continue
                    _, _shard, payload = message
                    for page_id_int, title, paragraphs in payload:
                        if limit is not None and processed_records >= limit:
                            break
                        article_batch.append(
                            Article(
                                page_id=page_id_int,
                                title=title,
                                plain_text_paragraphs=paragraphs,
                            )
                        )
                        processed_records += 1
                        if len(article_batch) >= batch_size:
                            flush_articles()

                elif kind == "link_batch":
                    _, _shard, payload = message
                    for page_id_int, to_title, anchor_text in payload:
                        link_accumulator.append(
                            InternalLink(
                                from_page_id=page_id_int,
                                to_title=to_title,
                                anchor_text=anchor_text,
                            )
                        )
                    if len(link_accumulator) >= LINK_FLUSH_THRESHOLD:
                        flush_links()

                elif kind == "shard_done":
                    pbar.update(1)
                elif kind == "worker_done":
                    remaining_workers -= 1
                else:
                    logger.warning("Unexpected message kind: %s", kind)
        finally:
            pbar.close()

        flush_articles()
        flush_links()

        return created_total, skipped_total, links_total

    def _resolve_from_article(self, batch_size: int) -> int:
        unresolved_qs = InternalLink.objects.filter(from_article__isnull=True, from_page_id__isnull=False)
        unresolved_count = unresolved_qs.count()
        if unresolved_count == 0:
            logger.info("No from_article links to resolve")
            return 0

        logger.info("Resolving from_article for %d links", unresolved_count)

        page_ids = list(
            unresolved_qs.values_list("from_page_id", flat=True).distinct()
        )
        page_id_to_article_id: Dict[int, int] = {}
        for i in tqdm(range(0, len(page_ids), batch_size), desc="Mapping page_ids", unit="batch", dynamic_ncols=True):
            batch = page_ids[i : i + batch_size]
            page_id_to_article_id.update(
                dict(Article.objects.filter(page_id__in=batch).values_list("page_id", "id"))
            )

        updated_total = 0
        for _ in tqdm(range(0, unresolved_count, batch_size), desc="Updating from_article", unit="batch", dynamic_ncols=True):
            links = list(unresolved_qs[:batch_size])
            if not links:
                break
            to_update: List[InternalLink] = []
            for link in links:
                aid = page_id_to_article_id.get(link.from_page_id)
                if aid is not None:
                    link.from_article_id = aid
                    to_update.append(link)
            if to_update:
                with transaction.atomic():
                    InternalLink.objects.bulk_update(to_update, ["from_article"], batch_size=batch_size)
                updated_total += len(to_update)

        logger.info("Resolved from_article for %d links", updated_total)
        return updated_total

    def _resolve_to_article(self, batch_size: int) -> int:
        unresolved_qs = InternalLink.objects.filter(to_article__isnull=True)
        unresolved_count = unresolved_qs.count()
        if unresolved_count == 0:
            logger.info("No to_article links to resolve")
            return 0

        logger.info("Resolving to_article for %d links", unresolved_count)

        titles = list(
            unresolved_qs.values_list("to_title", flat=True).distinct()
        )
        title_to_article_id: Dict[str, int] = {}
        for i in tqdm(range(0, len(titles), batch_size), desc="Mapping titles", unit="batch", dynamic_ncols=True):
            batch = titles[i : i + batch_size]
            title_to_article_id.update(
                dict(Article.objects.filter(title__in=batch).values_list("title", "id"))
            )

        updated_total = 0
        for _ in tqdm(range(0, unresolved_count, batch_size), desc="Updating to_article", unit="batch", dynamic_ncols=True):
            links = list(unresolved_qs[:batch_size])
            if not links:
                break
            to_update: List[InternalLink] = []
            for link in links:
                aid = title_to_article_id.get(link.to_title)
                if aid is not None:
                    link.to_article_id = aid
                    to_update.append(link)
            if to_update:
                with transaction.atomic():
                    InternalLink.objects.bulk_update(to_update, ["to_article"], batch_size=batch_size)
                updated_total += len(to_update)

        logger.info("Resolved to_article for %d links", updated_total)
        return updated_total


