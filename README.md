# wiki-search
A wikipedia dump processing pipeline

## Count Articles

Count the number of articles in the Wikipedia dump:

```bash
# Quick estimate (recommended)
python wiki_search/manage.py count_articles --estimate

# Sample specific number of files
python wiki_search/manage.py count_articles --sample 100

# Full count (takes hours)
python wiki_search/manage.py count_articles

# Verbose output
python wiki_search/manage.py count_articles --estimate --verbose
```

Based on sampling, the dump contains approximately **5,357,970 articles** across 15,517 files.

## Load data

The loader is now a single-step command that wipes the DB, ingests the pre-decompressed dump, extracts internal links, and resolves link foreign keys.

Requirements:
- The HotpotQA 2017 dump must be pre-decompressed into `data/processed/enwiki-20171001-pages-meta-current-withlinks-processed/` (decompression is handled by a separate script).

```bash
# One-step load + link resolution
python wiki_search/manage.py load_wiki_dump --workers 6 --batch-size 5000

# Optional: process only a subset
python wiki_search/manage.py load_wiki_dump --limit 200000
```

### Options for load_wiki_dump

| Flag | Purpose |
| ---- | ------- |
| `--processed-dir PATH` | Root of pre-decompressed shards (default path under data/processed). |
| `--batch-size N` | DB flush size for articles (default: 5000). |
| `--workers N` | Number of worker processes (default: CPU-1). |
| `--limit N` | Stop after processing N articles (smoke tests). |

Notes:
- This command always drops data at start by calling `clean_db` (non-interactive, fastest drop+recreate on SQLite).
- It no longer performs decompression, checkpointing, signal handling, or profiling.
- Internal link resolution (both from_article via page_id and to_article via title) happens automatically at the end.

#### Performance characteristics

- Worker processes stream articles to the coordinator in batches.
- The coordinator deduplicates page IDs per batch before inserting, allowing large `--batch-size` values without duplicate penalties.
- Batch inserts run inside transactions sized by `--batch-size`.
- Internal links are extracted during loading; foreign keys are resolved after ingestion in the same command.

## Summarize database

```bash
python wiki_search/manage.py db_summary
```

To monitor loading progress continuously:

```bash
watch --interval 30 python wiki_search/manage.py db_summary
```

## Random Article

```bash
python wiki_search/manage.py random_articles --max-paragraphs 5
```

## Database cleanup

Fastly purge all search_engine tables and reclaim space:

```bash
# Default (fast pragmas enabled), non-interactive
python wiki_search/manage.py clean_db --yes

# Suppress progress/count queries
python wiki_search/manage.py clean_db --yes --no-progress

# Disable fast SQLite pragmas (safer but slower)
python wiki_search/manage.py clean_db --yes --no-fast-pragmas

# Absolute fastest (SQLite only): drop + recreate tables
python wiki_search/manage.py clean_db --yes --drop-recreate
```

Notes:
- The command deletes `InternalLink`, `Redirect`, `TFIDFIndex`, `Vocabulary`, then `Article`, then runs `VACUUM`.
- With SQLite, fast PRAGMAs are applied by default for speed and restored afterward. Use `--no-fast-pragmas` to disable.
- `--drop-recreate` is destructive but typically the fastest option for very large datasets.
