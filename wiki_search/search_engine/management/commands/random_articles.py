from __future__ import annotations

import random
from typing import List, Tuple

from django.core.management.base import BaseCommand, CommandError

from search_engine.models import Article


def choose_two_indices(total: int) -> Tuple[int, int]:
    if total < 2:
        raise CommandError("Not enough articles in database to pick two.")
    a = random.randrange(total)
    b = random.randrange(total - 1)
    if b >= a:
        b += 1
    return (a, b)


class Command(BaseCommand):
    help = "Print two random articles' titles and content"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--max-paragraphs", type=int, default=5, help="Max paragraphs to print per article")

    def handle(self, *args, **options):
        max_paragraphs: int = options["max_paragraphs"]

        total = Article.objects.count()
        idx1, idx2 = choose_two_indices(total)

        # Use a stable ordering by primary key to pick via offset
        qs = Article.objects.order_by("id").only("title", "plain_text_paragraphs")

        art1 = qs[idx1]
        art2 = qs[idx2]

        self._print_article(art1.title, art1.plain_text_paragraphs, max_paragraphs)
        self.stdout.write("")
        self._print_article(art2.title, art2.plain_text_paragraphs, max_paragraphs)

    def _print_article(self, title: str, paragraphs: List[str], max_paragraphs: int) -> None:
        self.stdout.write(f"Title: {title}")
        self.stdout.write("Content:")
        count = 0
        for p in paragraphs or []:
            self.stdout.write(f"- {p}")
            count += 1
            if count >= max_paragraphs:
                break

