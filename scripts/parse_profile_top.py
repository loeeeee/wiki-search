#!/usr/bin/env python3
import argparse
import pstats
from pathlib import Path


def summarize_profile(path: Path, top: int = 20) -> str:
    stats = pstats.Stats(str(path))
    stats.sort_stats(pstats.SortKey.CUMULATIVE)
    lines = [f"Top {top} cumulative functions for {path.name}:"]
    # Capture print_stats to a string by redirecting stream
    import io, sys
    buf = io.StringIO()
    old = stats.stream
    stats.stream = buf
    stats.print_stats(top)
    stats.stream = old
    lines.append(buf.getvalue())
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", type=Path, nargs="+", help=".prof files")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    for p in args.profile:
        if not p.exists():
            print(f"Missing: {p}")
            continue
        print(summarize_profile(p, args.top))


if __name__ == "__main__":
    main()


