#!/usr/bin/env python3
"""Report on and compare the heap graphs DotRush's `dotnet-gcdump --format Json` writes.

The file is read as a stream. Its per-object arrays (`nodes`, `edges`, `addresses`, `dominators`,
`retainedSizes`) grow with the heap and run to hundreds of megabytes on a large one, so they are
parsed one buffer at a time and a command keeps only what it needs: `report` holds a type index
and a dominator per object, `diff` holds nothing per object. `types` and the scalar fields are
bounded by the number of distinct types and are decoded whole.
"""

from __future__ import annotations

import argparse
import codecs
import heapq
import json
import re
import sys
from array import array
from pathlib import Path


CHUNK_BYTES = 1 << 20
PER_OBJECT_ARRAYS = frozenset({"nodes", "edges", "addresses", "dominators", "retainedSizes"})
# The graph reader gives large arrays and strings one type per size bucket, named like
# "System.Byte[] (Bytes > 10K)". To whoever reads a report they are one type.
SIZE_BUCKET = re.compile(r" \(Bytes > \d+[KM]\)$")
# A dominator chain is walked this far before the path is cut short.
MAX_PATH_STEPS = 256
DECODER = json.JSONDecoder()


class JsonStream:
    """A forward-only cursor over a JSON document that buffers no more than it must."""

    def __init__(self, handle, chunk_bytes: int) -> None:
        self._handle = handle
        self._chunk_bytes = chunk_bytes
        self._buffer = b""
        self._pos = 0
        self._eof = False

    def _fill(self) -> bool:
        if self._eof:
            return False
        data = self._handle.read(self._chunk_bytes)
        if not data:
            self._eof = True
            return False
        self._buffer = self._buffer[self._pos:] + data
        self._pos = 0
        return True

    def _peek(self) -> int:
        """Skip whitespace and return the next byte, or -1 at the end of the document."""
        while True:
            while self._pos < len(self._buffer) and self._buffer[self._pos] in b" \t\r\n":
                self._pos += 1
            if self._pos < len(self._buffer):
                return self._buffer[self._pos]
            if not self._fill():
                return -1

    def _expect(self, token: bytes) -> None:
        if self._peek() != token[0]:
            raise ValueError(f"malformed JSON: expected {token.decode()!r}")
        self._pos += 1

    def value(self):
        """Decode the next value whole. Only for values that stay small."""
        if self._peek() < 0:
            raise ValueError("malformed JSON: unexpected end of document")
        window = 4096
        while True:
            while len(self._buffer) - self._pos < window and self._fill():
                pass
            at_end = self._eof and self._pos + window >= len(self._buffer)
            # The incremental decoder holds back a multi-byte character cut by the window edge.
            text = codecs.getincrementaldecoder("utf-8")().decode(
                self._buffer[self._pos:self._pos + window], final=at_end
            )
            try:
                value, end = DECODER.raw_decode(text)
            except json.JSONDecodeError:
                if at_end:
                    raise
            else:
                # A value that ends exactly at the window edge may be a number the edge cut short.
                if end < len(text) or at_end:
                    self._pos += len(text[:end].encode("utf-8"))
                    return value
            window *= 2

    def members(self):
        """Yield each key of the object at the cursor. The caller must consume its value."""
        self._expect(b"{")
        if self._peek() == ord("}"):
            self._pos += 1
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise ValueError("malformed JSON: object key is not a string")
            self._expect(b":")
            yield key
            separator = self._peek()
            self._pos += 1
            if separator == ord("}"):
                return
            if separator != ord(","):
                raise ValueError("malformed JSON: expected ',' or '}' after a value")

    def int_chunks(self):
        """Yield the integers of a flat numeric array, one list per buffer.

        The closing bracket is found by a plain byte search, which is sound only because these
        arrays hold nothing but numbers: no strings, no nesting.
        """
        self._expect(b"[")
        while True:
            close = self._buffer.find(b"]", self._pos)
            if close >= 0:
                segment = self._buffer[self._pos:close]
                self._pos = close + 1
                if segment.strip():
                    yield list(map(int, segment.split(b",")))
                return
            comma = self._buffer.rfind(b",", self._pos)
            if comma >= 0:
                segment = self._buffer[self._pos:comma]
                self._pos = comma + 1
                yield list(map(int, segment.split(b",")))
            if not self._fill():
                raise ValueError("malformed JSON: unexpected end of document inside an array")

    def skip_int_array(self) -> None:
        self._expect(b"[")
        while True:
            close = self._buffer.find(b"]", self._pos)
            if close >= 0:
                self._pos = close + 1
                return
            self._pos = len(self._buffer)
            if not self._fill():
                raise ValueError("malformed JSON: unexpected end of document inside an array")


