from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Tuple

import numpy as np

from django.db.models import QuerySet

from .models import Article, TFIDFIndex, Vocabulary


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
    return float(np.sqrt(np.sum(np.square(np.fromiter(values, dtype=float)))))


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


