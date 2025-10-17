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

```bash
python wiki_search/manage.py load_wiki_dump --limit 10000
```

## Summarize database

```bash
python wiki_search/manage.py db_summary
```

To monitor loading progress, one can

```bash
watch --interval python wiki_search/manage.py db_summary
```

## Random Article

```bash
python wiki_search/manage.py random_articles --max-paragraphs 5
```