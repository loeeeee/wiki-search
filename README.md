# Wiki Search

## Hybrid Search (TF-IDF + PageRank)

Single-threaded hybrid search combines inverted index TF-IDF relevance with PageRank authority and returns the top 20 results by default.

**Performance (with quality improvements)**: 4.90 searches/second, 203.67ms average latency (1000-search benchmark)
**Note**: Search quality improvements have been implemented (coverage tracking, multi-term filtering, max-normalization, title boosting, deterministic tie-breaking) but performance target (20 searches/sec) is not met. See `docs-vibe/0116-search-quality-improvements.md` for details.

**Performance (baseline)**: 20.40 searches/second, 48.74ms average latency (before quality improvements)

Usage:

```python
from search_engine.search import search_hybrid

results = search_hybrid("alan turing computability", limit=20)
for article, score in results:
    print(article.title, score)
```

Benchmark (target ≥ 20 searches/sec):

```bash
python manage.py benchmark_search --num-searches 1000 --profile-output search_benchmark_profile.txt
```

Implementation details: Uses (term_id, tf_idf_score) composite index with per-term limit of 20 postings. Cached NLTK tokenizer. Linear score blend (alpha=0.7) after [0,1] normalization.

A Wikipedia dump processing pipeline with interactive search capabilities

**SOFTWARE DEFINED DATA**

## Quick Start

Get up and running in 5 minutes:

1. **Clone and setup environment:**
   ```bash
   git clone <repository-url>
   cd wiki-search
   nix-shell  # Activates NixOS environment
   uv sync    # Install Python dependencies
   ```

2. **Configure database:**
   ```bash
   # Create .env file with PostgreSQL credentials
   echo "POSTGRES_DB=wiki_search
   POSTGRES_USER=your_username
   POSTGRES_PASSWORD=your_password
   POSTGRES_HOST=172.22.0.133
   POSTGRES_PORT=5432" > .env
   
   # Load environment and migrate
   set -a; source .env; set +a
   python wiki_search/manage.py migrate
   ```

3. **Load sample data and start web app:**
   ```bash
   # Load limited dataset for testing
   python wiki_search/manage.py load_wiki_dump --limit 10000
   
   # Start web server
   cd wiki_search
   python manage.py runserver 0.0.0.0:8000
   ```

4. **Access the application:**
   - Search interface: http://localhost:8000
   - Database status: http://localhost:8000/status
   - Admin interface: http://localhost:8000/admin

## Environment Setup

### Prerequisites

- **NixOS** development environment (configured via `shell.nix`)
- **Python 3.13+** with virtual environment at `.venv/`
- **PostgreSQL** database server
- **uv** package manager for Python dependencies
- **AMD GPU with ROCm** (historical; see docs-vibe/archives/)

### NixOS Environment

The project uses `shell.nix` to configure the development environment:

```bash
# Activate NixOS environment
nix-shell

# This provides:
# - Python 3.13 with development tools
# - PostgreSQL client libraries
# - System libraries (gcc, zlib, bzip2, etc.)
# - Build tools for Python packages
```

### Database Configuration

1. **Create environment file:**
   ```bash
   cp .env.example .env  # If template exists
   # Or create manually:
   echo "POSTGRES_DB=wiki_search
   POSTGRES_USER=your_username
   POSTGRES_PASSWORD=your_password
   POSTGRES_HOST=172.22.0.133
   POSTGRES_PORT=5432" > .env
   ```

2. **Load environment variables:**
   ```bash
   set -a; source .env; set +a
   ```

3. **Install dependencies and migrate:**
   ```bash
   uv sync
   python wiki_search/manage.py migrate
   ```

4. **Test database connection:**
   ```bash
   python wiki_search/manage.py db_summary
   ```

### Documentation Quick Links

- Overview: docs-vibe/0100-overview.md
- Architecture: TF-IDF (docs-vibe/0101-tfidf-architecture.md), PageRank (docs-vibe/0102-pagerank-architecture.md), QA (docs-vibe/0103-qa-generation-architecture.md)
- Benchmarks summary: docs-vibe/0104-benchmarks-summary.md
- Historical/GPU docs: see docs-vibe/archives/

