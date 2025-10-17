from __future__ import annotations

import bz2
import io
import json
import os
import logging
import shutil
import signal
import sys
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from search_engine.ingest.parser import extract_plain_paragraphs
from search_engine.models import Article

try:
    from tqdm import tqdm  # type: ignore
    _tqdm_available = True
except Exception:  # pragma: no cover
    _tqdm_available = False
    def tqdm(iterable, **kwargs):  # type: ignore
        return iterable

logger = logging.getLogger(__name__)

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
    # Wrap BZ2 in a buffered reader to increase throughput
    with bz2.open(file_path, mode="rb") as raw:
        with io.BufferedReader(raw, buffer_size=1024 * 1024) as buf:  # 1MB buffer
            for line in buf:
                if not line.strip():
                    continue
                try:
                    yield _json.loads(line)
                except Exception:
                    # If orjson in binary mode returns bytes, try decode fallback
                    try:
                        yield _json.loads(line.decode("utf-8"))
                    except Exception:
                        continue


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
                "last_updated": None
            }
        
        try:
            with open(checkpoint_path, 'r') as f:
                data = json.load(f)
                # Ensure all required keys exist
                return {
                    "completed_shards": data.get("completed_shards", []),
                    "total_articles_created": data.get("total_articles_created", 0),
                    "total_articles_skipped": data.get("total_articles_skipped", 0),
                    "last_updated": data.get("last_updated")
                }
        except Exception as exc:
            logger.warning("Failed to load checkpoint: %s. Starting fresh.", exc)
            return {
                "completed_shards": [],
                "total_articles_created": 0,
                "total_articles_skipped": 0,
                "last_updated": None
            }

    def _save_checkpoint(self, completed_shards: List[str], articles_created: int, articles_skipped: int) -> None:
        """Save checkpoint data to file."""
        checkpoint_path = self._get_checkpoint_path()
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.checkpoint_data.update({
            "completed_shards": completed_shards,
            "total_articles_created": articles_created,
            "total_articles_skipped": articles_skipped,
            "last_updated": datetime.now().isoformat()
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
            "--clear-checkpoint",
            action="store_true",
            help="Clear checkpoint and start fresh",
        )

    def handle(self, *args, **opts):
        logging.basicConfig(level=logging.INFO)
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
            logger.info("Previous run created %d articles, skipped %d duplicates", 
                       self.checkpoint_data["total_articles_created"],
                       self.checkpoint_data["total_articles_skipped"])
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

        created_total, skipped_total = self._run_pipeline(bz2_files, batch_size, limit, workers, completed_shards_set)

        self.stdout.write(self.style.SUCCESS(f"Created {created_total} new articles, skipped {skipped_total} duplicates"))

    def _flush_batch(self, batch: List[Article], batch_size: int) -> Tuple[int, int]:
        """Flush batch to database and return (created_count, skipped_count)."""
        if not batch:
            return 0, 0
        
        # Count existing articles to determine how many were actually created
        page_ids = [article.page_id for article in batch]
        existing_count = Article.objects.filter(page_id__in=page_ids).count()
        
        with transaction.atomic():
            Article.objects.bulk_create(batch, batch_size=batch_size, ignore_conflicts=True)
        
        created_count = len(batch) - existing_count
        skipped_count = existing_count
        batch.clear()
        return created_count, skipped_count

    def _run_pipeline(self, shards: List[Path], batch_size: int, limit: Optional[int], workers: int, completed_shards_set: Set[str]) -> Tuple[int, int]:
        """Run a multi-process pipeline: parser workers -> single writer.

        Workers parse shards and emit tuples (shard_path, page_id:int, title:str, paragraphs:List[str]).
        The writer constructs Article instances and bulk inserts, tracking shard completion.
        """
        from multiprocessing import Process, Queue

        shard_q: Queue = Queue()
        out_q: Queue = Queue(maxsize=10000)

        def put_shards() -> None:
            for s in shards:
                shard_q.put(s)
            for _ in range(workers):
                shard_q.put(None)

        def worker_loop() -> None:
            while True:
                shard = shard_q.get()
                if shard is None:
                    break
                for rec in iter_jsonl_bz2(shard):
                    page_id_raw = rec.get("id")
                    title = rec.get("title")
                    text = rec.get("text") or []
                    try:
                        page_id_int = int(page_id_raw)
                    except Exception:
                        continue
                    paragraphs = extract_plain_paragraphs(text)
                    # Include shard path for tracking completion
                    out_q.put((shard, page_id_int, title, paragraphs))

        # Start shard feeder
        feeder = Process(target=put_shards)
        feeder.start()

        # Start workers
        procs = [Process(target=worker_loop) for _ in range(workers)]
        for p in procs:
            p.daemon = True
            p.start()

        # Writer loop in main process (owns Django ORM)
        created_total = 0
        skipped_total = 0
        batch: List[Article] = []
        processed_records = 0
        alive = workers
        current_shard: Optional[Path] = None
        completed_shards_list = list(completed_shards_set)
        
        # Progress tracking
        total_shards = len(shards)
        processed_shards = len(completed_shards_set)
        
        with tqdm(total=total_shards, desc="Processing shards", unit="shard", 
                 initial=processed_shards, leave=True, dynamic_ncols=True) as pbar:
            
            while True:
                # Check for shutdown signal
                if self.shutdown_requested:
                    logger.info("Shutdown requested, saving checkpoint...")
                    break
                    
                if limit and processed_records >= limit:
                    break
                    
                try:
                    item = out_q.get(timeout=0.5)
                except Exception:
                    # Check if workers are done
                    alive = sum(1 for p in procs if p.is_alive())
                    if alive == 0 and out_q.empty():
                        break
                    continue
                    
                shard_path, page_id_int, title, paragraphs = item
                
                # Track shard completion
                if current_shard != shard_path:
                    if current_shard is not None:
                        # Mark previous shard as completed
                        relative_path = current_shard.relative_to(current_shard.parent.parent)
                        shard_key = str(relative_path)
                        if shard_key not in completed_shards_list:
                            completed_shards_list.append(shard_key)
                        pbar.update(1)
                        
                        # Save checkpoint periodically (every 10 shards)
                        if len(completed_shards_list) % 10 == 0:
                            self._save_checkpoint(completed_shards_list, created_total, skipped_total)
                    
                    current_shard = shard_path
                
                batch.append(Article(page_id=page_id_int, title=title, plain_text_paragraphs=paragraphs))
                processed_records += 1
                
                if len(batch) >= batch_size or (limit and processed_records >= limit):
                    created, skipped = self._flush_batch(batch, batch_size)
                    created_total += created
                    skipped_total += skipped

        # Final flush
        if batch:
            created, skipped = self._flush_batch(batch, batch_size)
            created_total += created
            skipped_total += skipped

        # Mark final shard as completed if we were processing it
        if current_shard is not None:
            relative_path = current_shard.relative_to(current_shard.parent.parent)
            shard_key = str(relative_path)
            if shard_key not in completed_shards_list:
                completed_shards_list.append(shard_key)
            pbar.update(1)

        # Save final checkpoint
        self._save_checkpoint(completed_shards_list, created_total, skipped_total)

        # Graceful shutdown of workers
        try:
            for p in procs:
                if p.is_alive():
                    p.terminate()
            if feeder.is_alive():
                feeder.terminate()
        except Exception:
            pass
            
        for p in procs:
            p.join(timeout=5)
        feeder.join(timeout=5)

        return created_total, skipped_total


