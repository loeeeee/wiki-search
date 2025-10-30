"""
# QA Dataset Hybrid Search Evaluation (Task 0120)

## User Intent (verbatim)
"Your task is to manually evaluating the results of hybrid search function. Additionally, current search function returns too little results, and cannot fill up the 129k token limit, you also need to carefully address it.

You should profile and evaluate performance during the development. The script should process at least 5 query per second. You should test it with 20 query limit."

## Rephrased Objectives
- Inspect and improve the hybrid search usage inside `generate_qa_dataset.py`, focusing on distractor selection and context saturation for up to 129k tokens.
- Ensure the QA dataset generator can process at least five entries per second when running a 20-entry sample, with profiling artifacts saved under `data/profiling/`.
- Produce manual evaluation artifacts that help review supporting/distractor quality and search result adequacy.

## Current Understanding
- The command loads HotpotQA entries, pre-fetches supporting articles, then repeatedly calls `search_hybrid` to find distractor content.
- Distractor gathering stops early whenever search results are exhausted or token limits are reached, frequently leaving large context caps under-filled.
- Performance bottlenecks are concentrated in repeated hybrid search invocations and token counting for distractors.

## Implementation Plan
1. **CLI Enhancements**
   Add options controlling search breadth (`--search-limit`, `--max-candidates`), evaluation report output path, and profiling output destination (defaulting to `data/profiling/`).
2. **Distractor Expansion**
   Introduce adaptive tail padding: if hybrid search results are insufficient, relax constraints (e.g., fall back to TF–IDF-only mode or enlarge candidate pools) to approach the 129k-token goal. Filter out extremely short documents to maintain quality.
3. **Performance Instrumentation**
   Strengthen timing analytics (aggregate statistics, throughput check) and assert/warn when throughput falls below 5 entries/sec for runs with at least 20 entries. Retain tqdm progress.
4. **Manual Evaluation Output**
   Emit structured summaries (JSON/CSV/Markdown) describing supporting versus distractor docs, token counts, and hybrid scores for sampled entries to aid manual inspection.
5. **Profiling Integration**
   When profiling is enabled, persist `cProfile` stats and a textual summary under `data/profiling/qa_dataset_generation.*`.

## Performance & Quality Goals
- Throughput ≥ 5 QA entries per second on a 20-entry limit run.
- Achieve near-saturation of the 128k context cap (allowing headroom for metadata) without sacrificing relevance.
- Capture profiling artifacts and manual evaluation reports for downstream review.

## Next Steps
- Implement CLI and internal adjustments in `generate_qa_dataset.py`.
- Run benchmark with `--limit 20` under profiling to verify throughput target and gather evidence.
- Update this document and README after measurements, summarizing observed metrics and evaluation findings.
"""
