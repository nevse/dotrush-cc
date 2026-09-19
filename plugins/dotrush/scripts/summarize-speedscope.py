#!/usr/bin/env python3
"""Summarize managed CPU stacks, or with --allocations the allocation profile, in a Speedscope JSON file."""

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
# Leaves the converter puts under a thread that is not running: native or blocked code, and, when the
# trace has TPL task events, an await. None of them is CPU.
WAITING_LEAVES = {"UNMANAGED_CODE_TIME", "BLOCKED_TIME", "AWAIT_TIME"}
# With TPL task events the converter stitches each task's stack under the stack that started it,
# joined by these markers, which are no functions either.
ASYNC_MARKERS = {"STARTING TASK", "UNKNOWN_ASYNC"}


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


# The allocation profile's leaves: an allocated type and the runtime's allocation kind, which is no parameter list.
ALLOCATION_KIND = re.compile(r" \((Small|Large|Pinned)\)$")


def shorten(name: str) -> str:
    """Drop an IL parameter list. An empty `()` is already short, so it is left alone."""
    if ALLOCATION_KIND.search(name):
        return name
    return IL_SIGNATURE.sub("(...)", name)


def display_names(names: list[str]) -> dict[str, str]:
    """Map each full frame name to the shortest form that stays unambiguous among `names`."""
    counts = collections.Counter(shorten(name) for name in names)
    return {name: (shorten(name) if counts[shorten(name)] == 1 else name) for name in names}


def is_structural(name: str) -> bool:
    return (
        name in STRUCTURAL_NAMES
        or name in ASYNC_MARKERS
        or PROCESS_ROOT.match(name) is not None
        or name.startswith("Thread (")
    )


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
        # Whole stacks, root first, with the same split, for the call trees under --focus.
        self.stacks: collections.Counter[tuple[str, ...]] = collections.Counter()
        self.unmanaged_stacks: collections.Counter[tuple[str, ...]] = collections.Counter()

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
    waiting = leaf in WAITING_LEAVES
    if waiting:
        totals.unmanaged += weight
        exclusive, inclusive, stacks = totals.unmanaged_exclusive, totals.unmanaged_inclusive, totals.unmanaged_stacks
    else:
        exclusive, inclusive, stacks = totals.exclusive, totals.inclusive, totals.stacks

    if waiting or leaf == "CPU_TIME":
        stack_names.pop()

    meaningful = [name for name in stack_names if not is_structural(name) and not name.endswith("_TIME")]
    if not meaningful:
        return

    if waiting:
        totals.unmanaged_on_stack += weight
    else:
        totals.managed += weight
    exclusive[meaningful[-1]] += weight
    for name in dict.fromkeys(meaningful):
        inclusive[name] += weight
    stacks[tuple(meaningful)] += weight


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


