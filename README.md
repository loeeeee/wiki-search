# Wiki Search

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
- **AMD GPU with ROCm** (optional, for GPU acceleration)

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

### GPU Acceleration Setup (Optional)

For AMD GPU acceleration, install PyTorch with ROCm support:

```bash
# Install PyTorch with ROCm support
pip install torch --index-url https://download.pytorch.org/whl/rocm5.7

# Verify GPU availability
python -c "import torch; print(torch.cuda.is_available())"

# Install project with GPU dependencies
uv sync --extra gpu
```

**GPU Requirements:**
- AMD GPU with ROCm 5.0+ support (RX 6000 series or newer)
- 8GB+ VRAM recommended for large datasets
- Linux OS

**Usage:**
```bash
# GPU-accelerated PageRank
python wiki_search/manage.py build_pagerank --use-gpu --rebuild

# CPU-based TF-IDF indexing with ProcessPool parallelism
python wiki_search/manage.py build_tfidf_index --rebuild

# Test with limited dataset
python wiki_search/manage.py build_tfidf_index --limit 1000
```

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

*Current Performance (optimized with initializer pattern, 6000 articles):*
- 6000 articles: **433 articles/second** (13.86s total)
- Pass 1: 3.07s (22.1%)
- Pass 2: 10.78s (77.8%)
  - Vocabulary: 0.60s
  - Term mapping: 0.76s
  - Inverted Index: 9.31s (pipelined CSV building + DB writes)
- Configuration: 12 csv-workers, 12 db-workers, batch size 400

*Performance Breakdown (6000 articles):*
- **CSV Building**: Fully parallelized (all 12 workers active)
- **Database Writes**: Overlapped with CSV building via pipeline
- **Memory**: Process-local shared data (initializer pattern)

*Previous Baseline (before optimizations):*
- 6000 articles: 204.13 articles/second (29.39s total)
- **2.12x slower** than current optimized implementation
- CSV building bottlenecked by 7.2s pickle serialization overhead

**Optimization Notes:**
- PostgreSQL COPY provides 4.2x speedup over Django ORM
- Multiprocess tokenization achieves 2000-5000 articles/second in Pass 1
- Initializer pattern eliminates pickle serialization bottleneck (7.2s to negligible)
- Pipeline architecture overlaps CSV building with DB writes for better throughput
- CSV building: 93% improvement (9.25s to 0.64s with initializer)
- Process-local shared data enables true multi-core parallelism
- CPU utilization: All workers fully active without serialization bottleneck
- Overall throughput: 2.12x improvement over baseline (204 to 433 articles/sec)

**Use Cases:**
- Production builds of TF-IDF indexes
- Processing large datasets (> 1k articles)
- Development and debugging with clean code structure

**Database Tables:**
- **Vocabulary**: Global term statistics (term, document_frequency, idf_value)
- **InvertedIndex**: Term-article-score mappings for fast search

For detailed performance analysis, see:
- [docs-vibe/0052-csv-building-parallelization-fix.md](docs-vibe/0052-csv-building-parallelization-fix.md) - CSV building parallelization fix (433 articles/sec)
- [docs-vibe/0051-concurrent-db-io-pass2.md](docs-vibe/0051-concurrent-db-io-pass2.md) - Concurrent Pass 2 implementation (200+ articles/sec)
- [docs-vibe/0047-cpu-scalability-refactor.md](docs-vibe/0047-cpu-scalability-refactor.md) - Multiprocess tokenization details

### Build TF-IDF Index (Optimized Multi-Thread)

Build TF-IDF index and inverted index for search functionality using optimized GPU acceleration with multiple threadpools:

```bash
# Build TF-IDF index with optimized GPU acceleration (default)
python wiki_search/manage.py build_tfidf_index --limit 100000

# Rebuild existing index
python wiki_search/manage.py build_tfidf_index --rebuild

# With custom GPU processes and reader workers
python wiki_search/manage.py build_tfidf_index --gpu-threads 4 --reader-threads 32 --verbose

# With separate writer pools and profiling
python wiki_search/manage.py build_tfidf_index --separate-writers --profile --verbose

# Custom workers and database writers
python wiki_search/manage.py build_tfidf_index --workers 8 --db-workers 48 --verbose

# Test with limited dataset
python wiki_search/manage.py build_tfidf_index --limit 1000
```