## Database Management Commands

### Database Summary

View current database statistics:

```bash
python wiki_search/manage.py db_summary
```

Monitor loading progress:
```bash
watch --interval 30 python wiki_search/manage.py db_summary
```

### Database Cleanup

Fast cleanup of all search_engine tables:

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

**Options:**
- `--yes`: Run non-interactively and skip confirmation
- `--no-progress`: Do not show progress bars or perform COUNT(*) queries
- `--no-fast-pragmas`: Disable fast SQLite pragmas (safer but slower)
- `--drop-recreate`: SQLite-only, drop and recreate tables (fastest for large datasets)

### Random Articles

Display random articles for testing:

```bash
python wiki_search/manage.py random_articles --max-paragraphs 5
```

**Options:**
- `--max-paragraphs N`: Maximum paragraphs to print per article (default: 5)

## Data Processing Commands

### Count Articles

Count articles in the Wikipedia dump:

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

**Options:**
- `--processed-dir PATH`: Path to processed directory (default: data/processed/enwiki-20171001-pages-meta-current-withlinks-processed)
- `--sample N`: Sample only N files for quick estimate
- `--estimate`: Use sampling to estimate total (samples 1% of files)
- `--verbose`: Enable verbose logging

Based on sampling, the dump contains approximately **5,357,970 articles** across 15,517 files.

### Load Wikipedia Dump

Load Wikipedia dump into database with automatic link resolution:

```bash
# One-step load + link resolution (optimized with 6 database workers)
python wiki_search/manage.py load_wiki_dump --workers 6 --db-workers 6 --batch-size 5000

# Optional: process only a subset
python wiki_search/manage.py load_wiki_dump --limit 200000

# Performance tuning for different systems
python wiki_search/manage.py load_wiki_dump --workers 4 --db-workers 4  # 4-core system
python wiki_search/manage.py load_wiki_dump --workers 8 --db-workers 8  # 8-core system
```

**Options:**
- `--processed-dir PATH`: Root of pre-decompressed shards (default: data/processed/enwiki-20171001-pages-meta-current-withlinks-processed)
- `--batch-size N`: DB flush size for articles (default: 5000)
- `--workers N`: Number of worker processes (default: CPU-1)
- `--db-workers N`: Number of database writer threads (default: 96)
- `--producer-threads N`: Number of I/O producer threads per worker for concurrent bz2 decompression (default: 2)
- `--limit N`: Stop after processing N articles (smoke tests)
- `--profile`: Enable detailed profiling with cProfile

**Performance Notes:**
- This command always drops data at start by calling `clean_db`
- Internal link resolution happens automatically at the end
- Uses persistent database connections and parallel link resolution for 2-3x faster processing
- I/O-optimized concurrent processing with configurable producer threads

### Resolve Links

Separate link resolution with merged optimization:

```bash
# Run link resolution separately
python wiki_search/manage.py resolve_links --batch-size 5000 --db-workers 96

# Run with custom settings
python wiki_search/manage.py resolve_links --batch-size 10000 --db-workers 48

# Rebuild all link resolutions from scratch
python wiki_search/manage.py resolve_links --rebuild --batch-size 5000 --db-workers 96
```

**Options:**
- `--batch-size N`: Batch size for processing (default: 5000)
- `--db-workers N`: Number of database worker threads (default: 96)
- `--rebuild`: Clear existing link resolutions and rebuild from scratch

**Performance Optimization:**
- Merged approach resolves both `from_article` and `to_article` foreign keys in single database pass
- 50% reduction in database queries
- Single progress bar for entire operation
- Better query optimization by database engine

## Search Index Commands

### Build TF-IDF Index (Multiprocess)

Build TF-IDF index using multiprocess parallel approach with PostgreSQL COPY optimization:

```bash
# Build with default concurrent Pass 2 (200+ articles/sec)
python wiki_search/manage.py build_tfidf_simple --limit 10000 --rebuild

# Optimal default configuration (9 csv-workers, 9 db-workers, batch size 290)
python wiki_search/manage.py build_tfidf_simple --rebuild

# Custom workers and batch size for different systems
python wiki_search/manage.py build_tfidf_simple --rebuild \
    --db-workers 12 --csv-workers 12 --batch-size 250

# Custom Pass 1 configuration
python wiki_search/manage.py build_tfidf_simple --rebuild --cpu-workers 16 --batch-size-per-worker 200

# Test with profiling
python wiki_search/manage.py build_tfidf_simple --limit 1000 --profile --rebuild

# Full rebuild with verbose logging
python wiki_search/manage.py build_tfidf_simple --rebuild --verbose

# Fast rebuild for smoke test (5000 articles)
python wiki_search/manage.py build_tfidf_simple --limit 5000 --rebuild --verbose
```

**Options:**
- `--limit N`: Limit number of articles to process (default: all)
- `--profile`: Enable cProfile profiling
- `--rebuild`: Clear existing Vocabulary and InvertedIndex before building
- `--verbose`: Enable verbose logging
- `--cpu-workers N`: Number of CPU worker processes for Pass 1 (default: all available cores)
- `--batch-size-per-worker N`: Articles per worker batch in Pass 1 (default: 50)
- `--batch-size N`: Articles per batch for Pass 2 inverted index (default: 290, optimized for 200+ articles/sec)
- `--csv-workers N`: Worker processes for CSV building in Pass 2 (default: 9)
- `--db-workers N`: Worker threads for database writes in Pass 2 (default: 9)

**Fast Rebuild Behavior:**
- When `--rebuild` is specified, the command now uses PostgreSQL `TRUNCATE TABLE <tables> RESTART IDENTITY CASCADE` to clear existing TF-IDF data instantly, followed by `VACUUM ANALYZE` to refresh planner statistics. This mirrors the approach used by `clean_db` and significantly speeds up rebuilds.

**Architecture:**
- **Pass 1**: Build term frequency (TF) and document frequency (DF)
  - Multiprocess tokenization using NLTK with ProcessPoolExecutor
  - Database reads use `.iterator()` for memory efficiency
  - Configurable workers and batch size for optimal performance
  - Cache TF maps in memory, aggregate global DF dictionary
- **Pass 2**: Build IDF values and inverted index (concurrent by default)
  - Calculate IDF = log(N / df)
  - Producer-consumer pipeline with:
    - ProcessPoolExecutor for CSV buffer building (CPU-bound, bypasses GIL)
    - ThreadPoolExecutor for database COPY operations (I/O-bound)
    - Parallel batch processing for 2.2x speedup over sequential approach

**Performance Characteristics:**

*Current Performance (fully optimized, 10000 articles):*
- 10000 articles: **451 articles/second** (22.2s total)
- Pass 1: 4.5s (20%)
- Pass 2: 17.3s (78%)
  - Vocabulary: 1.1s (CSV: 0.66s, DB: 0.44s)
  - Term mapping: 0.14s (optimized with values_list)
  - Inverted Index: 16.0s (CSV: 4.6s, DB: 10.8s pipelined)
- Configuration: 16 cpu-workers, 48 csv-workers, 48 db-workers, batch size 600

*Performance Breakdown (10000 articles):*
- **CSV Building**: List joining (50% faster than StringIO)
- **Database Writes**: Overlapped with CSV building via pipeline
- **Memory**: Process-local shared data (initializer pattern)
- **Term Mapping**: Bulk query with values_list (86% faster)

*Previous Baseline (before optimizations):*
- 10000 articles: 323 articles/second (30.9s total)
- **39.6% improvement** with code-level optimizations
- Original bottlenecks: StringIO overhead, ORM iteration, excessive workers