class Capture:
    """One speedscope file: its threads, the totals of the selected ones, and how they are to be ranked."""

    def __init__(self, path: Path, thread: str | None, nettrace: Path | None) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        names = [frame.get("name", "<unnamed>") for frame in document["shared"]["frames"]]
        profiles = document.get("profiles", [])
        self.path = path
        self.async_stitched = any(name in ASYNC_MARKERS or name == "AWAIT_TIME" for name in names)
        self.threads = [Thread(profile, names) for profile in profiles]
        if not self.threads:
            raise ValueError(f"no profiles found in {path}")
        units = {profile.get("unit", "unknown") for profile in profiles}
        self.unit = units.pop() if len(units) == 1 else "mixed-units"
        self.wall_clock = max(float(p.get("endValue", 0)) for p in profiles) - min(
            float(p.get("startValue", 0)) for p in profiles
        )
        self.totals = Totals()
        for item in self.threads:
            self.totals.add(item.totals)
        # Without a single sample tagged managed the tag carries no information (see CORELIB_SUFFIXES),
        # so rank what was on the stack rather than a ranking of untagged gaps, or none at all. Decided
        # over the whole capture, so a thread picked with --thread is measured the same way.
        self.untagged = self.totals.cpu_tagged == 0 and self.totals.unmanaged_on_stack > 0
        self.selected = self.threads
        if thread is not None:
            self.selected = [item for item in self.threads if item.id == thread]
            if not self.selected:
                raise ValueError(f"no thread {thread} in {path}")
            self.totals = Totals()
            for item in self.selected:
                self.totals.add(item.totals)
        self.runtime = runtime_version(nettrace) if nettrace else None

    def stacks(self, on_stack: bool) -> collections.Counter[tuple[str, ...]]:
        """The stacks behind `rankings`, measured the same way."""
        if not on_stack:
            return self.totals.stacks
        if self.untagged:
            return self.totals.unmanaged_stacks
        return self.totals.stacks + self.totals.unmanaged_stacks

    def rankings(self, on_stack: bool) -> tuple[collections.Counter[str], collections.Counter[str], float]:
        """Exclusive and inclusive weights and their total, as managed CPU or as time on stack."""
        totals = self.totals
        if not on_stack:
            return totals.exclusive, totals.inclusive, totals.managed
        if self.untagged:
            return totals.unmanaged_exclusive, totals.unmanaged_inclusive, totals.unmanaged_on_stack
        # A capture with tags, compared against one without: on-stack time is every sample, whatever its tag.
        return (
            totals.exclusive + totals.unmanaged_exclusive,
            totals.inclusive + totals.unmanaged_inclusive,
            totals.managed + totals.unmanaged_on_stack,
        )


ASYNC_WARNING = (
    "The {which} has TPL task events, so the converter stitched each task's stack under the stack that "
    "started it: a thread's rows include work its tasks ran on other threads, and awaits count as blocked "
    "time. For CPU rankings, capture without System.Threading.Tasks.TplEventSource."
)


def signed(value: float) -> str:
    return f"{value:+.2f}"


def print_change(
    title: str,
    baseline: tuple[collections.Counter[str], float],
    current: tuple[collections.Counter[str], float],
    limit: int,
) -> None:
    """Rank functions by how much their share of their own capture's total moved, in percentage points."""
    (before, before_total), (after, after_total) = baseline, current
    print(title)
    print("Baseline%\tCurrent%\tChange\tBaselineWeight\tCurrentWeight\tFunction")

    def share(value: float, total: float) -> float:
        return value * 100 / total if total else 0.0

    rows = []
    for name in set(before) | set(after):
        old, new = before.get(name, 0.0), after.get(name, 0.0)
        if old <= 0 and new <= 0:
            continue
        change = share(new, after_total) - share(old, before_total)
        rows.append((name, old, new, change))
    if not rows:
        print("(no managed samples)")
        return
    rows.sort(key=lambda row: (-abs(row[3]), row[0]))
    shown = display_names([row[0] for row in rows[:limit]])
    for name, old, new, change in rows[:limit]:
        print(
            f"{share(old, before_total):.2f}\t{share(new, after_total):.2f}\t{signed(change)}\t"
            f"{old:.2f}\t{new:.2f}\t{shown[name]}"
        )


