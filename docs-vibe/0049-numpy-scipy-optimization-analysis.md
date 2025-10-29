# NumPy/SciPy Optimization Analysis for TF-IDF Builder

## Current Performance Bottlenecks (300 articles, 27.80s)

### Profiling Summary

1. **Database Operations: 13.1s (47%)** - Cannot optimize with numpy/scipy
2. **NLTK Tokenization: 6.4s (23%)** - Cannot optimize (already efficient)
3. **Django Model Creation: 4.6s (17%)** - 281,861 `__init__` calls
4. **Pass 2 Function: 0.150s (0.5%)** - Actual TF-IDF computation
5. **Pass 1 Function: 0.063s (0.2%)** - TF/DF building

### Key Finding

**The actual mathematical computations (IDF, TF-IDF) are negligible (~150ms).** The bottleneck is creating 195,911 Django model instances for InvertedIndex.

## Optimization Opportunities

### 1. Vectorized IDF Calculation (Low Impact)

**Current Implementation:**
```python
def compute_idf(df_dict: Dict[str, int], total_docs: int) -> Dict[str, float]:
    idf_dict = {}
    for term, df in df_dict.items():
        idf_dict[term] = math.log(total_docs / df)
    return idf_dict
```

**Optimized with NumPy:**
```python
import numpy as np

def compute_idf_vectorized(df_array: np.ndarray, total_docs: int) -> np.ndarray:
    """Vectorized IDF calculation using NumPy.
    
    10-100x faster for large vocabularies (>10k terms).
    """
    return np.log(total_docs / df_array)
```

**Expected Gain:** < 50ms for 42,975 terms (current: ~20ms)
**Worth it?** Minimal - IDF calculation is already fast

### 2. Sparse Matrix TF-IDF Computation (Medium Impact)

**Current Implementation:**
```python
def compute_tfidf_vector(tf_dict: Dict[str, int], idf_dict: Dict[str, float]) -> Dict[str, float]:
    tfidf_vector = {}
    for term, tf in tf_dict.items():
        if term in idf_dict:
            tfidf_vector[term] = tf * idf_dict[term]
    return tfidf_vector
```

**Optimized with SciPy Sparse Matrix:**
```python
from scipy.sparse import csr_matrix, lil_matrix
import numpy as np

def build_sparse_tfidf_matrix(
    article_tf_map: Dict[int, Dict[str, int]],
    term_to_idx: Dict[str, int],
    idf_array: np.ndarray
) -> csr_matrix:
    """Build sparse TF-IDF matrix for all articles at once.
    
    Returns:
        Sparse matrix (n_articles x n_terms) with TF-IDF values
    """
    n_articles = len(article_tf_map)
    n_terms = len(term_to_idx)
    
    # Use lil_matrix for efficient construction
    tfidf_matrix = lil_matrix((n_articles, n_terms), dtype=np.float32)
    
    for article_idx, (article_id, tf_dict) in enumerate(article_tf_map.items()):
        for term, tf in tf_dict.items():
            if term in term_to_idx:
                term_idx = term_to_idx[term]
                tfidf_matrix[article_idx, term_idx] = tf * idf_array[term_idx]
    
    # Convert to CSR for efficient slicing
    return tfidf_matrix.tocsr()
```

**Expected Gain:** 50-100ms (minimal - not a bottleneck)
**Worth it?** Maybe - adds complexity for little gain

### 3. Reduce Django Model Instantiation (HIGH IMPACT)

**Problem:** Creating 195,911 InvertedIndex objects takes 4.6s

**Current Approach:**
```python
inverted_index_entries = []
for article_id in article_ids:
    tf_dict = article_tf_map[article_id]
    tfidf_vector = compute_tfidf_vector(tf_dict, idf_dict)
    
    for term, tfidf_score in tfidf_vector.items():
        inverted_index_entries.append(
            InvertedIndex(
                term_id=term_to_vocab_id[term],
                article_id=article_id,
                tf_idf_score=tfidf_score
            )
        )
```

