from __future__ import annotations

import logging
from typing import Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.sparse.linalg import norm
except ImportError:
    raise ImportError("scipy and numpy are required for PageRank computation. Please install them.")

from django.db import connection

logger = logging.getLogger(__name__)


def build_adjacency_matrix(limit: int | None = None) -> Tuple[csr_matrix, List[int], Dict[int, int]]:
    """Build sparse adjacency matrix from InternalLink graph.
    
    Returns:
        adjacency_matrix: Sparse matrix where A[i,j] = 1 if article j links to article i
        article_ids: List of article IDs in matrix order
        id_to_index: Mapping from article ID to matrix index
    """
    logger.info("Building adjacency matrix from InternalLink graph...")
    
    with connection.cursor() as cursor:
        # Get all valid links in a single query - this is more efficient
        if limit:
            cursor.execute("""
                SELECT from_article_id, to_article_id
                FROM search_engine_internallink
                WHERE from_article_id IS NOT NULL 
                  AND to_article_id IS NOT NULL
                  AND from_article_id != to_article_id  -- Skip self-loops
                LIMIT %s
            """, [limit])
        else:
            cursor.execute("""
                SELECT from_article_id, to_article_id
                FROM search_engine_internallink
                WHERE from_article_id IS NOT NULL 
                  AND to_article_id IS NOT NULL
                  AND from_article_id != to_article_id  -- Skip self-loops
            """)
        links = cursor.fetchall()
    
    if not links:
        logger.warning("No valid links found")
        return csr_matrix((0, 0)), [], {}
    
    # Extract article IDs from links and create mapping
    all_article_ids = set()
    for from_id, to_id in links:
        all_article_ids.add(from_id)
        all_article_ids.add(to_id)
    
    article_ids = sorted(all_article_ids)
    id_to_index = {article_id: idx for idx, article_id in enumerate(article_ids)}
    n = len(article_ids)
    
    logger.info(f"Building {n}x{n} adjacency matrix for {n} articles with links")
    
    # Build sparse matrix
    rows, cols = [], []
    for from_id, to_id in links:
        if from_id in id_to_index and to_id in id_to_index:
            # A[i,j] = 1 if article j links to article i
            rows.append(id_to_index[to_id])
            cols.append(id_to_index[from_id])
    
    # Create sparse matrix
    data = np.ones(len(rows), dtype=np.float64)
    adjacency_matrix = csr_matrix((data, (rows, cols)), shape=(n, n))
    
    logger.info(f"Built adjacency matrix with {adjacency_matrix.nnz} non-zero entries")
    return adjacency_matrix, article_ids, id_to_index


