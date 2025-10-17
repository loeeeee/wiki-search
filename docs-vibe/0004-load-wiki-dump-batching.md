# 0004 — load_wiki_dump batching optimizations

## Summary

- Batched worker emissions into `record_batch` messages so each process ships groups of articles at a time, reducing queue contention and pickle overhead.
- Coordinator now deduplicates page IDs within a batch prior to database insertion, preventing duplicate constraint churn while retaining data integrity.
- `bulk_create` executes inside transactions sized by `--batch-size`, and inserts only the truly new records.
- Added shard key caching in the coordinator to avoid repeated path computations during progress handling.

## Implementation notes

- Workers accumulate parsed `(page_id, title, paragraphs)` tuples in memory until the batch threshold (min(`--batch-size`, 128)) is reached, then emit a single queue message.
- The main loop unpacks batches, instantiates `Article` objects, and flushes when the configured batch size is met.
- `_flush_batch` now builds a set of pre-existing page IDs and filters duplicates per batch before writing, tracking skipped counts accurately.

## Usage

```bash
uv run python wiki_search/manage.py load_wiki_dump --workers 6 --batch-size 5000
```

- Increase `--workers` to saturate CPU cores; batching ensures the coordinator keeps up with additional producers.
- Tune `--batch-size` to balance memory footprint and database throughput. Larger values amortize transaction overhead but require more RAM.
- `--limit` remains useful for smoke testing performance changes.
