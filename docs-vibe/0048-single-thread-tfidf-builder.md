# Single-Thread TF-IDF Builder

## User Intent (Original Words)

"Your task is to create a single process, single threaded TF-IDF builder for the Django project. You should ignore all the previous development documents in docs-vibe. The goal of the script is to achieve a 20 article per second processing speed. You need to use NLTK for the tokenizer. You need to profile the performance and bottleneck during the development. You need to have two pass process. The first pass builds TF, and cache all the necessary thing in memory. The second part builds IDF. Each part should have their own function with modular helper functions."

Additional clarifications:
- "We are going to create inverted index. All the TF IDF vector can be stored in memory."
- "We need to save the InvertedIndex we created in the script to the database in the end."
- Model changes: Remove Redirect and TFIDFIndex models, change CharField to TextField for PostgreSQL

## Rephrased Intent

Create a simplified, single-process, single-threaded TF-IDF indexing system with the following characteristics:
- Target performance: 20 articles per second
- Two-pass architecture: Pass 1 builds TF/DF, Pass 2 builds IDF and inverted index
- Uses NLTK tokenizer for text processing
- In-memory TF-IDF vectors (not persisted to database)
- Persists Vocabulary and InvertedIndex to database
- Integrated cProfile profiling to identify bottlenecks
- Clean, modular code structure

## Architecture

### Two-Pass Processing

**Pass 1: Term Frequency and Document Frequency**
- Read all articles from database
- Tokenize paragraphs using NLTK tokenizer
- Build in-memory data structures:
  - `article_tf_map`: {article_id: {term: count}}
  - `global_df`: {term: num_docs_containing}
  - `total_docs`: int
- Cache everything for Pass 2

**Pass 2: IDF Calculation and Inverted Index**
- Calculate IDF values: log(N / df) for each term
- Save Vocabulary table with term statistics
- Compute TF-IDF scores for each article/term pair
- Build inverted index entries
- Batch save InvertedIndex to database

### Data Flow

```
Database (Articles)
    ↓
Pass 1: Tokenization & TF/DF Building
    ↓
In-Memory Cache (article_tf_map, global_df)
    ↓
Pass 2: IDF Calculation & TF-IDF Scoring
    ↓
Database (Vocabulary + InvertedIndex)
```

### Model Changes

**Removed Models:**
- `Redirect`: Not used in this implementation
- `TFIDFIndex`: Replaced by in-memory vectors + inverted index

**Modified Models:**
- Changed all `CharField` to `TextField` for better PostgreSQL compatibility
- `Vocabulary`: Stores term, document_frequency, idf_value
- `InvertedIndex`: Stores term (FK), article (FK), tf_idf_score

**Article Model:**
- No changes, reads `plain_text_paragraphs` JSONField

## Implementation Details

### File Structure

**New Command:** `wiki_search/search_engine/management/commands/build_tfidf_simple.py`
- Single file implementation (~300-400 lines)
- Modular helper functions
- Integrated profiling support

### Core Functions

1. **handle()** - Django command entry point
   - Parse command-line arguments
   - Setup logging and profiling
   - Orchestrate Pass 1 and Pass 2
   - Display final statistics

2. **pass1_build_tf_df()** - Build TF and DF structures
   - Query articles from database
   - Tokenize using NLTK
   - Build article_tf_map and global_df
   - Return Pass1Result dataclass

3. **pass2_build_tfidf()** - Build IDF and inverted index
   - Calculate IDF values
   - Create Vocabulary entries
   - Build inverted index entries
   - Batch save to database

### Helper Functions

4. **tokenize_article()** - Tokenize article paragraphs
   - Join paragraphs into text
   - Tokenize using NLTKTokenizer
   - Count term frequencies
   - Return TF dictionary

5. **compute_idf()** - Calculate IDF values
   - Apply formula: log(N / df)
   - Return IDF dictionary

6. **create_vocabulary_entries()** - Create Vocabulary objects
   - Bulk create term entries with statistics

7. **create_inverted_index_entries()** - Create InvertedIndex objects
   - Build term->article->score mappings
   - Bulk create in batches

### Data Structures

```python
@dataclass
class Pass1Result:
    article_tf_map: Dict[int, Dict[str, int]]  # article_id -> {term: count}
    global_df: Dict[str, int]  # term -> num_docs
    total_docs: int
    article_ids: List[int]
```

## Performance Optimizations

1. **Single tokenizer instance**: Reuse NLTKTokenizer to avoid reinitialization
2. **Batch database operations**: Use bulk_create() for Vocabulary and InvertedIndex
3. **In-memory processing**: Keep TF-IDF vectors in memory, avoid unnecessary DB writes
4. **Efficient data structures**: Use dictionaries for O(1) lookups
5. **NumPy operations**: Use numpy for efficient mathematical operations

## Command Interface

```bash
python manage.py build_tfidf_simple [options]
```

**Arguments:**
- `--limit N`: Process only first N articles (default: all)
- `--profile`: Enable cProfile profiling
- `--rebuild`: Clear existing Vocabulary and InvertedIndex before building
- `--batch-size N`: Database batch size for bulk operations (default: 500)
- `--verbose`: Enable verbose logging

