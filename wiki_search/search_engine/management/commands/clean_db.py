from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
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
            """Fast truncate using DELETE without WHERE and disabled FK checks."""
            with connection.cursor() as cur:
                # Disable foreign key constraints temporarily
                cur.execute("PRAGMA foreign_keys = OFF")
                
                # Delete all rows (equivalent to TRUNCATE in SQLite)
                # Order matters: delete children before parents to avoid issues
                cur.execute(f"DELETE FROM {InternalLink._meta.db_table}")
                links_deleted = cur.rowcount or 0
                
                cur.execute(f"DELETE FROM {Redirect._meta.db_table}")
                redirects_deleted = cur.rowcount or 0
                
                cur.execute(f"DELETE FROM {Article._meta.db_table}")
                articles_deleted = cur.rowcount or 0
                
                # Re-enable foreign key constraints
                cur.execute("PRAGMA foreign_keys = ON")
                
                # Commit once at the end
                connection.commit()
                
            return articles_deleted, redirects_deleted, links_deleted

        # Fast truncate approach
        articles_deleted, redirects_deleted, links_deleted = truncate_all_tables()

        # VACUUM to reclaim space (SQLite only)
        try:
            with connection.cursor() as cur:
                cur.execute("VACUUM")
        except Exception as exc:  # pragma: no cover
            logger.warning("VACUUM failed: %s", exc)

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted Articles={articles_deleted}, Redirects={redirects_deleted}, InternalLinks={links_deleted} and vacuumed DB"
            )
        )


