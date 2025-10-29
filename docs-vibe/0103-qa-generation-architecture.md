## QA Generation Architecture

Generates a QA dataset from HotpotQA with supporting/distractor documents pulled from the local search engine.

### Flow
1. Input parsing (HotpotQA JSON)
2. Supporting facts → exact-title article lookups
3. Distractors → hybrid search (TF-IDF relevance + PageRank authority)
4. Token counting (tiktoken) for context-size buckets
5. Streaming writes to output JSON files per context size

### Parallelism
- Multiprocessing for per-entry processing
- Bounded memory via streaming and batched DB access

### Options
- `--context-sizes`: token cutoffs (e.g., 8000 32000 128000)
- `--limit`: subset for smoke tests
- `--workers`: process count
- `--verbose`, `--profile`: diagnostics

### Performance
- See 0104-benchmarks-summary.md for throughput
- Detailed measurements in `docs-vibe/archives/`


