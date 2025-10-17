from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from tqdm import tqdm
from django.db import connection

from search_engine.models import Article, InternalLink, Redirect


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete all data from search_engine tables and VACUUM the SQLite database"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--yes", action="store_true", help="Run non-interactively and skip confirmation")

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

        def truncate_all_tables():
            """Fast truncate using chunked DELETE with progress bars and disabled FK checks."""
            chunk_size = 10000
            
            with connection.cursor() as cur:
                # Disable foreign key constraints temporarily
                cur.execute("PRAGMA foreign_keys = OFF")
                
                # Get total row counts for progress bars
                cur.execute(f"SELECT COUNT(*) FROM {InternalLink._meta.db_table}")
                total_links = cur.fetchone()[0]
                
                cur.execute(f"SELECT COUNT(*) FROM {Redirect._meta.db_table}")
                total_redirects = cur.fetchone()[0]
                
                cur.execute(f"SELECT COUNT(*) FROM {Article._meta.db_table}")
                total_articles = cur.fetchone()[0]
                
                # Delete InternalLinks with progress bar
                links_deleted = 0
                if total_links > 0:
                    with tqdm(total=total_links, desc="Deleting InternalLinks", unit="rows") as pbar:
                        while True:
                            cur.execute(f"DELETE FROM {InternalLink._meta.db_table} WHERE rowid IN (SELECT rowid FROM {InternalLink._meta.db_table} LIMIT {chunk_size})")
                            deleted = cur.rowcount or 0
                            if deleted == 0:
                                break
                            links_deleted += deleted
                            pbar.update(deleted)
                
                # Delete Redirects with progress bar
                redirects_deleted = 0
                if total_redirects > 0:
                    with tqdm(total=total_redirects, desc="Deleting Redirects", unit="rows") as pbar:
                        while True:
                            cur.execute(f"DELETE FROM {Redirect._meta.db_table} WHERE rowid IN (SELECT rowid FROM {Redirect._meta.db_table} LIMIT {chunk_size})")
                            deleted = cur.rowcount or 0
                            if deleted == 0:
                                break
                            redirects_deleted += deleted
                            pbar.update(deleted)
                
                # Delete Articles with progress bar
                articles_deleted = 0
                if total_articles > 0:
                    with tqdm(total=total_articles, desc="Deleting Articles", unit="rows") as pbar:
                        while True:
                            cur.execute(f"DELETE FROM {Article._meta.db_table} WHERE rowid IN (SELECT rowid FROM {Article._meta.db_table} LIMIT {chunk_size})")
                            deleted = cur.rowcount or 0
                            if deleted == 0:
                                break
                            articles_deleted += deleted
                            pbar.update(deleted)
                
                # Re-enable foreign key constraints
                cur.execute("PRAGMA foreign_keys = ON")
                
                # Commit once at the end
                connection.commit()
                
            return articles_deleted, redirects_deleted, links_deleted

        # Fast truncate approach
        articles_deleted, redirects_deleted, links_deleted = truncate_all_tables()

        # VACUUM to reclaim space (SQLite only)
        try:
            with tqdm(total=1, desc="Vacuuming database", unit="operation") as pbar:
                with connection.cursor() as cur:
                    cur.execute("VACUUM")
                pbar.update(1)
        except Exception as exc:  # pragma: no cover
            logger.warning("VACUUM failed: %s", exc)

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted Articles={articles_deleted}, Redirects={redirects_deleted}, InternalLinks={links_deleted} and vacuumed DB"
            )
        )


