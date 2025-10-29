"""Common profiling utilities for performance measurement."""

from __future__ import annotations

import cProfile
import logging
import os
import pstats
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil
from django.conf import settings

logger = logging.getLogger(__name__)


def get_memory_usage() -> float:
    """Get current memory usage in MB.
    
    Returns:
        Memory usage in megabytes
    """
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / 1024 / 1024


@contextmanager
def phase_timer(phase_name: str, verbose: bool = True):
    """Context manager for timing and memory tracking of a phase.
    
    Args:
        phase_name: Name of the phase being timed
        verbose: Whether to log phase timing info
        
    Yields:
        Dict with timing and memory stats (updated on exit)
    """
    stats: dict[str, Any] = {
        'phase': phase_name,
        'start_time': 0.0,
        'end_time': 0.0,
        'duration': 0.0,
        'start_memory_mb': 0.0,
        'end_memory_mb': 0.0,
        'memory_delta_mb': 0.0
    }
    
    start_time = time.perf_counter()
    start_memory = get_memory_usage()
    stats['start_time'] = start_time
    stats['start_memory_mb'] = start_memory
    
    if verbose:
        logger.info(f"Starting phase: {phase_name}")
        logger.info(f"  Initial memory usage: {start_memory:.2f} MB")
    
    try:
        yield stats
    finally:
        end_time = time.perf_counter()
        end_memory = get_memory_usage()
        duration = end_time - start_time
        memory_delta = end_memory - start_memory
        
        stats['end_time'] = end_time
        stats['end_memory_mb'] = end_memory
        stats['duration'] = duration
        stats['memory_delta_mb'] = memory_delta
        
        if verbose:
            logger.info(f"Completed phase: {phase_name}")
            logger.info(f"  Duration: {duration:.2f}s")
            logger.info(f"  Final memory usage: {end_memory:.2f} MB")
            logger.info(f"  Memory delta: {memory_delta:+.2f} MB")


class ProfileManager:
    """Manager for cProfile profiling with automatic file saving."""
    
    def __init__(self, profile_name: str, enabled: bool = True):
        """Initialize profile manager.
        
        Args:
            profile_name: Base name for profile files
            enabled: Whether profiling is enabled
        """
        self.profile_name = profile_name
        self.enabled = enabled
        self.profiler = cProfile.Profile() if enabled else None
        self.profile_dir = Path(settings.BASE_DIR).parent / "data" / "profiles"
        
        # Ensure profile directory exists
        if enabled:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
    
    def start(self) -> None:
        """Start profiling."""
        if self.enabled and self.profiler:
            self.profiler.enable()
            logger.info(f"Profiling started: {self.profile_name}")
    
    def stop(self) -> None:
        """Stop profiling."""
        if self.enabled and self.profiler:
            self.profiler.disable()
            logger.info(f"Profiling stopped: {self.profile_name}")
    
    def save(self, timestamp: str | None = None) -> tuple[Path, Path]:
        """Save profile data to files.
        
        Args:
            timestamp: Optional timestamp string for filename
            
        Returns:
            Tuple of (profile_file_path, summary_file_path)
        """
        if not self.enabled or not self.profiler:
            return Path(), Path()
        
        if timestamp is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
        
        # Save binary profile
        profile_file = self.profile_dir / f"{self.profile_name}_{timestamp}.prof"
        self.profiler.dump_stats(str(profile_file))
        logger.info(f"Profile data saved: {profile_file}")
        
        # Save human-readable summary
        summary_file = self.profile_dir / f"{self.profile_name}_{timestamp}.txt"
        with open(summary_file, 'w') as f:
            stats = pstats.Stats(self.profiler, stream=f)
            stats.strip_dirs()
            stats.sort_stats('cumulative')
            stats.print_stats(50)  # Top 50 functions
        logger.info(f"Profile summary saved: {summary_file}")
        
        return profile_file, summary_file
    
    def __enter__(self) -> ProfileManager:
        """Context manager entry."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.stop()