**Optimization Notes:**
- PostgreSQL COPY provides 4.2x speedup over Django ORM
- Multiprocess tokenization achieves 2000-5000 articles/second in Pass 1
- Initializer pattern eliminates pickle serialization bottleneck
- List joining for CSV building: 50% faster than StringIO
- Bulk term-to-ID mapping: 86% faster than ORM iteration
- Pipeline architecture overlaps CSV building with DB writes for better throughput
- Process-local shared data enables true multi-core parallelism
- Further speedup to 800+ articles/sec requires database tuning (see docs-vibe/0053)

**Use Cases:**
- Production builds of TF-IDF indexes
- Processing large datasets (> 1k articles)
- Development and debugging with clean code structure

**Database Tables:**
- **Vocabulary**: Global term statistics (term, document_frequency, idf_value)
- **InvertedIndex**: Term-article-score mappings for fast search

For detailed performance analysis, see:
- [docs-vibe/archives/0052-csv-building-parallelization-fix.md](docs-vibe/archives/0052-csv-building-parallelization-fix.md) - CSV building parallelization fix (433 articles/sec)
- [docs-vibe/archives/0051-concurrent-db-io-pass2.md](docs-vibe/archives/0051-concurrent-db-io-pass2.md) - Concurrent Pass 2 implementation (200+ articles/sec)
- [docs-vibe/archives/0047-cpu-scalability-refactor.md](docs-vibe/archives/0047-cpu-scalability-refactor.md) - Multiprocess tokenization details

### Additional Notes
Legacy GPU setup and related commands have been archived. See `docs-vibe/archives/` for historical context.

### Build PageRank

Build PageRank scores for articles using the InternalLink graph:

```bash
# Optimized parallel build (recommended)
python wiki_search/manage.py build_pagerank --rebuild --db-workers 64 --batch-size 300

# Single-threaded baseline
python wiki_search/manage.py build_pagerank --rebuild

# With profiling and verbose output
python wiki_search/manage.py build_pagerank --rebuild --db-workers 64 --batch-size 300 --profile --verbose

# Test with limited dataset
python wiki_search/manage.py build_pagerank --limit 10000 --rebuild --db-workers 32 --batch-size 500
```

**Options:**
- `--rebuild`: Clear existing PageRank scores before building
- `--limit N`: Limit number of links to process (for testing)
- `--db-workers N`: Number of parallel database writer threads (default: 1, recommended: 64)
- `--batch-size N`: Records per batch for parallel storage (default: 10000, recommended: 300)
- `--db-read-workers N`: Number of parallel database readers for graph loading (default: 1, not recommended >1)
- `--profile`: Enable detailed profiling with cProfile
- `--verbose`: Enable verbose logging
- `--damping FLOAT`: PageRank damping factor (default: 0.85)
- `--max-iter N`: Maximum number of iterations (default: 100)
- `--tolerance FLOAT`: Convergence tolerance (default: 1e-6)

**Performance Characteristics:**

*Optimized Parallel (64 workers, 300 batch size):*
- **100k articles**: 1.21s (48,972 articles/second) - **9.0x faster**
- **5.4M articles (full)**: 285.6s (17,801 articles/second) - **4.3x faster**
- **Storage phase**: 12.0% of time (was 96.6%) - **30.9x speedup**
- **Memory usage**: ~4 GB for full dataset

*Single-Threaded Baseline:*
- **100k articles**: 10.89s (5,391 articles/second)
- **5.4M articles (proj)**: 1,218s (4,502 articles/second)
- **Storage phase**: 96.6% of time (bottleneck)

*Phase Breakdown (5.4M articles, optimized):*
- **Delete**: 0.66s (0.2%) - Fast TRUNCATE
- **Compute**: 251s (88.0%) - PageRank algorithm (new bottleneck at scale)
  - Database query (54M links): ~103s (36%)
  - PageRank iterations: ~21s (7%)
  - Matrix processing: ~127s (44%)
- **Store**: 34s (12.0%) - Parallel batch COPY (optimized)

