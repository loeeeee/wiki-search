# wiki-search
A wikipedia dump processing pipeline

## Database Setup

This project uses PostgreSQL as the database backend, connecting to a server at `172.22.0.133`.

### Environment Configuration

1. **Copy the environment template:**
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` with your actual PostgreSQL credentials:**
   ```bash
   POSTGRES_DB=wiki_search
   POSTGRES_USER=your_actual_username
   POSTGRES_PASSWORD=your_actual_password
   POSTGRES_HOST=172.22.0.133
   POSTGRES_PORT=5432
   ```

3. **Load environment variables before running Django commands:**
   ```bash
   set -a; source .env; set +a
   ```

4. **Install dependencies and run migrations:**
   ```bash
   uv sync
   python wiki_search/manage.py migrate
   ```

5. **Test the database connection:**
   ```bash
   python wiki_search/manage.py db_summary
   ```

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
# One-step load + link resolution (optimized with 6 database workers)
python wiki_search/manage.py load_wiki_dump --workers 6 --db-workers 6 --batch-size 5000

# Optional: process only a subset
python wiki_search/manage.py load_wiki_dump --limit 200000

# Performance tuning for different systems
python wiki_search/manage.py load_wiki_dump --workers 4 --db-workers 4  # 4-core system
python wiki_search/manage.py load_wiki_dump --workers 8 --db-workers 8  # 8-core system
```

### Options for load_wiki_dump

| Flag | Purpose |
| ---- | ------- |
| `--processed-dir PATH` | Root of pre-decompressed shards (default path under data/processed). |
| `--batch-size N` | DB flush size for articles (default: 5000). |
| `--workers N` | Number of worker processes (default: CPU-1). |
| `--db-workers N` | Number of database writer threads (default: 6). |
| `--limit N` | Stop after processing N articles (smoke tests). |

Notes:
- This command always drops data at start by calling `clean_db` (non-interactive, optimized for the database backend).
- It no longer performs decompression, checkpointing, signal handling, or profiling.
- Internal link resolution (both from_article via page_id and to_article via title) happens automatically at the end.
- **Performance optimized:** Uses persistent database connections and parallel link resolution for 2-3x faster processing.

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
- The command deletes `InternalLink`, `Redirect`, `TFIDFIndex`, `Vocabulary`, then `Article`, then optimizes the database.
- With SQLite, fast PRAGMAs are applied by default for speed and restored afterward. Use `--no-fast-pragmas` to disable.
- With PostgreSQL, `VACUUM ANALYZE` is run to optimize the database.
- `--drop-recreate` is SQLite-only and destructive but typically the fastest option for very large datasets.
