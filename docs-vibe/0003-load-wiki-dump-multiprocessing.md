# 0003 — load_wiki_dump multiprocessing refactor

## Summary

- Reworked the `load_wiki_dump` management command to use explicit worker signalling with a `ShardStatus` dataclass for checkpoint durability.
- Replaced the ad-hoc queue-empty polling with a controlled event/queue protocol that recognises `record`, `shard_done`, `partial_shard`, `shard_deferred`, and `worker_done` messages.
- Added graceful cancellation through a shared `Event`; workers now stop after ongoing work and report partial progress, allowing safe restarts.
- Normalised checkpoint data to persist completed, partial, and deferred shards, preventing false positives when resuming a run.

## Implementation notes

- `JoinableQueue` now feeds shard paths to workers; the main process owns ORM interactions while workers only parse and emit records.
- The new `ShardStatus` object centralises checklist state; `_save_checkpoint` takes this dataclass to serialise progress atomically.
- Workers emit explicit status messages so the writer loop no longer relies on `Queue.empty()` or `Process.is_alive()`.
- Partial or deferred shards are removed from the completed set before checkpoints are flushed, ensuring resumptions reprocess unfinished work.
- Limits (`--limit`) and shutdown signals set a stop event, triggering workers to finish quickly without losing inflight messages.

## Usage

```bash
uv run python manage.py load_wiki_dump --workers 6 --batch-size 5000
```

- Resume behaviour is automatic; the command reads `data/.load_checkpoint.json` and skips completed shards.
- Use `--clear-checkpoint` to force a fresh import or `--limit` to stage smaller batches.
- If interrupted, rerun the command; partial/deferred shards are logged and processed on the next execution.
