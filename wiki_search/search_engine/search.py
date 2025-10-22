from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Tuple

try:
    import numpy as np
except ImportError:
    # Fallback to math.sqrt for vector_l2_norm if numpy is not available
    import math
    np = None

from django.db.models import QuerySet

from .models import Article, InvertedIndex, PageRank, TFIDFIndex, Vocabulary


_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "is",
    "are",
    "of",
    "to",
    "in",
    "for",
    "on",
    "with",
    "as",
    "by",
    "at",
}


def tokenize(text: str | None) -> List[str]:
    if not text:
        return []
    tokens = [m.group(0).lower() for m in _WORD_RE.finditer(text)]
    return [t for t in tokens if t not in _STOPWORDS]


def compute_tf(tokens: Iterable[str]) -> Dict[str, float]:
    token_list = list(tokens)
    if not token_list:
        return {}
    counts = Counter(token_list)
    total = float(len(token_list))
    return {term: count / total for term, count in counts.items()}


def compute_idf(total_docs: int, document_frequency: int) -> float:
    # Use 1 + df for numerical stability; add-one smoothing
    return math.log((1.0 + total_docs) / (1.0 + float(document_frequency))) + 1.0


def vector_l2_norm(values: Iterable[float]) -> float:
    if np is not None:
        return float(np.sqrt(np.sum(np.square(np.fromiter(values, dtype=float)))))
    else:
        # Fallback implementation using math
        return math.sqrt(sum(x * x for x in values))


def search_by_title_exact(query: str, limit: int = 10) -> QuerySet[Article]:
    return Article.objects.filter(title__iexact=query).order_by('id')[:limit]


def _build_query_vector(tokens: List[str]) -> Tuple[Dict[int, float], float]:
    if not tokens:
        return ({}, 0.0)
    tf = compute_tf(tokens)
    # Load vocabulary idf map for only tokens in query
    vocab_rows = Vocabulary.objects.filter(term__in=list(tf.keys())).only("id", "term", "idf_value")
    term_to_id: Dict[str, Tuple[int, float]] = {v.term: (v.id, v.idf_value) for v in vocab_rows}
    vec: Dict[int, float] = {}
    for term, tf_val in tf.items():
        info = term_to_id.get(term)
        if not info:
            continue
        term_id, idf_val = info
        vec[term_id] = tf_val * idf_val
    norm = vector_l2_norm(vec.values()) if vec else 0.0
    return (vec, norm)


def _cosine_similarity(q_vec: Dict[int, float], q_norm: float, d_vec: Dict[str, float], d_norm: float) -> float:
    if q_norm == 0.0 or d_norm == 0.0:
        return 0.0
    # d_vec keys are JSON keys (strings); convert to ints once
    score = 0.0
    for term_id, q_w in q_vec.items():
        dw = d_vec.get(str(term_id))
        if dw:
            score += q_w * float(dw)
    return score / (q_norm * d_norm)


def search_by_tfidf(query: str, limit: int = 10) -> List[Tuple[Article, float]]:
    """Original TF-IDF search that scans all documents (slow for large datasets)."""
    tokens = tokenize(query)
    q_vec, q_norm = _build_query_vector(tokens)
    if not q_vec:
        return []
    # NOTE: For production-scale performance we should restrict to candidates via an inverted index.
    # For initial correctness and unit tests, we scan existing TFIDFIndex rows.
    results: List[Tuple[Article, float]] = []
    for row in TFIDFIndex.objects.select_related("article").only("tfidf_vector", "l2_norm", "article__title"):
        score = _cosine_similarity(q_vec, q_norm, row.tfidf_vector or {}, float(row.l2_norm))
        if score > 0.0:
            results.append((row.article, score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:limit]


def search_by_tfidf_optimized(query: str, limit: int = 10, use_inverted_index: bool = True) -> List[Tuple[Article, float]]:
    """Fast TF-IDF search using inverted index for candidate filtering."""
    tokens = tokenize(query)
    q_vec, q_norm = _build_query_vector(tokens)
    
    if not q_vec or not use_inverted_index:
        return search_by_tfidf(query, limit)  # fallback to original method
    
    # Get candidate articles via inverted index (FAST!)
    # Only articles containing at least one query term
    term_ids = list(q_vec.keys())
    candidates = InvertedIndex.objects.filter(
        term_id__in=term_ids
    ).values('article_id').distinct()
    
    candidate_ids = [c['article_id'] for c in candidates]
    
    if not candidate_ids:
        return []  # No articles contain any query terms
    
    # Score only candidates (not all articles!)
    results: List[Tuple[Article, float]] = []
    for row in TFIDFIndex.objects.filter(
        article_id__in=candidate_ids
    ).select_related("article").only("tfidf_vector", "l2_norm", "article__title"):
        score = _cosine_similarity(q_vec, q_norm, row.tfidf_vector or {}, float(row.l2_norm))
        if score > 0.0:
            results.append((row.article, score))
    
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:limit]


def search_hybrid(
    query: str, 
    limit: int = 10, 
    tfidf_weight: float = 0.7,
    pagerank_weight: float = 0.3
) -> List[Tuple[Article, float]]:
    """Combine TF-IDF relevance + PageRank authority for hybrid ranking."""
    # Get TF-IDF results (get more candidates for better hybrid ranking)
    tfidf_results = search_by_tfidf_optimized(query, limit=limit*3, use_inverted_index=True)
    
    if not tfidf_results:
        return []
    
    # Load PageRank scores for candidates
    article_ids = [art.id for art, _ in tfidf_results]
    pr_map = dict(
        PageRank.objects.filter(article_id__in=article_ids)
        .values_list('article_id', 'score')
    )
    
    # Normalize TF-IDF scores to [0, 1] range
    tfidf_scores = [score for _, score in tfidf_results]
    max_tfidf = max(tfidf_scores) if tfidf_scores else 1.0
    
    # Normalize PageRank scores to [0, 1] range
    pr_scores = [pr_map.get(art.id, 0.0) for art, _ in tfidf_results]
    max_pr = max(pr_scores) if pr_scores and max(pr_scores) > 0 else 1.0
    
    # Combine scores
    hybrid_results = []
    for article, tfidf_score in tfidf_results:
        pr_score = pr_map.get(article.id, 0.0)
        combined = (
            tfidf_weight * (tfidf_score / max_tfidf) + 
            pagerank_weight * (pr_score / max_pr)
        )
        hybrid_results.append((article, combined))
    
    hybrid_results.sort(key=lambda x: x[1], reverse=True)
    return hybrid_results[:limit]


def search_by_pagerank(query_title: str, limit: int = 10) -> List[Tuple[Article, float]]:
    """Find articles by PageRank score (for exploration and authority-based search)."""
    articles = Article.objects.filter(
        title__icontains=query_title
    ).select_related('pagerank').order_by('-pagerank__score')[:limit]
    
    return [(a, a.pagerank.score if hasattr(a, 'pagerank') else 0.0) for a in articles]


def search_by_pagerank_only(limit: int = 10) -> List[Tuple[Article, float]]:
    """Get top articles by PageRank score only (for exploring most authoritative pages)."""
    articles = Article.objects.filter(
        pagerank__isnull=False
    ).select_related('pagerank').order_by('-pagerank__score')[:limit]
    
    return [(a, a.pagerank.score) for a in articles]


