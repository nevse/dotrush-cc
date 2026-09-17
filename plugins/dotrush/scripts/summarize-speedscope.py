#!/usr/bin/env python3
"""Summarize managed CPU stacks in a Speedscope JSON file."""

from __future__ import annotations

import argparse
import collections
import json
import mmap
import re
from pathlib import Path


STRUCTURAL_NAMES = {"(Non-Activities)", "Threads"}
PROCESS_ROOT = re.compile(r"^Process(?:\d+)?\s")
# Speedscope frame names carry full IL signatures, which routinely run past 400 characters and
# bury the method name. Trimmed for display only — aggregation always keys on the full name, and
# a trimmed name that would collide with another row keeps its signature.
IL_SIGNATURE = re.compile(r"\(.+\)$", re.DOTALL)
THREAD_ID = re.compile(r"^Thread \((\d+)\)")


# The sample profiler tags each sample as running managed code or not, and the converter turns that
# into a CPU_TIME or UNMANAGED_CODE_TIME leaf. Some runtimes tag every sample as not managed, even in a
# pure managed loop (seen on macOS arm64 with .NET 9.0.20 and 10.0.0-10.0.3; 8.0.31 and 10.0.4+ are
# fine). A capture with no CPU_TIME at all is therefore ambiguous: an idle target, or such a runtime.
# Its managed frames are then ranked by time on stack, waits included, instead of being dropped.
CORELIB_SUFFIXES = [
    f"{separator}System.Private.CoreLib.dll".encode("utf-16-le") for separator in ("/", "\\")
]
RUNTIME_DIR = re.compile(r"Microsoft\.NETCore\.App[/\\]([0-9][^/\\]{0,40})$")


def runtime_version(nettrace: Path) -> str | None:
    """Read the target's runtime version from the CoreLib path in the trace's module events."""
    try:
        with nettrace.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            for suffix in CORELIB_SUFFIXES:
                end = data.find(suffix)
                while end >= 0:
                    start = max(0, end - 512)
                    start += (end - start) % 2
                    prefix = data[start:end].decode("utf-16-le", errors="replace")
                    match = RUNTIME_DIR.search(prefix)
                    if match:
                        return match.group(1)
                    end = data.find(suffix, end + 2)
    except (OSError, ValueError):
        return None
    return None


def shorten(name: str) -> str:
    """Drop an IL parameter list. An empty `()` is already short, so it is left alone."""
    return IL_SIGNATURE.sub("(...)", name)


def display_names(names: list[str]) -> dict[str, str]:
    """Map each full frame name to the shortest form that stays unambiguous among `names`."""
    counts = collections.Counter(shorten(name) for name in names)
    return {name: (shorten(name) if counts[shorten(name)] == 1 else name) for name in names}


def is_structural(name: str) -> bool:
    return name in STRUCTURAL_NAMES or PROCESS_ROOT.match(name) is not None or name.startswith("Thread (")


class Totals:
    def __init__(self) -> None:
        self.managed = 0.0
        # Only samples the runtime tagged as managed; managed-leaf gaps between events carry no tag.
        self.cpu_tagged = 0.0
        self.unmanaged = 0.0
        # Samples tagged unmanaged that still have managed frames above the tag, with the same
        # rankings as the managed ones. Used only when the capture has no managed sample at all.
        self.unmanaged_on_stack = 0.0
        self.exclusive: collections.Counter[str] = collections.Counter()
        self.inclusive: collections.Counter[str] = collections.Counter()
        self.unmanaged_exclusive: collections.Counter[str] = collections.Counter()
        self.unmanaged_inclusive: collections.Counter[str] = collections.Counter()

    def add(self, other: Totals) -> None:
        for name, value in vars(other).items():
            if isinstance(value, collections.Counter):
                getattr(self, name).update(value)
            else:
                setattr(self, name, getattr(self, name) + value)


