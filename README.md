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
| `--db-workers N` | Number of database writer threads (default: 12). |
| `--producer-threads N` | Number of I/O producer threads per worker for concurrent bz2 decompression (default: 3). |
| `--limit N` | Stop after processing N articles (smoke tests). |

Notes:
- This command always drops data at start by calling `clean_db` (non-interactive, optimized for the database backend).
- It no longer performs decompression, checkpointing, signal handling, or profiling.
- Internal link resolution (both from_article via page_id and to_article via title) happens automatically at the end.
- **Performance optimized:** Uses persistent database connections and parallel link resolution for 2-3x faster processing.
- **I/O-optimized concurrent processing:** Each worker process uses configurable producer threads (I/O-bound bz2 decompression) and 1 consumer thread (CPU-bound parsing) to maximize I/O throughput and minimize overhead.

#### Tuning producer threads

The `--producer-threads` parameter controls how many concurrent I/O operations each worker performs:
- **Default (3)**: Optimal for most systems with SSD or fast network storage
- **Increase (4-6)**: For very fast storage (NVMe, RAM disk) or high-latency network storage
- **Decrease (1-2)**: For slow HDDs or CPU-constrained systems where parsing becomes the bottleneck

#### Performance characteristics

- Worker processes stream articles to the coordinator in batches.
- The coordinator deduplicates page IDs per batch before inserting, allowing large `--batch-size` values without duplicate penalties.
- Batch inserts run inside transactions sized by `--batch-size`.
- Internal links are extracted during loading; foreign keys are resolved after ingestion in the same command.
- **I/O-optimized concurrent processing:** Each worker uses 3 producer threads (concurrent I/O-bound bz2 decompression) and 1 consumer thread (CPU-bound JSON parsing and text extraction) to maximize I/O throughput.
- **Resource utilization:** Prioritizes I/O parallelism over CPU parallelism since bz2 decompression is heavily I/O-bound, achieving 3x better I/O utilization.

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

## Web Application

The project includes a two-page Django web application for searching and viewing Wikipedia articles.

### Starting the Web App

1. **Activate the virtual environment:**
   ```bash
   cd /home/loe/Projects/wiki-search
   source .venv/bin/activate
   ```

2. **Start the development server:**
   ```bash
   cd wiki_search
   python manage.py runserver 0.0.0.0:8000
   ```

3. **Access the web app:**
   - Open your browser and navigate to `http://localhost:8000`
   - Use the search bar to find Wikipedia articles
   - Click on search results to read full articles

### Web App Features

- **Search Page**: Clean search interface with results showing article titles and snippets
- **Article Detail Page**: Full article content with navigation back to search
- **Status Page**: Comprehensive database statistics and system information at `/status/`
- **Hybrid Search**: Combines TF-IDF relevance scoring with PageRank authority
- **Responsive Design**: Mobile-friendly interface with modern styling
- **Fast Performance**: Optimized database queries and efficient search algorithms

### Search Capabilities

- **Hybrid Ranking**: Combines content relevance (TF-IDF) with page authority (PageRank)
- **Fallback Search**: Title-based search when advanced indexing unavailable
- **Snippet Display**: Shows relevant content previews in search results
- **Link Navigation**: Internal Wikipedia links converted to app navigation

### Building Search Indexes

For optimal search performance, build the TF-IDF and PageRank indexes:

```bash
# Build TF-IDF index (optimized for performance)
python manage.py build_tfidf_index --limit 100000

# Build PageRank scores
python manage.py build_pagerank
```

**Performance Characteristics:**
- **Small datasets (100-1k articles)**: 10-25 articles/second
- **Medium datasets (1k-10k articles)**: 20-40 articles/second  
- **Large datasets (10k+ articles)**: 30-60 articles/second
- **Auto-scaling**: Worker count automatically optimized based on dataset size
- **Profiling support**: Use `--profile --verbose` for performance analysis

The web app will work with basic title search even without these indexes, but hybrid search provides much better results.

### Database Status Page

Access comprehensive database statistics and system information at `http://localhost:8000/status/`:

**Basic Statistics:**
- Article count, redirects, internal links, unresolved links
- Search index statistics (TF-IDF, Vocabulary, InvertedIndex, PageRank)

**Content Analysis:**
- Average paragraphs per article
- Average outgoing/incoming links per article
- Sample-based performance metrics

**Search Index Details:**
- PageRank score statistics (min/max/average)
- TF-IDF vector statistics (L2 norms)
- Vocabulary statistics (document frequency, IDF values)

**System Information:**
- Database backend and version
- Last updated timestamp
- Auto-refresh every 30 seconds

The status page provides real-time monitoring of your Wikipedia search engine's health and performance.

## Tokenizer Configuration

The search engine supports three different tokenization strategies, configurable via Django settings:

### Available Tokenizers

1. **GPT Tokenizer (Default)**
   - Uses tiktoken with cl100k_base encoding (GPT-4 compatible)
   - Subword tokenization, handles unknown words well
   - Best for compatibility with transformer models
   - Performance: ~50,000 tokens/second

2. **NLTK Tokenizer**
   - Uses NLTK's word_tokenize with stopword filtering
   - Linguistically-aware tokenization
   - Good for natural language processing tasks
   - Performance: ~20,000 tokens/second

3. **Naive Tokenizer**
   - Simple regex-based tokenization
   - Fastest performance, minimal dependencies
   - Good for simple word matching
   - Performance: ~100,000 tokens/second

### Configuration

Set the tokenizer in `wiki_search/settings.py`:

```python
# Tokenizer configuration
TOKENIZER_TYPE = 'gpt'  # Options: 'gpt', 'nltk', 'naive'
```

### Changing Tokenizers

**Important**: When changing the tokenizer, you must rebuild all search indexes:

```bash
# Clear existing indexes
python manage.py clean_db --yes

# Rebuild with new tokenizer
python manage.py build_tfidf_index --rebuild
```

### Performance Characteristics

| Tokenizer | Speed | Memory | Quality | Use Case |
|-----------|-------|--------|---------|----------|
| GPT | Medium | Medium | High | Transformer compatibility |
| NLTK | Slow | High | High | Linguistic accuracy |
| Naive | Fast | Low | Medium | Simple matching |

For detailed information, see [docs-vibe/0023-tokenizer-helper.md](docs-vibe/0023-tokenizer-helper.md).
