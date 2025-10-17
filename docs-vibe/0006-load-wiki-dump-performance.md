# 0006 — load_wiki_dumpp performance improvements

## Summary

- Larger worker batch emissions to reduce IPC overhead (`--worker-batch-size`, default 2048)
- Switched to SimpleQueue (or unbounded Queue fallback) for results
- Accumulate link writes in coordinator with high flush threshold
- Removed per-batch duplicate pre-check; rely on `ignore_conflicts=True`
- Enabled SQLite ingest PRAGMAs (WAL, synchronous=NORMAL, cache tuning) during this command
- Reduced checkpoint frequency to lower I/O

## Usage

```bash
python wiki_search/manage.py load_wiki_dump \
  --workers 24 \
  --batch-size 5000 \
  --worker-batch-size 2048
```

After load:
```bash
python wiki_search/manage.py resolve_links --resolve-to-article
```

## Tuning tips
- Increase `--workers` towards CPU cores; start with cores-1
- Increase `--worker-batch-size` (1024–4096) to cut queue overhead further
- Keep `--batch-size` large (5k–20k) to amortize transactions
- Ensure `lxml` and `orjson` are installed: `uv pip install .[perf]`

## Notes
- PRAGMA tuning is applied only for SQLite and only within this command
- Created/skipped counts are approximate due to conflict-ignore writes

