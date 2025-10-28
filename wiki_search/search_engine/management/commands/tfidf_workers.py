"""
Worker functions for TF-IDF index building with multiprocessing support.

This module provides worker functions for the GPU-accelerated TF-IDF index builder,
designed to work with multiprocessing (fork context). Each function handles Django
database connection cleanup and provides specialized processing capabilities.

Architecture:
    Document Frequency Computation:
        - _compute_doc_freq_batch(): Tokenizes articles and counts unique terms
        
    TF-IDF Computation (CPU):
        - _build_tfidf_batch(): CPU-based TF-IDF computation for small batches
        - _build_tfidf_batch_cpu_fallback(): CPU fallback for test mode
        
    TF-IDF Computation (GPU):
        - _build_tfidf_batch_gpu(): GPU-accelerated batch TF-IDF computation

Key Features:
    - Proper Django database connection handling for multiprocessing
    - Lightweight tuple returns to minimize serialization overhead
    - GPU acceleration with CPU tokenization fallback
    - Test mode support for development without GPU

Multiprocessing Considerations:
    - Uses fork multiprocessing context (Django already initialized in parent)
    - Closes inherited database connections in each worker process
    - Returns lightweight data structures to minimize IPC overhead
"""

from collections import Counter
from typing import Dict, List, Tuple

# Import Django modules at top level (works with fork since Django is already set up in parent)
from search_engine.search import compute_tf, vector_l2_norm, compute_tfidf_batch_gpu
from search_engine.tokenizer import tokenize


def _compute_doc_freq_batch(article_tuples: List[Tuple[int, List[str]]]) -> Tuple[Counter, List[Tuple[int, List[str], List[int]]]]:
    """
    Worker: tokenize article paragraphs and return local document frequency counter.
    
    Processes a batch of articles to compute document frequency (how many articles
    contain each unique term). This is used in Pass 1 of the TF-IDF index building
    process to build the vocabulary.
    
    Args:
        article_tuples: List of (article_id, paragraphs) tuples where:
            - article_id (int): Unique article identifier
            - paragraphs (List[str]): List of paragraph text strings
            
    Returns:
        Tuple:
            - Counter: Document frequency counter where keys are terms and values are doc counts
            - List of (article_id, tokens, token_counts_per_paragraph) for reuse in Pass 2
            
    Implementation:
        - Closes inherited database connections for multiprocessing safety
        - Tokenizes each paragraph using NLTK tokenizer
        - Tracks unique terms per article (each article contributes at most 1 to DF)
        - Returns Counter of unique terms across the entire batch
        
    Multiprocessing Notes:
        - Designed for fork multiprocessing context
        - Closes Django database connections inherited from parent process
        - Returns lightweight Counter object for efficient IPC
    """
    # Close inherited database connections (required for multiprocessing)
    # Django connections are inherited from parent process and must be closed
    from django.db import connections
    for conn in connections.all():
        conn.close()
    
    # Document frequency computation: count unique terms across articles
    doc_freq = Counter()
    pretokenized: List[Tuple[int, List[str], List[int]]] = []
    for article_id, paragraphs in article_tuples:
        seen_terms = set()
        tokens: List[str] = []
        token_counts: List[int] = []
        for para in paragraphs:
            para_tokens = tokenize(para)
            tokens.extend(para_tokens)
            token_counts.append(len(para_tokens))
            seen_terms.update(para_tokens)
        doc_freq.update(seen_terms)
        pretokenized.append((article_id, tokens, token_counts))
    return doc_freq, pretokenized


def _build_tfidf_batch(
    article_tuples: List[Tuple[int, List[str]]],
    term_to_id: Dict[str, int],
    term_to_idf: Dict[str, float]
) -> Tuple[
    List[Tuple[int, Dict[int, float], float, List[int]]],  # (article_id, tfidf_vec, l2_norm, token_counts)
    List[Tuple[int, int, float]]  # (term_id, article_id, tfidf_score) for InvertedIndex
]:
    """
    Worker: compute TF-IDF vectors, inverted index tuples, and token counts using CPU.
    
    Processes a batch of articles to compute TF-IDF vectors and inverted index entries.
    This is the CPU-based implementation used for smaller batches or when GPU is not available.
    
    Args:
        article_tuples: List of (article_id, paragraphs) tuples
        term_to_id: Mapping from vocabulary terms to term IDs
        term_to_idf: Mapping from vocabulary terms to IDF values
        
    Returns:
        Tuple containing:
            - tfidf_tuples: List of (article_id, tfidf_vector, l2_norm, token_counts)
            - inverted_tuples: List of (term_id, article_id, tfidf_score) for InvertedIndex
            
    Implementation:
        - Closes inherited database connections for multiprocessing safety
        - Tokenizes paragraphs using NLTK tokenizer
        - Computes TF scores for each article
        - Multiplies TF by IDF to get TF-IDF scores
        - Computes L2 normalization for cosine similarity
        - Creates inverted index entries for efficient search
        
    Performance:
        - CPU-based computation suitable for small batches
        - Returns lightweight tuples to minimize serialization overhead
        - Processes articles sequentially within batch
    """
    # Close inherited database connections (required for multiprocessing)
    # Django connections are inherited from parent process and must be closed
    from django.db import connections
    for conn in connections.all():
        conn.close()
    
    # Initialize result containers for TF-IDF computation
    tfidf_tuples = []
    inverted_tuples = []
    
    for article_id, paragraphs in article_tuples:
        tokens = []
        token_counts = []
        
        # Compute token counts per paragraph for paragraph-level analysis
        for para in paragraphs:
            para_tokens = tokenize(para)
            tokens.extend(para_tokens)
            token_counts.append(len(para_tokens))
        
        # Compute TF scores for this article
        tf = compute_tf(tokens)
        vec = {}
        
        # Convert TF scores to TF-IDF scores and create inverted index entries
        for term, tf_val in tf.items():
            term_id = term_to_id.get(term)
            idf_val = term_to_idf.get(term)
            if term_id is None or idf_val is None:
                continue  # Skip terms not in vocabulary
            tfidf_score = tf_val * idf_val
            vec[term_id] = tfidf_score
            # Create inverted index entry for efficient search
            inverted_tuples.append((term_id, article_id, tfidf_score))
        
        # Compute L2 normalization for cosine similarity
        l2_norm = vector_l2_norm(vec.values()) if vec else 0.0
        tfidf_tuples.append((article_id, vec, l2_norm, token_counts))
    
    return tfidf_tuples, inverted_tuples


