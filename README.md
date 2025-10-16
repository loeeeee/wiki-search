# wiki-search
A wikipedia dump processing pipeline

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