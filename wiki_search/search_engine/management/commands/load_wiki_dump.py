from __future__ import annotations

import bz2
import json
import os
import logging
import shutil
import signal
import sys
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty
from typing import Dict, Iterator, List, Optional, Set, Tuple

from multiprocessing import Event, JoinableQueue, Process, Queue

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db import transaction

from search_engine.ingest.parser import extract_plain_paragraphs, extract_internal_links
from search_engine.models import Article, InternalLink

from tqdm import tqdm

logger = logging.getLogger(__name__)


@dataclass
class ShardStatus:
    completed: List[str]
    partial: List[str]
    deferred: List[str]

try:  # Prefer orjson if available
    import orjson as _json  # type: ignore
except Exception:  # pragma: no cover
    logger.warning("orjson not found, using json instead")
    import json as _json  # type: ignore

def _default_paths() -> tuple[Path, Path]:
    base = settings.BASE_DIR.parent
    archive = base / "data" / "raw" / "enwiki-20171001-pages-meta-current-withlinks-processed.tar.bz2"
    processed = base / "data" / "processed" / "enwiki-20171001-pages-meta-current-withlinks"
    return archive, processed


def _fast_extract_with_system_tar(archive: Path, target_dir: Path) -> bool:
    """Try to extract using system tar with parallel bzip2 (lbzip2/pbzip2).

    Returns True if extraction was performed, False if not available.
    """
    tar_path = shutil.which("tar")
    if not tar_path:
        return False
    # Prefer lbzip2, then pbzip2
    for prog in ("lbzip2", "pbzip2"):
        if shutil.which(prog):
            try:
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                cmd = [tar_path, "-I", prog, "-xf", str(archive), "-C", str(target_dir.parent)]
                logger.info("Using fast extractor: %s", " ".join(cmd))
                subprocess.run(cmd, check=True)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Fast extraction failed with %s: %s. Falling back.", prog, exc)
                return False
    return False


def _detect_top_dir_name(archive: Path) -> Optional[str]:
    """Best-effort: read a few headers to infer the top-level directory name."""
    try:
        with tarfile.open(archive, mode="r:bz2") as tf:
            for _ in range(32):  # read up to 32 members to find a name
                member = tf.next()
                if member is None:
                    break
                if member.name:
                    return member.name.split("/")[0]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to detect top dir name: %s", exc)
    return None


def _choose_extracted_dir(target_dir: Path, archive: Path) -> Path:
    """Resolve the actual extracted directory, falling back to heuristics."""
    if target_dir.exists():
        return target_dir
    # Try by reading top dir name from archive
    name = _detect_top_dir_name(archive)
    if name:
        candidate = target_dir.parent / name
        if candidate.exists():
            return candidate
    # Fallback: pick the only directory matching prefix
    prefix = "enwiki-20171001-pages-meta-current-withlinks"
    for child in target_dir.parent.iterdir():
        if child.is_dir() and child.name.startswith(prefix):
            return child
    return target_dir


