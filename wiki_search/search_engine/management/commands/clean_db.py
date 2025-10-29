from __future__ import annotations

import logging
from pathlib import Path
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from tqdm import tqdm
from django.db import connection

from search_engine.models import Article, InternalLink, Vocabulary, InvertedIndex, PageRank


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete all data from search_engine tables and optimize the database"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--yes", action="store_true", help="Run non-interactively and skip confirmation")
        parser.add_argument(
            "--no-progress",
            action="store_true",
            help="Do not show progress bars or perform COUNT(*) queries",
        )

    def handle(self, *args, **options):
        confirm = options["yes"]
        if not confirm:
            self.stdout.write(
                "This will delete ALL Articles and InternalLinks. Type 'yes' to continue: ",
                ending="",
            )
            try:
                user_in = input().strip().lower()
            except EOFError:
                user_in = ""
            if user_in != "yes":
                self.stdout.write("Aborted.")
                return

        def truncate_all_tables() -> tuple[int, int, int]:
            """Fast PostgreSQL truncate using TRUNCATE CASCADE.

            Returns: (articles_deleted, redirects_deleted, links_deleted)
            """
            use_progress = not bool(options.get("no_progress"))

            # Table names in dependency order (child tables first)
            tables = [
                InternalLink._meta.db_table,
                InvertedIndex._meta.db_table,
                PageRank._meta.db_table,
                Vocabulary._meta.db_table,
                Article._meta.db_table,
            ]

            total_articles = total_links = 0

            start_ts = time.perf_counter()
            with connection.cursor() as cur:
                # Get counts for progress display if requested
                if use_progress:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {InternalLink._meta.db_table}")
                        total_links = int(cur.fetchone()[0])
                        cur.execute(f"SELECT COUNT(*) FROM {Article._meta.db_table}")
                        total_articles = int(cur.fetchone()[0])
                    except Exception:  # noqa: BLE001
                        total_articles = total_links = 0

                # Use PostgreSQL TRUNCATE CASCADE for maximum speed
                logger.info("Executing TRUNCATE CASCADE")
                table_list = ", ".join(tables)
                truncate_sql = f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"
                
                if use_progress and (total_links or total_articles):
                    with tqdm(total=1, desc="Truncating all tables", unit="operation") as pbar:
                        cur.execute(truncate_sql)
                        pbar.update(1)
                else:
                    cur.execute(truncate_sql)

            elapsed = time.perf_counter() - start_ts
            logger.info("Data deletion completed in %.2fs", elapsed)
            return total_articles, total_links

        # Fast truncate approach
        articles_deleted, links_deleted = truncate_all_tables()

        # Delete loading progress checkpoint file
        checkpoint_path = settings.BASE_DIR.parent / "data" / ".load_checkpoint.json"
        if checkpoint_path.exists():
            checkpoint_path.unlink()
            self.stdout.write("Deleted loading progress checkpoint file")

        # Optimize PostgreSQL database
        try:
            if options.get("no_progress"):
                with connection.cursor() as cur:
                    cur.execute("VACUUM ANALYZE")
            else:
                with tqdm(total=1, desc="Vacuuming and analyzing database", unit="operation") as pbar:
                    with connection.cursor() as cur:
                        cur.execute("VACUUM ANALYZE")
                    pbar.update(1)
        except Exception as exc:  # pragma: no cover
            logger.warning("Database optimization failed: %s", exc)

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted Articles={articles_deleted}, InternalLinks={links_deleted} and optimized PostgreSQL database"
            )
        )


