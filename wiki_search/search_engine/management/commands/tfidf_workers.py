"""Worker functions for TF-IDF index building with proper Django initialization for spawn multiprocessing."""

from collections import Counter
from typing import Dict, List, Tuple


def _compute_doc_freq_batch(article_tuples: List[Tuple[int, List[str]]]) -> Counter:
    """Worker: tokenize article paragraphs, return local df Counter.
    
    Input: lightweight tuples (article_id, paragraphs)
    Output: Counter of unique terms seen across batch
    """
    # Initialize Django for spawn multiprocessing - MUST be first
    import os
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wiki_search.settings')
    django.setup()
    
    # Import Django modules after setup
    from search_engine.tokenizer import tokenize
    
    doc_freq = Counter()
    for article_id, paragraphs in article_tuples:
        seen_terms = set()
        for para in paragraphs:
            seen_terms.update(tokenize(para))
        doc_freq.update(seen_terms)
    return doc_freq


def _build_tfidf_batch(
    article_tuples: List[Tuple[int, List[str]]],
    term_to_id: Dict[str, int],
    term_to_idf: Dict[str, float]
) -> Tuple[
    List[Tuple[int, Dict[int, float], float, List[int]]],  # (article_id, tfidf_vec, l2_norm, token_counts)
    List[Tuple[int, int, float]]  # (term_id, article_id, tfidf_score) for InvertedIndex
]:
    """Worker: compute TF-IDF vectors, inverted index tuples, and token counts.
    
    Returns lightweight tuples to minimize serialization overhead.
    """
    # Initialize Django for spawn multiprocessing - MUST be first
    import os
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wiki_search.settings')
    django.setup()
    
    # Import Django modules after setup
    from search_engine.search import compute_tf, vector_l2_norm
    from search_engine.tokenizer import tokenize
    
    tfidf_tuples = []
    inverted_tuples = []
    
    for article_id, paragraphs in article_tuples:
        tokens = []
        token_counts = []
        
        # Compute token counts per paragraph
        for para in paragraphs:
            para_tokens = tokenize(para)
            tokens.extend(para_tokens)
            token_counts.append(len(para_tokens))
        
        tf = compute_tf(tokens)
        vec = {}
        for term, tf_val in tf.items():
            term_id = term_to_id.get(term)
            idf_val = term_to_idf.get(term)
            if term_id is None or idf_val is None:
                continue
            tfidf_score = tf_val * idf_val
            vec[term_id] = tfidf_score
            inverted_tuples.append((term_id, article_id, tfidf_score))
        
        l2_norm = vector_l2_norm(vec.values()) if vec else 0.0
        tfidf_tuples.append((article_id, vec, l2_norm, token_counts))
    
    return tfidf_tuples, inverted_tuples


def _build_tfidf_batch_gpu(
    article_tuples: List[Tuple[int, List[str]]],
    term_to_id: Dict[str, int],
    term_to_idf: Dict[str, float],
    device
) -> Tuple[
    List[Tuple[int, Dict[int, float], float, List[int]]],  # (article_id, tfidf_vec, l2_norm, token_counts)
    List[Tuple[int, int, float]]  # (term_id, article_id, tfidf_score) for InvertedIndex
]:
    """GPU-accelerated worker: compute TF-IDF vectors, inverted index tuples, and token counts.
    
    Returns lightweight tuples to minimize serialization overhead.
    """
    # Initialize Django for spawn multiprocessing - MUST be first
    import os
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wiki_search.settings')
    django.setup()
    
    # Import Django modules after setup
    from search_engine.search import compute_tfidf_batch_gpu
    from search_engine.tokenizer import tokenize
    
    # Extract tokens for batch processing
    article_tokens = []
    article_ids = []
    token_counts_list = []
    
    for article_id, paragraphs in article_tuples:
        tokens = []
        token_counts = []
        
        # Compute token counts per paragraph
        for para in paragraphs:
            para_tokens = tokenize(para)
            tokens.extend(para_tokens)
            token_counts.append(len(para_tokens))
        
        article_tokens.append(tokens)
        article_ids.append(article_id)
        token_counts_list.append(token_counts)
    
    # GPU-accelerated batch TF-IDF computation
    tfidf_vectors, l2_norms = compute_tfidf_batch_gpu(
        article_tokens, term_to_id, term_to_idf, device
    )
    
    # Convert results to expected format
    tfidf_tuples = []
    inverted_tuples = []
    
    for i, (article_id, vec, l2_norm, token_counts) in enumerate(
        zip(article_ids, tfidf_vectors, l2_norms, token_counts_list)
    ):
        # Create inverted index tuples
        for term_id, tfidf_score in vec.items():
            inverted_tuples.append((term_id, article_id, tfidf_score))
        
        tfidf_tuples.append((article_id, vec, l2_norm, token_counts))
    
    return tfidf_tuples, inverted_tuples