def ensure_decompressed(archive: Path, target_dir: Path, force: bool = False, prefer_fast: bool = False) -> Path:
    # First, check if there's already a decompressed folder
    if not force:
        candidate = _choose_extracted_dir(target_dir, archive)
        if candidate.exists() and find_bz2_files(candidate):
            logger.info("Found existing decompressed directory: %s", candidate)
            return candidate

    if target_dir.exists() and not force:
        logger.info("Processed directory exists: %s", target_dir)
        return target_dir
    if not archive.exists():
        raise CommandError(f"Archive not found: {archive}")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Decompressing %s -> %s", archive, target_dir.parent)

    # Attempt fast multi-core extraction via system tar if requested/available
    if prefer_fast:
        used_fast = _fast_extract_with_system_tar(archive, target_dir)
        if used_fast:
            resolved = _choose_extracted_dir(target_dir, archive)
            if not resolved.exists():
                logger.warning("Expected processed directory not found after fast extraction: %s", resolved)
            return resolved

    with tarfile.open(archive, mode="r:bz2") as tf:
        # Basic disk space check: require at least 2x archive size free
        try:
            free_bytes = shutil.disk_usage(str(target_dir.parent)).free
            archive_size = archive.stat().st_size
            if free_bytes < 2 * archive_size:
                logger.warning(
                    "Low free space: %s bytes free, archive %s bytes. Extraction may fail.",
                    free_bytes,
                    archive_size,
                )
            else:
                logger.info("Enough free space: %s bytes free, archive %s bytes.", free_bytes, archive_size)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Disk space check failed: %s", exc)

        # Stream extraction to avoid loading all members (getmembers) which is
        # very slow for large bz2 archives
        extracted_any = False
        for member in tqdm(
            tf,
            desc="Extracting",
            unit="files",
            leave=True,
            dynamic_ncols=True,
            ascii=True,
            mininterval=0.5,
            file=sys.stdout,
        ):
            tf.extract(member, path=target_dir.parent)
            extracted_any = True
        if not extracted_any:
            logger.warning("No members extracted from archive: %s", archive)
        resolved = _choose_extracted_dir(target_dir, archive)
        if not resolved.exists():
            logger.warning("Expected processed directory not found after extraction: %s", resolved)
        return resolved


def find_bz2_files(root: Path) -> List[Path]:
    results: List[Path] = []
    if not root.exists():
        return results
    # If the expected root doesn't contain shards, try a common alternative
    # folder name ending with "-processed" (as seen in some dumps)
    if not any(root.glob("*/wiki_*.bz2")):
        alt = root.parent / f"{root.name}-processed"
        if alt.exists():
            root = alt

    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        for p in sorted(sub.glob("wiki_*.bz2")):
            results.append(p)
    return results


def iter_jsonl_bz2(file_path: Path) -> Iterator[dict]:
    try:
        with bz2.open(file_path, mode="rb") as raw:
            for line in raw:
                if not line.strip():
                    continue
                try:
                    yield _json.loads(line)
                except Exception:
                    try:
                        yield _json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
    except (EOFError, OSError) as e:
        logger.warning("Skipping corrupted file %s: %s", file_path, e)
        return
    except Exception as e:
        logger.error("Unexpected error reading file %s: %s", file_path, e)
        return


