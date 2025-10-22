#!/usr/bin/env bash
set -euo pipefail

# Benchmark harness for load_wiki_dump scaling tests
# - Runs sweeps over workers and producer-threads with --limit=10000
# - Captures logs and aggregates key metrics into a CSV
#
# Usage:
#   scripts/bench_ingest_scaling.sh [--dry-run]
#
# Requirements:
#   - Django project with manage.py in repo root
#   - Optional tools: pidstat, iostat, vmstat (metrics capture auto-skips if missing)

ROOT_DIR="$(cd "$(dirname "$0")"/.. && pwd)"
cd "$ROOT_DIR"

# Locate manage.py (project structure keeps it under wiki_search/manage.py)
if [[ -f manage.py ]]; then
  MANAGE="manage.py"
elif [[ -f wiki_search/manage.py ]]; then
  MANAGE="wiki_search/manage.py"
else
  echo "manage.py not found (checked $ROOT_DIR and $ROOT_DIR/wiki_search)." >&2
  exit 1
fi

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# Config
LIMIT=${LIMIT:-10000}
BATCH_SIZE=${BATCH_SIZE:-5000}
DB_WORKERS=${DB_WORKERS:-12}
WORKERS_LIST=${WORKERS_LIST:-"1 2 4 8 12"}
PRODUCER_THREADS_LIST=${PRODUCER_THREADS_LIST:-"1 3"}

# Multi-scale testing
SCALE_LIMITS=${SCALE_LIMITS:-"10000 100000 500000 800000"}
ENABLE_DB_MONITORING=${ENABLE_DB_MONITORING:-true}

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="$ROOT_DIR/data/bench/ingest_$TS"
mkdir -p "$OUT_DIR"

RESULTS_CSV="$OUT_DIR/results.csv"
echo "timestamp,workers,db_workers,producer_threads,batch_size,limit,elapsed_s,articles_created,dups_skipped,links_created,throughput_articles_per_s,log_path,db_monitor_log" > "$RESULTS_CSV"

have_cmd() { command -v "$1" >/dev/null 2>&1; }

# Choose runner: prefer uv if present to ensure deps are resolved
RUNNER_PREFIX=()
if have_cmd uv; then
  RUNNER_PREFIX=(uv run)
fi

