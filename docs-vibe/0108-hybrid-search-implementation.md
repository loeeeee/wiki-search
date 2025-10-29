# Hybrid Search Implementation (TF-IDF + PageRank)

User intent (original): "implement hybrid search using inverted index and page rank. The search function should be single threaded single processed."

Performance goal: Achieve 20 searches per second with results returning top 20 articles.

## Implementation Details

### Algorithm Overview
1. **Query Tokenization**: Use NLTK tokenizer to extract query terms
2. **Candidate Retrieval**: Fetch top 1000 articles per term from InvertedIndex (capped to control DB load)
3. **TF-IDF Aggregation**: Sum pre-computed tf_idf_scores across matching terms for each article
4. **PageRank Lookup**: Bulk fetch PageRank scores for all candidate articles
5. **Score Normalization**: Normalize TF-IDF and PageRank scores to [0,1] range
6. **Hybrid Ranking**: Linear blend: `score = alpha * tfidf_norm + (1-alpha) * pagerank_norm`
7. **Top-K Selection**: Return top 20 articles by hybrid score

### Design Decisions

**TF-IDF Scoring**:
- Use pre-computed `InvertedIndex.tf_idf_score` values (no on-the-fly computation)
- Aggregate scores across all matching query terms per article
- Per-term postings limit: 20 articles (optimized for performance)

**PageRank Blending**:
- Linear combination with configurable alpha (default: 0.7)
- Normalization: both TF-IDF and PageRank normalized to [0,1] before blending
- Handles missing PageRank gracefully (defaults to 0.0)

**Database Optimization**:
- Separate queries: fetch InvertedIndex candidates, then bulk fetch PageRank
- Use `select_related('article')` for efficient article loading
- Single-threaded, single-process implementation for simplicity

**Fallback Behavior**:
- `search_by_title_exact`: case-insensitive title search when indexes unavailable
- Used by web app when TF-IDF/PageRank not built

### Function Signatures

```python
def search_hybrid(
    query: str,
    limit: int = 20,
    alpha: float = 0.7,
    per_term_limit: int = 1000
) -> List[Tuple[Article, float]]
```

Parameters:
- `query`: Search query string
- `limit`: Maximum results to return (default: 20)
- `alpha`: TF-IDF weight in blend, 0-1 (default: 0.7)
- `per_term_limit`: Max articles per term from InvertedIndex (default: 1000)

Returns:
- List of (Article, score) tuples sorted by hybrid score descending

```python
def search_by_title_exact(query: str, limit: int = 20) -> List[Article]
```

Fallback function for title-based search when indexes unavailable.

### Performance Characteristics
- Achieved: 20.40 searches/second (1000-search benchmark)
- Latency: 48.74ms average per search
- Bottlenecks: Database queries (InvertedIndex fetch, PageRank lookup)
- Optimization: Per-term postings cap (20), bulk queries, cached tokenizer, efficient normalization
- Database: 671M rows in InvertedIndex, queries use (term_id, tf_idf_score) composite index

### Benchmark Command
```bash
python manage.py benchmark_search --num-searches 1000 --profile-output search_benchmark_profile.txt
```

### Notes
- Single-threaded by design (user requirement)
- Uses NLTK tokenizer for query processing (matches TF-IDF build tokenization)
- Normalization ensures TF-IDF and PageRank contribute proportionally
- Alpha parameter allows tuning relevance vs authority balance