class Thread:
    def __init__(self, profile: dict, names: list[str]) -> None:
        self.name = profile.get("name", "<unnamed>")
        self.totals = Totals()
        self.duration = summarize_profile(profile, names, self.totals)
        # Evented profiles only: how often the sampled stack changed. With no thread-state tags this
        # is what tells a working thread from one parked in a wait for the whole capture.
        self.stack_changes = len(profile.get("events", []))
        match = THREAD_ID.match(self.name)
        self.id = match.group(1) if match else None


def record_stack(stack: list[int], weight: float, names: list[str], totals: Totals) -> None:
    if weight <= 0 or not stack:
        return

    stack_names = [names[index] for index in stack]
    leaf = stack_names[-1]
    if leaf == "CPU_TIME":
        totals.cpu_tagged += weight
    if leaf == "UNMANAGED_CODE_TIME":
        totals.unmanaged += weight
        exclusive, inclusive = totals.unmanaged_exclusive, totals.unmanaged_inclusive
    else:
        exclusive, inclusive = totals.exclusive, totals.inclusive

    if leaf in ("CPU_TIME", "UNMANAGED_CODE_TIME"):
        stack_names.pop()

    meaningful = [name for name in stack_names if not is_structural(name) and not name.endswith("_TIME")]
    if not meaningful:
        return

    if leaf == "UNMANAGED_CODE_TIME":
        totals.unmanaged_on_stack += weight
    else:
        totals.managed += weight
    exclusive[meaningful[-1]] += weight
    for name in dict.fromkeys(meaningful):
        inclusive[name] += weight


def summarize_profile(profile: dict, names: list[str], totals: Totals) -> float:
    duration = float(profile.get("endValue", 0)) - float(profile.get("startValue", 0))

    if profile.get("type") == "evented":
        stack: list[int] = []
        previous = float(profile.get("startValue", 0))
        for event in profile.get("events", []):
            current = float(event["at"])
            record_stack(stack, current - previous, names, totals)
            frame = int(event["frame"])
            if event["type"] == "O":
                stack.append(frame)
            elif stack and stack[-1] == frame:
                stack.pop()
            elif frame in stack:
                stack = stack[: stack.index(frame)]
            previous = current
        record_stack(stack, float(profile.get("endValue", previous)) - previous, names, totals)
    elif profile.get("type") == "sampled":
        samples = profile.get("samples", [])
        weights = profile.get("weights")
        if weights is None:
            default_weight = duration / len(samples) if samples else 0.0
            weights = [default_weight] * len(samples)
        for stack, weight in zip(samples, weights):
            record_stack(stack, float(weight), names, totals)

    return duration


def print_threads(threads: list[Thread], on_stack: bool, limit: int) -> None:
    if on_stack:
        order = "stack changes; a thread with few changes and one top function was most likely blocked"
        key = lambda thread: (-thread.stack_changes, thread.name)
    else:
        order = "managed CPU"
        key = lambda thread: (-thread.totals.managed, -thread.stack_changes, thread.name)
    print(f"=== Top {limit} threads by {order} ===")
    print("StackChanges\tWeight\tThread\tTopFunction")
    for thread in sorted(threads, key=key)[:limit]:
        exclusive = thread.totals.unmanaged_exclusive if on_stack else thread.totals.exclusive
        weight = thread.totals.unmanaged_on_stack if on_stack else thread.totals.managed
        top = "-"
        if exclusive:
            name, value = min(exclusive.items(), key=lambda item: (-item[1], item[0]))
            top = f"{value * 100 / weight:.2f}% {shorten(name)}"
        print(f"{thread.stack_changes}\t{weight:.2f}\t{thread.name}\t{top}")


