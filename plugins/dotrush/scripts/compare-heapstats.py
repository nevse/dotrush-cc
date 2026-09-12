#!/usr/bin/env python3
"""Compare two `dotnet-gcdump report` heap statistics tables.

Ranks per-type OBJECT COUNTS, not bytes. `dotnet-gcdump report` prints one size per type, and
PerfView sets that size from the first object it stores for the type (`SetNode`: the size is
assigned only while `TypeInfo.Size < 0`), so for any variable-sized type — every string, and
every array below the 1000-byte bucketing threshold — it is one arbitrary object's size, not a
mean and not a total. Multiplying it by the count produces a retained-bytes figure that can move
opposite to reality, so this tool does not compute one. The heap-wide byte total is printed
because `GC Heap bytes` is reported directly and is sound.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


# `dotnet-gcdump report` renders every count with "N0" and no IFormatProvider, so the grouping
# separator is whatever the current culture uses. These ten are every separator .NET's ICU data
# produces: "," ".", "'" (de-CH), U+00A0, U+202F (fr-FR), U+2009, U+066C (Arabic/Persian),
# U+060C (N'Ko), U+2E41 (Adlam), U+12C8 (Ge'ez). Columns are split on ASCII space/tab only,
# because \s also matches the space-like separators above.
GROUPED_NUMBER = r"\d[\d,.'   ٬،⹁ወ]*"
ROW = re.compile(rf"^[ \t]*({GROUPED_NUMBER})[ \t]+({GROUPED_NUMBER})[ \t]+(.+?)[ \t]*$")
TOTAL_BYTES = re.compile(rf"^[ \t]*({GROUPED_NUMBER})[ \t]+GC Heap bytes[ \t]*$")
TOTAL_OBJECTS = re.compile(rf"^[ \t]*({GROUPED_NUMBER})[ \t]+GC Heap objects[ \t]*$")
# The trailing module bracket is optional: the printer omits it when ModuleName is empty, which
# is what PerfView produces for CCWs and `UNKNOWN 0x...` types. Requiring it left those rows
# unmerged, so one type split across a bucket boundary became two unrelated deltas.
SIZE_BUCKET = re.compile(r"[ \t]+\(Bytes > [^)]+\)(?=([ \t]+\[[^]]+\])?[ \t]*$)")


def to_int(text: str) -> int:
    """Read a culture-formatted integer, whatever its grouping separator."""
    return int(re.sub(r"\D", "", text))


def parse_report(path: Path) -> tuple[dict[str, tuple[int, int]], int, int]:
    """Map each type to (object count, largest sampled object size)."""
    result: dict[str, tuple[int, int]] = {}
    total_bytes = 0
    total_objects = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        total_match = TOTAL_BYTES.match(line)
        if total_match:
            total_bytes = to_int(total_match.group(1))
            continue
        total_match = TOTAL_OBJECTS.match(line)
        if total_match:
            total_objects = to_int(total_match.group(1))
            continue
        match = ROW.match(line)
        if not match:
            continue
        object_size = to_int(match.group(1))
        count = to_int(match.group(2))
        type_name = SIZE_BUCKET.sub("", match.group(3))
        previous_count, previous_sample = result.get(type_name, (0, 0))
        result[type_name] = (previous_count + count, max(previous_sample, object_size))
    if not result:
        raise ValueError(f"no heap-stat rows found in {path}")
    return result, total_bytes, total_objects


def signed(value: int) -> str:
    return f"{value:+d}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be positive")

    baseline, baseline_bytes, baseline_objects = parse_report(args.baseline)
    current, current_bytes, current_objects = parse_report(args.current)
    rows = []
    for type_name in baseline.keys() | current.keys():
        before_count, _ = baseline.get(type_name, (0, 0))
        after_count, sample_size = current.get(type_name, (0, 0))
        delta_count = after_count - before_count
        if delta_count != 0:
            rows.append((delta_count, before_count, after_count, sample_size, type_name))
    rows.sort(key=lambda row: (-row[0], -row[2], row[4]))

    print("Metric\tBaseline\tCurrent\tDelta")
    print(
        f"HeapBytes\t{baseline_bytes}\t{current_bytes}\t"
        f"{signed(current_bytes - baseline_bytes)}"
    )
    print(
        f"HeapObjects\t{baseline_objects}\t{current_objects}\t"
        f"{signed(current_objects - baseline_objects)}"
    )
    print()
    print("# SampleObjectBytes is one sampled object's size, not a per-type total or mean.")
    print("DeltaCount\tBaselineCount\tCurrentCount\tSampleObjectBytes\tType")
    if not rows:
        print("(no per-type count changes)")
    else:
        for delta_count, before_count, after_count, sample_size, type_name in rows[: args.limit]:
            print(
                f"{signed(delta_count)}\t{before_count}\t{after_count}\t"
                f"{sample_size}\t{type_name}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