**Optimization Details:**
- Parallel storage uses ThreadPoolExecutor with batch-level transactions
- Each worker gets independent database connection via `connection.cursor()`
- Optimal configuration: 64 workers, 300 records per batch
- Storage bottleneck eliminated: 96.6% → 12.0% of time
- Note: Parallel database reads (--db-read-workers >1) create contention and are slower

**Next Optimization Steps:**
Current: 285.6s for 5.4M articles, Target: 15s, Remaining gap: 19x speedup needed

1. **✅ COMPLETE: Parallel storage** (4.3x achieved): ThreadPoolExecutor with 64 workers
2. **Database query optimization** (2-3x): Add composite index on InternalLink, query tuning
3. **Approximate PageRank** (5-10x): Monte Carlo sampling, early stopping
4. **Incremental updates** (10-100x): Cache and update only changed portions

For detailed optimization analysis:
- Storage optimization: [docs-vibe/0113-pagerank-parallel-storage-optimization.md](docs-vibe/0113-pagerank-parallel-storage-optimization.md)
- Single-threaded baseline: [docs-vibe/0111-pagerank-single-threaded-implementation.md](docs-vibe/0111-pagerank-single-threaded-implementation.md)

### Benchmark Search Performance

Benchmark search retrieval speed with profiling and example results:

```bash
# Run benchmark with default settings (1000 searches)
python wiki_search/manage.py benchmark_search

# Run with custom number of searches
python wiki_search/manage.py benchmark_search --num-searches 500

# Run with verbose logging
python wiki_search/manage.py benchmark_search --verbose

# Disable example result display
python wiki_search/manage.py benchmark_search --no-show-examples

# Custom profile output file
python wiki_search/manage.py benchmark_search --profile-output search_benchmark_profile.txt
```

Quick smoke test inside nix-shell (deterministic by default):

```bash
cd /home/loe/Projects/wiki-search
nix-shell --run "uv sync && python wiki_search/manage.py benchmark_search --num-searches 2 --no-show-examples"
```

**Options:**
- `--num-searches N`: Number of searches to execute (default: 1000)
- `--profile-output PATH`: Output file for cProfile results (default: `search_benchmark_profile.txt`)
- `--verbose`: Enable verbose logging
- `--show-examples`: Display example search results (default: True)
- `--no-show-examples`: Disable example result display
- `--seed SEED`: Deterministic seed (default: 42)
- `--randomize`: Opt-out of deterministic mode (no fixed seed)
- `--queries-file PATH`: Load queries from file (one per line)
- `--save-queries PATH`: Save generated queries to file
- `--export-results PATH`: Export per-query results CSV (query,rank,article_id,title,score)

**Output:**
- Console: Progress bar, summary metrics, throughput comparison (target: 20 searches/sec), example results, top bottlenecks
- Log file: `benchmark_search.log` (detailed execution log)
- Profile file: `search_benchmark_profile.txt` (cProfile statistics with top 50 functions)

**Determinism:**
- Deterministic by default. Two runs with the same seed and unchanged DB produce identical queries and exported CSVs.

**Performance Target:**
- Target: **20 searches per second** (single-threaded)
- Each search returns top 20 results using `search_hybrid()` (TF-IDF + PageRank)
- Test queries are randomly sampled from article titles in the database

**Use Cases:**
- Measure baseline search performance
- Identify bottlenecks in search retrieval
- Profile database query patterns
- Validate search index optimization
- Compare performance across different database configurations

For detailed documentation, see [docs-vibe/0106-search-benchmark.md](docs-vibe/0106-search-benchmark.md).

## QA Dataset Commands

### Generate QA Dataset

Generate question-answering dataset for LLM training from HotpotQA data:

```bash
# Test with small dataset and profiling (recommended)
python wiki_search/manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --limit 10 \
  --profile \
  --verbose

# Production run (default 100 entries)
python wiki_search/manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed

# Full dataset (7,405 entries, ~17.5 minutes)
python wiki_search/manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --limit 0

# Custom context sizes
python wiki_search/manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --context-sizes 8000 32000 128000 \
  --limit 500
```