def _build_tfidf_batch_cpu_fallback(
    article_tuples: List[Tuple[int, List[str]]],
    term_to_id: Dict[str, int],
    term_to_idf: Dict[str, float]
) -> Tuple[
    List[Tuple[int, Dict[int, float], float, List[int]]],  # (article_id, tfidf_vec, l2_norm, token_counts)
    List[Tuple[int, int, float]]  # (term_id, article_id, tfidf_score) for InvertedIndex
]:
    """
    CPU fallback for GPU functions in test mode.
    
    Provides the same interface as the GPU version but uses CPU computation.
    This function is used only for development testing when GPU is not available
    or when --test-mode is enabled.
    
    Args:
        article_tuples: List of (article_id, paragraphs) tuples
        term_to_id: Mapping from vocabulary terms to term IDs
        term_to_idf: Mapping from vocabulary terms to IDF values
        
    Returns:
        Tuple containing:
            - tfidf_tuples: List of (article_id, tfidf_vector, l2_norm, token_counts)
            - inverted_tuples: List of (term_id, article_id, tfidf_score) for InvertedIndex
            
    Implementation:
        - Identical to _build_tfidf_batch() but with explicit CPU fallback documentation
        - Closes inherited database connections for multiprocessing safety
        - Uses CPU-based tokenization and TF-IDF computation
        - Returns same data structure as GPU version for interface compatibility
        
    Use Cases:
        - Development testing without GPU hardware
        - CI/CD environments without GPU support
        - Debugging GPU-related issues
        - Small datasets where GPU overhead isn't justified
    """
    # Close inherited database connections (required for multiprocessing)
    # Django connections are inherited from parent process and must be closed
    from django.db import connections
    for conn in connections.all():
        conn.close()
    
    # Initialize result containers for TF-IDF computation (CPU fallback)
    tfidf_tuples = []
    inverted_tuples = []
    
    for article_id, paragraphs in article_tuples:
        tokens = []
        token_counts = []
        
        # Compute token counts per paragraph (CPU tokenization)
        for para in paragraphs:
            para_tokens = tokenize(para)
            tokens.extend(para_tokens)
            token_counts.append(len(para_tokens))
        
        # Compute TF scores for this article (CPU computation)
        tf = compute_tf(tokens)
        vec = {}
        
        # Convert TF scores to TF-IDF scores and create inverted index entries
        for term, tf_val in tf.items():
            term_id = term_to_id.get(term)
            idf_val = term_to_idf.get(term)
            if term_id is None or idf_val is None:
                continue  # Skip terms not in vocabulary
            tfidf_score = tf_val * idf_val
            vec[term_id] = tfidf_score
            # Create inverted index entry for efficient search
            inverted_tuples.append((term_id, article_id, tfidf_score))
        
        # Compute L2 normalization for cosine similarity (CPU computation)
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
    """
    GPU-accelerated worker: compute TF-IDF vectors, inverted index tuples, and token counts.
    
    Processes a batch of articles using GPU acceleration for TF-IDF computation.
    This is the high-performance implementation used in Pass 2 of the TF-IDF index
    building process for large batches (typically 10,000 articles).
    
    Args:
        article_tuples: List of (article_id, paragraphs) tuples
        term_to_id: Mapping from vocabulary terms to term IDs
        term_to_idf: Mapping from vocabulary terms to IDF values
        device: PyTorch device (cuda/cpu) for GPU processing
        
    Returns:
        Tuple containing:
            - tfidf_tuples: List of (article_id, tfidf_vector, l2_norm, token_counts)
            - inverted_tuples: List of (term_id, article_id, tfidf_score) for InvertedIndex
            
    Implementation:
        - Closes inherited database connections for multiprocessing safety
        - Tokenizes paragraphs on CPU (NLTK tokenizer)
        - Transfers tokenized data to GPU for batch processing
        - Uses compute_tfidf_batch_gpu() for GPU-accelerated TF-IDF computation
        - Computes L2 normalization on GPU
        - Creates inverted index entries for efficient search
        
    GPU Pipeline:
        1. CPU tokenization of all articles in batch
        2. Transfer tokenized data to GPU memory
        3. GPU batch TF computation
        4. GPU TF-IDF multiplication (TF * IDF)
        5. GPU L2 normalization
        6. Transfer results back to CPU
        
    Performance:
        - GPU-accelerated computation for large batches
        - 5-10x speedup over CPU implementation
        - Batch processing minimizes GPU memory transfers
        - Returns lightweight tuples to minimize serialization overhead
    """
    # Close inherited database connections (required for multiprocessing)
    # Django connections are inherited from parent process and must be closed
    from django.db import connections
    for conn in connections.all():
        conn.close()
    
    # Extract tokens for batch processing on GPU
    # Prepare data structures for GPU-accelerated computation
    article_tokens = []
    article_ids = []
    token_counts_list = []
    
    for article_id, paragraphs in article_tuples:
        tokens = []
        token_counts = []
        
        # Compute token counts per paragraph (CPU tokenization)
        # Tokenization happens on CPU, computation on GPU
        for para in paragraphs:
            para_tokens = tokenize(para)
            tokens.extend(para_tokens)
            token_counts.append(len(para_tokens))
        
        # Collect data for batch GPU processing
        article_tokens.append(tokens)
        article_ids.append(article_id)
        token_counts_list.append(token_counts)
    
    # GPU-accelerated batch TF-IDF computation (full pipeline)
    # This is the core GPU processing step that provides 5-10x speedup
    tfidf_vectors, l2_norms = compute_tfidf_batch_gpu(
        article_tokens, term_to_id, term_to_idf, device
    )
    
    # Convert GPU results to expected format for database storage
    tfidf_tuples = []
    inverted_tuples = []
    
    for i, (article_id, vec, l2_norm, token_counts) in enumerate(
        zip(article_ids, tfidf_vectors, l2_norms, token_counts_list)
    ):
        # Create inverted index tuples for efficient search
        for term_id, tfidf_score in vec.items():
            inverted_tuples.append((term_id, article_id, tfidf_score))
        
        # Store TF-IDF vector with metadata
        tfidf_tuples.append((article_id, vec, l2_norm, token_counts))
    
    return tfidf_tuples, inverted_tuples


