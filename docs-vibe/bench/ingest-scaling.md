## Ingest Scaling (limit=10,000) - Report

This document captures results of scaling experiments for `load_wiki_dump.py` with `--limit=10000`.

### How to run

```bash
scripts/bench_ingest_scaling.sh
```

Outputs will be placed under `data/bench/ingest_<timestamp>/` with:
- `results.csv`: one row per run including elapsed and throughput
- Per-run logs and optional system metrics
- Recent cProfile files copied into each run folder

### Record your environment

- App host: LXC, same machine as Postgres
- Postgres: sibling LXC, 12 cores available
- Storage: high-IO disk array

### Paste results summary here

- Environment: app LXC → Postgres LXC on same host; fast disk array; Postgres 12 cores.
- Config: `--limit=10000`, `--batch-size=5000`, `--db-workers=12`, workers∈{1,2,4,8,12}, producer-threads∈{1,3}.

#### Throughput vs workers (producer-threads=3)

| workers | elapsed (s) | throughput (articles/s) |
|--------:|------------:|------------------------:|
| 2       | 138.94      | 71.97 |
| 4       | 133.49      | ~74.9 |
| 8       | 132.48      | 75.49 |
| 12      | 131.24      | 76.20 |

Observation: Minimal gains beyond 2 workers; plateau ~72–76 articles/s.

Producer threads=1 showed similar elapsed (±2–3%), confirming non-I/O-bound behavior for `--limit=10000`.

#### cProfile hotspots (ingestion phase)

- Dominant time in DB insert path:
  - `django.db.models.query._insert` / `execute_sql` / `backends.utils.execute`
  - `psycopg.connection.wait` and thread joins during DB writer pool shutdown
- Many seconds spent in `threading.join` / `concurrent.futures` waits (DB flush completion), indicating the pipeline stalls on DB writes.
- Object construction overhead for links is large (millions of rows), but overshadowed by DB execution time.

#### Why scaling stalls >2 cores

- The ingestion pipeline becomes DB-bound; adding more CPU workers doesn’t increase throughput once the DB writers saturate.
- Cross-process/task overhead shows up as waits (`as_completed`, `threading.wait`), but these are secondary to DB time.
- Increasing producer threads doesn’t help (disk is fast; decompression/JSON not the bottleneck at this limit).

#### Recommendations (next steps)

- Reduce DB insert overhead for links and articles:
  - Prefer server-side COPY (bulk import) for `InternalLink` and possibly `Article`.
  - Consider temporary UNLOGGED tables for ingest; build indexes after load.
  - Group larger batches per flush to reduce round-trips; or single writer with COPY.
- Decrease IPC pressure by letting workers write directly (COPY) or stream smaller chunks more frequently.
- Optionally raise `--db-workers` only if COPY is not used; otherwise 1–2 COPY streams may be optimal.

Data files: see `data/bench/<latest>/results.csv` and per-run `profiles/` for full details.


