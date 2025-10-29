# QA Dataset Generation: Single-Threaded Implementation with Profiling

## User Intent

Original request: "Simplify the generate_qa_dataset.py by making it single threaded single processed. You need to profile and evaluate bottleneck during the development. The goal is to complete the script in 15 seconds. You should test the speed with 100 QA in the beginning."

Rephrased: Convert the multiprocessing-based QA dataset generation script to a single-threaded implementation with comprehensive profiling to identify performance bottlenecks and optimize toward a 15-second target for processing 100 QA entries.

## Implementation Overview

Converted `generate_qa_dataset.py` from ProcessPoolExecutor-based multiprocessing to single-threaded execution with cProfile profiling support and detailed timing instrumentation.

## Changes Made

### 1. Removed Multiprocessing Infrastructure

- Removed imports: `ProcessPoolExecutor`, `as_completed`, `cpu_count`
- Removed `--workers` CLI argument (previously defaulted to CPU count)
- Removed `process_qa_entry_worker()` function (lines 60-214 in original)
- Removed Django database connection management code (multiprocessing-specific)

### 2. Simplified Processing Logic

Replaced `process_qa_entries_parallel()` with `process_qa_entries()`:
- Uses simple for loop with tqdm progress bar
- All processing logic inlined directly in main method
- Direct Django ORM access (no connection management needed)
- Simplified error handling (no process boundary crossing)

### 3. Added Profiling Infrastructure

New imports:
```python
import cProfile
import pstats
import time
from collections import defaultdict
```

New CLI argument:
- `--profile`: Enable cProfile profiling and save results to `qa_dataset_generation.prof`

Profiling features:
- Wraps main processing in `cProfile.Profile()` context when enabled
- Saves detailed profile stats to file
- Prints top 30 time-consuming functions sorted by cumulative time
- Manual timing checkpoints for key operations

### 4. Timing Instrumentation

Added detailed timing for:
- `article_lookup`: Time to fetch supporting articles from database
- `token_counting`: Time to count tokens in supporting documents
- `search_operations`: Time for all search_hybrid calls per entry
- `entry_total`: Total time per QA entry

Timing statistics include:
- Total time across all entries
- Average time per operation
- Min/Max time for operations

### 5. Updated Default Behavior

Changed `--limit` default from None to 100 for initial testing focus.

## Code Structure

### Main Processing Flow

```
handle() -> process_qa_entries() -> _print_timing_stats() -> generate_output_files()
```

### process_qa_entries() Method

Single-threaded processing with instrumentation:

1. Initialize results dictionary and statistics counters
2. Loop through qa_data with tqdm progress bar
3. For each entry:
   - Extract basic fields (id, question, answer, supporting_facts)
   - Fetch supporting articles (timed)
   - Count tokens in supporting docs (timed)
   - Execute search queries for distractors (timed)
   - Round-robin select distractor documents up to 128k token limit
   - Calculate context size variants (8k, 32k, 128k)
   - Create _QAEntry dataclass and add to results
4. Return results and timing statistics

### Timing Statistics Output

Format:
```
Timing Statistics:
  article_lookup:
    Total: 2.45s
    Average: 24.50ms
    Min: 15.23ms
    Max: 45.67ms
  token_counting:
    Total: 5.67s
    Average: 56.70ms
    Min: 30.12ms
    Max: 120.34ms
  search_operations:
    Total: 12.34s
    Average: 123.40ms
    Min: 80.45ms
    Max: 200.56ms
  entry_total:
    Total: 20.78s
    Average: 207.80ms
    Min: 150.23ms
    Max: 350.45ms
```

## Usage

### Basic Usage (100 entries, default)
```bash
nix-shell --run "python manage.py generate_qa_dataset"
```

### With Profiling
```bash
nix-shell --run "python manage.py generate_qa_dataset --profile"
```

### Custom Limit
```bash
nix-shell --run "python manage.py generate_qa_dataset --limit 50 --profile"
```

### Analyze Profile Output
```bash
python -m pstats qa_dataset_generation.prof
# Then use interactive commands like:
# sort cumulative
# stats 50
```

## Expected Performance Bottlenecks

Based on code analysis, likely bottlenecks:

1. **Token Counting**: `count_article_tokens()` calls `tokenize_gpt()` for every paragraph
   - Each article tokenized multiple times (supporting docs, distractor docs)
   - No caching of token counts

2. **Search Operations**: `search_hybrid()` executes multiple database queries
   - Vocabulary lookup
   - Multiple InvertedIndex queries (one per query term)
   - PageRank bulk fetch
   - Article bulk fetch
   - Called once per supporting fact per entry

3. **Article Lookups**: Individual `Article.objects.get()` calls
   - N+1 query problem for supporting articles
   - Repeated queries for same articles when counting tokens

4. **Distractor Processing**: Round-robin selection with repeated token counting
   - Token counting happens twice per distractor (once to check, once to store)

## Profiling Results (10 Entries)

### Total Time: 259 seconds (25.9s per entry)
**Goal: 0.15s per entry (for 15s/100 entries) - Need 173x speedup!**

### Bottleneck Analysis

**article_lookup: 244.94s (94.5% of total time)**
- Average: 24.5 seconds per entry
- Min: 11.7s, Max: 75.1s
- **ROOT CAUSE**: N+1 query problem with `Article.objects.get(title__iexact=title)`
- Each article is queried multiple times (once for fetch, once for token counting)
- Database query execution time: 131s actual execution

**search_operations: 4.04s (1.6%)**
- Average: 404ms per entry
- Multiple `search_hybrid()` calls per entry

**token_counting: 1.39s (0.5%)**
- Average: 139ms per entry
- Repeated tokenization of same articles

### cProfile Top Functions
1. Database queries: 247s cumulative (django ORM + psycopg)
2. Token counting: 9.7s (tokenize_gpt called 31,044 times)
3. Search operations: 4.0s (search_hybrid called 25 times)

### Critical Issues

1. **Repeated Article Queries**: Same articles fetched multiple times
   - Once in supporting_docs loop
   - Again in token counting
   - Again for distractors
   - No caching whatsoever

2. **title__iexact Queries**: Case-insensitive lookups are slow
   - Each query scans database
   - No index utilization

3. **Redundant Tokenization**: Articles tokenized multiple times
   - Each paragraph tokenized separately
   - No token count caching

## Next Steps

1. **Immediate Optimizations** (Required for 15s goal):
   - Add article caching (dict lookup instead of DB query)
   - Add token count caching
   - Batch article lookups at start
   - Pre-compute article token counts

2. **Future Optimizations** (Nice to have):
   - Optimize search operations
   - Consider pre-building article index
   - Database query optimization

## Files Modified

- `wiki_search/search_engine/management/commands/generate_qa_dataset.py`: Complete rewrite to single-threaded

## Testing Commands

```bash
# Test with 100 entries and profiling
nix-shell --run "python manage.py generate_qa_dataset --limit 100 --profile"

# Test with 10 entries for quick validation
nix-shell --run "python manage.py generate_qa_dataset --limit 10 --verbose"

# Full run without profiling overhead
nix-shell --run "python manage.py generate_qa_dataset --limit 100"
```

## Performance Target

Goal: Complete 100 QA entries in under 15 seconds

Current baseline: To be measured after first profiled run.

