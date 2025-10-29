# QA Dataset Generation: Single-Threaded Implementation Report

## Summary

Successfully converted `generate_qa_dataset.py` from multiprocessing to single-threaded implementation with comprehensive profiling support.

## Changes Implemented

### 1. Code Simplification
- Removed all multiprocessing infrastructure (ProcessPoolExecutor, worker functions)
- Removed `--workers` CLI argument
- Inlined all processing logic into single-threaded loop
- Simplified error handling (no process boundaries)
- **Lines of code**: Reduced from 408 to 391 lines (4% reduction)

### 2. Profiling Infrastructure Added
- Added cProfile support with `--profile` flag
- Manual timing instrumentation for key operations:
  - Article lookups
  - Token counting
  - Search operations
  - Per-entry total time
- Profile output saved to `qa_dataset_generation.prof`
- Top 30 functions report with cumulative time sorting

### 3. CLI Changes
- Changed `--limit` default from None to 100 (for testing focus)
- Added `--profile` flag for profiling
- Removed `--workers` argument (no longer needed)
- Kept all other options unchanged

## Profiling Results (10 Entries Baseline)

### Performance Metrics
- **Total time**: 259.3 seconds
- **Average per entry**: 25.9 seconds
- **Target**: 0.15 seconds per entry (for 15s/100 entries goal)
- **Required speedup**: 173x

### Bottleneck Analysis

| Operation | Time (s) | % of Total | Avg per Entry (ms) |
|-----------|----------|------------|-------------------|
| Article lookups | 244.94 | 94.5% | 24,494 |
| Search operations | 4.04 | 1.6% | 404 |
| Token counting | 1.39 | 0.5% | 139 |
| Other | 8.93 | 3.4% | 893 |

### Root Cause: N+1 Query Problem

**Primary Issue**: `Article.objects.get(title__iexact=title)` called repeatedly
- Each supporting article queried 2-3 times:
  1. Initial fetch for supporting_docs
  2. Again for token counting
  3. Again for distractor token counting
- No caching mechanism
- Database execution time: 131s (51% of total)

**Secondary Issues**:
- `title__iexact` queries are slow (case-insensitive, no index optimization)
- Repeated tokenization of same articles
- No pre-computation or batching

## cProfile Top Functions

```
1. Django ORM queries: 247s cumulative
2. tokenize_gpt: 9.7s (31,044 calls)
3. search_hybrid: 4.0s (25 calls)
```

## Recommended Optimizations

### Critical (Required for 15s goal):
1. **Article Caching**: Pre-fetch all articles, store in dict by title
   - Eliminates 260 database queries per 10 entries
   - Expected speedup: 50-100x on article lookups
2. **Token Count Caching**: Cache article token counts
   - Avoids re-tokenizing same articles
   - Expected speedup: 5-10x on token counting
3. **Batch Queries**: Collect all needed titles, fetch in bulk
   - Single query instead of N queries
   - Expected speedup: 20-50x

### Optional (Nice to have):
- Pre-build title index with case-insensitive lookup
- Optimize search_hybrid for batch queries
- Consider materialized view for article token counts

## Files Modified

1. `wiki_search/search_engine/management/commands/generate_qa_dataset.py`
   - Complete rewrite (391 lines)
   - Single-threaded with profiling support

2. `docs-vibe/0114-qa-dataset-single-threaded.md`
   - Created: Implementation documentation with profiling results

3. `README.md`
   - Updated: Command usage examples
   - Updated: Performance metrics
   - Added: Profiling instructions

## Usage

### Test with profiling (10 entries):
```bash
cd /home/loe/Projects/wiki-search
nix-shell --run "cd wiki_search && python manage.py generate_qa_dataset \
  --input ../data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir ../data/processed \
  --limit 10 \
  --profile"
```

### Analyze profile:
```bash
python -m pstats qa_dataset_generation.prof
# In pstats shell:
> sort cumulative
> stats 30
```

## Output Files Generated

- `qa_dataset_8000.json`: 10 entries (≤8k tokens context)
- `qa_dataset_32000.json`: 10 entries (≤32k tokens context)
- `qa_dataset_128000.json`: 10 entries (≤128k tokens context)
- `qa_dataset_generation.prof`: cProfile data
- `qa_dataset_10_entries.log`: Execution log

## Next Steps

To achieve 15-second goal for 100 entries:
1. Implement article caching (highest priority)
2. Implement token count caching (high priority)
3. Test with 100 entries to verify speedup
4. Iterate on remaining bottlenecks if needed

## Execution Time

- Implementation: ~30 minutes
- Testing (10 entries): ~4.5 minutes
- Documentation: Complete
- Total: ~35 minutes

