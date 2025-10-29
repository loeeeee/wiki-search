"""
Search functions for hybrid TF-IDF + PageRank retrieval.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Tuple

from django.db.models import Q

from .models import Article, InvertedIndex, PageRank, Vocabulary
from .tokenizer import tokenize

logger = logging.getLogger(__name__)


def search_hybrid(
    query: str,
    limit: int = 20,
    alpha: float = 0.7,
    max_candidates: int = 500
) -> List[Tuple[Article, float]]:
    """
    Hybrid search combining TF-IDF relevance with PageRank authority.
    
    Args:
        query: Search query string
        limit: Maximum number of results to return (default: 20)
        alpha: Weight for TF-IDF score in linear blend, 0-1 (default: 0.7)
               Final score = alpha * tfidf_norm + (1-alpha) * pagerank_norm
        max_candidates: Maximum total InvertedIndex entries to fetch (default: 500)
    
    Returns:
        List of (Article, hybrid_score) tuples sorted by score descending
    """
    # Tokenize query
    query_terms = tokenize(query)
    
    if not query_terms:
        logger.debug("Empty query after tokenization")
        return []
    
    # Fetch vocabulary IDs for query terms
    vocab_lookup = {
        v.term: v.id 
        for v in Vocabulary.objects.filter(term__in=query_terms)
    }
    
    if not vocab_lookup:
        logger.debug(f"No vocabulary matches for query terms: {query_terms}")
        return []
    
    vocab_ids = list(vocab_lookup.values())
    
    # Query each term separately to use (term_id, tf_idf_score) index efficiently
    # Then merge results in Python
    article_tfidf_scores: Dict[int, float] = defaultdict(float)
    
    # Limit per term (smaller limit = faster queries, but may miss relevant docs)
    per_term_limit = 20
    
    for vocab_id in vocab_ids:
        # This query uses the (term_id, tf_idf_score) index efficiently
        entries = list(
            InvertedIndex.objects
            .filter(term_id=vocab_id)
            .order_by('-tf_idf_score')[:per_term_limit]
            .values_list('article_id', 'tf_idf_score', named=False)
        )
        
        for article_id, score in entries:
            article_tfidf_scores[article_id] += score
    
    if not article_tfidf_scores:
        logger.debug("No articles found in InvertedIndex")
        return []
    
    # Get candidate article IDs
    candidate_ids = list(article_tfidf_scores.keys())
    
    # Bulk fetch PageRank scores
    pagerank_lookup = {
        pr.article_id: pr.score
        for pr in PageRank.objects.filter(article_id__in=candidate_ids)
    }
    
    # Normalize TF-IDF scores to [0, 1]
    tfidf_scores = list(article_tfidf_scores.values())
    tfidf_min = min(tfidf_scores)
    tfidf_max = max(tfidf_scores)
    tfidf_range = tfidf_max - tfidf_min
    
    if tfidf_range > 0:
        tfidf_normalized = {
            article_id: (score - tfidf_min) / tfidf_range
            for article_id, score in article_tfidf_scores.items()
        }
    else:
        # All TF-IDF scores are identical
        tfidf_normalized = {article_id: 1.0 for article_id in article_tfidf_scores.keys()}
    
    # Normalize PageRank scores to [0, 1]
    if pagerank_lookup:
        pr_scores = list(pagerank_lookup.values())
        pr_min = min(pr_scores)
        pr_max = max(pr_scores)
        pr_range = pr_max - pr_min
        
        if pr_range > 0:
            pr_normalized = {
                article_id: (score - pr_min) / pr_range
                for article_id, score in pagerank_lookup.items()
            }
        else:
            # All PageRank scores are identical
            pr_normalized = {article_id: 1.0 for article_id in pagerank_lookup.keys()}
    else:
        pr_normalized = {}
    
    # Compute hybrid scores
    hybrid_scores = []
    for article_id in candidate_ids:
        tfidf_norm = tfidf_normalized[article_id]
        pr_norm = pr_normalized.get(article_id, 0.0)  # Default to 0 if no PageRank
        
        hybrid_score = alpha * tfidf_norm + (1.0 - alpha) * pr_norm
        hybrid_scores.append((article_id, hybrid_score))
    
    # Sort by hybrid score descending and take top limit
    hybrid_scores.sort(key=lambda x: x[1], reverse=True)
    top_article_ids = [article_id for article_id, _ in hybrid_scores[:limit]]
    
    # Bulk fetch articles
    articles = {
        article.id: article
        for article in Article.objects.filter(id__in=top_article_ids)
    }
    
    # Build results maintaining score order
    results = []
    for article_id, score in hybrid_scores[:limit]:
        if article_id in articles:
            results.append((articles[article_id], score))
    
    logger.debug(f"Hybrid search returned {len(results)} results for query: {query}")
    return results


def search_by_title_exact(query: str, limit: int = 20) -> List[Article]:
    """
    Fallback search by exact title match (case-insensitive).
    
    Args:
        query: Search query string
        limit: Maximum number of results to return (default: 20)
    
    Returns:
        List of Article objects matching the title
    """
    articles = Article.objects.filter(title__iexact=query)[:limit]
    return list(articles)