class Snapshot:
    def __init__(self) -> None:
        self.fields: dict[str, object] = {}
        self.types: list | None = None
        # Indexed by type index: object count and summed object size.
        self.counts: list[int] = []
        self.type_bytes: list[int] = []
        # Indexed by object, and kept only for the retention report.
        self.node_types: array | None = None
        self.dominators: array | None = None
        self.largest: list[tuple[int, int]] | None = None

    def label(self, type_index: int) -> tuple[str, str]:
        entry = self.types[type_index] if self.types and type_index < len(self.types) else {}
        name = SIZE_BUCKET.sub("", entry.get("name") or f"<type {type_index}>")
        # Module paths differ between runtime versions; the file name is what identifies it.
        module = (entry.get("module") or "").replace("\\", "/").rsplit("/", 1)[-1]
        return name, module

    def is_synthetic(self, type_index: int) -> bool:
        # Root and grouping nodes ("[.NET Roots]", "[static var X.y]") are the only zero-size ones.
        return self.type_bytes[type_index] == 0

    def by_type(self) -> dict[tuple[str, str], tuple[int, int]]:
        merged: dict[tuple[str, str], list[int]] = {}
        for type_index, count in enumerate(self.counts):
            if count and not self.is_synthetic(type_index):
                row = merged.setdefault(self.label(type_index), [0, 0])
                row[0] += self.type_bytes[type_index]
                row[1] += count
        return {label: (row[0], row[1]) for label, row in merged.items()}


def read_nodes(stream: JsonStream, snapshot: Snapshot, *, keep_node_types: bool) -> None:
    counts = snapshot.counts
    type_bytes = snapshot.type_bytes
    node_types = array("i") if keep_node_types else None
    carry: list[int] = []
    # Each object is a [typeIndex, size, childCount] triplet, and a buffer can end mid-triplet.
    for chunk in stream.int_chunks():
        if carry:
            chunk = carry + chunk
        whole = len(chunk) - len(chunk) % 3
        carry = chunk[whole:]
        type_column = chunk[0:whole:3]
        if not type_column:
            continue
        if node_types is not None:
            node_types.extend(type_column)
        missing = max(type_column) + 1 - len(counts)
        if missing > 0:
            counts.extend([0] * missing)
            type_bytes.extend([0] * missing)
        for type_index, size in zip(type_column, chunk[1:whole:3]):
            counts[type_index] += 1
            type_bytes[type_index] += size
    if carry:
        raise ValueError("malformed gcdump JSON: nodes is not a list of [type, size, children] triplets")
    snapshot.node_types = node_types


