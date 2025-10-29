## TF-IDF Architecture (CPU-Only)

This document summarizes the CPU-only TF-IDF indexing pipeline used in production.

### Components
- Tokenization: NLTK-based word tokenization and stopword filtering
- Vocabulary: global document frequency and IDF values
- Inverted Index: term → [(article_id, score)] postings

### Build Pipeline
1. Pass 1 (TF/DF collection)
   - Multiprocess tokenization with ProcessPool
   - Stream articles via Django `.iterator()` to bound memory
   - Accumulate per-article TF and global DF
2. Pass 2 (IDF + postings)
   - Compute IDF = log(N / df)
   - Build CSV buffers in parallel processes
   - COPY into PostgreSQL with a dedicated writer threadpool

### Parallelism Model
- CPU workers (Pass 1) for tokenization and TF maps
- CSV builders (Pass 2) via ProcessPool for CPU-bound serialization
- DB writer threads (Pass 2) for I/O-bound COPY operations
- Batch sizing chosen to overlap CPU and I/O for high throughput

### Operational Notes
- Rebuilds use TRUNCATE + VACUUM ANALYZE for fast, clean states
- Safe to run with `--limit` for smoke tests and profiling
- Verbose logging and `--profile` are supported

### Performance
- See 0104-benchmarks-summary.md for current throughput
- Detailed logs and iteration history preserved under `docs-vibe/archives/`