def print_diff(baseline: Capture, current: Capture, limit: int) -> None:
    # Two captures differ in length and thread count, so functions are compared by their share of their own
    # capture's total. Both sides use one measure: if either has no managed-tagged sample, both use time on stack.
    on_stack = baseline.untagged or current.untagged
    before_exclusive, before_inclusive, before_total = baseline.rankings(on_stack)
    after_exclusive, after_inclusive, after_total = current.rankings(on_stack)
    total_name = "ManagedOnStackTime" if on_stack else "ManagedSampledTime"
    measure = "on-stack time (running and blocked)" if on_stack else "managed CPU"

    print(f"Baseline\t{baseline.path.resolve()}")
    print(f"Current\t{current.path.resolve()}")
    print(f"Unit\t{baseline.unit}" if baseline.unit == current.unit else f"Unit\t{baseline.unit} -> {current.unit}")
    print(f"Runtime\t{baseline.runtime or 'unknown'} -> {current.runtime or 'unknown'}")
    print(f"Threads\t{len(baseline.threads)} -> {len(current.threads)}")
    for label, capture in (("Baseline", baseline), ("Current", current)):
        if capture.selected is not capture.threads:
            print(f"{label}Thread\t{capture.selected[0].name}")
    print(f"WallClockDuration\t{baseline.wall_clock:.2f} -> {current.wall_clock:.2f}")
    print(f"{total_name}\t{before_total:.2f} -> {after_total:.2f}")
    if on_stack:
        untagged = [label for label, capture in (("baseline", baseline), ("current", current)) if capture.untagged]
        print(
            f"Warning\tNo sample in the {' and '.join(untagged)} capture is tagged as running managed code, so both "
            "captures are compared by time on stack, blocked time included (see trace-report). Compare one working "
            "thread from each capture for numbers closer to CPU."
        )
    stitched = [label for label, capture in (("baseline", baseline), ("current", current)) if capture.async_stitched]
    if stitched:
        print("Warning\t" + ASYNC_WARNING.format(which=" and ".join(stitched) + " capture"))
    print(
        f"Measure\tChange is the current share minus the baseline share of each capture's own {total_name}, "
        "in percentage points; weights are in the captures' unit"
    )
    print()
    print_change(
        f"=== Top {limit} functions by change in exclusive {measure} ===",
        (before_exclusive, before_total), (after_exclusive, after_total), limit,
    )
    print()
    print_change(
        f"=== Top {limit} functions by change in inclusive {measure} ===",
        (before_inclusive, before_total), (after_inclusive, after_total), limit,
    )


PARAMETER_LIST = re.compile(r"\(.*\)$", re.DOTALL)


def bare(name: str) -> str:
    """A frame name without its parameter list, an empty `()` included, unlike `shorten`. An allocated type's
    kind, as in `System.String (Small)`, goes the same way."""
    return PARAMETER_LIST.sub("", name).rstrip()


# A --focus branch under this share of the focus function's own time is folded into one row.
FOCUS_MIN_SHARE = 0.01


def match_focus(
    needle: str, inclusive: collections.Counter[str], kind: str = "function", kinds: str = "functions"
) -> str:
    """The one sampled function `needle` names, or with --allocations the one function or allocated type; `kind` and
    `kinds` name what is matched in the errors. Tried in order, and the first that matches anything decides: the
    full name with its whole signature; the name without its parameter list; the end of that name after a `.`, `!` or
    `:` (`Method`, `Type.Method`), ignoring case; any part of it, ignoring case. Past the first, a parameter list on
    `needle` is dropped too, so a trimmed `Type.Method(...)` copied from a row names every overload alike, and so is
    an allocation kind, so `System.String` names `System.String (Small)` when no other kind was allocated."""
    names = [name for name, value in inclusive.items() if value > 0]
    stripped = bare(needle)
    folded = stripped.casefold()
    ending = re.compile(f"(^|[.!:]){re.escape(folded)}$")
    tiers = (
        lambda name: name == needle,
        lambda name: bare(name) == stripped,
        lambda name: ending.search(bare(name).casefold()) is not None,
        lambda name: folded in bare(name).casefold(),
    )
    for test in tiers:
        matches = sorted((name for name in names if test(name)), key=lambda name: (-inclusive[name], name))
        if len(matches) == 1:
            return matches[0]
        if matches:
            # Full names, untrimmed: passed back, each one resolves in the first tier.
            listed = "\n".join(f"  {inclusive[name]:.2f}\t{name}" for name in matches[:10])
            more = f"\n  ... and {len(matches) - 10} more" if len(matches) > 10 else ""
            raise ValueError(
                f"--focus {needle!r} matches {len(matches)} {kinds}; pass more of the name, or a name exactly "
                f"as listed here (inclusive weight first):\n{listed}{more}"
            )
    raise ValueError(f"no sampled {kind} matches --focus {needle!r}")


