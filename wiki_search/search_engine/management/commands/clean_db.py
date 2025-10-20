from __future__ import annotations

import logging
from pathlib import Path
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from tqdm import tqdm
from django.db import connection

from search_engine.models import Article, InternalLink, Redirect, TFIDFIndex, Vocabulary


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
        parser.add_argument(
            "--no-fast-pragmas",
            action="store_true",
            help="Do not apply fast SQLite PRAGMAs during cleanup (SQLite only)",
        )
        parser.add_argument(
            "--drop-recreate",
            action="store_true",
            help="SQLite only: drop and recreate tables instead of deleting rows (fastest, destructive)",
        )

    def handle(self, *args, **options):
        confirm = options["yes"]
        if not confirm:
            self.stdout.write(
                "This will delete ALL Articles, Redirects, and InternalLinks. Type 'yes' to continue: ",
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
            """Fast truncate using single-statement deletes within one transaction.

            Returns: (articles_deleted, redirects_deleted, links_deleted)
            """
            use_progress = not bool(options.get("no_progress"))
            use_fast_pragmas = not bool(options.get("no_fast_pragmas"))
            use_drop_recreate = bool(options.get("drop_recreate"))

            # Table names
            tbl_article = Article._meta.db_table
            tbl_redirect = Redirect._meta.db_table
            tbl_link = InternalLink._meta.db_table
            tbl_tfidf = TFIDFIndex._meta.db_table
            tbl_vocab = Vocabulary._meta.db_table

            total_articles = total_redirects = total_links = 0

            start_ts = time.perf_counter()
            with connection.cursor() as cur:
                # Capture original PRAGMAs to restore later (SQLite only)
                orig = {}
                if connection.vendor == "sqlite":
                    for key in (
                        "foreign_keys",
                        "journal_mode",
                        "synchronous",
                        "locking_mode",
                        "temp_store",
                        "cache_size",
                        "mmap_size",
                    ):
                        try:
                            cur.execute(f"PRAGMA {key}")
                            row = cur.fetchone()
                            if row is not None:
                                orig[key] = row[0]
                        except Exception:  # noqa: BLE001
                            pass

                # Apply fast PRAGMAs
                if connection.vendor == "sqlite":
                    try:
                        cur.execute("PRAGMA foreign_keys = OFF")
                        if use_fast_pragmas:
                            cur.execute("PRAGMA locking_mode = EXCLUSIVE")
                            # Try the fastest journal setting first; fall back if unsupported
                            try:
                                cur.execute("PRAGMA journal_mode = OFF")
                                _ = cur.fetchone()
                            except Exception:  # noqa: BLE001
                                cur.execute("PRAGMA journal_mode = DELETE")
                                _ = cur.fetchone()
                            cur.execute("PRAGMA synchronous = OFF")
                            cur.execute("PRAGMA temp_store = MEMORY")
                            try:
                                cur.execute("PRAGMA mmap_size = 30000000000")
                            except Exception:  # noqa: BLE001
                                pass
                            try:
                                cur.execute("PRAGMA cache_size = -200000")
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001
                        logger.warning("Failed to apply fast SQLite PRAGMAs; continuing with defaults")

                # Optionally compute counts for progress only
                if use_progress:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {tbl_link}")
                        total_links = int(cur.fetchone()[0])
                        cur.execute(f"SELECT COUNT(*) FROM {tbl_redirect}")
                        total_redirects = int(cur.fetchone()[0])
                        cur.execute(f"SELECT COUNT(*) FROM {tbl_article}")
                        total_articles = int(cur.fetchone()[0])
                    except Exception:  # noqa: BLE001
                        total_articles = total_redirects = total_links = 0

                # Begin one big transaction
                try:
                    if connection.vendor == "sqlite":
                        cur.execute("BEGIN IMMEDIATE")
                except Exception:  # noqa: BLE001
                    pass

                # Execute fast path
                if connection.vendor == "sqlite" and use_drop_recreate:
                    # Drop and recreate tables
                    logger.info("Dropping and recreating tables (fastest path)")
                    # Drop in dependency order
                    cur.executescript(
                        ";\n".join(
                            [
                                f"DROP TABLE IF EXISTS {tbl_link}",
                                f"DROP TABLE IF EXISTS {tbl_redirect}",
                                f"DROP TABLE IF EXISTS {tbl_tfidf}",
                                f"DROP TABLE IF EXISTS {tbl_vocab}",
                                f"DROP TABLE IF EXISTS {tbl_article}",
                            ]
                        )
                    )
                    # Recreate via schema editor to keep Django schema in sync
                    from django.db import connections

                    with connections[connection.alias].schema_editor() as editor:
                        editor.create_model(Article)
                        editor.create_model(Redirect)
                        editor.create_model(InternalLink)
                        editor.create_model(Vocabulary)
                        editor.create_model(TFIDFIndex)
                    links_deleted = total_links
                    redirects_deleted = total_redirects
                    articles_deleted = total_articles
                else:
                    # Bulk delete in dependency order
                    logger.info("Executing bulk DELETE statements")
                    statements = [
                        f"DELETE FROM {tbl_link};",
                        f"DELETE FROM {tbl_redirect};",
                        f"DELETE FROM {tbl_tfidf};",
                        f"DELETE FROM {tbl_vocab};",
                        f"DELETE FROM {tbl_article};",
                    ]

                    if use_progress and (total_links or total_redirects or total_articles):
                        descs = [
                            ("Deleting InternalLinks", total_links),
                            ("Deleting Redirects", total_redirects),
                            ("Deleting TFIDFIndex", 0),
                            ("Deleting Vocabulary", 0),
                            ("Deleting Articles", total_articles),
                        ]
                        for (desc, total), stmt in zip(descs, statements):
                            pbar = None
                            if total:
                                pbar = tqdm(total=total, desc=desc, unit="rows")
                            cur.execute(stmt)
                            if pbar is not None:
                                # We do not know exact rowcount reliably here; assume total
                                pbar.update(total)
                                pbar.close()
                    else:
                        # Execute statements individually (PostgreSQL doesn't support executescript)
                        for stmt in statements:
                            cur.execute(stmt)

                    # Best-effort row counts
                    links_deleted = total_links
                    redirects_deleted = total_redirects
                    articles_deleted = total_articles

                # Re-enable foreign key constraints
                if connection.vendor == "sqlite":
                    cur.execute("PRAGMA foreign_keys = ON")

                # Commit once at the end of deletion phase
                connection.commit()

                # Restore PRAGMAs after commit
                if connection.vendor == "sqlite" and orig:
                    try:
                        if "foreign_keys" in orig:
                            cur.execute(f"PRAGMA foreign_keys = {int(bool(orig['foreign_keys']))}")
                        if "locking_mode" in orig:
                            cur.execute(f"PRAGMA locking_mode = {orig['locking_mode']}")
                        if "journal_mode" in orig:
                            cur.execute(f"PRAGMA journal_mode = {orig['journal_mode']}")
                            _ = cur.fetchone()
                        if "synchronous" in orig:
                            cur.execute(f"PRAGMA synchronous = {orig['synchronous']}")
                        if "temp_store" in orig:
                            cur.execute(f"PRAGMA temp_store = {orig['temp_store']}")
                        if "cache_size" in orig:
                            cur.execute(f"PRAGMA cache_size = {orig['cache_size']}")
                        if "mmap_size" in orig:
                            cur.execute(f"PRAGMA mmap_size = {orig['mmap_size']}")
                    except Exception:  # noqa: BLE001
                        logger.warning("Failed to restore original SQLite PRAGMAs")

            elapsed = time.perf_counter() - start_ts
            logger.info("Data deletion completed in %.2fs", elapsed)
            return articles_deleted, redirects_deleted, links_deleted

        # Fast truncate approach
        articles_deleted, redirects_deleted, links_deleted = truncate_all_tables()

        # Delete loading progress checkpoint file
        checkpoint_path = settings.BASE_DIR.parent / "data" / ".load_checkpoint.json"
        if checkpoint_path.exists():
            checkpoint_path.unlink()
            self.stdout.write("Deleted loading progress checkpoint file")

        # Optimize database (VACUUM for SQLite, VACUUM ANALYZE for PostgreSQL)
        try:
            if connection.vendor == "sqlite":
                if options.get("no_progress"):
                    with connection.cursor() as cur:
                        cur.execute("VACUUM")
                else:
                    with tqdm(total=1, desc="Vacuuming database", unit="operation") as pbar:
                        with connection.cursor() as cur:
                            cur.execute("VACUUM")
                        pbar.update(1)
            elif connection.vendor == "postgresql":
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

        db_type = connection.vendor
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted Articles={articles_deleted}, Redirects={redirects_deleted}, InternalLinks={links_deleted} and optimized {db_type} database"
            )
        )


