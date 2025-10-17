from __future__ import annotations

from typing import List, Tuple

from django.test import TestCase

from .models import Article, TFIDFIndex, Vocabulary
from .search import (
    compute_idf,
    compute_tf,
    search_by_tfidf,
    search_by_title_exact,
    tokenize,
    vector_l2_norm,
)


class TokenizationTests(TestCase):
    def test_basic_tokenization(self):
        tokens = tokenize("The Quick, brown foxes!")
        self.assertEqual(tokens, ["quick", "brown", "foxes"])

    def test_empty(self):
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize(None), [])  # type: ignore[arg-type]


class TFIDFMathTests(TestCase):
    def test_tf_and_idf(self):
        tf = compute_tf(["cat", "cat", "dog"])  # cat: 2/3, dog: 1/3
        self.assertAlmostEqual(tf["cat"], 2.0 / 3.0, places=6)
        self.assertAlmostEqual(tf["dog"], 1.0 / 3.0, places=6)

        idf = compute_idf(total_docs=3, document_frequency=1)
        self.assertGreater(idf, 1.0)

    def test_l2_norm(self):
        self.assertAlmostEqual(vector_l2_norm([3.0, 4.0]), 5.0)


class TitleSearchTests(TestCase):
    def setUp(self):
        Article.objects.create(page_id=1, title="Cat", plain_text_paragraphs=["Cats are animals."])
        Article.objects.create(page_id=2, title="Dog", plain_text_paragraphs=["Dogs are friendly."])

    def test_exact_title(self):
        qs = search_by_title_exact("Cat")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().title, "Cat")

    def test_exact_title_case_insensitive(self):
        qs = search_by_title_exact("cat")
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().title, "Cat")


class TFIDFSearchTests(TestCase):
    def setUp(self):
        # Small corpus
        a1 = Article.objects.create(page_id=1, title="Cats", plain_text_paragraphs=["Cats purr and meow."])
        a2 = Article.objects.create(page_id=2, title="Dogs", plain_text_paragraphs=["Dogs bark and wag tails."])
        a3 = Article.objects.create(page_id=3, title="Foxes", plain_text_paragraphs=["Foxes are wild canids."])

        # Build minimal vocabulary and tf-idf for the small set
        docs: List[Tuple[Article, List[str]]] = []
        for art in (a1, a2, a3):
            tokens: List[str] = []
            for p in art.plain_text_paragraphs:
                tokens.extend(tokenize(p))
            docs.append((art, list(set(tokens))))

        total = len(docs)
        df = {}
        for _, terms in docs:
            for t in terms:
                df[t] = df.get(t, 0) + 1

        vocab_rows = [
            Vocabulary(term=t, document_frequency=c, idf_value=compute_idf(total, c)) for t, c in df.items()
        ]
        Vocabulary.objects.bulk_create(vocab_rows)

        # Build per-doc TF-IDF
        for art in (a1, a2, a3):
            tokens: List[str] = []
            for p in art.plain_text_paragraphs:
                tokens.extend(tokenize(p))
            tf = compute_tf(tokens)
            vec = {}
            for v in Vocabulary.objects.filter(term__in=list(tf.keys())):
                vec[str(v.id)] = float(tf[v.term] * v.idf_value)
            TFIDFIndex.objects.create(article=art, tfidf_vector=vec, l2_norm=vector_l2_norm(vec.values()))

    def test_single_term_query(self):
        res = search_by_tfidf("meow", limit=5)
        self.assertTrue(len(res) >= 1)
        self.assertEqual(res[0][0].title, "Cats")

    def test_multi_term_query(self):
        res = search_by_tfidf("dogs wag", limit=5)
        self.assertTrue(len(res) >= 1)
        self.assertEqual(res[0][0].title, "Dogs")

    def test_no_match(self):
        res = search_by_tfidf("quantum", limit=5)
        self.assertEqual(res, [])