def select_largest(snapshot: Snapshot, retained_chunks, limit: int) -> list[tuple[int, int]]:
    """Rank objects by retained size, skipping any whose immediate dominator has the same type.

    Without the skip a linked list of a million nodes fills every row with its own tail, each
    row retaining one node less than the row above.
    """
    node_types = snapshot.node_types
    dominators = snapshot.dominators
    key_ids: dict[tuple[str, str], int] = {}
    type_keys = [key_ids.setdefault(snapshot.label(index), len(key_ids)) for index in range(len(snapshot.counts))]
    synthetic = [snapshot.is_synthetic(index) for index in range(len(snapshot.counts))]
    ranked: list[tuple[int, int]] = []
    node = 0
    for chunk in retained_chunks:
        for retained in chunk:
            if len(ranked) < limit or retained > ranked[0][0]:
                type_index = node_types[node]
                dominator = dominators[node]
                if not synthetic[type_index] and (
                    dominator < 0 or type_keys[node_types[dominator]] != type_keys[type_index]
                ):
                    # Negated so that among equal sizes the lower object index ranks first.
                    if len(ranked) < limit:
                        heapq.heappush(ranked, (retained, -node))
                    else:
                        heapq.heapreplace(ranked, (retained, -node))
            node += 1
    if node != len(node_types) or node != len(dominators):
        raise ValueError("malformed gcdump JSON: the per-object arrays disagree on the object count")
    return [(retained, -negated) for retained, negated in sorted(ranked, reverse=True)]


def load(path: Path, *, retention_limit: int = 0, chunk_bytes: int = CHUNK_BYTES) -> Snapshot:
    snapshot = Snapshot()
    stored_retained: array | None = None
    with path.open("rb") as handle:
        stream = JsonStream(handle, chunk_bytes)
        for key in stream.members():
            if key == "types":
                snapshot.types = stream.value()
            elif key == "nodes":
                read_nodes(stream, snapshot, keep_node_types=retention_limit > 0)
            elif key == "dominators" and retention_limit:
                snapshot.dominators = array("i")
                for chunk in stream.int_chunks():
                    snapshot.dominators.extend(chunk)
            elif key == "retainedSizes" and retention_limit:
                if snapshot.types is not None and snapshot.node_types is not None and snapshot.dominators is not None:
                    # The writer emits retainedSizes last, so it is ranked as it streams.
                    snapshot.largest = select_largest(snapshot, stream.int_chunks(), retention_limit)
                else:
                    stored_retained = array("q")
                    for chunk in stream.int_chunks():
                        stored_retained.extend(chunk)
            elif key in PER_OBJECT_ARRAYS:
                stream.skip_int_array()
            else:
                snapshot.fields[key] = stream.value()
    if stored_retained is not None:
        if snapshot.node_types is None or snapshot.dominators is None:
            raise ValueError(f"{path} has retainedSizes but no nodes or dominators")
        snapshot.largest = select_largest(snapshot, [stored_retained], retention_limit)
    if not any(snapshot.type_bytes):
        raise ValueError(f"no heap objects found in {path}")
    return snapshot


def retention_path(snapshot: Snapshot, node: int) -> str:
    """Name the dominator chain above an object, nearest first, collapsing runs of one type."""
    runs: list[list] = []
    current = snapshot.dominators[node]
    steps = 0
    while current >= 0 and steps < MAX_PATH_STEPS:
        name = snapshot.label(snapshot.node_types[current])[0]
        if runs and runs[-1][0] == name:
            runs[-1][1] += 1
        else:
            runs.append([name, 1])
        current = snapshot.dominators[current]
        steps += 1
    parts = [f"{name} x{count}" if count > 1 else name for name, count in runs]
    if current >= 0:
        parts.append("...")
    if len(parts) > 6:
        parts = parts[:4] + ["..."] + parts[-2:]
    return " <- ".join(parts) if parts else "(not dominated)"


def render(label: tuple[str, str]) -> str:
    name, module = label
    return f"{name}  [{module}]" if module else name


def percent(part: int, total: int) -> str:
    return f"{100 * part / total:.2f}%" if total else "0.00%"


def signed(value: int) -> str:
    return f"{value:+d}"