def print_ranking(title: str, values: collections.Counter[str], total: float, limit: int) -> None:
    print(title)
    print("Percent\tWeight\tFunction")
    ranked = sorted(
        ((name, value) for name, value in values.items() if value > 0),
        key=lambda item: (-item[1], item[0]),
    )
    if not ranked:
        # An idle or all-interop capture samples no managed frames. That is a result, not a
        # failure: the header block above still reports the unmanaged-or-blocked interval.
        print("(no managed samples)")
        return
    shown = display_names([name for name, _ in ranked[:limit]])
    for name, value in ranked[:limit]:
        percent = value * 100 / total if total else 0.0
        percent_text = "<0.01%" if 0 < percent < 0.01 else f"{percent:.2f}%"
        print(f"{percent_text}\t{value:.2f}\t{shown[name]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("speedscope", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--nettrace", type=Path, help="the trace the file was converted from, to name the runtime")
    parser.add_argument("--thread", help="rank only this thread, by the id in its 'Thread (<id>)' name")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.thread is not None and not args.thread.isdigit():
        parser.error(f"--thread must be a numeric thread id, got {args.thread!r}")

    document = json.loads(args.speedscope.read_text(encoding="utf-8"))
    names = [frame.get("name", "<unnamed>") for frame in document["shared"]["frames"]]
    threads = [Thread(profile, names) for profile in document.get("profiles", [])]
    if not threads:
        raise ValueError(f"no profiles found in {args.speedscope}")
    units = {profile.get("unit", "unknown") for profile in document["profiles"]}
    starts = [float(profile.get("startValue", 0)) for profile in document["profiles"]]
    ends = [float(profile.get("endValue", 0)) for profile in document["profiles"]]

    totals = Totals()
    for thread in threads:
        totals.add(thread.totals)
    # Without a single sample tagged managed the tag carries no information (see CORELIB_SUFFIXES),
    # so rank what was on the stack rather than a ranking of untagged gaps, or none at all. Decided
    # over the whole capture, so a thread picked with --thread is measured the same way.
    on_stack = totals.cpu_tagged == 0 and totals.unmanaged_on_stack > 0

    selected = threads
    if args.thread is not None:
        selected = [thread for thread in threads if thread.id == args.thread]
        if not selected:
            raise ValueError(f"no thread {args.thread} in {args.speedscope}")
        totals = Totals()
        for thread in selected:
            totals.add(thread.totals)

    runtime = runtime_version(args.nettrace) if args.nettrace else None
    unit = units.pop() if len(units) == 1 else "mixed-units"
    print(f"Source\t{args.speedscope.resolve()}")
    print(f"Unit\t{unit}")
    print(f"Runtime\t{runtime or 'unknown'}")
    # WallClockDuration is the capture window. Every other time here is summed across threads,
    # so on a multi-threaded target they exceed it — SampledThreadTime by roughly the thread
    # count. Reporting only the sum (as "ProfileDuration") read as elapsed time and was wrong.
    print(f"Threads\t{len(threads)}")
    if args.thread is not None:
        print(f"SelectedThread\t{selected[0].name}")
    print(f"WallClockDuration\t{max(ends) - min(starts):.2f}")
    print(f"SampledThreadTime\t{sum(thread.duration for thread in selected):.2f}")
    print(f"ManagedSampledTime\t{totals.managed:.2f}")
    print(f"UnmanagedOrBlockedTime\t{totals.unmanaged:.2f}")
    if on_stack:
        print(f"ManagedOnStackTime\t{totals.unmanaged_on_stack:.2f}")
        print(
            "Warning\tNo sample in the capture is tagged as running managed code, yet managed frames were "
            "on the stack. Either every thread was blocked or in native code for the whole capture, or the "
            "target's runtime tags every sample this way (.NET 9 and 10.0.0-10.0.3 do on macOS arm64). "
            "Rankings are time on stack, blocked time included, as shares of ManagedOnStackTime; rank a "
            "working thread alone with its id from the thread table."
        )
        exclusive, inclusive, total = totals.unmanaged_exclusive, totals.unmanaged_inclusive, totals.unmanaged_on_stack
        measure = "on-stack time (running and blocked)"
    else:
        exclusive, inclusive, total = totals.exclusive, totals.inclusive, totals.managed
        measure = "managed CPU"
    if args.thread is None:
        print()
        print_threads(threads, on_stack, args.limit)
    print()
    print_ranking(f"=== Top {args.limit} functions by exclusive {measure} ===", exclusive, total, args.limit)
    print()
    print_ranking(f"=== Top {args.limit} functions by inclusive {measure} ===", inclusive, total, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
