# wiki-search
A wikipedia dump processing pipeline

**SOFTWARE DEFINED DATA**

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

### Separate Link Resolution

Link resolution can now be run independently using the `resolve_links` command with **merged optimization**:

```bash
# Run link resolution separately (useful for re-processing links)
python wiki_search/manage.py resolve_links --batch-size 5000 --db-workers 96

# Run with custom settings
python wiki_search/manage.py resolve_links --batch-size 10000 --db-workers 48

# Rebuild all link resolutions from scratch
python wiki_search/manage.py resolve_links --rebuild --batch-size 5000 --db-workers 96
```

**Performance Optimization**: The link resolution now uses a merged approach that resolves both `from_article` and `to_article` foreign keys in a single database pass, providing:
- 50% reduction in database queries
- Single progress bar for the entire operation
- Better query optimization by the database engine
- Reduced I/O overhead

**Rebuild Option**: The `--rebuild` flag clears all existing link resolutions and rebuilds them from scratch:
- Clears `from_article` and `to_article` foreign keys for all links in batched manner
- Re-resolves all links using the existing optimization
- Useful for fixing corrupted link data or after schema changes

This is useful when:
- Re-running link resolution without reloading articles
- Debugging link resolution issues
- Optimizing link resolution performance independently
- Fixing corrupted link relationships with `--rebuild`

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
# Build TF-IDF index with token counts (optimized for performance)
python manage.py build_tfidf_index --limit 100000

# Build PageRank scores (optimized for performance)
python manage.py build_pagerank --rebuild --profile --verbose
```

**Performance Characteristics:**
- **Small datasets (100-1k articles)**: 10-25 articles/second
- **Medium datasets (1k-10k articles)**: 20-40 articles/second  
- **Large datasets (10k+ articles)**: 30-60 articles/second
- **Auto-scaling**: Worker count automatically optimized based on dataset size
- **Profiling support**: Use `--profile --verbose` for performance analysis

**Token Counting Integration:**
- The TF-IDF build process now automatically computes token counts for each paragraph
- Token counts are stored in the `paragraph_token_counts` field as a parallel array to `plain_text_paragraphs`
- Uses the same tokenizer as the search engine (configurable via `TOKENIZER_TYPE` setting)
- No additional processing time - computed during existing tokenization pass
- Enables paragraph-level analysis and search result ranking by content length

The web app will work with basic title search even without these indexes, but hybrid search provides much better results.

### PageRank Build Optimization

The PageRank build process has been optimized for high performance with parallel database operations:

```bash
# Standard PageRank build (parallel database operations)
python manage.py build_pagerank

# With custom parallel workers
python manage.py build_pagerank --db-read-workers 4 --db-write-workers 4

# With profiling and verbose output
python manage.py build_pagerank --rebuild --profile --verbose

# Performance monitoring
python manage.py build_pagerank --profile --verbose 2>&1 | grep "Memory usage"
```

**New Parallel Optimization Features:**
- **Parallel Graph Loading**: ID range-based batching with ThreadPoolExecutor (2-4x speedup)
- **Parallel Storage**: Multi-threaded PostgreSQL COPY operations (2-3x speedup)
- **Connection Management**: Each thread gets its own database connection
- **Index Optimization**: Drop indexes before writes, rebuild after
- **Auto-scaling**: Smart worker count selection based on dataset size

**Legacy Optimization Features:**
- **PostgreSQL COPY**: 3-5x faster storage than ORM bulk_create
- **Raw SQL DELETE**: 10x+ faster deletion than ORM batching
- **Memory efficient**: 50-70% memory reduction by avoiding Article object loading
- **Comprehensive profiling**: Phase timing, memory tracking, and cProfile integration

**Performance Characteristics:**
- **Small datasets (1k articles)**: 1.7-2x speedup over baseline
- **Medium datasets (10k articles)**: 2-2.5x speedup over baseline
- **Large datasets (100k+ articles)**: 2.5-3.5x speedup over baseline
- **Memory usage**: Scales linearly with dataset size
- **Database load**: Optimized with parallel operations

**CLI Options:**
- `--db-read-workers N`: Number of parallel workers for reading links (default: 4)
- `--db-write-workers N`: Number of parallel workers for writing scores (default: 4)
- `--batch-size N`: Batch size for database operations (default: 1000)
- `--limit N`: Limit number of links to process (for testing)

For detailed optimization information, see [docs-vibe/0027-pagerank-optimization.md](docs-vibe/0027-pagerank-optimization.md) and [docs-vibe/0029-multiprocessing-pagerank-feasibility.md](docs-vibe/0029-multiprocessing-pagerank-feasibility.md).

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

## QA Dataset Generation

Generate a question-answering dataset for LLM training from HotpotQA data with supporting and distractor documents.

### Generate QA Dataset

Create a QA dataset with multiple context sizes (8k, 32k, 128k tokens). Uses ProcessPoolExecutor for optimal CPU utilization:

```bash
# Test on toy dataset first
python manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1_toy.json \
  --output-dir data/processed \
  --context-sizes 8000 32000 128000 \
  --verbose

# Process full dataset (when available)
python manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --context-sizes 8000 32000 128000 \
  --workers 8 \
  --limit 1000
```

### Output Files

The command generates three JSON files in `data/processed/`:
- `qa_dataset_8000.json` - entries with context_size ≤ 8k tokens
- `qa_dataset_32000.json` - entries with context_size ≤ 32k tokens  
- `qa_dataset_128000.json` - entries with context_size ≤ 128k tokens

### Output Schema

Each entry follows this schema:
```json
{
  "id": "string",
  "question": "string", 
  "gold_answer": "string",
  "supporting_docs": [{"title": "string", "text": "string"}, ...],
  "distractor_docs": [{"title": "string", "text": "string"}, ...],
  "context_size": int
}
```

### Command Options

| Flag | Purpose |
|------|---------|
| `--input PATH` | Path to HotpotQA JSON file (default: data/raw/hotpot_dev_fullwiki_v1.json) |
| `--output-dir PATH` | Directory for output files (default: data/processed) |
| `--context-sizes N1 N2 N3` | Context size limits in tokens (default: 8000 32000 128000) |
| `--limit N` | Limit number of entries to process (for testing) |
| `--workers N` | Number of worker processes (default: CPU count) |
| `--verbose` | Enable verbose logging |

### Processing Logic

1. **Supporting Documents**: Extract articles from database using exact title matching from `supporting_facts`
2. **Distractor Documents**: Use hybrid search (TF-IDF + PageRank) with supporting fact titles as queries, excluding supporting docs
3. **Context Filtering**: Skip entries where supporting docs alone exceed context limits
4. **Token Counting**: Use GPT tokenizer (tiktoken cl100k_base) for consistent token counting
5. **Output Generation**: Create separate files for each context size with appropriate filtering

### Performance Characteristics

- **Multiprocessing**: Uses all CPU cores by default for parallel processing
- **Token Counting**: ~50,000 tokens/second using GPT tokenizer
- **Search Performance**: Uses hybrid search (TF-IDF + PageRank) with inverted index for better quality distractor documents
- **Memory Efficient**: Streams processing to handle large datasets
- **Progress Tracking**: Real-time progress bars and comprehensive logging
- **Speed**: ~5-6 seconds per entry with 8 workers (vs ~13-15 seconds sequential)

For detailed information, see [docs-vibe/0031-qa-dataset-generation.md](docs-vibe/0031-qa-dataset-generation.md) and [docs-vibe/0033-qa-dataset-hybrid-search.md](docs-vibe/0033-qa-dataset-hybrid-search.md).