def build_adjacency_matrix_parallel(workers: int = 4, limit: int | None = None) -> Tuple[csr_matrix, List[int], Dict[int, int]]:
    """Build sparse adjacency matrix using parallel database reads.
    
    Uses ID range-based batching to parallelize database reads, following the pattern
    from load_wiki_dump.py. Each thread gets its own database connection.
    
    Args:
        workers: Number of parallel database workers
        limit: Optional limit on number of links to process
        
    Returns:
        adjacency_matrix: Sparse matrix where A[i,j] = 1 if article j links to article i
        article_ids: List of article IDs in matrix order
        id_to_index: Mapping from article ID to matrix index
    """
    logger.info(f"Building adjacency matrix using {workers} parallel database workers...")
    
    # Step 1: Get ID range with single query
    with connection.cursor() as cursor:
        if limit:
            cursor.execute("""
                SELECT MIN(id), MAX(id), COUNT(*)
                FROM search_engine_internallink
                WHERE from_article_id IS NOT NULL 
                  AND to_article_id IS NOT NULL
                  AND from_article_id != to_article_id
                LIMIT %s
            """, [limit])
        else:
            cursor.execute("""
                SELECT MIN(id), MAX(id), COUNT(*)
                FROM search_engine_internallink
                WHERE from_article_id IS NOT NULL 
                  AND to_article_id IS NOT NULL
                  AND from_article_id != to_article_id
            """)
        result = cursor.fetchone()
    
    if not result or result[2] == 0:
        logger.warning("No valid links found")
        return csr_matrix((0, 0)), [], {}
    
    min_id, max_id, total = result
    logger.info(f"Processing {total} links with ID range {min_id}-{max_id}")
    
    # Step 2: Create ID range batches
    batch_size = max(1, (max_id - min_id) // workers)
    id_ranges = []
    for i in range(workers):
        start_id = min_id + i * batch_size
        end_id = min_id + (i + 1) * batch_size if i < workers - 1 else max_id + 1
        id_ranges.append((start_id, end_id))
    
    logger.info(f"Created {len(id_ranges)} ID range batches for parallel processing")
    
    # Step 3: Parallel fetch using ThreadPoolExecutor
    def fetch_links_range(start_id: int, end_id: int) -> List[Tuple[int, int]]:
        """Fetch links for a specific ID range. Each thread gets its own DB connection."""
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT from_article_id, to_article_id
                FROM search_engine_internallink
                WHERE id >= %s AND id < %s
                  AND from_article_id IS NOT NULL
                  AND to_article_id IS NOT NULL
                  AND from_article_id != to_article_id
            """, [start_id, end_id])
            return cursor.fetchall()
    
    # Collect all links from parallel workers
    all_links = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch_links_range, start_id, end_id) 
                   for start_id, end_id in id_ranges]
        
        for future in as_completed(futures):
            try:
                links = future.result()
                all_links.extend(links)
            except Exception as e:
                logger.error(f"Error fetching links: {e}")
                raise
    
    if not all_links:
        logger.warning("No valid links found after parallel processing")
        return csr_matrix((0, 0)), [], {}
    
    logger.info(f"Retrieved {len(all_links)} links from {workers} parallel workers")
    
    # Step 4: Build matrix from aggregated links (same as original)
    all_article_ids = set()
    for from_id, to_id in all_links:
        all_article_ids.add(from_id)
        all_article_ids.add(to_id)
    
    article_ids = sorted(all_article_ids)
    id_to_index = {article_id: idx for idx, article_id in enumerate(article_ids)}
    n = len(article_ids)
    
    logger.info(f"Building {n}x{n} adjacency matrix for {n} articles with links")
    
    # Build sparse matrix
    rows, cols = [], []
    for from_id, to_id in all_links:
        if from_id in id_to_index and to_id in id_to_index:
            # A[i,j] = 1 if article j links to article i
            rows.append(id_to_index[to_id])
            cols.append(id_to_index[from_id])
    
    # Create sparse matrix
    data = np.ones(len(rows), dtype=np.float64)
    adjacency_matrix = csr_matrix((data, (rows, cols)), shape=(n, n))
    
    logger.info(f"Built adjacency matrix with {adjacency_matrix.nnz} non-zero entries using {workers} workers")
    return adjacency_matrix, article_ids, id_to_index


def compute_pagerank(
    damping: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
    verbose: bool = True,
    limit: int | None = None
) -> Tuple[Dict[int, float], int, float]:
    """Compute PageRank scores using power iteration.
    
    Args:
        damping: Damping factor (probability of following links vs random jump)
        max_iter: Maximum number of iterations
        tol: Convergence tolerance
        verbose: Whether to log progress
        
    Returns:
        pagerank_scores: Dict mapping article_id -> PageRank score
        iterations: Number of iterations until convergence
        residual: Final residual norm
    """
    logger.info(f"Computing PageRank with damping={damping}, max_iter={max_iter}, tol={tol}")
    
    # Build adjacency matrix
    adjacency_matrix, article_ids, id_to_index = build_adjacency_matrix(limit=limit)
    n = len(article_ids)
    
    if n == 0:
        logger.warning("No articles with links found")
        return {}, 0, 0.0
    
    # Normalize columns to get transition matrix
    # Each column should sum to 1 (outgoing links)
    col_sums = adjacency_matrix.sum(axis=0).A1  # Convert to 1D array
    col_sums[col_sums == 0] = 1  # Avoid division by zero for dangling nodes
    
    # Create transition matrix P where P[i,j] = 1/out_degree(j) if j->i
    transition_matrix = adjacency_matrix.multiply(1.0 / col_sums)
    
    # Handle dangling nodes (pages with no outgoing links)
    # They should link to all pages with equal probability
    dangling_mask = (col_sums == 1) & (adjacency_matrix.sum(axis=0).A1 == 0)
    dangling_indices = np.where(dangling_mask)[0] if np.any(dangling_mask) else np.array([], dtype=int)
    
    if len(dangling_indices) > 0:
        logger.info(f"Found {len(dangling_indices)} dangling nodes")
    
    # Initialize PageRank vector
    pagerank = np.ones(n) / n
    
    # Power iteration
    for iteration in range(max_iter):
        pagerank_old = pagerank.copy()
        
        # Standard PageRank update: pagerank = (1-damping)/n + damping * P * pagerank
        pagerank = (1 - damping) / n + damping * transition_matrix.dot(pagerank)
        
        # Handle dangling nodes using teleportation formula
        # Dangling nodes contribute their PageRank mass uniformly to all pages
        if len(dangling_indices) > 0:
            dangling_sum = damping * np.sum(pagerank[dangling_indices])
            pagerank += dangling_sum / n
        
        # Check convergence
        residual = np.linalg.norm(pagerank - pagerank_old, ord=1)
        
        if verbose and iteration % 10 == 0:
            logger.info(f"Iteration {iteration}: residual = {residual:.2e}")
        
        if residual < tol:
            logger.info(f"Converged after {iteration + 1} iterations (residual: {residual:.2e})")
            break
    else:
        logger.warning(f"Did not converge after {max_iter} iterations (residual: {residual:.2e})")
    
    # Normalize to ensure sum = 1
    pagerank = pagerank / pagerank.sum()
    
    # Convert to dictionary
    pagerank_scores = {article_ids[i]: float(pagerank[i]) for i in range(n)}
    
    logger.info(f"PageRank computation complete. Score range: [{min(pagerank):.6f}, {max(pagerank):.6f}]")
    logger.info(f"Sum of all scores: {sum(pagerank_scores.values()):.6f}")
    
    return pagerank_scores, iteration + 1, float(residual)


def compute_pagerank_parallel(
    damping: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
    verbose: bool = True,
    limit: int | None = None,
    db_read_workers: int = 4
) -> Tuple[Dict[int, float], int, float]:
    """Compute PageRank scores using parallel database reads.
    
    Args:
        damping: Damping factor (probability of following links vs random jump)
        max_iter: Maximum number of iterations
        tol: Convergence tolerance
        verbose: Whether to log progress
        limit: Optional limit on number of links to process
        db_read_workers: Number of parallel database workers for graph loading
        
    Returns:
        pagerank_scores: Dict mapping article_id -> PageRank score
        iterations: Number of iterations until convergence
        residual: Final residual norm
    """
    logger.info(f"Computing PageRank with parallel graph loading using {db_read_workers} workers")
    
    # Build adjacency matrix using parallel database reads
    adjacency_matrix, article_ids, id_to_index = build_adjacency_matrix_parallel(
        workers=db_read_workers, limit=limit
    )
    n = len(article_ids)
    
    if n == 0:
        logger.warning("No articles with links found")
        return {}, 0, 0.0
    
    # Normalize columns to get transition matrix
    # Each column should sum to 1 (outgoing links)
    col_sums = adjacency_matrix.sum(axis=0).A1  # Convert to 1D array
    col_sums[col_sums == 0] = 1  # Avoid division by zero for dangling nodes
    
    # Create transition matrix P where P[i,j] = 1/out_degree(j) if j->i
    transition_matrix = adjacency_matrix.multiply(1.0 / col_sums)
    
    # Handle dangling nodes (pages with no outgoing links)
    # They should link to all pages with equal probability
    dangling_mask = (col_sums == 1) & (adjacency_matrix.sum(axis=0).A1 == 0)
    dangling_indices = np.where(dangling_mask)[0] if np.any(dangling_mask) else np.array([], dtype=int)
    
    if len(dangling_indices) > 0:
        logger.info(f"Found {len(dangling_indices)} dangling nodes")
    
    # Initialize PageRank vector
    pagerank = np.ones(n) / n
    
    # Power iteration
    for iteration in range(max_iter):
        pagerank_old = pagerank.copy()
        
        # Standard PageRank update: pagerank = (1-damping)/n + damping * P * pagerank
        pagerank = (1 - damping) / n + damping * transition_matrix.dot(pagerank)
        
        # Handle dangling nodes using teleportation formula
        # Dangling nodes contribute their PageRank mass uniformly to all pages
        if len(dangling_indices) > 0:
            dangling_sum = damping * np.sum(pagerank[dangling_indices])
            pagerank += dangling_sum / n
        
        # Check convergence
        residual = np.linalg.norm(pagerank - pagerank_old, ord=1)
        
        if verbose and iteration % 10 == 0:
            logger.info(f"Iteration {iteration}: residual = {residual:.2e}")
        
        if residual < tol:
            logger.info(f"Converged after {iteration + 1} iterations (residual: {residual:.2e})")
            break
    else:
        logger.warning(f"Did not converge after {max_iter} iterations (residual: {residual:.2e})")
    
    # Normalize to ensure sum = 1
    pagerank = pagerank / pagerank.sum()
    
    # Convert to dictionary
    pagerank_scores = {article_ids[i]: float(pagerank[i]) for i in range(n)}
    
    logger.info(f"PageRank computation complete. Score range: [{min(pagerank):.6f}, {max(pagerank):.6f}]")
    logger.info(f"Sum of all scores: {sum(pagerank_scores.values()):.6f}")
    
    return pagerank_scores, iteration + 1, float(residual)


def get_top_pagerank_articles(limit: int = 10) -> List[Tuple[int, str, float]]:
    """Get articles with highest PageRank scores.
    
    Returns:
        List of (article_id, title, pagerank_score) tuples
    """
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT a.id, a.title, pr.score
            FROM search_engine_article a
            JOIN search_engine_pagerank pr ON a.id = pr.article_id
            ORDER BY pr.score DESC
            LIMIT %s
        """, [limit])
        return cursor.fetchall()


def get_pagerank_stats() -> Dict[str, float]:
    """Get PageRank statistics for monitoring."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT 
                COUNT(*) as total_articles,
                AVG(score) as avg_score,
                MIN(score) as min_score,
                MAX(score) as max_score,
                SUM(score) as sum_scores
            FROM search_engine_pagerank
        """)
        row = cursor.fetchone()
        
        if row and row[0] > 0:
            return {
                'total_articles': row[0],
                'avg_score': float(row[1]),
                'min_score': float(row[2]),
                'max_score': float(row[3]),
                'sum_scores': float(row[4])
            }
        else:
            return {
                'total_articles': 0,
                'avg_score': 0.0,
                'min_score': 0.0,
                'max_score': 0.0,
                'sum_scores': 0.0
            }