**Options:**
- `--rebuild`: Clear existing indexes before building
- `--batch-size N`: Articles per database batch (default: 500)
- `--limit N`: Limit number of articles (for testing)
- `--workers N`: Number of CPU consumer processes (default: CPU cores)
- `--db-workers N`: Number of database writer threads (default: 96)
- `--verbose`: Enable verbose logging
- `--profile`: Enable detailed profiling with cProfile
- `--cpu-process-batch-size N`: Articles per CPU batch (default: 1_000)
- `--cpu-threads N`: Number of parallel CPU consumer processes (default: CPU cores)
- `--reader-workers N`: Number of database reader threads (default: 16)
- `--separate-writers`: Enable separate writer pools for TF-IDF vs inverted index

**Optimized Architecture:**
- **Pass 1**: Producer-consumer model for document frequency calculation
  - Producers: CPU thread count reading from database
  - Consumers: CPU core count computing document frequency
- **Pass 2**: CPU-based TF-IDF computation with ProcessPool parallelism
  - **ProcessPool Architecture**: True parallelism with process isolation
  - **CPU Processes**: Multiple processes (default: CPU cores) processing batches in parallel
  - **Process Isolation**: Independent memory spaces per process
  - **Reader Pool**: Dedicated threadpool (default: 16) for async database prefetching
  - **Writer Pools**: Separate threadpools for TF-IDF and inverted index writes
  - **Concurrent Pipeline**: Database prefetch happens alongside CPU processing

**Performance Results:**
- **CPU-only processing**: 11.9 articles/second (100 articles in 8.39s)
- **ProcessPool parallelism**: True CPU parallelism without GIL limitations
- **Universal compatibility**: Works on any system without GPU requirements
- **Scalable architecture**: Performance scales with CPU core count
- **CPU utilization**: ProcessPool parallelism without GIL limitations
- **Database I/O**: Concurrent prefetch pipeline eliminates blocking reads
- **Architecture**: ProcessPool provides true parallelism with process isolation

**System Requirements:**
- **CPU**: Multi-core processor (defaults to CPU core count)
- **Memory**: Sufficient RAM for ProcessPool parallelism
- **Database**: PostgreSQL for optimal performance
- **No GPU required**: Universal compatibility across all systems

**Key Features:**
- **ProcessPool Architecture**: True CPU parallelism with process isolation
- **CPU-Only Processing**: No GPU dependencies for universal compatibility
- **Concurrent Pipeline**: Database prefetch happens alongside CPU processing
- **Explicit Data Passing**: Batch data passed explicitly (no shared memory)
- **Bulk Database Operations**: Single bulk UPDATE instead of N individual queries
- **Robust Error Handling**: Proper cleanup and logging with timeout protection
- **Auto-scaling**: Adjusts CPU process count based on dataset size

### Build PageRank

Build PageRank scores for articles using the InternalLink graph:

```bash
# Standard PageRank build (parallel database operations)
python wiki_search/manage.py build_pagerank

# With custom parallel workers
python wiki_search/manage.py build_pagerank --db-read-workers 4 --db-write-workers 4

# With profiling and verbose output
python wiki_search/manage.py build_pagerank --rebuild --profile --verbose

# Performance monitoring
python wiki_search/manage.py build_pagerank --profile --verbose 2>&1 | grep "Memory usage"
```

**Options:**
- `--damping FLOAT`: PageRank damping factor (default: 0.85)
- `--max-iterations N`: Maximum number of iterations (default: 100)
- `--tolerance FLOAT`: Convergence tolerance (default: 1e-6)
- `--rebuild`: Clear existing PageRank scores before building
- `--verbose`: Enable verbose logging
- `--threads N`: Number of threads for parallel database operations (default: 48)
- `--db-read-workers N`: Number of parallel workers for reading links (default: 48)
- `--db-write-workers N`: Number of parallel workers for writing scores (default: 48)
- `--batch-size N`: Batch size for database operations (default: 1000)
- `--limit N`: Limit number of links to process (for testing)
- `--profile`: Enable detailed profiling with cProfile

**Performance Characteristics:**
- **Small datasets (1k articles)**: 1.7-2x speedup over baseline
- **Medium datasets (10k articles)**: 2-2.5x speedup over baseline
- **Large datasets (100k+ articles)**: 2.5-3.5x speedup over baseline
- **Memory usage**: Scales linearly with dataset size
- **Database load**: Optimized with parallel operations

**Optimization Features:**
- **Parallel Graph Loading**: ID range-based batching with ThreadPoolExecutor (2-4x speedup)
- **Parallel Storage**: Multi-threaded PostgreSQL COPY operations (2-3x speedup)
- **Connection Management**: Each thread gets its own database connection
- **Index Optimization**: Drop indexes before writes, rebuild after
- **Auto-scaling**: Smart worker count selection based on dataset size

