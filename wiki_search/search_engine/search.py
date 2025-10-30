"""
Search functions for hybrid TF-IDF + PageRank retrieval.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
import time
import math as _math
from typing import Dict, List, Set, Tuple

from django.db.models import Q

from .models import Article, InvertedIndex, PageRank, Vocabulary
from .tokenizer import tokenize

logger = logging.getLogger(__name__)


def search_hybrid(
    query: str,
    limit: int = 20,
    alpha: float = 0.85,
    max_candidates: int = 500,
    coverage_bonus_weight: float = 0.0,
    strict_and_filter: bool = False,
    min_term_match_policy: str = "balanced",
    enable_partial_title_boost: bool = True
) -> List[Tuple[Article, float]]:
    """
    Hybrid search combining TF-IDF relevance with PageRank authority.
    
    Args:
        query: Search query string
        limit: Maximum number of results to return (default: 20)
        alpha: Weight for TF-IDF score in linear blend, 0-1 (default: 0.85)
               Final score = alpha * tfidf_norm + (1-alpha) * pagerank_norm
        max_candidates: Maximum total InvertedIndex entries to fetch (default: 500)
        coverage_bonus_weight: Weight for coverage bonus (default: 0.0)
        strict_and_filter: Enable strict AND filtering for queries with ≤5 terms (default: False)
        min_term_match_policy: balanced|strict|len2_strict (default: balanced)
        enable_partial_title_boost: Enable prefix/contains title boosts (default: True)
    
    Returns:
        List of (Article, hybrid_score) tuples sorted by score descending
    """
    # Tokenize query
    t0 = time.perf_counter()
    query_terms = tokenize(query)
    t_tokenize = time.perf_counter() - t0
    
    if not query_terms:
        logger.debug("Empty query after tokenization")
        return []
    
    # Fetch vocabulary IDs for query terms
    t1 = time.perf_counter()
    vocab_lookup = {
        v.term: v.id 
        for v in Vocabulary.objects.filter(term__in=query_terms)
    }
    t_vocab = time.perf_counter() - t1
    
    if not vocab_lookup:
        logger.debug(f"No vocabulary matches for query terms: {query_terms}")
        return []
    
    vocab_ids = list(vocab_lookup.values())
    
    # Query each term separately to use (term_id, tf_idf_score) index efficiently
    # Then merge results in Python
    article_tfidf_scores: Dict[int, float] = defaultdict(float)
    article_term_coverage: Dict[int, int] = defaultdict(int)
    
    # Dynamic per-term limit based on max_candidates
    per_term_limit = math.ceil(max_candidates / max(1, len(vocab_ids)))
    t2 = time.perf_counter()
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
            article_term_coverage[article_id] += 1
    t_postings = time.perf_counter() - t2
    
    if not article_tfidf_scores:
        logger.debug("No articles found in InvertedIndex")
        return []
    
    # Apply multi-term coverage filtering
    num_query_terms = len(query_terms)
    if num_query_terms == 1:
        min_term_match = 1
    else:
        if strict_and_filter and num_query_terms <= 5:
            min_term_match = num_query_terms
        elif min_term_match_policy == "strict":
            min_term_match = num_query_terms
        elif min_term_match_policy == "len2_strict" and num_query_terms == 2:
            min_term_match = 2
        else:
            # balanced default: require 2 for any multi-term
            min_term_match = 2
    
    # Filter candidates by coverage
    filtered_article_ids = [
        article_id for article_id in article_tfidf_scores.keys()
        if article_term_coverage[article_id] >= min_term_match
    ]
    
    if not filtered_article_ids:
        logger.debug(f"No articles matched min_term_match={min_term_match}")
        return []
    
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            f"Filtered {len(article_tfidf_scores)} candidates to {len(filtered_article_ids)} "
            f"with min_term_match={min_term_match}"
        )
    
    # Apply coverage bonus to TF-IDF scores
    # Concave via log1p for diminishing returns
    if coverage_bonus_weight != 0.0:
        for article_id in filtered_article_ids:
            coverage = article_term_coverage[article_id]
            if coverage > 1:
                coverage_bonus = coverage_bonus_weight * _math.log1p(coverage - 1)
                article_tfidf_scores[article_id] += coverage_bonus
    
    # Get candidate article IDs (only filtered ones)
    candidate_ids = filtered_article_ids
    
    # Bulk fetch PageRank scores
    t3 = time.perf_counter()
    pagerank_lookup = {
        pr.article_id: pr.score
        for pr in PageRank.objects.filter(article_id__in=candidate_ids)
    }
    t_pagerank = time.perf_counter() - t3
    
    # Max-normalize TF-IDF scores to [0, 1]
    tfidf_scores_for_candidates = [article_tfidf_scores[aid] for aid in candidate_ids]
    tfidf_max = max(tfidf_scores_for_candidates) if tfidf_scores_for_candidates else 0.0
    
    if tfidf_max > 0:
        tfidf_normalized = {
            article_id: article_tfidf_scores[article_id] / tfidf_max
            for article_id in candidate_ids
        }
    else:
        # All TF-IDF scores are zero or no candidates - keep raw scores
        tfidf_normalized = {
            article_id: article_tfidf_scores[article_id]
            for article_id in candidate_ids
        }
    
    # Max-normalize PageRank scores to [0, 1] and compute median for imputation
    if pagerank_lookup:
        pr_scores = list(pagerank_lookup.values())
        pr_max = max(pr_scores)
        median_pr = statistics.median(pr_scores)
        
        if pr_max > 0:
            pr_normalized = {
                article_id: score / pr_max
                for article_id, score in pagerank_lookup.items()
            }
            median_pr_normalized = median_pr / pr_max
        else:
            # All PageRank scores are zero - keep raw scores
            pr_normalized = pagerank_lookup.copy()
            median_pr_normalized = median_pr
    else:
        pr_normalized = {}
        median_pr_normalized = 0.0
    
    # Compute hybrid scores
    hybrid_scores = []
    for article_id in candidate_ids:
        tfidf_norm = tfidf_normalized[article_id]
        pr_norm = pr_normalized.get(article_id, median_pr_normalized)  # Use median for missing PageRank
        
        hybrid_score = alpha * tfidf_norm + (1.0 - alpha) * pr_norm
        hybrid_scores.append((article_id, hybrid_score))
    
    # Sort by hybrid score descending, then tfidf, then pagerank, then article_id
    hybrid_scores.sort(key=lambda x: (
        -x[1],  # hybrid_score desc
        -tfidf_normalized[x[0]],  # tfidf desc
        -pr_normalized.get(x[0], median_pr_normalized),  # pagerank desc
        x[0]  # article_id asc
    ))
    # Fetch slightly more articles than limit to handle missing ones
    fetch_ids = [article_id for article_id, _ in hybrid_scores[:limit + 10]]
    
    # Bulk fetch articles
    t4 = time.perf_counter()
    articles = {
        article.id: article
        for article in Article.objects.filter(id__in=fetch_ids)
    }
    t_articles = time.perf_counter() - t4
    
    # Build results maintaining score order, applying title boost
    results = []
    query_lower = query.lower()
    boosted_scores = {}  # Track boosted scores for re-sorting
    
    for article_id, score in hybrid_scores:
        if article_id in articles:
            article = articles[article_id]
            # Apply title exact-match boost (1.5x multiplier) in Python
            if article.title.lower() == query_lower:
                score = score * 1.5
                boosted_scores[article_id] = True
                logger.debug(f"Applied title boost to: {article.title}")
            elif enable_partial_title_boost:
                # Light boosts for prefix/contains when unique to avoid noise
                title_lower = article.title.lower()
                if title_lower.startswith(query_lower):
                    score = score * 1.1
                    boosted_scores[article_id] = True
                elif len(query_lower) >= 4 and query_lower in title_lower:
                    score = score * 1.05
                    boosted_scores[article_id] = True
            results.append((article, score, article_id))
            if len(results) >= limit:
                break
    
    # Re-sort results if any boosts were applied
    if boosted_scores:
        results.sort(key=lambda x: (
            -x[1],  # score desc
            -tfidf_normalized[x[2]],  # tfidf desc
            -pr_normalized.get(x[2], median_pr_normalized),  # pagerank desc
            x[2]  # article_id asc
        ))
        results = [(article, score) for article, score, _ in results[:limit]]
    else:
        results = [(article, score) for article, score, _ in results[:limit]]
    
    if logger.isEnabledFor(logging.DEBUG):
        t_total = t_tokenize + t_vocab + t_postings + t_pagerank + t_articles
        logger.debug(
            "search_hybrid timings ms="
            f"tokenize={t_tokenize*1000:.2f}, "
            f"vocab={t_vocab*1000:.2f}, "
            f"postings={t_postings*1000:.2f}, "
            f"pagerank={t_pagerank*1000:.2f}, "
            f"articles={t_articles*1000:.2f}, "
            f"total_approx={t_total*1000:.2f}"
        )
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