**Options:**
- `--input PATH`: Path to HotpotQA JSON file (default: data/raw/hotpot_dev_fullwiki_v1.json)
- `--output-dir PATH`: Directory for output files (default: data/processed)
- `--context-sizes N1 N2 N3`: Context size limits in tokens (default: 8000 32000 128000)
- `--limit N`: Limit number of entries to process (default: 100)
- `--profile`: Enable cProfile profiling and save to qa_dataset_generation.prof
- `--verbose`: Enable verbose logging
- `--debug`: Enable debug logging for troubleshooting

**Output Files:**
- `qa_dataset_8000.json` - entries with context_size ≤ 8k tokens
- `qa_dataset_32000.json` - entries with context_size ≤ 32k tokens
- `qa_dataset_128000.json` - entries with context_size ≤ 128k tokens

**Processing Logic:**
1. **Supporting Documents**: Extract articles from database using exact title matching from `supporting_facts`
2. **Distractor Documents**: Use hybrid search (TF-IDF + PageRank) with supporting fact titles as queries, excluding supporting docs
3. **Context Filtering**: Skip entries where supporting docs alone exceed context limits

**Performance Notes:**
- **Optimized**: 192x speedup over baseline (135ms vs 25,900ms per entry)
- **Pre-processing**: Batch fetches articles and pre-computes token counts for massive speedup
- **Caching**: Article cache and token cache eliminate N+1 query problem (99.99% query reduction)
- **Full dataset**: 7,405 entries in ~17.5 minutes (was projected to take 53+ hours)
- Use `--profile` to identify remaining bottlenecks (now dominated by search operations)
- Profile output saved to `qa_dataset_generation.prof` (analyze with `python -m pstats`)
- See [docs-vibe/0115-qa-dataset-optimization.md](docs-vibe/0115-qa-dataset-optimization.md) for optimization details

**Implementation Details:**
- Token counting uses GPT tokenizer (tiktoken cl100k_base) for LLM-compatible counts
- Timing statistics reported for article lookups, token counting, and search operations
- Supports verbose and debug logging modes for troubleshooting
- Real-time progress bars with tqdm for monitoring
- Comprehensive error handling with skip statistics

#### Profiling and Benchmarking (default metrics)

The command prints detailed timing metrics by default, including stage breakdown, throughput (entries/sec), and ETA when `--limit` is used. For quick baselines on representative subsets and an extrapolated full-run estimate:

```bash
python scripts/benchmark_generate_qa.py --input data/raw/hotpot_dev_fullwiki_v1.json --limits 300 1000
```

See `docs-vibe/0119-qa-dataset-profiling-baseline.md` for details.

## Web Application

The project includes a Django web application for searching and viewing Wikipedia articles.

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
   - Search interface: http://localhost:8000
   - Article detail: http://localhost:8000/article/<page_id>/
   - Database status: http://localhost:8000/status/
   - Admin interface: http://localhost:8000/admin/

### Web App Features

- **Search Page**: Clean search interface with results showing article titles and snippets
- **Article Detail Page**: Full article content with navigation back to search
- **Status Page**: Comprehensive database statistics and system information at `/status/`
- **Hybrid Search**: Combines TF-IDF relevance scoring with PageRank authority
- **Query Tokenization Display**: Shows users how their search queries are tokenized by the search engine
- **Responsive Design**: Mobile-friendly interface with modern styling
- **Fast Performance**: Optimized database queries and efficient search algorithms

### Search Capabilities

- **Hybrid Ranking**: Combines content relevance (TF-IDF) with page authority (PageRank)
- **Fallback Search**: Title-based search when advanced indexing unavailable
- **Snippet Display**: Shows relevant content previews in search results
- **Link Navigation**: Internal Wikipedia links converted to app navigation
- **Query Transparency**: Visual display of how search queries are tokenized using NLTK tokenizer

### Database Status Page

Access comprehensive database statistics and system information at `http://localhost:8000/status/`:

