from __future__ import annotations

import bz2
import io
import json
import logging
import random
from pathlib import Path
from typing import Iterator

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

try:
    from tqdm import tqdm  # type: ignore
    _tqdm_available = True
except ImportError:
    _tqdm_available = False
    def tqdm(iterable, **kwargs):  # type: ignore
        return iterable

logger = logging.getLogger(__name__)


def _default_processed_path() -> Path:
    """Get the default path to the processed directory."""
    base = settings.BASE_DIR.parent
    return base / "data" / "processed" / "enwiki-20171001-pages-meta-current-withlinks-processed"


def find_bz2_files(root: Path) -> list[Path]:
    """Find all wiki_*.bz2 files in the processed directory structure."""
    results: list[Path] = []
    if not root.exists():
        return results
    
    # Check for the expected structure with subdirectories
    if not any(root.glob("*/wiki_*.bz2")):
        # Try alternative folder name ending with "-processed"
        alt = root.parent / f"{root.name}-processed"
        if alt.exists():
            root = alt
        else:
            return results
    
    # Collect all bz2 files from subdirectories
    for subdir in sorted(root.iterdir()):
        if not subdir.is_dir():
            continue
        for bz2_file in sorted(subdir.glob("wiki_*.bz2")):
            results.append(bz2_file)
    
    return results


def iter_jsonl_bz2(file_path: Path) -> Iterator[dict]:
    """Iterate over JSON lines in a bz2 compressed file."""
    try:
        with bz2.open(file_path, mode="rb") as raw:
            with io.BufferedReader(raw, buffer_size=1024 * 1024) as buf:  # 1MB buffer
                for line_num, line in enumerate(buf, 1):
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.warning(f"JSON decode error in {file_path}:{line_num}: {e}")
                        continue
                    except Exception as e:
                        logger.warning(f"Unexpected error in {file_path}:{line_num}: {e}")
                        continue
    except Exception as e:
        logger.error(f"Failed to open {file_path}: {e}")


def count_articles_in_file(file_path: Path) -> int:
    """Count articles in a single bz2 file."""
    count = 0
    for record in iter_jsonl_bz2(file_path):
        # Check if this looks like a valid article record
        if isinstance(record, dict) and "id" in record and "title" in record:
            count += 1
    return count


class Command(BaseCommand):
    help = "Count the number of articles in the Wikipedia dump"

    def add_arguments(self, parser):
        parser.add_argument(
            "--processed-dir",
            type=str,
            default=str(_default_processed_path()),
            help="Path to processed directory (default: data/processed/enwiki-20171001-pages-meta-current-withlinks-processed)"
        )
        parser.add_argument(
            "--sample",
            type=int,
            help="Sample only N files for quick estimate"
        )
        parser.add_argument(
            "--estimate",
            action="store_true",
            help="Use sampling to estimate total (samples 1% of files)"
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose output"
        )

    def handle(self, *args, **options):
        # Configure logging
        log_level = logging.DEBUG if options["verbose"] else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(self.stdout),
                logging.FileHandler('count_articles.log')
            ]
        )

        processed_dir = Path(options["processed_dir"]).expanduser()
        self.stdout.write(f"Counting articles in: {processed_dir}")
        
        # Find all bz2 files
        bz2_files = find_bz2_files(processed_dir)
        if not bz2_files:
            raise CommandError(f"No bz2 files found in {processed_dir}")
        
        self.stdout.write(f"Found {len(bz2_files)} bz2 files")
        
        # Determine which files to process
        if options["sample"]:
            files_to_process = random.sample(bz2_files, min(options["sample"], len(bz2_files)))
            self.stdout.write(f"Sampling {len(files_to_process)} files out of {len(bz2_files)} total files")
        elif options["estimate"]:
            # Sample 1% of files for estimation
            sample_size = max(1, len(bz2_files) // 100)
            files_to_process = random.sample(bz2_files, sample_size)
            self.stdout.write(f"Estimating from {len(files_to_process)} files (1% sample)")
        else:
            files_to_process = bz2_files
            self.stdout.write(f"Processing all {len(files_to_process)} files")
        
        # Count articles in each file
        total_articles = 0
        file_counts = []
        
        self.stdout.write("Processing files...")
        for file_path in tqdm(files_to_process, desc="Counting articles", unit="files"):
            try:
                count = count_articles_in_file(file_path)
                file_counts.append((file_path.name, count))
                total_articles += count
                if options["verbose"]:
                    self.stdout.write(f"{file_path.name}: {count} articles")
            except Exception as e:
                self.stderr.write(f"Error processing {file_path}: {e}")
                continue
        
        # Report results
        if options["estimate"]:
            # Calculate estimate based on sample
            avg_articles_per_file = total_articles / len(files_to_process) if files_to_process else 0
            estimated_total = avg_articles_per_file * len(bz2_files)
            self.stdout.write(
                self.style.SUCCESS(f"Sample articles found: {total_articles:,}")
            )
            self.stdout.write(f"Average articles per file: {avg_articles_per_file:.1f}")
            self.stdout.write(
                self.style.SUCCESS(f"Estimated total articles: {estimated_total:,.0f}")
            )
            self.stdout.write(f"Processed {len(file_counts)} files out of {len(bz2_files)} total files")
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Total articles found: {total_articles:,}")
            )
            self.stdout.write(f"Processed {len(file_counts)} files")
        
        # Show some statistics
        if file_counts:
            counts = [count for _, count in file_counts]
            avg_count = sum(counts) / len(counts)
            min_count = min(counts)
            max_count = max(counts)
            
            self.stdout.write(f"Average articles per file: {avg_count:.1f}")
            self.stdout.write(f"Min articles in a file: {min_count:,}")
            self.stdout.write(f"Max articles in a file: {max_count:,}")
            
            # Show top 5 files with most articles
            top_files = sorted(file_counts, key=lambda x: x[1], reverse=True)[:5]
            self.stdout.write("Top 5 files by article count:")
            for filename, count in top_files:
                self.stdout.write(f"  {filename}: {count:,} articles")
