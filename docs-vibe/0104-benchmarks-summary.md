## Benchmarks Summary

Concise metrics for the current CPU-focused system. Detailed runs and profiling are preserved in `docs-vibe/archives/`.

### Environment
- CPU: multi-core Linux (see repo README for setup)
- DB: PostgreSQL
- Tokenizers: NLTK (search), tiktoken (QA)

### TF-IDF Build Throughput
- Optimized (10k articles): ~451 articles/sec (total ~22.2s)
- Notes: CSV joining + COPY I/O overlap; see `archives/0046-...`, `archives/0052-...`, `archives/0053-...`

### Query Performance
- Relevance: TF-IDF cosine with precomputed norms
- Authority: PageRank blending (hybrid)
- Latency: dominated by DB fetch; keep results window small and index coverage high

### PageRank Build
- Parallel readers/writers, batched COPY
- Runtime scales with edge count and batch size
- See `archives/0027-...` and `archives/0028-...` for iteration counts and timings

### QA Generation Throughput
- ~5–6 seconds per entry with 8 workers (I/O bound)
- Token counting: ~50k tokens/sec

### Notes
- Use `--limit` for smoke tests and `--profile` for timing
- For system-specific tuning, consult `shell.nix` and DB parameters


