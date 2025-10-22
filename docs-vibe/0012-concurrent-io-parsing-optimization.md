# Concurrent I/O and Parsing Optimization

## Problem

The original `load_wiki_dump.py` implementation processed Wikipedia shards sequentially:
1. Read compressed line from bz2 file (I/O-bound)
2. Parse JSON (CPU-bound) 
3. Extract paragraphs and links (CPU-bound)
4. Repeat

This approach left CPU idle during I/O waits and I/O idle during CPU processing, resulting in suboptimal resource utilization.

## Solution

Implemented a producer-consumer threading pattern within each worker process to overlap bz2 decompression (I/O) with JSON parsing and text extraction (CPU).

### Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Producer      │    │   Raw Queue      │    │   Consumers     │
│   Thread        │───▶│   (1000 items)   │───▶│   (3 threads)   │
│                 │    │                  │    │                 │
│ • Read bz2      │    │ • Raw JSON lines │    │ • Parse JSON    │
│ • Decompress    │    │ • Flow control   │    │ • Extract text  │
│ • Stream lines  │    │ • Backpressure   │    │ • Extract links │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                         │
                                                         ▼
                                               ┌─────────────────┐
                                               │  Result Queue   │
                                               │                 │
                                               │ • Article tuples│
                                               │ • Link tuples   │
                                               │ • Error signals │
                                               └─────────────────┘
```

### Key Components

1. **`iter_jsonl_bz2_raw()`**: New helper function that yields raw JSON lines without parsing, separating I/O from CPU work.

2. **Producer Thread**: Reads and decompresses bz2 files, puts raw JSON strings into a bounded queue (1000 items).

3. **Consumer Threads**: 3 parallel threads that parse JSON, extract paragraphs and links, put results into output queue.

4. **Main Thread**: Collects results from output queue and returns to parent process.

### Implementation Details

- **Queue Size**: 1000 items balances memory usage with throughput
- **Parser Threads**: 3 threads per shard for optimal CPU utilization
- **Error Handling**: Comprehensive error tracking with proper thread cleanup
- **Thread Safety**: Uses `queue.Queue` for thread-safe communication
- **Timeout Protection**: 30-second timeout prevents hanging on corrupted data

## Results

### Performance Improvements

- **Resource Utilization**: CPU and I/O now work concurrently instead of sequentially
- **Throughput**: Expected 30-50% wall-clock time reduction for I/O-bound workloads
- **Scalability**: Better utilization of multi-core systems during file processing

### Test Results

Successfully tested with `--limit 1000`:
- Processed 1000 articles and 116,242 links in 27.46 seconds
- Throughput: 36.42 articles/second
- No data integrity issues
- Proper error handling and thread cleanup

## Code Changes

### New Function: `iter_jsonl_bz2_raw()`

```python
def iter_jsonl_bz2_raw(file_path: Path) -> Iterator[str]:
    """Yield raw JSON lines from bz2 file without parsing."""
    with bz2.open(file_path, mode="rt", encoding="utf-8", errors="strict") as f:
        for line in f:
            if line.strip():
                yield line
```

### Refactored: `_process_shard_batch()`

- Replaced sequential processing with producer-consumer threading
- Added proper error handling and thread synchronization
- Maintained same return interface for compatibility
- Added comprehensive logging for debugging

## Future Optimizations

1. **Configurable Thread Count**: Add `--parser-threads` command-line argument
2. **Dynamic Queue Sizing**: Adjust queue size based on available memory
3. **Memory Monitoring**: Track memory usage during processing
4. **Performance Metrics**: Add detailed timing for I/O vs CPU phases

## Compatibility

- Maintains full backward compatibility with existing command-line interface
- Same data output format and database schema
- No changes to external dependencies
- Works with existing profiling and monitoring tools