def report(args: argparse.Namespace) -> int:
    snapshot = load(args.snapshot, retention_limit=args.limit, chunk_bytes=args.chunk_bytes)
    types = snapshot.by_type()
    heap_bytes = sum(size for size, _ in types.values())
    heap_objects = sum(count for _, count in types.values())

    print("Metric\tValue")
    for field, name in (("processName", "Process"), ("processId", "ProcessId"), ("timeCollected", "TimeCollected")):
        if snapshot.fields.get(field):
            print(f"{name}\t{snapshot.fields[field]}")
    print(f"HeapBytes\t{heap_bytes}")
    print(f"HeapObjects\t{heap_objects}")
    print(f"Types\t{len(types)}")
    print()
    print("# Bytes sums the size of every object of the type; it is exact, not sampled.")
    print("Bytes\tPercent\tCount\tType")
    rows = sorted(types.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))
    for label, (size, count) in rows[: args.limit]:
        print(f"{size}\t{percent(size, heap_bytes)}\t{count}\t{render(label)}")
    print()
    print("# RetainedBytes is what collecting the object would free: itself plus everything reachable only")
    print("# through it. Rows nest (a collection and its backing array both appear), so never add them up.")
    print("RetainedBytes\tPercent\tType\tRetainedBy")
    if snapshot.largest is None:
        print("(no retainedSizes in this file)")
    else:
        for retained, node in snapshot.largest:
            label = snapshot.label(snapshot.node_types[node])
            print(f"{retained}\t{percent(retained, heap_bytes)}\t{render(label)}\t{retention_path(snapshot, node)}")
    return 0


def diff(args: argparse.Namespace) -> int:
    before = load(args.baseline, chunk_bytes=args.chunk_bytes).by_type()
    after = load(args.current, chunk_bytes=args.chunk_bytes).by_type()
    rows = []
    for label in before.keys() | after.keys():
        before_bytes, before_count = before.get(label, (0, 0))
        after_bytes, after_count = after.get(label, (0, 0))
        if before_bytes != after_bytes or before_count != after_count:
            rows.append((after_bytes - before_bytes, after_count - before_count,
                         before_bytes, after_bytes, before_count, after_count, label))
    rows.sort(key=lambda row: (-row[0], -row[1], row[6]))

    before_heap = sum(size for size, _ in before.values())
    after_heap = sum(size for size, _ in after.values())
    before_objects = sum(count for _, count in before.values())
    after_objects = sum(count for _, count in after.values())
    print("Metric\tBaseline\tCurrent\tDelta")
    print(f"HeapBytes\t{before_heap}\t{after_heap}\t{signed(after_heap - before_heap)}")
    print(f"HeapObjects\t{before_objects}\t{after_objects}\t{signed(after_objects - before_objects)}")
    print()
    print("DeltaBytes\tDeltaCount\tBaselineBytes\tCurrentBytes\tBaselineCount\tCurrentCount\tType")
    if not rows:
        print("(no per-type changes)")
    for delta_bytes, delta_count, before_bytes, after_bytes, before_count, after_count, label in rows[: args.limit]:
        print(f"{signed(delta_bytes)}\t{signed(delta_count)}\t{before_bytes}\t{after_bytes}\t"
              f"{before_count}\t{after_count}\t{render(label)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chunk-bytes", type=int, default=CHUNK_BYTES, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    report_parser = commands.add_parser("report", help="per-type bytes and the largest retained objects")
    report_parser.add_argument("snapshot", type=Path)
    report_parser.add_argument("--limit", type=int, default=30)
    diff_parser = commands.add_parser("diff", help="per-type byte and count deltas between two snapshots")
    diff_parser.add_argument("baseline", type=Path)
    diff_parser.add_argument("current", type=Path)
    diff_parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)

    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.chunk_bytes < 1:
        parser.error("--chunk-bytes must be positive")
    try:
        return report(args) if args.command == "report" else diff(args)
    except (OSError, ValueError) as error:
        print(f"analyze-gcdump: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
