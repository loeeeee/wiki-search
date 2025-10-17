from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction

from search_engine.models import Article, TFIDFIndex, Vocabulary
from search_engine.search import compute_idf, compute_tf, tokenize, vector_l2_norm


class Command(BaseCommand):
    help = "Build TF-IDF index over Article.plain_text_paragraphs"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--rebuild", action="store_true", help="Clear existing index before building")
        parser.add_argument("--batch-size", type=int, default=1000)
        parser.add_argument("--limit", type=int, default=0, help="Limit number of articles (for testing)")

    def handle(self, *args, **options):
        batch_size: int = options["batch_size"]
        limit: int = options["limit"]
        rebuild: bool = options["rebuild"]

        if rebuild:
            TFIDFIndex.objects.all().delete()
            Vocabulary.objects.all().delete()

        # Pass 1: compute document frequencies
        self.stdout.write("Computing document frequencies...")
        doc_freq: Counter[str] = Counter()
        total_docs = 0

        qs = Article.objects.only("plain_text_paragraphs")
        if limit > 0:
            qs = qs.order_by("id")[:limit]

        for start in range(0, qs.count(), batch_size):
            for article in qs[start : start + batch_size]:
                total_docs += 1
                seen_terms = set()
                for para in article.plain_text_paragraphs or []:
                    seen_terms.update(tokenize(para))
                doc_freq.update(seen_terms)

        # Store vocabulary with IDF
        self.stdout.write("Saving vocabulary...")
        vocab_rows: List[Vocabulary] = []
        for term, df in doc_freq.items():
            vocab_rows.append(
                Vocabulary(term=term, document_frequency=int(df), idf_value=compute_idf(total_docs, int(df)))
            )
        Vocabulary.objects.bulk_create(vocab_rows, batch_size=1000)

        # Build map term -> idf for indexing pass
        term_to_id: Dict[str, int] = {v.term: v.id for v in Vocabulary.objects.only("id", "term")}
        term_to_idf: Dict[str, float] = {v.term: float(v.idf_value) for v in Vocabulary.objects.only("term", "idf_value")}

        # Pass 2: create TF-IDF vectors per article
        self.stdout.write("Building TF-IDF vectors...")
        qs = Article.objects.only("id", "plain_text_paragraphs")
        if limit > 0:
            qs = qs.order_by("id")[:limit]

        to_create: List[TFIDFIndex] = []
        for start in range(0, qs.count(), batch_size):
            for article in qs[start : start + batch_size]:
                tokens: List[str] = []
                for para in article.plain_text_paragraphs or []:
                    tokens.extend(tokenize(para))
                tf = compute_tf(tokens)
                vec: Dict[int, float] = {}
                for term, tf_val in tf.items():
                    idf = term_to_idf.get(term)
                    term_id = term_to_id.get(term)
                    if idf is None or term_id is None:
                        continue
                    vec[term_id] = tf_val * idf
                l2 = vector_l2_norm(vec.values()) if vec else 0.0
                to_create.append(
                    TFIDFIndex(article=article, tfidf_vector={str(k): float(v) for k, v in vec.items()}, l2_norm=float(l2))
                )
            with transaction.atomic():
                TFIDFIndex.objects.bulk_create(to_create, batch_size=500)
            to_create.clear()

        self.stdout.write("TF-IDF index build complete.")