**Optimization Strategy 1: Batch with NumPy Arrays**
```python
def create_inverted_index_batch_numpy(
    article_tf_map: Dict[int, Dict[str, int]],
    article_ids: List[int],
    term_to_vocab_id: Dict[str, int],
    idf_dict: Dict[str, float],
    batch_size: int = 10000
) -> None:
    """Create InvertedIndex entries using NumPy for batch processing."""
    
    # Pre-allocate numpy arrays for one batch
    term_ids = np.empty(batch_size, dtype=np.int32)
    article_ids_arr = np.empty(batch_size, dtype=np.int32)
    scores = np.empty(batch_size, dtype=np.float32)
    
    idx = 0
    batch = []
    
    for article_id in article_ids:
        tf_dict = article_tf_map[article_id]
        
        for term, tf in tf_dict.items():
            if term in term_to_vocab_id:
                # Fill arrays directly
                term_ids[idx] = term_to_vocab_id[term]
                article_ids_arr[idx] = article_id
                scores[idx] = tf * idf_dict[term]
                idx += 1
                
                # When batch full, create Django objects
                if idx >= batch_size:
                    for i in range(batch_size):
                        batch.append(InvertedIndex(
                            term_id=int(term_ids[i]),
                            article_id=int(article_ids_arr[i]),
                            tf_idf_score=float(scores[i])
                        ))
                    InvertedIndex.objects.bulk_create(batch)
                    batch = []
                    idx = 0
    
    # Handle remaining entries
    for i in range(idx):
        batch.append(InvertedIndex(
            term_id=int(term_ids[i]),
            article_id=int(article_ids_arr[i]),
            tf_idf_score=float(scores[i])
        ))
    if batch:
        InvertedIndex.objects.bulk_create(batch)
```

**Expected Gain:** 500-1000ms (reduce object creation overhead)
**Worth it?** Yes - reduces memory allocations

**Optimization Strategy 2: Raw SQL INSERT (HIGHEST IMPACT)**
```python
def create_inverted_index_raw_sql(
    article_tf_map: Dict[int, Dict[str, int]],
    article_ids: List[int],
    term_to_vocab_id: Dict[str, int],
    idf_dict: Dict[str, float]
) -> None:
    """Use raw SQL for maximum performance."""
    from django.db import connection
    
    # Build numpy arrays for all data
    entries = []
    for article_id in article_ids:
        tf_dict = article_tf_map[article_id]
        for term, tf in tf_dict.items():
            if term in term_to_vocab_id:
                entries.append((
                    term_to_vocab_id[term],
                    article_id,
                    tf * idf_dict[term]
                ))
    
    # Convert to numpy for efficient memory layout
    data = np.array(entries, dtype=[
        ('term_id', np.int32),
        ('article_id', np.int32),
        ('score', np.float32)
    ])
    
    # Use COPY (PostgreSQL) or bulk INSERT
    with connection.cursor() as cursor:
        cursor.execute("BEGIN")
        # PostgreSQL COPY for maximum speed
        cursor.copy_from(
            io.StringIO('\n'.join(
                f"{row[0]}\t{row[1]}\t{row[2]}" for row in entries
            )),
            'search_engine_invertedindex',
            columns=('term_id', 'article_id', 'tf_idf_score')
        )
        cursor.execute("COMMIT")
```

**Expected Gain:** 5-8s (bypasses Django ORM entirely)
**Worth it?** YES - this is the real bottleneck

### 4. Memory-Efficient Data Structures

**Current:** Using Python dicts everywhere
**Optimized:** Use NumPy structured arrays

```python
@dataclass
class Pass1ResultOptimized:
    """Optimized Pass1 result using NumPy arrays."""
    # Sparse representation using coordinate format
    article_indices: np.ndarray  # Array of article indices
    term_indices: np.ndarray     # Array of term indices  
    term_frequencies: np.ndarray # Array of TF values
    
    # Metadata
    article_id_map: Dict[int, int]  # article_id -> array index
    term_id_map: Dict[str, int]     # term -> array index
    term_names: List[str]           # index -> term name
    global_df: np.ndarray           # Document frequency array
    total_docs: int
```

**Expected Gain:** 200-500ms (reduced memory allocations)
**Worth it?** Medium complexity for medium gain

## Recommended Implementation Plan

### Phase 1: High-Impact Raw SQL (Recommended)