for LIMIT in $SCALE_LIMITS; do
  for PRODUCER_THREADS in $PRODUCER_THREADS_LIST; do
    for WORKERS in $WORKERS_LIST; do
    RUN_TAG="limit${LIMIT}_w${WORKERS}_db${DB_WORKERS}_pt${PRODUCER_THREADS}"
    RUN_DIR="$OUT_DIR/$RUN_TAG"
    mkdir -p "$RUN_DIR"

    LOG_FILE="$RUN_DIR/run.log"
    DB_MONITOR_LOG="$RUN_DIR/db_monitor.log"
    SYS_CPU_FILE="$RUN_DIR/pidstat_cpu.log"
    SYS_IO_FILE="$RUN_DIR/iostat.log"
    SYS_VMSTAT_FILE="$RUN_DIR/vmstat.log"

    echo "===== RUN $RUN_TAG ====="
    echo "  limit=$LIMIT batch_size=$BATCH_SIZE workers=$WORKERS db_workers=$DB_WORKERS producer_threads=$PRODUCER_THREADS"

    # Background system metrics (if available)
    METRICS_PIDS=()
    if have_cmd pidstat; then
      pidstat -durhl 1 > "$SYS_CPU_FILE" 2>&1 & METRICS_PIDS+=($!)
    fi
    if have_cmd iostat; then
      iostat -dx 1 > "$SYS_IO_FILE" 2>&1 & METRICS_PIDS+=($!)
    fi
    if have_cmd vmstat; then
      vmstat 1 > "$SYS_VMSTAT_FILE" 2>&1 & METRICS_PIDS+=($!)
    fi
    
    # Background database monitoring (if enabled)
    if [[ "$ENABLE_DB_MONITORING" == "true" ]]; then
      echo "Starting database monitoring..."
      ${RUNNER_PREFIX[@]} python scripts/monitor_postgres.py --interval=5 --output="$DB_MONITOR_LOG" &
      DB_MONITOR_PID=$!
      METRICS_PIDS+=($DB_MONITOR_PID)
      sleep 2  # Give monitor time to start
    fi

    CMD=(${RUNNER_PREFIX[@]} python "$MANAGE" load_wiki_dump \
      --limit="$LIMIT" \
      --batch-size="$BATCH_SIZE" \
      --workers="$WORKERS" \
      --db-workers="$DB_WORKERS" \
      --producer-threads="$PRODUCER_THREADS" \
      --profile)

    echo "Running: ${CMD[*]}"
    if $DRY_RUN; then
      echo "(dry-run) Skipping execution" | tee "$LOG_FILE"
    else
      # Capture wallclock timings around execution as well
      START_TS=$(date +%s)
      set +e
      "${CMD[@]}" 2>&1 | tee "$LOG_FILE"
      EXIT_CODE=${PIPESTATUS[0]}
      set -e
      END_TS=$(date +%s)
      ELAPSED=$((END_TS-START_TS))
    fi

    # Stop metrics
    for p in "${METRICS_PIDS[@]}"; do
      kill "$p" >/dev/null 2>&1 || true
    done
    
    # Wait a moment for monitoring to flush
    sleep 2

    # Extract metrics from log
    # Looking for lines emitted by the command's logger at the end:
    #   OVERALL EXECUTION TIME: X.XX seconds
    #   Throughput: YYY.YY articles/second
    #   Created A new articles, skipped B dups, created C links; resolved ...

    if [[ -f "$LOG_FILE" ]]; then
      ELAPSED_LINE=$(grep -E "OVERALL EXECUTION TIME" "$LOG_FILE" | tail -n1 || true)
      THROUGHPUT_LINE=$(grep -E "Throughput: .* articles/second" "$LOG_FILE" | tail -n1 || true)
      SUMMARY_LINE=$(grep -E "Created [0-9]+ new articles, skipped .* dups, created .* links;" "$LOG_FILE" | tail -n1 || true)

      # Defaults if parsing fails
      ELAPSED_VAL="${ELAPSED:-}"
      THROUGHPUT_VAL=""
      CREATED_VAL=""
      SKIPPED_VAL=""
      LINKS_VAL=""

      if [[ -n "$ELAPSED_LINE" ]]; then
        # Extract float before ' seconds'
        ELAPSED_VAL=$(echo "$ELAPSED_LINE" | awk '{print $(NF-1)}')
      fi
      if [[ -n "$THROUGHPUT_LINE" ]]; then
        THROUGHPUT_VAL=$(echo "$THROUGHPUT_LINE" | awk '{print $2}')
      fi
      if [[ -n "$SUMMARY_LINE" ]]; then
        # Created A new articles, skipped B dups, created C links; ...
        CREATED_VAL=$(echo "$SUMMARY_LINE" | sed -E 's/.*Created ([0-9]+) new.*/\1/')
        SKIPPED_VAL=$(echo "$SUMMARY_LINE" | sed -E 's/.*skipped ([0-9]+) dups.*/\1/')
        LINKS_VAL=$(echo "$SUMMARY_LINE" | sed -E 's/.*created ([0-9]+) links.*/\1/')
      fi

      echo "$(date -Is),$WORKERS,$DB_WORKERS,$PRODUCER_THREADS,$BATCH_SIZE,$LIMIT,$ELAPSED_VAL,$CREATED_VAL,$SKIPPED_VAL,$LINKS_VAL,$THROUGHPUT_VAL,$LOG_FILE,$DB_MONITOR_LOG" >> "$RESULTS_CSV"
    fi

    # Persist cProfile outputs
    PROFILE_DIR="$ROOT_DIR/data/profiles"
    if [[ -d "$PROFILE_DIR" ]]; then
      mkdir -p "$RUN_DIR/profiles"
      # Move only profiles from this time window (best effort: most recent files)
      find "$PROFILE_DIR" -maxdepth 1 -type f -printf "%T@ %p\n" | sort -nr | head -n 4 | awk '{print $2}' | while read -r f; do
        cp -n "$f" "$RUN_DIR/profiles/" || true
      done
    fi

    done
  done
done

echo "Results saved to: $RESULTS_CSV"
echo "Per-run logs under: $OUT_DIR"


