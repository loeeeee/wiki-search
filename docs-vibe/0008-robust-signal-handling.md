# 0008 — Robust Signal Handling for load_wiki_dump

## Summary

Refactored the `load_wiki_dump` management command to implement robust signal handling that ensures all processes (main script, worker processes, and subprocesses) terminate cleanly and quickly when Ctrl+C is pressed. The new implementation preserves resume functionality while providing multiple levels of shutdown response.

## Problem Solved

The previous implementation had several issues with signal handling:

1. **Subprocess not handled**: `subprocess.run()` for tar extraction didn't propagate signals
2. **Worker processes**: Daemon processes didn't respond quickly to termination signals
3. **No process group management**: Child processes survived parent termination
4. **Weak signal handling**: Workers only checked `stop_event` periodically, causing delays

## Solution Implementation

### Multi-Level Signal Response

The new signal handler provides three levels of response:

1. **First Ctrl+C**: Graceful shutdown (5 seconds)
   - Sets `stop_event` to signal workers
   - Workers finish current shard and report status
   - Checkpoint is saved with accurate state
   - All processes terminate cleanly

2. **Second Ctrl+C**: Forceful termination (2 seconds)
   - Immediately terminates all processes with SIGTERM
   - Bypasses graceful shutdown phase
   - Still attempts to save checkpoint

3. **Third Ctrl+C**: Emergency exit
   - Calls `os._exit(1)` for immediate termination
   - Used as last resort

### Process Management Improvements

#### Subprocess Handling
- Replaced `subprocess.run()` with `subprocess.Popen()` for better control
- Added process group management via `preexec_fn=os.setpgrp`
- Implemented timeout and proper cleanup in exception handlers
- Subprocess reference is stored for explicit termination

#### Worker Process Management
- Removed `daemon=True` to have explicit control over process lifecycle
- Added signal handlers to worker processes themselves
- Implemented faster exit checks during long-running operations
- Workers now respond to SIGTERM and SIGINT directly

#### Process Tracking
- All worker processes are tracked in `self.worker_processes`
- Extraction subprocess is tracked in `self.extraction_subprocess`
- Thread-safe shutdown with `threading.Lock()`

### Cleanup Sequence

The new `_cleanup_processes()` method implements a three-phase cleanup:

1. **Graceful Phase (5 seconds)**:
   - Set stop_event for workers
   - Wait for workers to finish current work
   - Process remaining messages in result_queue
   - Save checkpoint with accurate state

2. **Forceful Phase (2 seconds)**:
   - Send SIGTERM to all worker processes
   - Terminate extraction subprocess
   - Wait for processes to terminate

3. **Final Phase (1 second)**:
   - Send SIGKILL to any remaining processes
   - Clean up process references

### Resume Functionality Preservation

The new implementation **improves** resume functionality:

- Checkpoint is saved **after** workers report final status but **before** forceful termination
- This eliminates race conditions between worker termination and checkpoint save
- Partial shards are accurately tracked and will be reprocessed on next run
- No data loss occurs during graceful shutdown

## Usage

The command behavior remains the same for normal operation:

```bash
uv run python manage.py load_wiki_dump --workers 6 --batch-size 5000
```

### Signal Handling Behavior

- **Single Ctrl+C**: Graceful shutdown with checkpoint save
- **Double Ctrl+C**: Force immediate termination
- **Triple Ctrl+C**: Emergency exit (use only if stuck)

### Testing the Implementation

1. **Normal graceful shutdown**:
   ```bash
   # Start the command, then press Ctrl+C once
   uv run python manage.py load_wiki_dump --workers 4
   # Should complete current shard and save checkpoint
   ```

2. **Force termination**:
   ```bash
   # Start the command, press Ctrl+C twice quickly
   # Should terminate immediately without waiting
   ```

3. **Resume after interruption**:
   ```bash
   # After Ctrl+C, restart the command
   # Should resume from checkpoint and skip completed shards
   ```

4. **Subprocess termination**:
   ```bash
   # Start with --force-decompress to trigger tar extraction
   # Press Ctrl+C during extraction - should terminate tar process
   ```

## Technical Details

### New Methods Added

- `_cleanup_processes()`: Robust process termination with graceful/forceful phases
- `_force_cleanup()`: Immediate termination of all processes
- `_register_cleanup()`: Register atexit handler to prevent orphaned processes

### Modified Methods

- `_signal_handler()`: Multi-level signal response with signal counting
- `_fast_extract_with_system_tar()`: Process group management and subprocess tracking
- `worker_loop()`: Signal handlers and faster exit checks
- `handle()`: Registration of cleanup handlers

### Process Group Management

- Main script creates process group for all child processes
- Subprocesses use `preexec_fn=os.setpgrp` to join process group
- Signals are sent to entire process groups, not individual processes
- This ensures no orphaned processes remain after termination

## Benefits

1. **Faster response**: Ctrl+C now stops the script within 5 seconds instead of hanging
2. **No orphaned processes**: All child processes are properly terminated
3. **Preserved resume**: Checkpoint functionality is maintained and improved
4. **Robust cleanup**: Multiple fallback mechanisms ensure processes don't hang
5. **Better user experience**: Clear feedback on shutdown progress

## Notes

- The implementation is Unix-focused but includes Windows compatibility checks
- Process group management is disabled on Windows (`os.name != 'nt'`)
- All timeouts are configurable and can be adjusted if needed
- The graceful shutdown timeout (5 seconds) balances responsiveness with data integrity
