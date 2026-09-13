#!/usr/bin/env python3
"""Summarize the diagnostics the DotRush proxy mirrors into diagnostics.json.

The proxy keeps the latest textDocument/publishDiagnostics list per file together with a running
publish count. With --after, wait until the count passes that baseline and publishing has been quiet
for --quiet seconds before reporting, since DotRush sends no signal when an analysis finishes.
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse

SEVERITIES = {1: "error", 2: "warning", 3: "info", 4: "hint"}
NO_PUBLISH_EXIT = 3


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise SystemExit(f"{path}: not a diagnostics file: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise SystemExit(f"{path}: not a diagnostics file: no 'files' object")
    return data


def wait_for_burst(path, after, timeout, quiet, poll=0.25):
    """Return the store once a publish past `after` has been followed by `quiet` seconds of silence,
    or None when nothing past `after` arrives within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        data = load(path)
        if data and data.get("publishes", 0) > after and time.time() - data.get("updated", 0) >= quiet:
            return data
        if data is None or data.get("publishes", 0) <= after:
            if time.monotonic() >= deadline:
                return None
        time.sleep(poll)


def display_path(uri, root):
    parsed = urlparse(uri)
    path = Path(unquote(parsed.path)) if parsed.scheme == "file" else Path(uri)
    if root:
        try:
            return str(path.resolve().relative_to(Path(root).resolve()))
        except ValueError:
            pass
    return str(path)


def code_of(diagnostic):
    code = diagnostic.get("code")
    return "-" if code is None or code == "" else str(code)


def report(data, root, count, hints=False):
    rows = []
    for uri, diagnostics in data["files"].items():
        path = display_path(uri, root)
        for d in diagnostics:
            start = (d.get("range") or {}).get("start") or {}
            rows.append((
                d.get("severity") or 1,
                path,
                start.get("line", 0) + 1,
                start.get("character", 0) + 1,
                code_of(d),
                (d.get("message") or "").strip().splitlines()[0] if d.get("message") else "",
            ))
    rows.sort()

    severities = Counter(SEVERITIES.get(r[0], "error") for r in rows)
    # Hints are mostly unnecessary usings, many in generated obj/ files; count them but list them only on request.
    shown = rows if hints else [r for r in rows if r[0] != 4]
    hidden = len(rows) - len(shown)
    out = [
        f"Files: {len({r[1] for r in shown})}  "
        + "  ".join(f"{name.capitalize()}s: {severities.get(name, 0)}" for name in SEVERITIES.values()),
        f"Publishes: {data.get('publishes', 0)}",
    ]
    if not shown:
        out.append("")
        out.append("No diagnostics." if not rows else "Only hints; pass --hints to list them.")
        return "\n".join(out)

    codes = Counter((code_of_row, SEVERITIES.get(sev, "error")) for sev, _, _, _, code_of_row, _ in shown)
    out.append("")
    out.append("By code:")
    for (code, severity), n in sorted(codes.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(f"  {n:>5}  {severity:<7}  {code}")

    out.append("")
    out.append("Diagnostics (errors first):")
    for severity, path, line, column, code, message in shown[:count]:
        out.append(f"  {path}:{line}:{column}  {SEVERITIES.get(severity, 'error')}  {code}  {message}")
    if len(shown) > count:
        out.append(f"  ... {len(shown) - count} more; pass a larger count")
    if hidden:
        out.append(f"  {hidden} hint{'' if hidden == 1 else 's'} hidden; pass --hints to list")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("diagnostics", help="diagnostics.json written by the DotRush proxy")
    parser.add_argument("--count", type=int, default=50, help="diagnostics to list (default 50)")
    parser.add_argument("--root", help="print paths relative to this directory")
    parser.add_argument("--hints", action="store_true", help="also list hint-severity diagnostics")
    parser.add_argument("--after", type=int, help="wait for a publish past this count before reporting")
    parser.add_argument("--timeout", type=float, default=300, help="seconds to wait for the first publish (default 300)")
    parser.add_argument("--quiet", type=float, default=2, help="seconds without publishes that end a burst (default 2)")
    args = parser.parse_args()

    if args.after is None:
        data = load(args.diagnostics)
        if data is None:
            raise SystemExit(f"{args.diagnostics}: no diagnostics file; the proxy predates diagnostics capture or never started")
    else:
        data = wait_for_burst(args.diagnostics, args.after, args.timeout, args.quiet)
        if data is None:
            print(f"No diagnostics were published within {args.timeout:g}s.")
            return NO_PUBLISH_EXIT
    print(report(data, args.root, args.count, args.hints))
    return 0


if __name__ == "__main__":
    sys.exit(main())
