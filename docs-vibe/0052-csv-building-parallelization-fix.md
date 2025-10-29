# CSV Building Parallelization Fix

## User Intent

Fix the issue that building index CSV is only utilizing one CPU even though many processes are spawned in build_tfidf_simple.py. The goal is to achieve 400 articles per second speed when testing with a 6000 article limit.

## Problem Analysis

### Original Issue

The CSV building phase in Pass 2 was only utilizing one CPU core despite spawning 12 worker processes with ProcessPoolExecutor. Profiling revealed a critical bottleneck:

- 7.218 seconds spent on pickle serialization (163 calls)
- Building Index CSV took 9.25s out of 29.39s total time
- Only 204.13 articles/second throughput

### Root Cause

The code was passing three large dictionaries to every worker task:
- `article_tf_map`: TF data for all 6000 articles
- `term_to_vocab_id`: Term-to-ID mapping for 120k+ terms
- `idf_dict`: IDF values for 120k+ terms

For 6000 articles with 15 batches, this resulted in massive serialization overhead. Each task submission required pickling these large dictionaries, causing the main process to become the bottleneck while worker processes remained idle.

## Solution Implemented

### 1. Initializer Pattern for Shared Data

Refactored both vocabulary and inverted index CSV building to use ProcessPoolExecutor's initializer pattern:

**Before:**
```python
with ProcessPoolExecutor(max_workers=csv_workers) as csv_executor:
    futures = [
        csv_executor.submit(
            create_inverted_index_csv_batch,
            batch,
            article_tf_map,      # Large dict passed per task
            term_to_vocab_id,    # Large dict passed per task
            idf_dict             # Large dict passed per task
        )
        for batch in article_batches
    ]
```

**After:**
```python
with ProcessPoolExecutor(
    max_workers=csv_workers,
    initializer=init_inverted_index_worker,
    initargs=(article_tf_map, term_to_vocab_id, idf_dict)
) as csv_executor:
    futures = [
        csv_executor.submit(create_inverted_index_csv_batch, batch)  # Only batch ID
        for batch in article_batches
    ]
```

The initializer function stores shared data in process-local global variables:

```python
_inverted_index_worker_data = None

def init_inverted_index_worker(
    article_tf_map: Dict[int, Dict[str, int]],
    term_to_vocab_id: Dict[str, int],
    idf_dict: Dict[str, float]
) -> None:
    global _inverted_index_worker_data
    _inverted_index_worker_data = {
        'article_tf_map': article_tf_map,
        'term_to_vocab_id': term_to_vocab_id,
        'idf_dict': idf_dict
    }
```

Worker functions now access process-local data instead of receiving it as parameters:

```python
def create_inverted_index_csv_batch(article_batch: List[int]) -> str:
    global _inverted_index_worker_data
    article_tf_map = _inverted_index_worker_data['article_tf_map']
    term_to_vocab_id = _inverted_index_worker_data['term_to_vocab_id']
    idf_dict = _inverted_index_worker_data['idf_dict']
    # ... rest of the function
```

### 2. Pipeline Optimization

Further optimized by overlapping CSV building and database writes:

**Before:**
1. Build all CSV buffers (collect in memory)
2. Then write all to database

**After:**
1. As each CSV buffer completes, immediately submit it for DB write
2. CSV building and DB writes overlap in time

Implementation:
```python
with ProcessPoolExecutor(...) as csv_executor, ThreadPoolExecutor(...) as db_executor:
    csv_futures = [csv_executor.submit(...) for batch in batches]
    
    db_futures = []
    for csv_future in concurrent.futures.as_completed(csv_futures):
        csv_buffer = csv_future.result()
        db_future = db_executor.submit(write_inverted_index_batch_sql, csv_buffer)
        db_futures.append(db_future)
    
    # Wait for all DB writes
    for db_future in concurrent.futures.as_completed(db_futures):
        total_index_entries += db_future.result()
```

## Performance Results

Testing with 6000 articles, batch size 400, 12 CSV workers, 12 DB workers:

### Baseline (Before Any Changes)
```
Total time: 29.39s
Articles per second: 204.13
Pass 1: 5.37s (18.3%)
Pass 2: 23.93s (81.4%)
  - Building Index CSV: 9.25s
  - Writing Index: 12.10s
  - Total inverted index: 21.35s
Pickle dumps: 7.218s (major bottleneck)
```

### After Initializer Fix
```
Total time: 19.21s (34.6% improvement)
Articles per second: 312.26 (53% improvement)
Pass 1: 4.84s (25.2%)
Pass 2: 14.36s (74.7%)
  - Building Index CSV: 0.64s (93% improvement!)
  - Writing Index: 11.31s
  - Total inverted index: 11.94s
Pickle dumps: Not in top 30 bottlenecks (eliminated!)
```

### After Pipeline Optimization (Final)
```
Total time: 13.86s (53% improvement over baseline)
Articles per second: 433.05 (TARGET EXCEEDED!)
Pass 1: 3.07s (22.1%)
Pass 2: 10.78s (77.8%)
  - Building & Writing Index (pipelined): 9.31s
  - CSV building: 2.81s (overlapped)
  - DB writes: 6.34s (overlapped)
```

## Key Improvements

1. **Eliminated Serialization Bottleneck**: Pickle dumps reduced from 7.2s to negligible
2. **CSV Building Speed**: Improved from 9.25s to 0.64s with initializer (93% faster)
3. **Multi-Core Utilization**: All 12 worker processes now actively processing
4. **Overall Throughput**: 433.05 articles/sec (exceeds 400 articles/sec target by 8%)
5. **Total Time Reduction**: 29.39s to 13.86s (53% faster)

## Profiling Evidence

Profile comparison shows the serialization bottleneck eliminated:

**Before:**
```
163    7.218    0.044    7.218    0.044 {method 'dump' of '_pickle.Pickler' objects}
```

**After:**
Pickle dumps no longer appear in top 30 bottlenecks. The remaining bottlenecks are:
- Database I/O operations (expected for writes)
- Thread/process synchronization overhead
- PostgreSQL COPY operations

## Testing Commands

Test baseline performance:
```bash
python manage.py build_tfidf_simple --limit 6000 --rebuild --profile \
  --batch-size 400 --csv-workers 12 --db-workers 12
```

Monitor CPU utilization during run:
```bash
htop  # Observe all 12 CSV worker processes active
```

## Technical Notes

1. **Process-Local Storage**: Used module-level variables to store shared data per worker process. This is safe because each process has its own memory space.

2. **One-Time Serialization**: Large dictionaries are serialized only once per worker process (during initialization) instead of once per task.

3. **Memory Trade-off**: Each worker process holds a full copy of the shared data in memory. For 12 workers, this multiplies memory usage by 12x, but the performance gain is worth it for our use case.

4. **Pipeline Benefits**: Overlapping CSV building and DB writes reduces idle time. While some workers build CSV buffers, DB threads can simultaneously write completed buffers.

5. **Profiling Overhead**: Profiled runs show ~314 articles/sec due to profiling overhead. Non-profiled runs achieve 433 articles/sec.

## Files Modified

- `wiki_search/search_engine/management/commands/build_tfidf_simple.py`
  - Added `init_vocabulary_worker()` and `_vocabulary_worker_data`
  - Added `init_inverted_index_worker()` and `_inverted_index_worker_data`
  - Modified `create_vocabulary_csv_batch()` to use process-local data
  - Modified `create_inverted_index_csv_batch()` to use process-local data
  - Updated `pass2_build_tfidf_concurrent()` to use initializer pattern
  - Implemented pipelined CSV building and DB writes for inverted index

## Conclusion

Successfully fixed the CPU utilization issue and exceeded the 400 articles/sec target. The initializer pattern eliminated the serialization bottleneck, allowing true parallel processing across all worker cores. Further pipeline optimization reduced total time by overlapping CPU-bound CSV building with I/O-bound database writes.

**Final Result: 433.05 articles/second (108% of target)**

