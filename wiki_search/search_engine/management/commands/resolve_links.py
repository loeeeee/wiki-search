from __future__ import annotations

import logging
from typing import Dict, Optional

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from search_engine.models import Article, InternalLink

from tqdm import tqdm

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Resolve InternalLink foreign key references from page_id values"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--batch-size",
            type=int,
            default=5000,
            help="Batch size for bulk updates (default: 5000)"
        )
        parser.add_argument(
            "--resolve-to-article",
            action="store_true",
            help="Also resolve to_article based on to_title matching"
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose output"
        )

    def handle(self, *args, **options):
        log_level = logging.DEBUG if options["verbose"] else logging.INFO
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
        )

        batch_size: int = options["batch_size"]
        resolve_to_article: bool = options["resolve_to_article"]

        self.stdout.write("Starting link resolution process...")

        # Step 1: Resolve from_article foreign keys
        self._resolve_from_article(batch_size, options["verbose"])

        # Step 2: Optionally resolve to_article foreign keys
        if resolve_to_article:
            self._resolve_to_article(batch_size, options["verbose"])

        self.stdout.write(self.style.SUCCESS("Link resolution complete!"))

    def _resolve_from_article(self, batch_size: int, verbose: bool) -> None:
        """Resolve from_article foreign keys using from_page_id."""
        self.stdout.write("Resolving from_article foreign keys...")

        # Count unresolved links
        unresolved_count = InternalLink.objects.filter(
            Q(from_article__isnull=True) & Q(from_page_id__isnull=False)
        ).count()

        if unresolved_count == 0:
            self.stdout.write("No unresolved from_article links found.")
            return

        self.stdout.write(f"Found {unresolved_count:,} unresolved from_article links")

        # Build page_id -> article.id mapping
        self.stdout.write("Building page_id -> article.id mapping...")
        page_id_to_article_id: Dict[int, int] = {}
        
        # Get all unique page_ids that need resolution
        unresolved_page_ids = set(
            InternalLink.objects.filter(
                Q(from_article__isnull=True) & Q(from_page_id__isnull=False)
            ).values_list('from_page_id', flat=True).distinct()
        )
        
        self.stdout.write(f"Found {len(unresolved_page_ids):,} unique page_ids to resolve")
        
        # Build mapping by querying articles in batches
        page_id_list = list(unresolved_page_ids)
        for i in tqdm(
            range(0, len(page_id_list), batch_size),
            desc="Building mapping",
            unit="batch",
            disable=not verbose
        ):
            batch_page_ids = page_id_list[i:i + batch_size]
            batch_mapping = dict(
                Article.objects.filter(page_id__in=batch_page_ids).values_list('page_id', 'id')
            )
            page_id_to_article_id.update(batch_mapping)

        self.stdout.write(f"Successfully mapped {len(page_id_to_article_id):,} page_ids to article IDs")

        # Update links in batches
        self.stdout.write("Updating from_article foreign keys...")
        updated_total = 0
        not_found_total = 0

        # Process in batches to avoid loading all links into memory
        for i in tqdm(
            range(0, unresolved_count, batch_size),
            desc="Updating links",
            unit="batch"
        ):
            # Fetch batch of unresolved links
            links = list(
                InternalLink.objects.filter(
                    Q(from_article__isnull=True) & Q(from_page_id__isnull=False)
                )[:batch_size]
            )

            if not links:
                break

            # Update from_article for links where we found the article
            to_update = []
            not_found_page_ids = []
            
            for link in links:
                if link.from_page_id in page_id_to_article_id:
                    link.from_article_id = page_id_to_article_id[link.from_page_id]
                    to_update.append(link)
                else:
                    not_found_page_ids.append(link.from_page_id)

            # Bulk update
            if to_update:
                with transaction.atomic():
                    InternalLink.objects.bulk_update(to_update, ['from_article'], batch_size=batch_size)
                updated_total += len(to_update)

            not_found_total += len(not_found_page_ids)

            if not_found_page_ids and verbose:
                logger.debug(
                    "Could not resolve %d links (articles not found for page_ids: %s...)",
                    len(not_found_page_ids),
                    not_found_page_ids[:5]
                )

        self.stdout.write(
            self.style.SUCCESS(f"Updated {updated_total:,} from_article foreign keys")
        )
        if not_found_total > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Could not resolve {not_found_total:,} links (source articles not found)"
                )
            )

    def _resolve_to_article(self, batch_size: int, verbose: bool) -> None:
        """Resolve to_article foreign keys by matching to_title with article titles."""
        self.stdout.write("Resolving to_article foreign keys...")

        # Count unresolved to_article links
        unresolved_count = InternalLink.objects.filter(to_article__isnull=True).count()

        if unresolved_count == 0:
            self.stdout.write("No unresolved to_article links found.")
            return

        self.stdout.write(f"Found {unresolved_count:,} unresolved to_article links")

        # Build title -> article.id mapping
        self.stdout.write("Building title -> article.id mapping...")
        
        # Get all unique titles that need resolution
        unresolved_titles = set(
            InternalLink.objects.filter(to_article__isnull=True).values_list('to_title', flat=True).distinct()
        )
        
        self.stdout.write(f"Found {len(unresolved_titles):,} unique titles to resolve")
        
        # Build mapping by querying articles in batches
        title_to_article_id: Dict[str, int] = {}
        title_list = list(unresolved_titles)
        
        for i in tqdm(
            range(0, len(title_list), batch_size),
            desc="Building title mapping",
            unit="batch",
            disable=not verbose
        ):
            batch_titles = title_list[i:i + batch_size]
            batch_mapping = dict(
                Article.objects.filter(title__in=batch_titles).values_list('title', 'id')
            )
            title_to_article_id.update(batch_mapping)

        self.stdout.write(f"Successfully mapped {len(title_to_article_id):,} titles to article IDs")

        # Update links in batches
        self.stdout.write("Updating to_article foreign keys...")
        updated_total = 0
        not_found_total = 0

        # Process in batches
        for i in tqdm(
            range(0, unresolved_count, batch_size),
            desc="Updating to_article",
            unit="batch"
        ):
            # Fetch batch of unresolved links
            links = list(
                InternalLink.objects.filter(to_article__isnull=True)[:batch_size]
            )

            if not links:
                break

            # Update to_article for links where we found the article
            to_update = []
            not_found_titles = []
            
            for link in links:
                if link.to_title in title_to_article_id:
                    link.to_article_id = title_to_article_id[link.to_title]
                    to_update.append(link)
                else:
                    not_found_titles.append(link.to_title)

            # Bulk update
            if to_update:
                with transaction.atomic():
                    InternalLink.objects.bulk_update(to_update, ['to_article'], batch_size=batch_size)
                updated_total += len(to_update)

            not_found_total += len(set(not_found_titles))

            if not_found_titles and verbose:
                logger.debug(
                    "Could not resolve %d links to articles (titles not found: %s...)",
                    len(set(not_found_titles)),
                    list(set(not_found_titles))[:5]
                )

        self.stdout.write(
            self.style.SUCCESS(f"Updated {updated_total:,} to_article foreign keys")
        )
        if not_found_total > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Could not resolve {not_found_total:,} links (target articles not found)"
                )
            )