def _build_tfidf_batch_cpu_from_tokens(
    pretokenized: List[Tuple[int, List[str], List[int]]],
    term_to_id: Dict[str, int],
    term_to_idf: Dict[str, float],
) -> Tuple[
    List[Tuple[int, Dict[int, float], float, List[int]]],
    List[Tuple[int, int, float]]
]:
    """
    CPU TF-IDF/inverted index computation from pretokenized input.
    """
    tfidf_tuples: List[Tuple[int, Dict[int, float], float, List[int]]] = []
    inverted_tuples: List[Tuple[int, int, float]] = []
    for article_id, tokens, token_counts in pretokenized:
        tf = compute_tf(tokens)
        vec: Dict[int, float] = {}
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


def _build_tfidf_batch_gpu_from_tokens(
    pretokenized: List[Tuple[int, List[str], List[int]]],
    term_to_id: Dict[str, int],
    term_to_idf: Dict[str, float],
    device,
) -> Tuple[
    List[Tuple[int, Dict[int, float], float, List[int]]],
    List[Tuple[int, int, float]]
]:
    """
    GPU TF-IDF/inverted index computation from pretokenized input.
    Avoids re-tokenization in Pass 2 by reusing tokens from Pass 1.
    """
    # Close inherited database connections (required for multiprocessing)
    from django.db import connections
    for conn in connections.all():
        conn.close()

    article_ids: List[int] = []
    article_tokens: List[List[str]] = []
    token_counts_list: List[List[int]] = []
    for item in pretokenized:
        # Handle optional timestamp (4th element) from profiling
        if len(item) == 4:
            article_id, tokens, token_counts, _timestamp = item
        else:
            article_id, tokens, token_counts = item
        article_ids.append(article_id)
        article_tokens.append(tokens)
        token_counts_list.append(token_counts)

    tfidf_vectors, l2_norms = compute_tfidf_batch_gpu(
        article_tokens, term_to_id, term_to_idf, device
    )

    tfidf_tuples: List[Tuple[int, Dict[int, float], float, List[int]]] = []
    inverted_tuples: List[Tuple[int, int, float]] = []
    for article_id, vec, l2_norm, token_counts in zip(
        article_ids, tfidf_vectors, l2_norms, token_counts_list
    ):
        for term_id, tfidf_score in vec.items():
            inverted_tuples.append((term_id, article_id, tfidf_score))
        tfidf_tuples.append((article_id, vec, l2_norm, token_counts))

    return tfidf_tuples, inverted_tuples