## QA Dataset Commands

### Generate QA Dataset

Generate question-answering dataset for LLM training from HotpotQA data:

```bash
# Test on toy dataset first
python wiki_search/manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1_toy.json \
  --output-dir data/processed \
  --context-sizes 8000 32000 128000 \
  --verbose

# Process full dataset (when available)
python wiki_search/manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --context-sizes 8000 32000 128000 \
  --workers 8 \
  --limit 1000
```

**Options:**
- `--input PATH`: Path to HotpotQA JSON file (default: data/raw/hotpot_dev_fullwiki_v1.json)
- `--output-dir PATH`: Directory for output files (default: data/processed)
- `--context-sizes N1 N2 N3`: Context size limits in tokens (default: 8000 32000 128000)
- `--limit N`: Limit number of entries to process (for testing)
- `--workers N`: Number of worker processes (default: CPU count)
- `--verbose`: Enable verbose logging

**Output Files:**
- `qa_dataset_8000.json` - entries with context_size ≤ 8k tokens
- `qa_dataset_32000.json` - entries with context_size ≤ 32k tokens
- `qa_dataset_128000.json` - entries with context_size ≤ 128k tokens

**Processing Logic:**
1. **Supporting Documents**: Extract articles from database using exact title matching from `supporting_facts`
2. **Distractor Documents**: Use hybrid search (TF-IDF + PageRank) with supporting fact titles as queries, excluding supporting docs
3. **Context Filtering**: Skip entries where supporting docs alone exceed context limits
4. **Token Counting**: Use GPT tokenizer (tiktoken cl100k_base) for LLM-compatible token counting
5. **Output Generation**: Create separate files for each context size with appropriate filtering

**Performance Characteristics:**
- **Multiprocessing**: Uses all CPU cores by default for parallel processing
- **Token Counting**: ~50,000 tokens/second using GPT tokenizer
- **Search Performance**: Uses hybrid search (TF-IDF + PageRank) with inverted index for better quality distractor documents
- **Memory Efficient**: Streams processing to handle large datasets
- **Progress Tracking**: Real-time progress bars and comprehensive logging
- **Speed**: ~5-6 seconds per entry with 8 workers (vs ~13-15 seconds sequential)

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

For detailed information, see [docs-vibe/0037-nltk-tfidf-refactor.md](docs-vibe/0037-nltk-tfidf-refactor.md).

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

**Generation Speed:**
- **Sequential processing**: ~13-15 seconds per entry
- **8 workers**: ~5-6 seconds per entry
- **Token counting**: ~50,000 tokens/second using GPT tokenizer

**Memory Usage:**
- Scales linearly with dataset size
- Streams processing to handle large datasets
- Uses ProcessPoolExecutor for optimal CPU utilization

## Documentation

For detailed implementation information, see the documentation in `docs-vibe/`:

- [docs-vibe/0040-pass2-threadpool-optimization.md](docs-vibe/0040-pass2-threadpool-optimization.md) - Pass 2 threadpool optimization details
- [docs-vibe/0027-pagerank-optimization.md](docs-vibe/0027-pagerank-optimization.md) - PageRank optimization details
- [docs-vibe/0029-multiprocessing-pagerank-feasibility.md](docs-vibe/0029-multiprocessing-pagerank-feasibility.md) - Multiprocessing PageRank analysis
- [docs-vibe/0031-qa-dataset-generation.md](docs-vibe/0031-qa-dataset-generation.md) - QA dataset generation
- [docs-vibe/0033-qa-dataset-hybrid-search.md](docs-vibe/0033-qa-dataset-hybrid-search.md) - QA dataset hybrid search
- [docs-vibe/0023-tokenizer-helper.md](docs-vibe/0023-tokenizer-helper.md) - Original tokenizer configuration
- [docs-vibe/0037-nltk-tfidf-refactor.md](docs-vibe/0037-nltk-tfidf-refactor.md) - NLTK TF-IDF refactor
- [docs-vibe/0022-tfidf-gpu-overhaul.md](docs-vibe/0022-tfidf-gpu-overhaul.md) - TF-IDF GPU overhaul
- [docs-vibe/0039-tfidf-gpu-overhaul-complete.md](docs-vibe/0039-tfidf-gpu-overhaul-complete.md) - TF-IDF GPU overhaul completion