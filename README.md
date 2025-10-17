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

The loader supports resuming from checkpoints, graceful shutdown, and configurable parallelism.

```bash
# Typical ingest run with six workers
python wiki_search/manage.py load_wiki_dump --workers 6 --batch-size 5000
```

### Useful options

| Flag | Purpose |
| ---- | ------- |
| `--limit N` | Stop after processing N articles (useful for smoke tests). |
| `--clear-checkpoint` | Delete the checkpoint file and start from scratch. |
| `--force-decompress` | Re-extract the raw archive even if processed data exists. |
| `--skip-decompress` | Skip decompression entirely and use existing decompressed data. |
| `--no-fast-extract` | Disable the system tar + lbzip2 path and use Python extraction. |

The command stores progress in `data/.load_checkpoint.json`, tracking completed, partial, and deferred shards so reruns pick up where they stopped.

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
