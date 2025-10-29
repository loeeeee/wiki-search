# Deterministic benchmark mode (default)

User intent (original): "add a determinstic testing mode so that I can compare results easily" and make it the default.

Concise implementation details:
- Deterministic by default using seed 42 unless `--randomize` is provided.
- Stable query generation from DB using ordered `Article.id` and local RNG (`random.Random(seed)`).
- Optional query I/O: `--queries-file` to load; `--save-queries` to persist generated queries.
- Optional results export: `--export-results` writes CSV with columns: `query,rank,article_id,title,score`.
- Seeds applied: Python `random`; optionally `numpy` and `torch` if available.

CLI usage examples:
```bash
# Deterministic run (default), seed=42
python manage.py benchmark_search --num-searches 500 --export-results /tmp/bench.csv

# Deterministic with custom seed
python manage.py benchmark_search --seed 123 --num-searches 200

# Load a fixed query set from file
python manage.py benchmark_search --queries-file ./queries.txt --export-results ./results.csv

# Save the generated queries for future reuse
python manage.py benchmark_search --save-queries ./queries.txt --num-searches 300

# Opt out of determinism (non-seeded randomness)
python manage.py benchmark_search --randomize --num-searches 300
```

Notes:
- Two deterministic runs with the same seed and unchanged DB yield identical query lists and exported CSVs.
- If `--queries-file` has fewer lines than `--num-searches`, the queries are cycled to match the requested count.

