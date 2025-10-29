## Project Overview

This repository builds a Wikipedia search system with three CPU-focused components:

- TF-IDF indexing: vocabulary, inverted index, and query-time scoring
- PageRank: authority scores from the internal link graph
- QA dataset generation: structured data for LLM training and evaluation

The current emphasis is CPU-only performance, stability, and operational simplicity. Historical GPU work and verbose development logs are preserved in `docs-vibe/archives/` and referenced from the concise docs below.

### Documentation Map
- TF-IDF architecture: 0101-tfidf-architecture.md
- PageRank architecture: 0102-pagerank-architecture.md
- QA generation architecture: 0103-qa-generation-architecture.md
- Benchmarks summary: 0104-benchmarks-summary.md
- Search benchmark: 0106-search-benchmark.md
- Hybrid search implementation: 0108-hybrid-search-implementation.md
- Search quality improvements: 0116-search-quality-improvements.md

### Operational Highlights
- End-to-end rebuilds are fast and non-destructive to code; TRUNCATE-based rebuilds for TF-IDF reduce downtime.
- Multiprocess tokenization + concurrent CSV/DB pipelines deliver strong throughput on commodity CPUs.
- PageRank uses parallel database operations and batched I/O.

See `docs-vibe/archives/` for detailed engineering logs and profiling sessions.


