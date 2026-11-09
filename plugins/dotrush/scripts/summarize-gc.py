#!/usr/bin/env python3
"""Summarize the garbage collections in the 'gc' object of a `dotnet-trace convert --format Json` file."""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import sys
from pathlib import Path


def load_runtime_version():
    # The runtime line comes from the .nettrace exactly as the CPU and allocation reports read it.
    path = Path(__file__).with_name("summarize-speedscope.py")
    spec = importlib.util.spec_from_file_location("summarize_speedscope", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.runtime_version


def percentile(ordered: list[float], share: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def ms(value: float) -> str:
    return f"{value:.2f} ms"


def gc_mode(process: dict) -> str:
    heaps = process.get("heapCount") or 0
    parts = []
    if process.get("serverGC") is True:
        parts.append(f"server, {heaps} heaps" if heaps else "server")
    else:
        # TraceEvent only ever detects the server GC; without such an event the process ran the workstation GC.
        parts.append("workstation" + (f", {heaps} heap" + ("s" if heaps != 1 else "") if heaps else ""))
    concurrent = process.get("concurrent")
    if concurrent is not None:
        parts.append("concurrent (background gen2)" if concurrent else "non-concurrent")
    return ", ".join(parts)


def print_process(process: dict, limit: int) -> None:
    collections_ = process.get("collections", [])
    complete = [gc for gc in collections_ if gc.get("complete", True)]
    duration = float(process.get("durationMSec") or 0)
    pauses = sorted(float(gc["pauseMSec"]) for gc in complete)
    total_pause = sum(pauses)
    generations = collections.Counter(int(gc["generation"]) for gc in collections_)
    induced = sum(1 for gc in collections_ if gc.get("reason", "").startswith("Induced"))
    background = sum(1 for gc in collections_ if gc.get("type") == "BackgroundGC")

    print(f"Process\t{process.get('name') or 'unknown'} (pid {process.get('processId', '?')})")
    print(f"GCMode\t{gc_mode(process)}")
    hard_limit = process.get("hardLimitBytes") or 0
    if hard_limit > 0:
        print(f"HardLimit\t{hard_limit / (1024 * 1024):.2f} MB")
    print(f"CaptureDuration\t{ms(duration)}")
    by_generation = ", ".join(f"gen{gen} {generations[gen]}" for gen in sorted(generations))
    print(
        f"Collections\t{len(collections_)} ({by_generation}; induced {induced}; background {background}; "
        f"cut off by the capture {len(collections_) - len(complete)})"
    )
    if pauses:
        share = f" ({total_pause * 100 / duration:.2f}% of the capture)" if duration else ""
        print(f"PauseTotal\t{ms(total_pause)}{share}")
        print(
            f"Pause\tp50 {ms(percentile(pauses, 0.5))}, p95 {ms(percentile(pauses, 0.95))}, "
            f"p99 {ms(percentile(pauses, 0.99))}, max {ms(pauses[-1])}"
        )
    allocated = sum(float(gc.get("allocatedSinceLastMB") or 0) for gc in collections_)
    if allocated > 0:
        rate = f", {allocated * 1000 / duration:.2f} MB/s" if duration else ""
        print(f"AllocatedBetweenGCs\t{allocated:.2f} MB{rate}")
    if complete:
        after = [float(gc["heapAfterMB"]) for gc in complete]
        print(
            f"HeapAfterGC\tfirst {after[0]:.2f} MB, last {after[-1]:.2f} MB, "
            f"min {min(after):.2f} MB, max {max(after):.2f} MB"
        )
        print(f"HeapPeakBeforeGC\t{max(float(gc['heapBeforeMB']) for gc in complete):.2f} MB")
    print(
        "Measure\tPause is the time the runtime was suspended for a collection, from the start of the suspension "
        "to the end of the restart: what every managed thread waited. A background gen2 runs mostly beside the "
        "application, so its Duration far exceeds its Pause. Collections the capture cut off are counted but left "
        "out of the pause and heap figures. CaptureDuration spans the process's first to last event, startup "
        "included for a launch"
    )

    print()
    print("=== Collections by generation ===")
    print("Gen\tCount\tPauseTotal ms\tPauseMean ms\tPauseMax ms\tPromotedMean MB")
    for gen in sorted(generations):
        rows = [gc for gc in complete if int(gc["generation"]) == gen]
        if not rows:
            print(f"{gen}\t{generations[gen]}\t-\t-\t-\t-")
            continue
        gen_pauses = [float(gc["pauseMSec"]) for gc in rows]
        promoted = sum(float(gc.get("promotedMB") or 0) for gc in rows) / len(rows)
        print(
            f"{gen}\t{generations[gen]}\t{sum(gen_pauses):.2f}\t{sum(gen_pauses) / len(rows):.3f}\t"
            f"{max(gen_pauses):.3f}\t{promoted:.2f}"
        )

    print()
    print("=== Collections by reason ===")
    print("Reason\tCount\tPauseTotal ms\tPauseMax ms")
    reasons: dict[str, list[float]] = collections.defaultdict(list)
    counts = collections.Counter(gc.get("reason", "unknown") for gc in collections_)
    for gc in complete:
        reasons[gc.get("reason", "unknown")].append(float(gc["pauseMSec"]))
    for reason, count in sorted(counts.items(), key=lambda item: (-sum(reasons[item[0]]), -item[1], item[0])):
        values = reasons[reason]
        if values:
            print(f"{reason}\t{count}\t{sum(values):.2f}\t{max(values):.3f}")
        else:
            print(f"{reason}\t{count}\t-\t-")

    print()
    print(f"=== Top {limit} pauses ===")
    print("Number\tGen\tType\tReason\tAt ms\tPause ms\tDuration ms\tHeapBefore MB\tHeapAfter MB\tPromoted MB")
    longest = sorted(complete, key=lambda gc: (-float(gc["pauseMSec"]), int(gc.get("number", 0))))[:limit]
    for gc in longest:
        print(
            f"{gc.get('number', '?')}\t{gc['generation']}\t{gc.get('type', '?')}\t{gc.get('reason', '?')}\t"
            f"{float(gc.get('pauseStartMSec', gc.get('startMSec', 0))):.2f}\t{float(gc['pauseMSec']):.3f}\t"
            f"{float(gc.get('durationMSec') or 0):.3f}\t{float(gc['heapBeforeMB']):.2f}\t"
            f"{float(gc['heapAfterMB']):.2f}\t{float(gc.get('promotedMB') or 0):.2f}"
        )
    if not longest:
        print("(no complete collection)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json", type=Path, help="a .nettrace.json written by dotnet-trace convert --format Json")
    parser.add_argument("--limit", type=int, default=30, help="rows in the table of longest pauses")
    parser.add_argument("--nettrace", type=Path, help="the trace the file was converted from, to name the runtime")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    document = json.loads(args.json.read_text(encoding="utf-8"))
    gc = document.get("gc")
    if not isinstance(gc, dict):
        print(
            f"{args.json} has no 'gc' object: it was converted by a dotnet-trace that does not export GC events. "
            "Delete it and run gc-report on the .nettrace again to convert it with the pinned dotnet-trace",
            file=sys.stderr,
        )
        return 1
    processes = [process for process in gc.get("processes", []) if process.get("collections")]
    if not processes:
        print(
            f"no garbage collections in {args.json}: capture with --profile gc-collect (GC events only, low "
            "overhead) or gc-verbose, while the workload runs",
            file=sys.stderr,
        )
        return 1

    runtime = load_runtime_version()(args.nettrace) if args.nettrace else None
    print(f"Source\t{args.json.resolve()}")
    print(f"Runtime\t{runtime or 'unknown'}")
    for index, process in enumerate(processes):
        print()
        if len(processes) > 1:
            print(f"=== Process {index + 1} of {len(processes)} ===")
        print_process(process, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
