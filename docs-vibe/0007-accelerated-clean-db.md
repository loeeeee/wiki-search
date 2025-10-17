# Accelerated clean_db

This update makes `clean_db` dramatically faster on SQLite by replacing chunked subselect deletes with bulk deletes (or optional drop+recreate), and by applying fast PRAGMAs during the operation.

## Key changes
- Single-transaction bulk deletes in dependency order: `InternalLink`, `Redirect`, `TFIDFIndex`, `Vocabulary`, `Article`.
- Optional drop+recreate path for SQLite (`--drop-recreate`).
- Fast PRAGMAs during cleanup (restored afterward): `foreign_keys=OFF`, `locking_mode=EXCLUSIVE`, `journal_mode=OFF|DELETE`, `synchronous=OFF`, `temp_store=MEMORY`, `cache_size=-200000`, `mmap_size`.
- VACUUM at the end to reclaim space.
- CLI flags: `--no-progress`, `--no-fast-pragmas`, `--drop-recreate`.

## Usage
```bash
# Default (fast) and non-interactive
python wiki_search/manage.py clean_db --yes

# Suppress COUNT(*)/progress bars
python wiki_search/manage.py clean_db --yes --no-progress

# Disable fast PRAGMAs
python wiki_search/manage.py clean_db --yes --no-fast-pragmas

# Fastest (SQLite only): drop and recreate tables
python wiki_search/manage.py clean_db --yes --drop-recreate
```

## Expected performance
For ~8M rows on a modern SSD, runtime should drop from hours to minutes. Exact numbers depend on CPU, storage, and whether `--drop-recreate` is used.

## Safety notes
- Only use `--drop-recreate` if no other connections are using the DB and you accept full table rebuild.
- PRAGMAs are restored after cleanup. If the process crashes, you may need to reopen the DB to re-establish defaults.

