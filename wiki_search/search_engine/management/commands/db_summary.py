from __future__ import annotations

import json
from statistics import mean
from typing import List

from django.core.management.base import BaseCommand

from search_engine.models import Article, InternalLink, Redirect


class Command(BaseCommand):
    help = "Print a summary of current SQLite database contents"

    def handle(self, *args, **options):
        article_count = Article.objects.count()
        redirect_count = Redirect.objects.count()
        link_count = InternalLink.objects.count()
        unresolved_links = InternalLink.objects.filter(to_article__isnull=True).count()

        # Sample average paragraph count over a small window for speed
        sample_size = 1000
        qs = Article.objects.all().only("plain_text_paragraphs")[:sample_size]
        samples: List[int] = []
        for a in qs:
            try:
                samples.append(len(a.plain_text_paragraphs or []))
            except Exception:
                # plain_text_paragraphs should be JSON, but be robust to bad rows
                try:
                    samples.append(len(json.loads(a.plain_text_paragraphs) or []))
                except Exception:
                    pass

        avg_paragraphs = mean(samples) if samples else 0.0

        self.stdout.write("Database summary")
        self.stdout.write(f"- Articles: {article_count}")
        self.stdout.write(f"- Redirects: {redirect_count}")
        self.stdout.write(f"- InternalLinks: {link_count}")
        self.stdout.write(f"- Unresolved links (to_article is NULL): {unresolved_links}")
        self.stdout.write(f"- Avg paragraphs per sampled article ({len(samples)} samples): {avg_paragraphs:.2f}")


