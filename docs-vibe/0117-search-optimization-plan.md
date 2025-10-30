# Search Optimization Implementation Notes (PostgreSQL, NDCG@10)

## Intent (user's words)

"Implement the plan to improve search quality in `search.py`, profile and evaluate bottlenecks, and sustain 20 searches/sec. Optimize for balanced NDCG@10."

## Concise plan recap

- Instrument `search_hybrid()` with phase timings (debug-gated)
- Default `min_term_match=2` for multi-term; concave coverage bonus via log1p
- Keep TF-IDF + PageRank blend (`alpha=0.85`), candidate-based normalization
- Hooks for global PR stats cache (optional)
- Add `benchmark_search` and `evaluate_search` commands; store results under `data/profiling/`
- Verify Postgres indexes separately

## Usage

- Benchmark:
  - `python manage.py benchmark_search --queries data/queries.txt --workers 1 --iterations 1`
  - Outputs JSON to `data/profiling/`
- Evaluation (NDCG@10 primary):
  - `python manage.py evaluate_search --limit 200`
  - Outputs JSON to `data/profiling/`

## Notes

- Changes are parameter-guarded; defaults preserve performance goals
- Title boost beyond exact-match kept gated to avoid extra re-sorts