class Node:
    def __init__(self) -> None:
        self.weight = 0.0
        self.children: dict[str, Node] = {}

    def add(self, path, weight: float) -> None:
        node = self
        node.weight += weight
        for name in path:
            node = node.children.setdefault(name, Node())
            node.weight += weight


def focus_trees(stacks: collections.Counter[tuple[str, ...]], focus: str) -> tuple[Node, Node]:
    """What the focus function calls and what calls it, taken from its outermost frame in each stack, so a
    recursive function counts every sample once and its inner calls appear among its own callees."""
    callers, callees = Node(), Node()
    for stack, weight in stacks.items():
        if focus in stack:
            index = stack.index(focus)
            callers.add(reversed(stack[:index]), weight)
            callees.add(stack[index + 1:], weight)
    return callers, callees


class Tree:
    """How one focus tree is printed. `rest` names the part of a node's time that none of its children has, for
    the focus function and for every function in the tree that has children, so each level adds up. `root_rest`,
    when given, names that part for the focus row alone."""

    def __init__(self, rest: str, max_depth: int, limit: int, floor: float, root_rest: str | None = None) -> None:
        self.rest = rest
        self.root_rest = root_rest or rest
        self.max_depth, self.limit, self.floor = max_depth, limit, floor
        # (depth, label, weight, whether the label is a function name)
        self.rows: list[tuple[int, str, float, bool]] = []

    def add(self, node: Node, depth: int = 0) -> None:
        """Rows under `node`, heaviest first; siblings past `limit` or under `floor` are folded into one row."""
        entries = [(name, child.weight, child) for name, child in node.children.items()]
        remainder = node.weight - sum(child.weight for child in node.children.values())
        # A leaf below the focus function is all rest, which its own row already says.
        if remainder > 1e-9 and (depth == 0 or node.children):
            entries.append((self.root_rest if depth == 0 else self.rest, remainder, None))
        entries.sort(key=lambda entry: (-entry[1], entry[0]))
        shown = [entry for entry in entries[:self.limit] if entry[1] >= self.floor]
        folded = entries[len(shown):]
        for name, weight, child in shown:
            self.rows.append((depth, name, weight, child is not None))
            if child is not None and depth + 1 < self.max_depth:
                self.add(child, depth + 1)
        if folded:
            self.rows.append((depth, f"({len(folded)} more)", sum(entry[1] for entry in folded), False))


def print_tree(title: str, node: Node, tree: Tree, total: float) -> None:
    print(title)
    print("Percent\tOfFocus\tWeight\tFunction")
    tree.add(node)
    shown = display_names(sorted({label for _, label, _, function in tree.rows if function}))
    for depth, label, weight, function in tree.rows:
        percent = weight * 100 / total if total else 0.0
        of_focus = weight * 100 / node.weight if node.weight else 0.0
        print(f"{percent:.2f}%\t{of_focus:.2f}%\t{weight:.2f}\t{'  ' * depth}{shown[label] if function else label}")