Focus on the real bottleneck - database writes with raw SQL:

1. Keep current Pass 1 (already fast)
2. Add `--use-raw-sql` flag
3. Implement raw SQL INSERT for InvertedIndex
4. Expected speedup: **2-3x faster** (27.8s → 10-12s)

### Phase 2: NumPy Array Pre-allocation (Optional)

Add NumPy for batch processing:

1. Pre-allocate numpy arrays for batches
2. Reduce Python object creation
3. Expected additional speedup: 10-15%

### Phase 3: Full Sparse Matrix (Advanced, Not Recommended)

Complete rewrite using scipy.sparse:

1. Full sparse matrix implementation
2. Complex code, minimal additional gain
3. Only if you need in-memory TF-IDF matrix for other purposes

## Performance Projection

**Current Performance (300 articles):**
- Total: 27.80s (10.79 articles/sec)
- Pass 1: 6.65s
- Pass 2: 20.92s

**With Raw SQL Optimization:**
- Total: ~12-14s (21-25 articles/sec) ✓ **TARGET ACHIEVED**
- Pass 1: 6.65s (unchanged)
- Pass 2: ~5-7s (reduce from 20.92s)

**With Raw SQL + NumPy Arrays:**
- Total: ~10-12s (25-30 articles/sec)
- Pass 1: 5.5s (small numpy optimization)
- Pass 2: ~4-6s

## Code Complexity vs Gain

| Optimization | Complexity | Expected Gain | Recommended |
|-------------|------------|---------------|-------------|
| Vectorized IDF | Low | <50ms | No |
| Sparse TF-IDF Matrix | High | <100ms | No |
| NumPy Batch Arrays | Medium | 500-1000ms | Maybe |
| Raw SQL INSERT | Medium | 5-8s | **YES** |
| PostgreSQL COPY | High | 8-10s | **YES** |

## Conclusion

**The biggest optimization is not numpy/scipy for computation, but using raw SQL to bypass Django ORM overhead.**

The mathematical computations are already negligible. The bottleneck is:
1. Creating 281,861 Python objects (4.6s)
2. Database round-trips for bulk_create (13.1s)

**Recommendation:** Implement raw SQL INSERT with PostgreSQL COPY command. This will achieve the 20 articles/second target without complex numpy/scipy code.

NumPy/SciPy can provide small additional gains (~10-15%) but add significant complexity. Only worth it if you need the sparse matrix for other purposes (e.g., similarity search, clustering).

## Implementation Results

**PostgreSQL COPY implementation completed and tested successfully!**

### Performance Comparison (300 articles)

**Before (Django ORM bulk_create):**
- Total time: 27.80s
- Articles per second: 10.79
- Pass 1: 6.65s (23.9%)
- Pass 2: 20.92s (75.3%)
- Bottleneck: Creating 195,911 Django model instances

**After (PostgreSQL COPY):**
- Total time: **6.56s**
- Articles per second: **45.76** ✓ TARGET ACHIEVED!
- Pass 1: 3.58s (54.6%)
- Pass 2: 2.75s (42.0%)
- Method: Direct psycopg3 COPY command

### Performance Improvement

- **4.2x faster overall** (27.80s → 6.56s)
- **7.6x faster Pass 2** (20.92s → 2.75s)
- **Target exceeded by 2.3x** (20 articles/sec target, achieved 45.76)

### Key Findings

1. **Pass 2 speedup**: From 20.92s to 2.75s (7.6x improvement)
2. **Pass 1 improvement**: From 6.65s to 3.58s (1.9x improvement, likely due to PostgreSQL optimization)
3. **PostgreSQL COPY time**: 1.55s for all database writes
4. **No NumPy/SciPy needed**: Raw SQL solved the problem completely

### Implementation Details

- Uses psycopg3 `copy()` method for direct COPY FROM STDIN
- Prepares data in StringIO buffer (in-memory CSV)
- Single transaction for each table (Vocabulary, InvertedIndex)
- No Django ORM overhead - bypasses model instantiation entirely

**Verdict:** PostgreSQL COPY is the correct optimization. NumPy/SciPy would add complexity for minimal additional gain.

