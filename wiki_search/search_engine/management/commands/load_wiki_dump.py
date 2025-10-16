from __future__ import annotations

import bz2
import json
import logging
import shutil
import sys
import subprocess
import tarfile
from pathlib import Path
from typing import Iterator, List, Optional

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


def ensure_decompressed(archive: Path, target_dir: Path, force: bool = False, prefer_fast: bool = False) -> None:
    if target_dir.exists() and not force:
        logger.info("Processed directory exists: %s", target_dir)
        return
    if not archive.exists():
        raise CommandError(f"Archive not found: {archive}")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Decompressing %s -> %s", archive, target_dir.parent)

    # Attempt fast multi-core extraction via system tar if requested/available
    if prefer_fast:
        used_fast = _fast_extract_with_system_tar(archive, target_dir)
        if used_fast:
            if not target_dir.exists():
                logger.warning("Expected processed directory not found after fast extraction: %s", target_dir)
            return

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
        if not target_dir.exists():
            logger.warning("Expected processed directory not found after extraction: %s", target_dir)


def find_bz2_files(root: Path) -> List[Path]:
    results: List[Path] = []
    if not root.exists():
        return results
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        for p in sorted(sub.glob("wiki_*.bz2")):
            results.append(p)
    return results


def iter_jsonl_bz2(file_path: Path) -> Iterator[dict]:
    with bz2.open(file_path, mode="rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            yield json.loads(line)


class Command(BaseCommand):
    help = "Load Wikipedia dump into SQLite"

    def add_arguments(self, parser) -> None:
        default_archive, default_processed = _default_paths()
        parser.add_argument("--archive", default=str(default_archive))
        parser.add_argument("--processed-dir", default=str(default_processed))
        parser.add_argument("--batch-size", type=int, default=1000)
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

        if not skip_decompress:
            ensure_decompressed(archive, processed_dir, force=force_decompress, prefer_fast=fast_extract)
            logger.info("Decompressed archive: %s", archive)

        bz2_files = find_bz2_files(processed_dir)
        if not bz2_files:
            raise CommandError(f"No wiki_*.bz2 files found under {processed_dir}")

        logger.info("Found %d compressed shards", len(bz2_files))

        created_total = 0
        processed_records = 0

        for shard in tqdm(
            bz2_files,
            desc="Shards",
            unit="file",
            total=len(bz2_files),
            leave=True,
            dynamic_ncols=True,
            ascii=True,
            mininterval=0.5,
            file=sys.stdout,
        ):
            logger.info("Processing shard: %s", shard)
            batch: List[Article] = []
            for rec in tqdm(
                iter_jsonl_bz2(shard),
                desc=f"Records {shard.name}",
                unit="rec",
                leave=False,
                dynamic_ncols=True,
                ascii=True,
                mininterval=0.5,
                file=sys.stdout,
            ):
                page_id_raw = rec.get("id")
                title = rec.get("title")
                text = rec.get("text") or []
                try:
                    page_id_int = int(page_id_raw)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Skip record with invalid id=%r: %s", page_id_raw, exc)
                    continue

                paragraphs = extract_plain_paragraphs(text)
                batch.append(Article(page_id=page_id_int, title=title, plain_text_paragraphs=paragraphs))

                processed_records += 1
                if limit and processed_records >= limit:
                    created_total += self._flush_batch(batch, batch_size)
                    logger.info("Reached limit=%d. Stopping.", limit)
                    self.stdout.write(self.style.SUCCESS(f"Created ~{created_total} articles"))
                    return

                if len(batch) >= batch_size:
                    created_total += self._flush_batch(batch, batch_size)

            created_total += self._flush_batch(batch, batch_size)

        self.stdout.write(self.style.SUCCESS(f"Created ~{created_total} articles"))

    def _flush_batch(self, batch: List[Article], batch_size: int) -> int:
        if not batch:
            return 0
        with transaction.atomic():
            Article.objects.bulk_create(batch, batch_size=batch_size, ignore_conflicts=True)
        created = len(batch)
        batch.clear()
        return created