**Basic Statistics:**
- Article count, internal links, unresolved links
- Search index statistics (Vocabulary, InvertedIndex, PageRank)

**Content Analysis:**
- Average paragraphs per article
- Average outgoing/incoming links per article
- Sample-based performance metrics

**Search Index Details:**
- PageRank score statistics (min/max/average)
- Vocabulary statistics (document frequency, IDF values)

**System Information:**
- Database backend and version
- Last updated timestamp
- Auto-refresh every 30 seconds

## Configuration

### Tokenizer Configuration

The system uses different tokenization strategies based on use case:

#### TF-IDF and Search (NLTK Tokenizer)
- **Purpose**: TF-IDF indexing and web app search functionality
- **Tokenizer**: NLTK word_tokenize with stopword filtering
- **Benefits**: Better linguistic tokenization for search relevance
- **Performance**: ~20,000 tokens/second
- **Usage**: Automatic - no configuration needed

#### QA Dataset Generation (GPT Tokenizer)
- **Purpose**: Token counting for LLM context size calculations
- **Tokenizer**: tiktoken cl100k_base (GPT-4 compatible)
- **Benefits**: Accurate token counts for LLM compatibility
- **Performance**: ~50,000 tokens/second
- **Usage**: Automatic - no configuration needed

#### Configuration

The tokenizer selection is now automatic based on use case:

```python
# settings.py - kept for backward compatibility
TOKENIZER_TYPE = 'nltk'  # Not actively used, kept for compatibility
```

#### Rebuilding Indexes

**Important**: After this refactor, rebuild TF-IDF indexes to use NLTK tokenization:

```bash
# Clear existing indexes
python manage.py clean_db --yes

# Rebuild with NLTK tokenizer
python manage.py build_tfidf_index --rebuild
```

#### Performance Characteristics

| Use Case | Tokenizer | Speed | Memory | Quality | Purpose |
|----------|-----------|-------|--------|---------|---------|
| TF-IDF/Search | NLTK | ~20k/sec | High | High | Linguistic accuracy |
| QA Generation | GPT | ~50k/sec | Medium | High | LLM compatibility |

For detailed information, see [docs-vibe/archives/0037-nltk-tfidf-refactor.md](docs-vibe/archives/0037-nltk-tfidf-refactor.md).

## Performance Tuning

### Load Performance

**Producer Threads Tuning:**
- **Default (2)**: Optimal for most systems with SSD or fast network storage
- **Increase (4-6)**: For very fast storage (NVMe, RAM disk) or high-latency network storage
- **Decrease (1)**: For slow HDDs or CPU-constrained systems where parsing becomes the bottleneck

**Worker Configuration:**
- **4-core system**: `--workers 4 --db-workers 4`
- **8-core system**: `--workers 8 --db-workers 8`
- **High-memory system**: `--workers 12 --db-workers 96`

### Search Index Performance

**TF-IDF Build Performance:**
- **GPU Acceleration**: 5-10x speedup over CPU implementation
- **Producer-Consumer Architecture**: Eliminates database bottlenecks
- **Batch Processing**: Processes 10k articles simultaneously on GPU
- **Async Database Writes**: Non-blocking database operations

**PageRank Build Performance:**
- **Small datasets (1k articles)**: 1.7-2x speedup over baseline
- **Medium datasets (10k articles)**: 2-2.5x speedup over baseline
- **Large datasets (100k+ articles)**: 2.5-3.5x speedup over baseline

### QA Dataset Performance

**Generation Speed (optimized):**

| Entries | Total Time | Per Entry | Speedup |
|---------|------------|-----------|---------|
| 10      | 3.8s      | 380ms     | 68x     |
| 100     | 31s       | 310ms     | 84x     |
| 7,405   | 17.5min   | 135ms     | 192x    |

**Before Optimization (baseline):**
- **10 entries**: 259 seconds (25,900ms per entry)
- **Primary bottleneck**: N+1 database queries (94.5% of time)
- **Root cause**: Repeated article lookups with `Article.objects.get()`

