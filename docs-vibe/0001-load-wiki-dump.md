# Load Wikipedia dump

This command loads the HotpotQA 2017 Wikipedia dump into the SQLite database.

## Paths

- Archive: `data/raw/enwiki-20171001-pages-meta-current-withlinks-processed.tar.bz2`
- Processed dir: `data/processed/enwiki-20171001-pages-meta-current-withlinks`

## Usage

```bash
python wiki_search/manage.py load_wiki_dump --limit 10000
```

Options:

- `--archive PATH` override archive
- `--processed-dir PATH` override processed root
- `--batch-size N` default 1000
- `--limit N` stop after N records
- `--force-decompress` re-extract even if folder exists
- `--skip-decompress` skip extraction step

## Database summary

To summarize current DB contents:

```bash
python wiki_search/manage.py db_summary
```


