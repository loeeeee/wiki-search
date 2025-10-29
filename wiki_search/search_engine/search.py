from __future__ import annotations

import heapq
import logging
from collections import Counter
from typing import Dict, Iterable, List, Tuple

from django.db.models import F

from .models import Article, InvertedIndex, PageRank, Vocabulary
from .tokenizer import NLTKTokenizer


logger = logging.getLogger(__name__)


def _normalize_scores(score_map: Dict[int, float]) -> Dict[int, float]:
    if not score_map:
        return {}
    max_score = max(score_map.values())
    if max_score <= 0.0:
        return {k: 0.0 for k in score_map}
    return {k: v / max_score for k, v in score_map.items()}


def _fetch_articles_preserve_order(article_ids_in_order: List[int]) -> List[Article]:
    if not article_ids_in_order:
        return []
    articles_by_id = Article.objects.in_bulk(article_ids_in_order)
    return [articles_by_id[a_id] for a_id in article_ids_in_order if a_id in articles_by_id]


def search_hybrid(query: str, limit: int = 20) -> List[Tuple[Article, float]]:
    """Hybrid search using TF-IDF inverted index blended with PageRank.

    Single-threaded and single-processed as required.
    Returns list of (Article, final_score) sorted by score desc.
    """
    if not query or not isinstance(query, str):
        return []

    # Local tuning knobs (no module-level constants per project rules)
    max_postings_per_term = 2000
    alpha = 0.8  # blend weight for text relevance

    tokenizer = NLTKTokenizer()
    tokens = tokenizer.tokenize(query)
    if not tokens:
        return []

    query_tf = Counter(tokens)

    # Map tokens to vocabulary entries
    vocab_rows = list(
        Vocabulary.objects.filter(term__in=list(query_tf.keys()))
        .values("id", "term", "idf_value")
    )
    if not vocab_rows:
        return []

    term_to_vocab_id: Dict[str, int] = {row["term"]: row["id"] for row in vocab_rows}
    term_to_idf: Dict[str, float] = {row["term"]: float(row["idf_value"]) for row in vocab_rows}

    # Accumulate text scores over candidate articles
    text_scores: Dict[int, float] = {}
    for term, tf_q in query_tf.items():
        if term not in term_to_vocab_id:
            continue
        vocab_id = term_to_vocab_id[term]
        idf_val = term_to_idf.get(term, 0.0)
        if idf_val <= 0.0:
            continue

        # cap postings per term and order by tf-idf desc
        postings = (
            InvertedIndex.objects.filter(term_id=vocab_id)
            .order_by(F("tf_idf_score").desc())
            .values_list("article_id", "tf_idf_score")[:max_postings_per_term]
        )

        weight = idf_val  # simple query weight; extendable to 1+log(tf_q)
        for article_id, tfidf in postings:
            text_scores[article_id] = text_scores.get(article_id, 0.0) + weight * float(tfidf)

    if not text_scores:
        return []

    # Blend with PageRank
    candidate_ids = list(text_scores.keys())
    pr_rows = PageRank.objects.filter(article_id__in=candidate_ids).values_list("article_id", "score")
    pagerank_scores: Dict[int, float] = {a_id: float(score) for a_id, score in pr_rows}

    norm_text = _normalize_scores(text_scores)
    norm_pr = _normalize_scores(pagerank_scores)

    final_scores: Dict[int, float] = {}
    for a_id, t_score in norm_text.items():
        pr_score = norm_pr.get(a_id, 0.0)
        final_scores[a_id] = alpha * t_score + (1.0 - alpha) * pr_score

    if not final_scores:
        return []

    # Top-K selection and fetch articles
    top_items: List[Tuple[int, float]] = heapq.nlargest(limit, final_scores.items(), key=lambda kv: kv[1])
    ordered_ids = [a_id for a_id, _ in top_items]
    id_to_score = dict(top_items)

    articles = _fetch_articles_preserve_order(ordered_ids)
    # Ensure ordering matches scores list exactly
    result: List[Tuple[Article, float]] = []
    article_id_set = set(ordered_ids)
    for a in articles:
        if a.id in article_id_set:
            result.append((a, id_to_score[a.id]))

    # In rare cases where some articles are missing (deleted), filter them out
    return result

