# QA Profiling Command (profile_qa_generation)

## User intent (original words)
"Profile the QA dataset generation and reach 800 QA entries per second; test with 1000 entries. Align CLI with other scripts and remove default limits."

## Concise rephrasing
Add a profiling command that measures throughput and bottlenecks during QA dataset generation, targeting 800 entries/sec, tested with 1000 entries, and aligned CLI across commands.

## Usage

```bash
python manage.py profile_qa_generation \
  --input data/raw/hotpot_dev_fullwiki_v1.json \
  --output-dir data/processed \
  --context-sizes 8000 32000 128000 \
  --limit 1000 \
  --workers $(nproc) \
  --profile-db \
  --debug
```

## Notes
- No default for `--limit`; pass explicitly for testing.
- `--workers` defaults to available CPUs; override to experiment.
- When `--profile-db` is set, the command categorizes queries by Article, InvertedIndex, Vocabulary, PageRank, and Other.
- The command logs throughput and phase timing breakdown to help identify bottlenecks.
