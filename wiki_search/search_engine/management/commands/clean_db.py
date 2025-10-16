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
        parser.add_argument("--chunk-size", type=int, default=100_000, help="Rows per delete chunk")

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

        # Chunked raw SQL deletes to keep memory usage low
        def chunked_delete(table: str, pk: str = "id", chunk_size: int = 100_000) -> int:
            deleted_total = 0
            with connection.cursor() as cur:
                cur.execute(f"SELECT MIN({pk}), MAX({pk}) FROM {table}")
                row = cur.fetchone()
                if not row or row[0] is None:
                    return 0
                min_id, max_id = int(row[0]), int(row[1])
                start = min_id
                while start <= max_id:
                    end = start + chunk_size - 1
                    # Inline numeric bounds to avoid SQLite debug_sql formatting issues
                    cur.execute(f"DELETE FROM {table} WHERE {pk} BETWEEN {start} AND {end}")
                    deleted_total += cur.rowcount if cur.rowcount is not None else 0
                    # Commit per chunk to release resources early
                    connection.commit()
                    start = end + 1
            return deleted_total

        # Respect FK dependencies: delete children first
        chunk_size = int(options.get("chunk_size") or 100_000)
        links_deleted = chunked_delete(InternalLink._meta.db_table, chunk_size=chunk_size)
        redirects_deleted = chunked_delete(Redirect._meta.db_table, chunk_size=chunk_size)
        articles_deleted = chunked_delete(Article._meta.db_table, chunk_size=chunk_size)

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


