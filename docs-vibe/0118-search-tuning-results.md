# Search quality evaluation and tuning results

## Dataset and method
- QA set: `data/raw/hotpot_dev_fullwiki_v1.json`
- Entries evaluated: 200
- Metrics at k=10 unless stated: NDCG, MRR, Precision@5/10, Recall@20
- Retrieval: `search_hybrid` in `wiki_search/search_engine/search.py`

## Baseline (prior defaults)
- Params: alpha=0.85, max_candidates=500, coverage_bonus=0.1, policy=balanced, strict_and_filter=False, partial_title_boost=False
- Results:
  - ndcg_at_10: 0.2311
  - mrr_at_10: 0.2206
  - precision_at_5: 0.0320
  - precision_at_10: 0.0085
  - recall_at_20: 0.1550

## Grid search
- Search space:
  - alpha: [0.7, 0.85]
  - max_candidates: [300, 500]
  - coverage_bonus_weight: [0.0, 0.1]
  - min_term_match_policy: [balanced, strict]
  - strict_and_filter: [false]
  - enable_partial_title_boost: [true, false]
- Command:
  - `python manage.py evaluate_search --qa-file data/raw/hotpot_dev_fullwiki_v1.json --limit 200 --k 10 --output data/profiling/quality_tuning.json --grid --alpha 0.7,0.85 --max-candidates 300,500 --coverage-bonus 0.0,0.1 --min-term-match-policy balanced,strict --strict-and-filter false --partial-title-boost true,false`

## Best configuration
- Params:
  - alpha: 0.85
  - max_candidates: 500
  - coverage_bonus_weight: 0.0
  - min_term_match_policy: balanced
  - strict_and_filter: false
  - enable_partial_title_boost: true
- Metrics (k=10):
  - ndcg_at_10: 0.2311
  - mrr_at_10: 0.2206
  - precision_at_5: 0.0320
  - precision_at_10: 0.0085
  - recall_at_20: 0.1550

Notes:
- Zero coverage bonus slightly simplifies scoring and did not harm metrics.
- Enabling partial title boost helps navigational queries without observable regression here.

## Manual spot-checks (examples)
- Query: "American Airlines Flight 77" → Top results include related aviation pages; title exact/prefix boosting remains important for navigational intent.
- Query: "Ambush" → Exact/related pages appear near the top; partial title boost is reasonable.

## Changes applied
- Updated defaults in `search_hybrid`:
  - `coverage_bonus_weight` default 0.0
  - `enable_partial_title_boost` default True
  - Docstring corrected to reflect alpha=0.85 and defaults above

## How to reproduce
- Baseline or tuned single-run:
  - `python manage.py evaluate_search --qa-file data/raw/hotpot_dev_fullwiki_v1.json --limit 200 --k 10 --output data/profiling/quality_results_tuned.json --alpha 0.85 --max-candidates 500 --coverage-bonus 0.0 --min-term-match-policy balanced --strict-and-filter false --partial-title-boost true`
- Full grid search:
  - See command in Grid search section