**After Optimization:**
- **Eliminated N+1 problem**: Pre-fetching and caching of articles
- **Token count caching**: Pre-computation eliminates redundant tokenization
- **GPT tokenizer caching**: Single instance reused across all calls
- **Current bottleneck**: Search operations (67.7% of time for full dataset)

**Full Dataset Performance (7,405 entries):**
- **Pre-processing**: 58.7s (batch fetch 13,783 articles, pre-compute tokens)
- **Processing**: 16.6 minutes (search operations: 675.6s, entry processing: 322.0s)
- **Average**: 134.85ms per entry
- **Throughput**: 7.4 entries/second
- **Database queries**: 1 bulk query vs 26,000+ individual queries (99.99% reduction)

**Token Counting:**
- **Speed**: ~50,000 tokens/second using cached GPT tokenizer (tiktoken cl100k_base)
- **Optimization**: Cached tokenizer instance + pre-computed counts

**Memory Usage:**
- Scales with number of unique articles (~14k articles cached for full dataset)
- Single-threaded processing for simplicity and debuggability
- Comprehensive profiling support for performance optimization

**Implementation Details:**
- See [docs-vibe/0115-qa-dataset-optimization.md](docs-vibe/0115-qa-dataset-optimization.md) for optimization details
- See [docs-vibe/0114-qa-dataset-single-threaded.md](docs-vibe/0114-qa-dataset-single-threaded.md) for baseline profiling

## Documentation

High-level docs:
- [docs-vibe/0100-overview.md](docs-vibe/0100-overview.md)
- [docs-vibe/0101-tfidf-architecture.md](docs-vibe/0101-tfidf-architecture.md)
- [docs-vibe/0102-pagerank-architecture.md](docs-vibe/0102-pagerank-architecture.md)
- [docs-vibe/0103-qa-generation-architecture.md](docs-vibe/0103-qa-generation-architecture.md)
- [docs-vibe/0104-benchmarks-summary.md](docs-vibe/0104-benchmarks-summary.md)
- [docs-vibe/0106-search-benchmark.md](docs-vibe/0106-search-benchmark.md)

Archived engineering logs and historical GPU docs:
- [docs-vibe/archives/0040-pass2-threadpool-optimization.md](docs-vibe/archives/0040-pass2-threadpool-optimization.md)
- [docs-vibe/archives/0027-pagerank-optimization.md](docs-vibe/archives/0027-pagerank-optimization.md)
- [docs-vibe/archives/0029-multiprocessing-pagerank-feasibility.md](docs-vibe/archives/0029-multiprocessing-pagerank-feasibility.md)
- [docs-vibe/archives/0031-qa-dataset-generation.md](docs-vibe/archives/0031-qa-dataset-generation.md)
- [docs-vibe/archives/0033-qa-dataset-hybrid-search.md](docs-vibe/archives/0033-qa-dataset-hybrid-search.md)
- [docs-vibe/archives/0023-tokenizer-helper.md](docs-vibe/archives/0023-tokenizer-helper.md)
- [docs-vibe/archives/0037-nltk-tfidf-refactor.md](docs-vibe/archives/0037-nltk-tfidf-refactor.md)
- [docs-vibe/archives/0022-tfidf-gpu-overhaul.md](docs-vibe/archives/0022-tfidf-gpu-overhaul.md)
- [docs-vibe/archives/0039-tfidf-gpu-overhaul-complete.md](docs-vibe/archives/0039-tfidf-gpu-overhaul-complete.md)

### Profile QA Generation

Profile throughput and bottlenecks with a target of 800 entries/sec (test with 1000 entries):

```bash
python wiki_search/manage.py profile_qa_generation \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --context-sizes 8000 32000 128000 \
  --limit 1000 \
  --workers $(nproc) \
  --profile-db \
  --debug
```

The command saves cProfile output, logs a timing breakdown, and reports throughput (entries/sec) vs the 800/sec target.