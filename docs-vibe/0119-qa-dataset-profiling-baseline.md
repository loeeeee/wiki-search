# QA Dataset Profiling Baseline

## User intent (verbatim)
"Identify the performance issue with generate_qa_dataset.py. It now takes over an hour with new capping/dedup. Profile and benchmark first; do not optimize yet. Target: finish 7,405 in 5 minutes. Avoid concurrency for now. Single process cannot saturate a CPU core."

## Concise restatement
Add timing instrumentation and print default metrics to quantify where time is spent (preprocessing, search, distractor selection, context finalize, IO). Establish baseline throughput with representative limits (300, 1000), extrapolate to 7,405, and only then propose targeted optimizations.

## What was added (no logic changes)
- Default printed metrics in `generate_qa_dataset`: per-stage timing, throughput (entries/sec), ETA.
- More granular timing buckets: `preprocessing`, `search_operations`, `distractor_selection`, `context_finalize_estimation`, `context_finalize_exact`, `entry_total`, and per-file write time.
- Helper script `scripts/benchmark_generate_qa.py` to run subset benchmarks and summarize throughput, projecting ETA for 7,405 entries.

## How to run
```bash
# Inside nix-shell
uv sync
python wiki_search/manage.py generate_qa_dataset \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --limit 300 --profile --verbose

# Benchmark runner
python scripts/benchmark_generate_qa.py --input data/raw/hotpot_dev_fullwiki_v1.json --limits 300 1000
```

## Expected output (example fields)
- Processing Statistics: total, processed, skipped, errors
- Timing Statistics per bucket (total/avg/min/max)
- Processing time (entries loop), Throughput, ETA (when limited)
- Per-file write time

## Next steps
After collecting baseline numbers, identify the top two hotspots (likely search and context finalize re-tokenization) and propose surgical, single-thread optimizations.
