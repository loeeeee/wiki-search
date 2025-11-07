# QA Dataset Hybrid Command (Task 0121)

## User Intent (verbatim)
"Your task is to create a QA dataset for LLM training. The resulting data is in the format of JSON. It follows this schema,
{
  \"id\": \"string\",
  \"question\": \"string\",
  \"gold_answer\": \"string\",
  \"supporting_docs\": [{ \"title\": \"string\", \"text\": \"string\" }, { \"title\": \"string\", \"text\": \"string\" }],
  \"distractor_docs\": [{ \"title\": \"string\", \"text\": \"string\" }, ...],
  \"context_size\": \"int\"
}

The dataset you are working with is (1) hotpot_dev_fullwiki_v1.json  and (2) the wikipedia search engine we just developed.

To create the desired dataset, we have the following procedures.
(1) From hotpot_dev_fullwiki_v1.json , we get the \"id\", \"question\", \"gold_answer\" of the resulting dataset.
(2) Based on the \"title\" of the supporting_facts, we extract the full length article of the corresponding wikipedia article using the search engine. Then, we store it in \"supporting_docs\" of the resulting dataset.
(3) Based on the title of the \"supporting_facts\", we do a hybrid search in the wikipedia article, and get the top n best matching articles whose total article length hits 8k, 32k, 128k tokens. We put these articles in \"distractor_docs\".
(4) Summing up of the token number of supporting docs and distractor docs, we get \"context_size\".

Details:
Token calculation: we use the gpt tokenizer. We need to take title into consideration.
Token number capping: the sum of tokens for all articles is the context size. We will produce multiple resulting dataset based on different capping value.

During the development, you need to constantly monitoring script performance, and use profiling to identify the bottleneck. The goal is to achieve a 20 article per second speed in our first single threaded and single processed script."

"Your task is to create a new script from scratch."

"Sorry I mean a Django command."

## Rephrased Objectives
- Build a brand-new Django management command dedicated to generating QA datasets for LLM training, consuming `hotpot_dev_fullwiki_v1.json` plus the existing Wikipedia search engine.
- Produce JSON outputs conforming to the specified schema, with context caps at 8k, 32k, and 128k GPT tokens (title-inclusive), and verify `context_size` matches actual tokens used.
- Retrieve supporting documents by title directly from the database and gather distractors through hybrid search, expanding until each context cap is saturated without exceeding it.
- Instrument the command for fail-fast execution, comprehensive logging, tqdm progress, and profiling hooks that sustain a minimum throughput of 20 QA items per second in single-threaded mode.
- Persist profiling artifacts and evaluation summaries under `data/profiling/`, while keeping implementation modular with dataclasses, typing, and helper functions.

## Existing State & Gaps
- `generate_qa_dataset.py` currently implements similar functionality but mixes multiple concerns and lacks the from-scratch architecture the user now requests.
- The new command will replace reuse of legacy logic with a redesigned flow emphasizing modular components, explicit configuration dataclasses, and clearer performance instrumentation.
- Supporting utilities (`qa_helpers.py`, `search.py`) remain relevant; however, we must re-evaluate their usage to ensure they integrate cleanly with the new command without dragging legacy bottlenecks.

## Implementation Summary
1. **Command Scaffolding**
   - Added `search_engine/management/commands/build_qa_dataset.py` with dataclass-driven configuration, structured logging to both console and file, and tqdm progress feedback.
   - Exposed CLI flags covering input/output paths, context caps, search tuning, fallback behaviour, profiling toggles, evaluation sampling, and throughput guardrails.

2. **Processing Pipeline**
   - Load HotpotQA entries, deduplicate supporting titles, and batch-fetch corresponding articles via the ORM into an `ArticleCache` that tracks GPT token counts (title-inclusive).
   - For each QA instance, execute primary hybrid searches seeded by supporting titles, then adaptive fallbacks (question, question+answer, concatenated titles) until context caps are saturated or candidates exhausted.
   - Assemble context-specific outputs by slicing distractors per cap (8k/32k/128k tokens), emitting schema-compliant JSON entries with accurate `context_size` accounting.

3. **Performance & Profiling**
   - Captured per-entry timings (search, selection, context finalisation) and enforce throughput monitoring with warnings when falling below the 20 entries/sec single-threaded target for sufficiently large runs.
   - Integrated `ProfileManager` to persist optional cProfile artifacts under `data/profiling/` (binary `.prof` plus human-readable summaries).

4. **Outputs & Reporting**
   - Generate one JSON dataset per requested context cap. Evaluation report generation is optional and currently suppressed during smoke testing; structure is ready for future quality assessments.

## Validation
- Command executed via
  `nix-shell --command "uv run python wiki_search/manage.py build_qa_dataset --limit 5 --no-evaluation-report --profile-name build_qa_dataset_smoketest"`
- Processed 5 HotpotQA entries without errors.
- Throughput observed at ~0.17 entries/sec for the small sample; the run is dominated by hybrid search latency and limited data parallelism. The guardrail warning will surface automatically on larger batches if the 20 entries/sec target is not met. Further optimisation (e.g., caching or lighter fallback strategies) will be required to reach the goal.
- Output datasets written to:
  - `data/processed/qa_dataset_8000.json`
  - `data/processed/qa_dataset_32000.json`
  - `data/processed/qa_dataset_128000.json`
- Profiling artifacts produced under `data/profiling/` according to the selected profile name.

## Next Steps
- Investigate hybrid search batching and caching to improve throughput toward the 20 entries/sec target.
- Expand evaluation reporting once distractor quality review commences.
- Re-run the command on ≥20-entry batches after performance tuning and capture comparative profiling data.

This document now reflects the implemented command, validation results, and pending optimisation work.
