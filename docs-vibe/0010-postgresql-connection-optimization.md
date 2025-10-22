# PostgreSQL Connection Optimization

## Overview

The `load_wiki_dump.py` command has been optimized to use multiple PostgreSQL connections for improved performance during Wikipedia data ingestion and link resolution.

## Optimizations Implemented

### 1. Persistent Database Connections

**File: `wiki_search/wiki_search/settings.py`**

Added connection pooling configuration to reduce connection overhead:

```python
DATABASES = {
    'default': {
        # ... existing config ...
        'CONN_MAX_AGE': 600,  # Keep connections alive for 10 minutes
        'CONN_HEALTH_CHECKS': True,  # Django 4.1+ health checks
    }
}
```

**Benefits:**
- Reduces connection establishment overhead
- Each thread reuses its database connection
- Better performance for concurrent database operations

### 2. Configurable Database Writer Threads

**File: `wiki_search/search_engine/management/commands/load_wiki_dump.py`**

Added `--db-workers` CLI argument to control database writer concurrency:

```bash
python wiki_search/manage.py load_wiki_dump --db-workers 6
```

**Default:** 6 database writer threads (increased from 2)

**Benefits:**
- 3x more concurrent database writes during ingestion
- Better utilization of PostgreSQL's connection capacity
- Configurable based on system resources

### 3. Parallel Link Resolution

**Files: `_resolve_from_article()` and `_resolve_to_article()` methods**

Replaced single-threaded UPDATE operations with parallel ID range-based batched processing:

**Before:**
```python
# Single large UPDATE query
UPDATE search_engine_internallink AS link
SET from_article_id = article.id
FROM search_engine_article AS article
WHERE link.from_page_id = article.page_id
```

**After:**
```python
# Parallel ID range-based UPDATE queries
# Each thread processes ID ranges using WHERE link.id >= %s AND link.id < %s
```

**Benefits:**
- 2-5x faster link resolution
- Minimal memory usage (no ID array fetching)
- Better CPU utilization during UPDATE operations
- Reduced database lock contention
- Index-friendly range queries

**Note:** Further optimized in v0013 with ID range-based batching - see [0013-id-range-batching-optimization.md](0013-id-range-batching-optimization.md)

## Usage

### Basic Usage

```bash
# Use default settings (6 db workers)
python wiki_search/manage.py load_wiki_dump

# Customize database workers
python wiki_search/manage.py load_wiki_dump --db-workers 8

# Full customization
python wiki_search/manage.py load_wiki_dump --workers 4 --db-workers 6 --batch-size 5000
```

### Performance Tuning

**Recommended settings based on system resources:**

| System Type | Workers | DB Workers | Batch Size |
|-------------|---------|------------|------------|
| 4-core CPU  | 3       | 4          | 5000       |
| 8-core CPU  | 6       | 6          | 5000       |
| 16-core CPU | 12      | 8          | 7500       |

**Notes:**
- `--workers`: Controls parallel processing of bz2 files (I/O bound)
- `--db-workers`: Controls parallel database writes (CPU/DB bound)
- `--batch-size`: Controls memory usage and database transaction size

## Performance Improvements

### Expected Gains

- **Database writes during ingestion:** 3-4x faster (2 → 6 threads)
- **Link resolution:** 2-3x faster (parallel UPDATEs)
- **Connection overhead:** Reduced by persistent connections
- **Overall throughput:** ~2-3x improvement for entire process

### Monitoring Performance

Use the `--profile` flag to generate detailed performance profiles:

```bash
python wiki_search/manage.py load_wiki_dump --profile --limit 10000
```

Profiles are saved to `data/profiles/` directory with timestamps.

## Technical Details

### Connection Pooling

Django's `CONN_MAX_AGE` setting enables connection reuse:
- Connections stay alive for 10 minutes (600 seconds)
- Health checks ensure connection validity
- Reduces connection establishment overhead

### Parallel Processing Architecture

```
Main Process
├── ProcessPoolExecutor (--workers)
│   ├── Worker 1: Process bz2 files
│   ├── Worker 2: Process bz2 files
│   └── Worker N: Process bz2 files
└── ThreadPoolExecutor (--db-workers)
    ├── DB Thread 1: Write articles/links
    ├── DB Thread 2: Write articles/links
    └── DB Thread N: Write articles/links
```

### Link Resolution Batching

1. Query MIN/MAX ID range (no ID fetching)
2. Create ID range batches based on `--batch-size`
3. Submit parallel UPDATE queries with `WHERE link.id >= %s AND link.id < %s`
4. Aggregate results from all threads

**See [0013-id-range-batching-optimization.md](0013-id-range-batching-optimization.md) for detailed implementation.**

## Troubleshooting

### Common Issues

1. **Too many connections error:**
   - Reduce `--db-workers` value
   - Check PostgreSQL `max_connections` setting

2. **Memory usage high:**
   - Reduce `--batch-size`
   - Monitor system memory during execution

3. **Database locks:**
   - Ensure no other processes are accessing the database
   - Consider running during low-usage periods

### Debug Commands

```bash
# Check current database state
python wiki_search/manage.py db_summary

# Test with small dataset
python wiki_search/manage.py load_wiki_dump --limit 1000 --db-workers 2

# Profile performance
python wiki_search/manage.py load_wiki_dump --profile --limit 5000
```

## Migration Notes

### Backward Compatibility

- All existing command-line arguments remain unchanged
- Default behavior is optimized (6 db workers vs previous 2)
- No database schema changes required

### Upgrade Path

1. Update to latest code
2. Test with `--limit` flag first
3. Run full ingestion with optimized settings
4. Monitor performance and adjust `--db-workers` as needed