**Example Usage:**
```bash
# Test with 300 articles
python manage.py build_tfidf_simple --limit 300

# Test with profiling
python manage.py build_tfidf_simple --limit 300 --profile

# Full rebuild with 1000 articles
python manage.py build_tfidf_simple --limit 1000 --rebuild --profile
```

## Testing Strategy

1. Start with 300 articles (target: 15 seconds = 20 articles/sec)
2. Test with 1000 articles for extended profiling
3. Monitor memory usage during processing
4. Profile with cProfile to identify bottlenecks
5. Verify database entries are correct

## Success Criteria

- [ ] Single-process, single-threaded implementation
- [ ] Two-pass architecture with modular functions
- [ ] NLTK tokenization working correctly
- [ ] Vocabulary table populated with term statistics
- [ ] InvertedIndex table populated with term-article-score mappings
- [ ] cProfile integration showing detailed performance metrics
- [ ] Target performance: 20 articles/second (300 articles in ~15 seconds)
- [ ] Clean code following development_rules.md

## Performance Results

### Test Run: 300 Articles

**Overall Performance:**
- Articles processed: 300
- Total time: 27.80s
- Articles per second: 10.79
- Target: 20 articles/second
- **Result: Target missed by 9.21 articles/second (54% of target)**

**Pass 1 Performance (TF/DF Building):**
- Time: 6.65s (23.9% of total)
- Speed: 45.78 articles/second
- Unique terms extracted: 42,975
- Average terms per article: 653.0
- **Analysis: Pass 1 exceeds target by 2.3x**

**Pass 2 Performance (IDF/Inverted Index):**
- Time: 20.92s (75.3% of total)
- Speed: 14.34 articles/second
- Vocabulary entries created: 42,975
- Inverted index entries created: 195,911
- **Analysis: Pass 2 is the bottleneck (71% below target)**

### Profiling Results (cProfile Top Functions)

**Top Time Consumers:**

1. **Database Operations (13.1s total)**
   - `bulk_create()`: 13.14s cumulative
   - PostgreSQL connection waits: 7.88s
   - Query preparation: 5.74s
   - **Finding: Database writes dominate Pass 2**

2. **Tokenization (6.4s total)**
   - NLTK `word_tokenize()`: 4.64s
   - NLTK `tokenize()` wrapper: 6.38s cumulative
   - **Finding: NLTK is efficient, appropriate for task**

3. **Model Instantiation (4.6s)**
   - Django model `__init__()`: 281,861 calls, 4.65s
   - **Finding: Creating InvertedIndex objects is expensive**

4. **SQL Compilation (5.1s)**
   - SQL `as_sql()`: 5.07s
   - **Finding: Django ORM overhead for large bulk operations**

### Bottleneck Analysis

**Primary Bottleneck: Database Writes in Pass 2**

Pass 2 spends 75.3% of total time, primarily on database operations:

1. **InvertedIndex Bulk Creation (195,911 entries)**
   - Creating 195,911 InvertedIndex objects in memory
   - Batching into 392 batches (500 entries each)
   - Each batch requires SQL compilation + network round-trip
   - Average: 20.92s / 300 articles = 69.7ms per article
   
2. **Vocabulary Bulk Creation (42,975 entries)**
   - Much faster: 3.06s for 42,975 terms
   - Created in 86 batches
   - Average: 35.7μs per term

**Secondary Bottleneck: NLTK Tokenization**

- 6.4s for 300 articles = 21.3ms per article
- Reasonable performance for linguistic tokenization
- Trade-off: accuracy vs speed (acceptable)

**Optimization Opportunities:**

1. **Reduce InvertedIndex Object Creation**
   - Current: Create 195,911 model instances in memory
   - Alternative: Use `bulk_create(batch_size=1000)` or `bulk_create(batch_size=2000)`
   - Potential gain: 20-30% faster

2. **Database Connection Pooling**
   - 7.88s spent waiting on PostgreSQL connections
   - Alternative: Optimize PostgreSQL connection settings
   - Potential gain: 15-20% faster

3. **SQL Query Optimization**
   - 5.07s in SQL compilation (as_sql)
   - Alternative: Use raw SQL INSERT or COPY for bulk operations
   - Potential gain: 30-40% faster

**Why Single-Thread Falls Short:**

The 20 articles/second target is challenging for single-thread because:
- Database I/O dominates (75% of time)
- Single thread blocks on database writes
- Cannot overlap compute (tokenization) with I/O (database writes)
- PostgreSQL can handle more concurrent writes than single thread provides

**Achieving Target Would Require:**
- Multi-threaded database writes (separate writer threads)
- Overlap Pass 1 tokenization with Pass 2 database writes
- Raw SQL bulk INSERT instead of Django ORM
- Or accept current 10.79 articles/second as baseline for single-thread simplicity

## Next Steps

1. Implement the command with modular structure
2. Test with 300 articles
3. Profile and identify bottlenecks
4. Test with 1000 articles
5. Document results and update README