class Command(BaseCommand):
    help = "Load Wikipedia dump into SQLite"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.checkpoint_file: Optional[Path] = None
        self.checkpoint_data: Dict = {}
        self.shutdown_requested = False

    def _get_checkpoint_path(self) -> Path:
        """Get the checkpoint file path."""
        if self.checkpoint_file is None:
            base = settings.BASE_DIR.parent
            self.checkpoint_file = base / "data" / ".load_checkpoint.json"
        return self.checkpoint_file

    def _load_checkpoint(self) -> Dict:
        """Load checkpoint data from file."""
        checkpoint_path = self._get_checkpoint_path()
        if not checkpoint_path.exists():
            return {
                "completed_shards": [],
                "total_articles_created": 0,
                "total_articles_skipped": 0,
                "total_links_created": 0,
                "last_updated": None,
                "partial_shards": [],
                "deferred_shards": [],
            }

        try:
            with open(checkpoint_path, 'r') as f:
                data = json.load(f)
                # Ensure all required keys exist
                return {
                    "completed_shards": data.get("completed_shards", []),
                    "total_articles_created": data.get("total_articles_created", 0),
                    "total_articles_skipped": data.get("total_articles_skipped", 0),
                    "total_links_created": data.get("total_links_created", 0),
                    "last_updated": data.get("last_updated"),
                    "partial_shards": data.get("partial_shards", []),
                    "deferred_shards": data.get("deferred_shards", []),
                }
        except Exception as exc:
            logger.warning("Failed to load checkpoint: %s. Starting fresh.", exc)
            return {
                "completed_shards": [],
                "total_articles_created": 0,
                "total_articles_skipped": 0,
                "total_links_created": 0,
                "last_updated": None,
                "partial_shards": [],
                "deferred_shards": [],
            }

    def _save_checkpoint(self, status: ShardStatus, articles_created: int, articles_skipped: int, links_created: int = 0) -> None:
        """Save checkpoint data to file."""
        checkpoint_path = self._get_checkpoint_path()
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

        self.checkpoint_data.update({
            "completed_shards": status.completed,
            "total_articles_created": articles_created,
            "total_articles_skipped": articles_skipped,
            "total_links_created": links_created,
            "last_updated": datetime.now().isoformat(),
            "partial_shards": status.partial,
            "deferred_shards": status.deferred,
        })

        try:
            with open(checkpoint_path, 'w') as f:
                json.dump(self.checkpoint_data, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save checkpoint: %s", exc)

    def _clear_checkpoint(self) -> None:
        """Clear the checkpoint file."""
        checkpoint_path = self._get_checkpoint_path()
        if checkpoint_path.exists():
            checkpoint_path.unlink()
            logger.info("Cleared checkpoint file: %s", checkpoint_path)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info("Received signal %d, initiating graceful shutdown...", signum)
        self.shutdown_requested = True

    def add_arguments(self, parser) -> None:
        default_archive, default_processed = _default_paths()
        parser.add_argument("--archive", default=str(default_archive))
        parser.add_argument("--processed-dir", default=str(default_processed))
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
        parser.add_argument("--limit", type=int)
        parser.add_argument("--force-decompress", action="store_true")
        parser.add_argument("--skip-decompress", action="store_true")
        parser.add_argument(
            "--no-fast-extract",
            action="store_true",
            help="Disable fast extractor; use Python tarfile streaming",
        )
        parser.add_argument(
            "--worker-batch-size",
            type=int,
            default=2048,
            help="Max records per worker emission (reduces IPC overhead)",
        )
        parser.add_argument(
            "--clear-checkpoint",
            action="store_true",
            help="Clear checkpoint and start fresh",
        )

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO)

        def _enable_sqlite_ingest_pragmas() -> None:
            if connection.vendor != "sqlite":
                return
            try:
                with connection.cursor() as cur:
                    cur.execute("PRAGMA journal_mode=WAL;")
                    cur.execute("PRAGMA synchronous=NORMAL;")
                    cur.execute("PRAGMA temp_store=MEMORY;")
                    cur.execute("PRAGMA mmap_size=30000000000;")
                    cur.execute("PRAGMA cache_size=-200000;")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to apply SQLite PRAGMAs: %s", exc)

        _enable_sqlite_ingest_pragmas()
        archive = Path(opts["archive"]).expanduser()
        processed_dir = Path(opts["processed_dir"]).expanduser()
        batch_size: int = opts["batch_size"]
        limit: Optional[int] = opts.get("limit")
        force_decompress = bool(opts["force_decompress"])
        skip_decompress = bool(opts["skip_decompress"])
        fast_extract = not bool(opts["no_fast_extract"])  # default to fast extract
        workers: int = max(1, int(opts["workers"]))
        clear_checkpoint = bool(opts["clear_checkpoint"])

        # Handle checkpoint clearing
        if clear_checkpoint:
            self._clear_checkpoint()

        # Load checkpoint data
        self.checkpoint_data = self._load_checkpoint()
        completed_shards_set = set(self.checkpoint_data["completed_shards"])

        # Show resume info
        if completed_shards_set:
            logger.info("Resuming from checkpoint: %d shards already processed", len(completed_shards_set))
            logger.info("Previous run created %d articles, skipped %d duplicates, created %d links",
                       self.checkpoint_data["total_articles_created"],
                       self.checkpoint_data["total_articles_skipped"],
                       self.checkpoint_data["total_links_created"])
            if self.checkpoint_data["last_updated"]:
                logger.info("Last checkpoint: %s", self.checkpoint_data["last_updated"])
        else:
            logger.info("Starting fresh - no previous checkpoint found")

        if not skip_decompress:
            processed_dir = ensure_decompressed(archive, processed_dir, force=force_decompress, prefer_fast=fast_extract)
            logger.info("Decompressed archive: %s", archive)
        else:
            # When skipping decompression, still try to resolve the actual
            # extracted directory location by checking for an "-processed" suffix
            alt = processed_dir.parent / f"{processed_dir.name}-processed"
            if alt.exists():
                processed_dir = alt

        all_bz2_files = find_bz2_files(processed_dir)
        if not all_bz2_files:
            raise CommandError(f"No wiki_*.bz2 files found under {processed_dir}")

        # Filter out already processed shards
        bz2_files = []
        for shard_path in all_bz2_files:
            # Create relative path for checkpoint tracking (e.g., "AA/wiki_00.bz2")
            relative_path = shard_path.relative_to(processed_dir)
            shard_key = str(relative_path)
            if shard_key not in completed_shards_set:
                bz2_files.append(shard_path)

        if not bz2_files:
            logger.info("All shards already processed! Nothing to do.")
            self.stdout.write(self.style.SUCCESS("All shards already processed"))
            return

        logger.info("Found %d total shards, %d remaining to process; using %d workers",
                   len(all_bz2_files), len(bz2_files), workers)

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        initial_status = ShardStatus(
            completed=list(completed_shards_set),
            partial=list(self.checkpoint_data.get("partial_shards", [])),
            deferred=list(self.checkpoint_data.get("deferred_shards", [])),
        )

        worker_batch_size: int = int(opts.get("worker_batch_size") or 2048)

        created_total, skipped_total, links_total = self._run_pipeline(
            bz2_files,
            batch_size,
            limit,
            workers,
            completed_shards_set,
            initial_status,
            worker_batch_size,
        )

        self.stdout.write(self.style.SUCCESS(f"Created {created_total} new articles, skipped {skipped_total} duplicates, created {links_total} links"))

    def _flush_batch(self, batch: List[Article], batch_size: int) -> Tuple[int, int]:
        """Flush batch to database and return (created_count, skipped_count)."""
        if not batch:
            return 0, 0

        to_insert: List[Article] = []
        seen_new_ids: Set[int] = set()
        skipped_count = 0

        for article in batch:
            page_id = article.page_id
            if page_id in seen_new_ids:
                skipped_count += 1
                continue
            seen_new_ids.add(page_id)
            to_insert.append(article)

        created_count = 0

        if to_insert:
            with transaction.atomic():
                Article.objects.bulk_create(to_insert, batch_size=batch_size, ignore_conflicts=True)
            created_count = len(to_insert)

        batch.clear()
        return created_count, skipped_count

    def _flush_link_batch(self, link_batch: List[InternalLink], batch_size: int) -> int:
        """Flush link batch to database and return created_count."""
        if not link_batch:
            return 0

        created_count = 0
        try:
            with transaction.atomic():
                InternalLink.objects.bulk_create(link_batch, batch_size=batch_size, ignore_conflicts=True)
            created_count = len(link_batch)
        except Exception as exc:
            logger.error("Failed to bulk create links: %s", exc)
            # Try individual inserts as fallback
            for link in link_batch:
                try:
                    link.save()
                    created_count += 1
                except Exception:
                    continue

        link_batch.clear()
        return created_count

    def _run_pipeline(
        self,
        shards: List[Path],
        batch_size: int,
        limit: Optional[int],
        workers: int,
        completed_shards_set: Set[str],
        initial_status: ShardStatus,
        worker_batch_size: int,
    ) -> Tuple[int, int, int]:
        """Run a multi-process pipeline with explicit worker signalling and graceful shutdown."""
        base_dir = shards[0].parent.parent if shards else None

        shard_queue: JoinableQueue = JoinableQueue()
        # Prefer SimpleQueue for lower overhead (no size checks)
        try:
            from multiprocessing import SimpleQueue  # type: ignore
            result_queue = SimpleQueue()
        except Exception:  # pragma: no cover
            result_queue = Queue(maxsize=0)
        stop_event = Event()
        record_batch_size = max(1, min(batch_size, worker_batch_size))

        for shard in shards:
            shard_queue.put(shard)
        for _ in range(workers):
            shard_queue.put(None)

        def make_shard_key(path_str: str) -> str:
            shard_path = Path(path_str)
            if base_dir:
                try:
                    return str(shard_path.relative_to(base_dir))
                except ValueError:
                    pass
            return shard_path.name

        def worker_loop(worker_id: int) -> None:
            while True:
                shard = shard_queue.get()
                if shard is None:
                    shard_queue.task_done()
                    break

                shard_str = str(shard)
                try:
                    if stop_event.is_set():
                        result_queue.put(("shard_deferred", shard_str, 0))
                        continue

                    records_emitted = 0
                    finished_normally = True
                    batch_buffer: list[tuple[int, Optional[str], list[str]]] = []
                    link_buffer: list[tuple[int, str, str]] = []

                    try:
                        link_batch_size = max(100, record_batch_size * 10)  # Links are smaller
                        
                        for rec in iter_jsonl_bz2(shard):
                            if stop_event.is_set():
                                finished_normally = False
                                break

                            page_id_raw = rec.get("id")
                            if page_id_raw is None:
                                continue
                            try:
                                page_id_int = int(page_id_raw)
                            except Exception:
                                continue
                            title = rec.get("title")
                            text = rec.get("text") or []
                            paragraphs = extract_plain_paragraphs(text)
                            links = extract_internal_links(text)

                            # Add article to batch
                            batch_buffer.append((page_id_int, title, paragraphs))
                            
                            # Add links to link buffer
                            for target_title, anchor_text in links:
                                link_buffer.append((page_id_int, target_title, anchor_text))
                            
                            # Emit article batch when full
                            if len(batch_buffer) >= record_batch_size:
                                result_queue.put(("record_batch", shard_str, batch_buffer))
                                records_emitted += len(batch_buffer)
                                batch_buffer = []
                            
                            # Emit link batch when full
                            if len(link_buffer) >= link_batch_size:
                                result_queue.put(("link_batch", shard_str, link_buffer))
                                link_buffer = []

                        # Emit remaining buffers
                        if batch_buffer:
                            result_queue.put(("record_batch", shard_str, batch_buffer))
                            records_emitted += len(batch_buffer)
                        
                        if link_buffer:
                            result_queue.put(("link_batch", shard_str, link_buffer))
                    except Exception as e:
                        logger.error("Error processing shard %s: %s", shard, e)
                        finished_normally = False

                    completion_type = "shard_done" if finished_normally else "partial_shard"
                    result_queue.put((completion_type, shard_str, records_emitted))
                finally:
                    shard_queue.task_done()

                if stop_event.is_set():
                    continue

            result_queue.put(("worker_done", worker_id))

        procs = [Process(target=worker_loop, args=(idx,)) for idx in range(workers)]
        for proc in procs:
            proc.daemon = True
            proc.start()

        created_total = 0
        skipped_total = 0
        links_total = 0
        batch: List[Article] = []
        processed_records = 0
        link_accumulator: List[InternalLink] = []
        LINK_FLUSH_THRESHOLD = max(50_000, batch_size * 20)

        completed_shards_list = list(initial_status.completed)
        completed_shards_seen: Set[str] = set(completed_shards_list)
        partial_shards: Set[str] = set(initial_status.partial or [])
        deferred_shards: Set[str] = set(initial_status.deferred or [])

        for shard_key in list(partial_shards) + list(deferred_shards):
            if shard_key in completed_shards_seen:
                completed_shards_seen.remove(shard_key)
                completed_shards_list = [s for s in completed_shards_list if s != shard_key]

        workers_remaining = workers

        total_shards = len(shards)
        processed_shards = 0
        shard_key_cache: Dict[str, str] = {}

        def flush_batch() -> None:
            nonlocal created_total, skipped_total
            if batch:
                created, skipped = self._flush_batch(batch, batch_size)
                created_total += created
                skipped_total += skipped

        pbar = tqdm(
            total=total_shards,
            desc="Processing shards",
            unit="shard",
            initial=processed_shards,
            leave=True,
            dynamic_ncols=True,
        )
        try:
            while workers_remaining > 0:
                if self.shutdown_requested and not stop_event.is_set():
                    logger.info("Shutdown requested, signalling workers to stop...")
                    stop_event.set()
                if limit is not None and processed_records >= limit and not stop_event.is_set():
                    logger.info("Limit %s reached; signalling workers to stop...", limit)
                    stop_event.set()

                try:
                    message = result_queue.get(timeout=0.5)
                except Empty:
                    continue

                kind = message[0]

                if kind == "record_batch":
                    if limit is not None and processed_records >= limit:
                        continue
                    _, shard_str, payload = message
                    for page_id_int, title, paragraphs in payload:
                        if limit is not None and processed_records >= limit:
                            break
                        batch.append(
                            Article(
                                page_id=page_id_int,
                                title=title,
                                plain_text_paragraphs=paragraphs,
                            )
                        )
                        processed_records += 1
                        if len(batch) >= batch_size:
                            flush_batch()
                elif kind == "link_batch":
                    _, shard_str, link_payload = message
                    for page_id, target_title, anchor_text in link_payload:
                        link_accumulator.append(
                            InternalLink(
                                from_page_id=page_id,
                                to_title=target_title,
                                anchor_text=anchor_text,
                            )
                        )
                    if len(link_accumulator) >= LINK_FLUSH_THRESHOLD:
                        links_total += self._flush_link_batch(link_accumulator, batch_size)
                elif kind == "shard_done":
                    _, shard_str, _count = message
                    shard_key = shard_key_cache.setdefault(shard_str, make_shard_key(shard_str))
                    partial_shards.discard(shard_key)
                    deferred_shards.discard(shard_key)
                    if shard_key not in completed_shards_seen:
                        completed_shards_seen.add(shard_key)
                        completed_shards_list.append(shard_key)
                        pbar.update(1)
                        # Periodic checkpointing
                        if completed_shards_list and len(completed_shards_list) % 50 == 0:
                            self._save_checkpoint(
                                ShardStatus(
                                    completed=list(completed_shards_list),
                                    partial=sorted(list(partial_shards)),
                                    deferred=sorted(list(deferred_shards)),
                                ),
                                created_total,
                                skipped_total,
                                links_total,
                            )
                        # Opportunistically flush accumulated links at shard boundaries
                        if link_accumulator and len(link_accumulator) >= LINK_FLUSH_THRESHOLD:
                            links_total += self._flush_link_batch(link_accumulator, batch_size)
                elif kind == "partial_shard":
                    _, shard_str, _count = message
                    shard_key = shard_key_cache.setdefault(shard_str, make_shard_key(shard_str))
                    partial_shards.add(shard_key)
                    deferred_shards.discard(shard_key)
                    if shard_key in completed_shards_seen:
                        completed_shards_seen.remove(shard_key)
                        completed_shards_list = [s for s in completed_shards_list if s != shard_key]
                elif kind == "shard_deferred":
                    _, shard_str, _count = message
                    shard_key = shard_key_cache.setdefault(shard_str, make_shard_key(shard_str))
                    deferred_shards.add(shard_key)
                    partial_shards.discard(shard_key)
                    if shard_key in completed_shards_seen:
                        completed_shards_seen.remove(shard_key)
                        completed_shards_list = [s for s in completed_shards_list if s != shard_key]
                elif kind == "worker_done":
                    workers_remaining -= 1
                else:
                    logger.debug("Received unexpected message type from worker: %s", kind)
        finally:
            pbar.close()

        flush_batch()
        if link_accumulator:
            links_total += self._flush_link_batch(link_accumulator, batch_size)

        if partial_shards:
            logger.info("Partial shards detected (will remain pending): %s", sorted(list(partial_shards)))
        if deferred_shards:
            logger.info("Deferred shards due to cancellation: %s", sorted(list(deferred_shards)))

        self._save_checkpoint(
            ShardStatus(
                completed=list(completed_shards_list),
                partial=sorted(list(partial_shards)),
                deferred=sorted(list(deferred_shards)),
            ),
            created_total,
            skipped_total,
            links_total,
        )

        stop_event.set()
        shard_queue.join()

        for proc in procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()

        return created_total, skipped_total, links_total
