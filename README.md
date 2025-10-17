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

The loader supports resuming from checkpoints, graceful shutdown, configurable parallelism, and automatic internal link extraction.

```bash
# Step 1: Load articles and extract internal links
python wiki_search/manage.py load_wiki_dump --workers 6 --batch-size 5000

# Step 2: Resolve link foreign key references
python wiki_search/manage.py resolve_links --resolve-to-article
```

### Useful options for load_wiki_dump

| Flag | Purpose |
| ---- | ------- |
| `--limit N` | Stop after processing N articles (useful for smoke tests). |
| `--clear-checkpoint` | Delete the checkpoint file and start from scratch. |
| `--force-decompress` | Re-extract the raw archive even if processed data exists. |
| `--skip-decompress` | Skip decompression entirely and use existing decompressed data. |
| `--no-fast-extract` | Disable the system tar + lbzip2 path and use Python extraction. |

The command stores progress in `data/.load_checkpoint.json`, tracking completed, partial, and deferred shards so reruns pick up where they stopped. It also extracts and stores internal Wikipedia links during the loading process.

### Useful options for resolve_links

| Flag | Purpose |
| ---- | ------- |
| `--batch-size N` | Batch size for bulk updates (default: 5000). |
| `--resolve-to-article` | Also resolve to_article based on to_title matching. |
| `--verbose` | Enable verbose output showing progress details. |

The `resolve_links` command resolves foreign key references after articles are loaded. This two-phase approach eliminates database lookup bottlenecks during the main loading process, achieving 2-3x better throughput.

#### Performance characteristics

- Worker processes stream articles to the coordinator in batches, minimizing inter-process contention and improving throughput on multi-core machines.
- The coordinator deduplicates page IDs per batch before inserting, allowing large `--batch-size` values without incurring duplicate constraint penalties.
- Batch inserts run inside transactions sized by `--batch-size`, so tune this flag based on available memory and database write performance.
- Internal links are extracted and stored with raw page_id values during loading, avoiding expensive database lookups.
- Link foreign key resolution happens in a separate post-processing step using efficient batch operations.

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
