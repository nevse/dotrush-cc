#!/usr/bin/env python3
"""Add elapsed time, main assembly and command line to `dotnet-trace ps` / `dotnet-gcdump ps` output.

The tools print pid, process name and executable path only, so every process started through the `dotnet` host
reads `dotnet  /usr/local/share/dotnet/dotnet`. The operating system's `ps` knows each one's arguments.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

# A command line longer than this keeps its tail: the assembly and the arguments that tell runs apart sit there.
COMMAND_LIMIT = 200
ASSEMBLY_SUFFIXES = (".dll", ".exe")


def parse_listing(text: str) -> list[tuple[int, str, str]]:
    """(pid, name, path) for each row the tool printed; lines that do not start with a pid are skipped."""
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 2 or not fields[0].isdigit():
            continue
        rows.append((int(fields[0]), fields[1], fields[2].strip() if len(fields) > 2 else ""))
    return rows


def system_details(pids: list[int]) -> dict[int, tuple[str, str]]:
    """pid -> (elapsed, command line) from `ps`; a process that already exited is missing."""
    if not pids:
        return {}
    # Every process rather than `-p <pids>`: macOS ps prints nothing at all when one listed pid is out of range.
    # -ww keeps long command lines whole.
    result = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=", "-o", "etime=", "-o", "command="],
        capture_output=True, text=True, check=False,
    )
    wanted = set(pids)
    details = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2 and fields[0].isdigit() and int(fields[0]) in wanted:
            details[int(fields[0])] = (fields[1], fields[2] if len(fields) > 2 else "")
    return details


def main_assembly(name: str, command: str) -> str:
    """The first .dll/.exe argument (`dotnet App.dll`, `dotnet exec … testhost.dll`), else the process name."""
    for word in command.split()[1:]:
        if word.lower().endswith(ASSEMBLY_SUFFIXES):
            return word.rsplit("/", 1)[-1]
    return name


def shorten(command: str) -> str:
    return command if len(command) <= COMMAND_LIMIT else "…" + command[-(COMMAND_LIMIT - 1):]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter", help="keep rows whose name, path or command line contains this (any case)")
    args = parser.parse_args()

    rows = parse_listing(sys.stdin.read())
    details = system_details([pid for pid, _, _ in rows])
    needle = args.filter.casefold() if args.filter else None
    shown = []
    for pid, name, path in rows:
        elapsed, command = details.get(pid, ("-", path))
        if needle is not None and not any(needle in text.casefold() for text in (name, path, command)):
            continue
        shown.append((pid, elapsed, name, main_assembly(name, command), shorten(command)))

    if not shown:
        if needle is None:
            print("dotrush-profile: no .NET process to attach to", file=sys.stderr)
        else:
            print(f"dotrush-profile: no .NET process matches '{args.filter}' ({len(rows)} listed)", file=sys.stderr)
        return 1
    print("PID\tELAPSED\tNAME\tASSEMBLY\tCOMMAND")
    for row in shown:
        print("\t".join(map(str, row)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
