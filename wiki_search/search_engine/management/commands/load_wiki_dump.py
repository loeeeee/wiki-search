from __future__ import annotations

import bz2
import io
import os
import logging
import shutil
import sys
import subprocess
import tarfile
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

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

        if not skip_decompress:
            processed_dir = ensure_decompressed(archive, processed_dir, force=force_decompress, prefer_fast=fast_extract)
            logger.info("Decompressed archive: %s", archive)
        else:
            # When skipping decompression, still try to resolve the actual
            # extracted directory location by checking for an "-processed" suffix
            alt = processed_dir.parent / f"{processed_dir.name}-processed"
            if alt.exists():
                processed_dir = alt

        bz2_files = find_bz2_files(processed_dir)
        if not bz2_files:
            raise CommandError(f"No wiki_*.bz2 files found under {processed_dir}")

        logger.info("Found %d compressed shards; using %d workers", len(bz2_files), workers)

        created_total = self._run_pipeline(bz2_files, batch_size, limit, workers)

        self.stdout.write(self.style.SUCCESS(f"Created ~{created_total} articles"))

    def _flush_batch(self, batch: List[Article], batch_size: int) -> int:
        if not batch:
            return 0
        with transaction.atomic():
            Article.objects.bulk_create(batch, batch_size=batch_size, ignore_conflicts=True)
        created = len(batch)
        batch.clear()
        return created

    def _run_pipeline(self, shards: List[Path], batch_size: int, limit: Optional[int], workers: int) -> int:
        """Run a multi-process pipeline: parser workers -> single writer.

        Workers parse shards and emit tuples (page_id:int, title:str, paragraphs:List[str]).
        The writer constructs Article instances and bulk inserts.
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
                    out_q.put((page_id_int, title, paragraphs))

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
        batch: List[Article] = []
        processed_records = 0
        alive = workers
        while True:
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
            page_id_int, title, paragraphs = item
            batch.append(Article(page_id=page_id_int, title=title, plain_text_paragraphs=paragraphs))
            processed_records += 1
            if len(batch) >= batch_size or (limit and processed_records >= limit):
                created_total += self._flush_batch(batch, batch_size)

        # Final flush
        created_total += self._flush_batch(batch, batch_size)

        # If we exited due to limit, stop workers quickly
        if limit and processed_records >= limit:
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

        return created_total