def print_focus(capture: Capture, on_stack: bool, measure: str, needle: str, depth: int, limit: int) -> None:
    exclusive, inclusive, total = capture.rankings(on_stack)
    focus = match_focus(needle, inclusive)
    callers, callees = focus_trees(capture.stacks(on_stack), focus)
    floor = inclusive[focus] * FOCUS_MIN_SHARE

    def share(value: float) -> str:
        return f"{value * 100 / total if total else 0.0:.2f}%\t{value:.2f}"

    print(f"Focus\t{focus}")
    print(f"FocusInclusive\t{share(inclusive[focus])}")
    print(f"FocusExclusive\t{share(exclusive[focus])}")
    print(
        f"Measure\tPercent is a share of all {measure}, OfFocus a share of the focus function's inclusive time, "
        f"taken from its outermost call in each stack; indentation is one call level; rows past {limit} per "
        f"level or under {FOCUS_MIN_SHARE:.0%} of the focus function are folded into '(N more)'"
    )
    print()
    print_tree(
        f"=== Callers of the focus function, {depth} levels up, by inclusive {measure} ===",
        callers, Tree("(no caller: outermost managed frame)", depth, limit, floor), total,
    )
    print()
    print_tree(
        f"=== Callees of the focus function, {depth} levels down, by inclusive {measure} ===",
        callees, Tree("(self)", depth, limit, floor), total,
    )


def print_report(capture: Capture, limit: int, focus: str | None = None, depth: int = 8) -> None:
    on_stack = capture.untagged
    totals = capture.totals
    print(f"Source\t{capture.path.resolve()}")
    print(f"Unit\t{capture.unit}")
    print(f"Runtime\t{capture.runtime or 'unknown'}")
    # WallClockDuration is the capture window. Every other time here is summed across threads,
    # so on a multi-threaded target they exceed it — SampledThreadTime by roughly the thread
    # count. Reporting only the sum (as "ProfileDuration") read as elapsed time and was wrong.
    print(f"Threads\t{len(capture.threads)}")
    if capture.selected is not capture.threads:
        print(f"SelectedThread\t{capture.selected[0].name}")
    print(f"WallClockDuration\t{capture.wall_clock:.2f}")
    print(f"SampledThreadTime\t{sum(thread.duration for thread in capture.selected):.2f}")
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
        measure = "on-stack time (running and blocked)"
    else:
        measure = "managed CPU"
    if capture.async_stitched:
        print("Warning\t" + ASYNC_WARNING.format(which="capture"))
    if focus is not None:
        print_focus(capture, on_stack, measure, focus, depth, limit)
        return
    exclusive, inclusive, total = capture.rankings(on_stack)
    if capture.selected is capture.threads:
        print()
        print_threads(capture.threads, on_stack, limit)
    print()
    print_ranking(f"=== Top {limit} functions by exclusive {measure} ===", exclusive, total, limit)
    print()
    print_ranking(f"=== Top {limit} functions by inclusive {measure} ===", inclusive, total, limit)


# The profile `dotnet-trace convert --format Json` writes from the GCAllocationTick events of a capture.
ALLOCATION_PROFILE = "Allocations"
MB = 1024 * 1024
NO_MANAGED_FRAME = "[no managed frame]"


