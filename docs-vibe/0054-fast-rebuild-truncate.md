# Fast rebuild for TF-IDF builder via TRUNCATE CASCADE

## User intent (original words)

Implement what is learned from clean db to speed up rebuild process in `build_tfidf_simple.py`. We will be testing the script with a 5000 article limit.

## Concise plan and implementation notes

- Replace slow ORM deletes in `--rebuild` path with PostgreSQL `TRUNCATE TABLE <tables> RESTART IDENTITY CASCADE` to instantly clear dependent tables and reset sequences.
- Run `VACUUM ANALYZE` after truncation to refresh planner stats.
- Add timing and clear logs around the truncate/vacuum operations.

## Files touched

- `wiki_search/search_engine/management/commands/build_tfidf_simple.py`

## Usage

Run a fast rebuild for a subset (example: 5000 articles):

```bash
python manage.py build_tfidf_simple \
  --limit 5000 \
  --rebuild \
  --verbose
```

## Notes

- `TRUNCATE ... CASCADE` enforces FK integrity while being much faster than row-by-row deletes.
- `RESTART IDENTITY` keeps autoincrement sequences in sync post-wipe.
- This mirrors the approach used by the `clean_db` command for maximal throughput.


