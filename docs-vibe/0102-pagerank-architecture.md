## PageRank Architecture (CPU)

PageRank computes authority scores over the internal Wikipedia link graph.

### Pipeline
1. Graph Extraction
   - Read `InternalLink` edges in ID-batched ranges
   - Parallel readers reduce database latency
2. Iterative Computation
   - Standard damping model with configurable tolerance and max iterations
   - Vector iteration with periodic persistence to database
3. Storage
   - COPY-based bulk writes in batches
   - Index management to speed writes (drop-before-write, rebuild-after)

### Parallelism and I/O
- Threaded readers for edges
- Threaded writers for batched PageRank scores
- Batched commits for throughput and reduced lock contention

### Operations
- `--rebuild` clears existing scores
- `--limit` supports smoke tests
- `--profile` enables detailed timing

### Performance
- See 0104-benchmarks-summary.md for runtimes and scaling
- Historical optimization notes are available in `docs-vibe/archives/`