class Allocations:
    """The allocation profile of a `--format Json` file: one sample per GCAllocationTick, weighted by the bytes
    allocated since the previous tick, with the allocated type and kind (`System.String (Small)`) as its leaf.
    Weights are kept in MB, so the focus trees print them like any other weight."""

    def __init__(self, path: Path, nettrace: Path | None) -> None:
        document = json.loads(path.read_text(encoding="utf-8"))
        names = [frame.get("name", "<unnamed>") for frame in document["shared"]["frames"]]
        profiles = document.get("profiles", [])
        allocations = [profile for profile in profiles if profile.get("name", "").startswith(ALLOCATION_PROFILE)]
        if not allocations:
            raise ValueError(
                f"no allocation samples in {path}: capture with --profile gc-verbose, which records GCAllocationTick"
            )
        self.path = path
        self.total = 0.0
        self.samples = 0
        self.types: collections.Counter[str] = collections.Counter()
        self.exclusive: collections.Counter[str] = collections.Counter()
        self.inclusive: collections.Counter[str] = collections.Counter()
        # How many samples each row rests on: a row backed by a handful is an estimate of a handful of ticks.
        self.type_samples: collections.Counter[str] = collections.Counter()
        self.exclusive_samples: collections.Counter[str] = collections.Counter()
        self.inclusive_samples: collections.Counter[str] = collections.Counter()
        # Whole stacks, root first and ending in the type, for the trees under --focus.
        self.stacks: collections.Counter[tuple[str, ...]] = collections.Counter()
        for profile in allocations:
            for stack, weight in zip(profile.get("samples", []), profile.get("weights", [])):
                if not stack or weight <= 0:
                    continue
                size = float(weight) / MB
                frames = [names[index] for index in stack]
                allocated = frames.pop()
                frames = [name for name in frames if not is_structural(name)]
                self.total += size
                self.samples += 1
                self.types[allocated] += size
                self.type_samples[allocated] += 1
                allocator = frames[-1] if frames else NO_MANAGED_FRAME
                self.exclusive[allocator] += size
                self.exclusive_samples[allocator] += 1
                for name in dict.fromkeys(frames):
                    self.inclusive[name] += size
                    self.inclusive_samples[name] += 1
                self.stacks[(*frames, allocated)] += size
        # The thread profiles run on the capture's clock; the CPU and allocation profiles are sums of weights.
        timed = [profile for profile in profiles if profile.get("type") == "evented"]
        self.wall_clock = (
            max(float(p.get("endValue", 0)) for p in timed) - min(float(p.get("startValue", 0)) for p in timed)
            if timed else None
        )
        self.runtime = runtime_version(nettrace) if nettrace else None


def print_allocation_ranking(
    title: str, column: str, values: collections.Counter[str], samples: collections.Counter[str], total: float,
    limit: int,
) -> None:
    print(title)
    print(f"Percent\tMB\tSamples\t{column}")
    ranked = sorted(values.items(), key=lambda item: (-item[1], item[0]))[:limit]
    if not ranked:
        print("(no allocation samples)")
        return
    shown = display_names([name for name, _ in ranked])
    for name, value in ranked:
        print(f"{value * 100 / total if total else 0.0:.2f}%\t{value:.2f}\t{samples[name]}\t{shown[name]}")


def print_allocation_focus(allocations: Allocations, needle: str, depth: int, limit: int) -> None:
    # A type is a leaf of every stack, so focusing on it answers "who allocates this"; it has no callees.
    candidates = allocations.inclusive + allocations.types
    focus = match_focus(needle, candidates, "function or type", "functions or types")
    is_type = focus in allocations.types
    callers, callees = focus_trees(allocations.stacks, focus)
    floor = candidates[focus] * FOCUS_MIN_SHARE
    total = allocations.total

    def share(value: float) -> str:
        return f"{value * 100 / total if total else 0.0:.2f}%\t{value:.2f}"

    print(f"Focus\t{focus}")
    if is_type:
        print(f"FocusBytes\t{share(allocations.types[focus])}")
    else:
        print(f"FocusInclusive\t{share(allocations.inclusive[focus])}")
        print(f"FocusExclusive\t{share(allocations.exclusive[focus])}")
    print(
        "Measure\tPercent is a share of AllocatedMB, OfFocus a share of the focus row's MB, taken from its outermost "
        f"call in each stack; indentation is one call level; rows past {limit} per level or under "
        f"{FOCUS_MIN_SHARE:.0%} of the focus row are folded into '(N more)'"
    )
    print()
    # A type's stack starts at the type only when the tick had no managed frame, which the rankings name the same way.
    # Further up, a caller's own share is still the stacks it was the outermost frame of.
    print_tree(
        f"=== Callers of the focus {'type' if is_type else 'function'}, {depth} levels up, by allocated MB ===",
        callers,
        Tree("(no caller: outermost managed frame)", depth, limit, floor, NO_MANAGED_FRAME if is_type else None),
        total,
    )
    if not is_type:
        print()
        print_tree(
            f"=== Callees of the focus function, {depth} levels down, by allocated MB; types are the leaves ===",
            callees, Tree("(self)", depth, limit, floor), total,
        )


