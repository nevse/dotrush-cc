#!/usr/bin/env python3
"""Summarize managed CPU stacks in a Speedscope JSON file."""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path


STRUCTURAL_NAMES = {"(Non-Activities)", "Threads"}
PROCESS_ROOT = re.compile(r"^Process(?:\d+)?\s")


def is_structural(name: str) -> bool:
    return name in STRUCTURAL_NAMES or PROCESS_ROOT.match(name) is not None or name.startswith("Thread (")


def record_stack(
    stack: list[int],
    weight: float,
    names: list[str],
    exclusive: collections.Counter[str],
    inclusive: collections.Counter[str],
) -> tuple[float, float]:
    if weight <= 0 or not stack:
        return 0.0, 0.0

    stack_names = [names[index] for index in stack]
    leaf = stack_names[-1]
    if leaf == "UNMANAGED_CODE_TIME":
        return 0.0, weight

    if leaf == "CPU_TIME":
        stack_names.pop()

    meaningful = [name for name in stack_names if not is_structural(name) and not name.endswith("_TIME")]
    if not meaningful:
        return 0.0, 0.0

    exclusive[meaningful[-1]] += weight
    for name in dict.fromkeys(meaningful):
        inclusive[name] += weight
    return weight, 0.0


def summarize_profile(
    profile: dict,
    names: list[str],
    exclusive: collections.Counter[str],
    inclusive: collections.Counter[str],
) -> tuple[float, float, float]:
    managed = 0.0
    unmanaged = 0.0
    duration = float(profile.get("endValue", 0)) - float(profile.get("startValue", 0))

    if profile.get("type") == "evented":
        stack: list[int] = []
        previous = float(profile.get("startValue", 0))
        for event in profile.get("events", []):
            current = float(event["at"])
            sampled, native = record_stack(stack, current - previous, names, exclusive, inclusive)
            managed += sampled
            unmanaged += native
            frame = int(event["frame"])
            if event["type"] == "O":
                stack.append(frame)
            elif stack and stack[-1] == frame:
                stack.pop()
            elif frame in stack:
                stack = stack[: stack.index(frame)]
            previous = current
        sampled, native = record_stack(
            stack,
            float(profile.get("endValue", previous)) - previous,
            names,
            exclusive,
            inclusive,
        )
        managed += sampled
        unmanaged += native
    elif profile.get("type") == "sampled":
        samples = profile.get("samples", [])
        weights = profile.get("weights")
        if weights is None:
            default_weight = duration / len(samples) if samples else 0.0
            weights = [default_weight] * len(samples)
        for stack, weight in zip(samples, weights):
            sampled, native = record_stack(stack, float(weight), names, exclusive, inclusive)
            managed += sampled
            unmanaged += native

    return managed, unmanaged, duration


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
    for name, value in ranked[:limit]:
        percent = value * 100 / total if total else 0.0
        percent_text = "<0.01%" if 0 < percent < 0.01 else f"{percent:.2f}%"
        print(f"{percent_text}\t{value:.2f}\t{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("speedscope", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    document = json.loads(args.speedscope.read_text(encoding="utf-8"))
    names = [frame.get("name", "<unnamed>") for frame in document["shared"]["frames"]]
    exclusive: collections.Counter[str] = collections.Counter()
    inclusive: collections.Counter[str] = collections.Counter()
    managed = unmanaged = duration = 0.0
    units = set()

    for profile in document.get("profiles", []):
        profile_managed, profile_unmanaged, profile_duration = summarize_profile(
            profile, names, exclusive, inclusive
        )
        managed += profile_managed
        unmanaged += profile_unmanaged
        duration += profile_duration
        units.add(profile.get("unit", "unknown"))

    if not units:
        raise ValueError(f"no profiles found in {args.speedscope}")

    unit = units.pop() if len(units) == 1 else "mixed-units"
    print(f"Source\t{args.speedscope.resolve()}")
    print(f"Unit\t{unit}")
    print(f"ProfileDuration\t{duration:.2f}")
    print(f"ManagedSampledTime\t{managed:.2f}")
    print(f"UnmanagedOrBlockedTime\t{unmanaged:.2f}")
    print()
    print_ranking(f"=== Top {args.limit} functions by exclusive managed CPU ===", exclusive, managed, args.limit)
    print()
    print_ranking(f"=== Top {args.limit} functions by inclusive managed CPU ===", inclusive, managed, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
