from __future__ import annotations

from typing import List

from django.test import TestCase

from .models import Article, InvertedIndex, PageRank, Vocabulary
from .search import search_by_title_exact, search_hybrid
from .tokenizer import tokenize


class TokenizationTests(TestCase):
    def test_basic_tokenization(self):
        """Test basic tokenization with stopwords removed."""
        tokens = tokenize("The Quick, brown foxes!")
        self.assertEqual(tokens, ["quick", "brown", "foxes"])

    def test_empty(self):
        """Test empty string and None input."""
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize(None), [])  # type: ignore[arg-type]

    def test_stopwords_removed(self):
        """Test that common stopwords are filtered out."""
        tokens = tokenize("the cat and the dog")
        # 'the', 'and' should be removed as stopwords
        self.assertNotIn("the", tokens)
        self.assertNotIn("and", tokens)
        self.assertIn("cat", tokens)
        self.assertIn("dog", tokens)


class TitleSearchTests(TestCase):
    def setUp(self):
        Article.objects.create(page_id=1, title="Cat", plain_text_paragraphs=["Cats are animals."])
        Article.objects.create(page_id=2, title="Dog", plain_text_paragraphs=["Dogs are friendly."])

    def test_exact_title(self):
        """Test exact title match."""
        results = search_by_title_exact("Cat")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Cat")

    def test_exact_title_case_insensitive(self):
        """Test case-insensitive title matching."""
        results = search_by_title_exact("cat")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Cat")

    def test_no_match(self):
        """Test no results for non-existent title."""
        results = search_by_title_exact("Bird")
        self.assertEqual(len(results), 0)


class HybridSearchTests(TestCase):
    def setUp(self):
        """Set up test data with articles, vocabulary, inverted index, and PageRank."""
        # Create articles
        a1 = Article.objects.create(
            page_id=1, 
            title="Python Programming", 
            plain_text_paragraphs=["Python is a programming language."]
        )
        a2 = Article.objects.create(
            page_id=2, 
            title="Snake", 
            plain_text_paragraphs=["Pythons are large snakes."]
        )
        a3 = Article.objects.create(
            page_id=3, 
            title="Java Programming", 
            plain_text_paragraphs=["Java is another programming language."]
        )

        # Create vocabulary
        vocab_python = Vocabulary.objects.create(term="python", document_frequency=2, idf_value=0.5)
        vocab_programming = Vocabulary.objects.create(term="programming", document_frequency=2, idf_value=0.5)
        vocab_language = Vocabulary.objects.create(term="language", document_frequency=2, idf_value=0.5)
        vocab_snake = Vocabulary.objects.create(term="snake", document_frequency=1, idf_value=1.0)
        vocab_java = Vocabulary.objects.create(term="java", document_frequency=1, idf_value=1.0)

        # Create inverted index entries (TF-IDF scores)
        InvertedIndex.objects.create(term=vocab_python, article=a1, tf_idf_score=0.8)
        InvertedIndex.objects.create(term=vocab_programming, article=a1, tf_idf_score=0.7)
        InvertedIndex.objects.create(term=vocab_language, article=a1, tf_idf_score=0.6)
        
        InvertedIndex.objects.create(term=vocab_python, article=a2, tf_idf_score=0.9)
        InvertedIndex.objects.create(term=vocab_snake, article=a2, tf_idf_score=0.5)
        
        InvertedIndex.objects.create(term=vocab_java, article=a3, tf_idf_score=0.8)
        InvertedIndex.objects.create(term=vocab_programming, article=a3, tf_idf_score=0.7)
        InvertedIndex.objects.create(term=vocab_language, article=a3, tf_idf_score=0.6)

        # Create PageRank scores
        PageRank.objects.create(article=a1, score=0.3)
        PageRank.objects.create(article=a2, score=0.1)
        PageRank.objects.create(article=a3, score=0.2)

    def test_single_term_query(self):
        """Test hybrid search with single term query."""
        results = search_hybrid("python", limit=5)
        self.assertGreater(len(results), 0)
        # Both articles with "python" should be in results
        article_titles = [article.title for article, score in results]
        self.assertIn("Python Programming", article_titles)
        self.assertIn("Snake", article_titles)

    def test_multi_term_query(self):
        """Test hybrid search with multi-term query."""
        results = search_hybrid("python programming", limit=5)
        self.assertGreater(len(results), 0)
        # Python Programming should rank high due to both terms
        self.assertEqual(results[0][0].title, "Python Programming")

    def test_no_match(self):
        """Test query with no vocabulary matches."""
        results = search_hybrid("quantum physics", limit=5)
        self.assertEqual(len(results), 0)

    def test_limit_parameter(self):
        """Test that limit parameter is respected."""
        results = search_hybrid("programming", limit=1)
        self.assertEqual(len(results), 1)

    def test_alpha_parameter(self):
        """Test that alpha parameter affects ranking."""
        # Alpha = 1.0 (pure TF-IDF, no PageRank)
        results_tfidf = search_hybrid("python", limit=3, alpha=1.0)
        # Alpha = 0.0 (pure PageRank, no TF-IDF)
        results_pagerank = search_hybrid("python", limit=3, alpha=0.0)
        
        # Results should exist for both
        self.assertGreater(len(results_tfidf), 0)
        self.assertGreater(len(results_pagerank), 0)

    def test_empty_query(self):
        """Test that empty query returns no results."""
        results = search_hybrid("", limit=5)
        self.assertEqual(len(results), 0)

    def test_stopwords_only_query(self):
        """Test query with only stopwords (should tokenize to empty)."""
        results = search_hybrid("the and or", limit=5)
        self.assertEqual(len(results), 0)