def print_allocations(allocations: Allocations, limit: int, focus: str | None = None, depth: int = 8) -> None:
    print(f"Source\t{allocations.path.resolve()}")
    print(f"Runtime\t{allocations.runtime or 'unknown'}")
    print(f"AllocationSamples\t{allocations.samples}")
    print(f"AllocatedMB\t{allocations.total:.2f}")
    if allocations.wall_clock:
        print(f"WallClockDuration\t{allocations.wall_clock:.2f} ms")
        print(f"AllocationRate\t{allocations.total * 1000 / allocations.wall_clock:.2f} MB/s")
    print(
        "Measure\tEstimates from GCAllocationTick: the runtime raises one event per ~100 KB allocated and charges "
        "that whole amount to the object that crossed the threshold, so a row resting on few samples is noise. "
        "Types carry the runtime's allocation kind (Small, Large for the LOH, Pinned). Percent is a share of "
        "AllocatedMB"
    )
    if focus is not None:
        print_allocation_focus(allocations, focus, depth, limit)
        return
    print()
    print_allocation_ranking(
        f"=== Top {limit} types by allocated MB ===", "Type",
        allocations.types, allocations.type_samples, allocations.total, limit,
    )
    print()
    print_allocation_ranking(
        f"=== Top {limit} functions by exclusive allocated MB (the frame that allocated) ===", "Function",
        allocations.exclusive, allocations.exclusive_samples, allocations.total, limit,
    )
    print()
    print_allocation_ranking(
        f"=== Top {limit} functions by inclusive allocated MB ===", "Function",
        allocations.inclusive, allocations.inclusive_samples, allocations.total, limit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("speedscope", type=Path)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--nettrace", type=Path, help="the trace the file was converted from, to name the runtime")
    parser.add_argument("--thread", help="rank only this thread, by the id in its 'Thread (<id>)' name")
    parser.add_argument("--baseline", type=Path, help="compare against this earlier speedscope file")
    parser.add_argument("--baseline-nettrace", type=Path, help="the trace the baseline was converted from")
    parser.add_argument("--baseline-thread", help="the thread id to compare in the baseline")
    parser.add_argument("--focus", help="print the callers and callees of the one function this names; with "
                        "--allocations it may name an allocated type, which prints its callers only")
    parser.add_argument("--depth", type=int, default=8, help="the levels in each --focus tree")
    parser.add_argument("--allocations", action="store_true",
                        help="report the allocation profile of a `dotnet-trace convert --format Json` file")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.depth < 1:
        parser.error("--depth must be positive")
    if args.focus is not None and not args.focus.strip():
        parser.error("--focus needs a function name")
    if args.allocations and (args.baseline is not None or args.thread is not None):
        parser.error("--allocations reports one capture across all threads")
    if args.focus is not None and args.baseline is not None:
        parser.error("--focus reports one capture, not a comparison")
    for option in ("thread", "baseline_thread"):
        value = getattr(args, option)
        if value is not None and not value.isdigit():
            parser.error(f"--{option.replace('_', '-')} must be a numeric thread id, got {value!r}")
    if args.baseline is None and (args.baseline_nettrace or args.baseline_thread):
        parser.error("--baseline-nettrace and --baseline-thread need --baseline")
    if args.baseline is not None and (args.thread is None) != (args.baseline_thread is None):
        parser.error("a comparison selects a thread in both captures or in neither")

    try:
        if args.allocations:
            print_allocations(Allocations(args.speedscope, args.nettrace), args.limit, args.focus, args.depth)
            return 0
        current = Capture(args.speedscope, args.thread, args.nettrace)
        if args.baseline is None:
            print_report(current, args.limit, args.focus, args.depth)
        else:
            print_diff(Capture(args.baseline, args.baseline_thread, args.baseline_nettrace), current, args.limit)
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
