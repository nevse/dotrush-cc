#!/usr/bin/env python3
"""PreToolUse hook: sends a text search for a C# symbol to the LSP instead.

Claude Code reaches for Grep (or grep/rg in Bash) to find a symbol even with a language server running: the LSP tool
is deferred and asks for a file position, so a search comes first and the LSP is never called. This hook denies a
recursive search whose pattern is only C# identifiers, in a C# workspace, and says how to get the same answer from
DotRush: workspace/symbol through the plugin's CLI for the position, then the LSP tool. The same search sent again is
let through, so a text match (a string, a comment) is one retry away.

Usage: lsp-first.py <state dir>. Reads the hook input on stdin; prints a deny decision or nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(PLUGIN_ROOT, "scripts", "dotrush-cli.sh")
PROJECT_FILES = (".sln", ".slnx", ".slnf", ".csproj")
SKIP_DIRS = {".git", "bin", "obj", "node_modules", ".vs", ".idea"}
# Declaration keywords a symbol search often starts with: `class Money`, `new Money(`.
KEYWORDS = r"(?:class|interface|record|struct|enum|new|void|async|static|public|private|internal|protected|override)"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STATE_MAX_AGE = 24 * 60 * 60
GREP_PROGRAMS = {"grep", "egrep", "ugrep", "rg"}
# grep and rg options that take a value as the next argument.
VALUE_OPTIONS = {"-e", "-f", "-A", "-B", "-C", "-m", "--max-count", "-g", "--glob", "-t", "--type", "--include",
                 "--exclude", "--exclude-dir", "-T", "--type-not", "-d", "-D", "--color", "--colour"}


def symbol_names(pattern):
    """The identifiers a pattern searches for, or None when it is a real regex or does not look like C# code."""
    pattern = pattern.strip()
    if pattern.startswith("(") and pattern.endswith(")"):
        pattern = pattern[1:-1]
    names = []
    for part in re.split(r"\\?\|", pattern):
        part = re.sub(r"\\[bB<>]|[\^$]", "", part.strip())
        part = re.sub(rf"^{KEYWORDS}(?:\\s\+|\\s\*|\s)+", "", part)
        part = re.sub(r"(?:\\s\*|\\s\+|\s)*\\?\(\s*$", "", part)
        part = part.replace("\\.", ".")
        segments = part.split(".")
        if not all(IDENTIFIER.fullmatch(s) for s in segments):
            return None
        name = segments[-1]
        pascal = name[0].isupper() and any(c.islower() for c in name) and len(name) >= 3
        camel = name[0].islower() and any(c.isupper() for c in name)
        if not (pascal or camel):
            return None
        names.append(name)
    return names or None


def has_csharp_project(root, depth=3):
    root = os.path.abspath(root)
    base = root.count(os.sep)
    for current, dirs, files in os.walk(root):
        if any(f.endswith(PROJECT_FILES) for f in files):
            return True
        if current.count(os.sep) - base >= depth:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
    return False


def in_csharp_scope(filters, paths, cwd):
    """True when the search covers C# code: a C# filter, a .cs file, or a workspace holding a C# project."""
    if filters:
        return any(f.lower() in ("cs", "csharp") or re.search(r"\.cs\b|[{,]cs\b", f.lower()) for f in filters)
    # A file, or a path naming one by its extension, decides by that extension.
    files = [p for p in paths if os.path.isfile(os.path.join(cwd, p))
             or (os.path.splitext(p)[1] and not os.path.isdir(os.path.join(cwd, p)))]
    if files:
        return any(p.endswith(".cs") for p in files)
    return has_csharp_project(cwd)


def grep_search(tool_input):
    filters = [f for f in (tool_input.get("glob"), tool_input.get("type")) if f]
    path = tool_input.get("path")
    return tool_input.get("pattern") or "", filters, [path] if path else []


def bash_searches(command):
    """Each recursive grep, rg or git grep in a command, as (pattern, filters, paths)."""
    searches = []
    for segment in re.split(r"\|\||&&|[|;]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if not tokens:
            continue
        if tokens[:2] == ["git", "grep"]:
            args, recursive = tokens[2:], True
        elif os.path.basename(tokens[0]) in GREP_PROGRAMS:
            args, recursive = tokens[1:], os.path.basename(tokens[0]) == "rg"
        else:
            continue
        pattern, filters, rest, i = None, [], [], 0
        while i < len(args):
            arg = args[i]
            if arg == "--":
                rest.extend(args[i + 1:])
                break
            name, eq, value = arg.partition("=")
            if arg.startswith("--") and eq:
                if name in ("--include", "--glob", "--type"):
                    filters.append(value)
                elif name == "--regexp":
                    pattern = value
                elif name == "--recursive":
                    recursive = True
            elif arg in VALUE_OPTIONS and i + 1 < len(args):
                value = args[i + 1]
                if arg == "-e":
                    pattern = value
                elif arg in ("-g", "--glob", "-t", "--type", "--include"):
                    filters.append(value)
                i += 1
            elif arg.startswith("--"):
                recursive = recursive or arg in ("--recursive", "--dereference-recursive")
            elif arg.startswith("-") and len(arg) > 1:
                recursive = recursive or "r" in arg[1:] or "R" in arg[1:]
            else:
                rest.append(arg)
            i += 1
        if pattern is None and rest:
            pattern, rest = rest[0], rest[1:]
        if recursive and pattern:
            searches.append((pattern, filters, rest))
    return searches


def seen_before(state_dir, session_id, key):
    """Records the search; True when this session already sent it, which lets the repeat through."""
    path = os.path.join(state_dir, "lsp-first", f"{hashlib.sha1(session_id.encode()).hexdigest()[:16]}.json")
    try:
        with open(path) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    now = time.time()
    seen = {k: t for k, t in seen.items() if now - t < STATE_MAX_AGE}
    digest = hashlib.sha1(key.encode()).hexdigest()
    repeat = digest in seen
    seen[digest] = now
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(seen, f)
        os.replace(tmp, path)
    except OSError:
        return True  # without a record a repeat could not get through, so never deny
    return repeat


def reason(names):
    name = names[0]
    query = json.dumps({"query": name})
    return (
        f"dotrush: `{'`, `'.join(names)}` looks like a C# symbol, and this workspace has the DotRush C# language server. "
        "Ask it instead of searching text: it returns only the real declarations and references, in one call.\n"
        f"1. Where it is declared: \"{CLI}\" request workspace/symbol '{query}' "
        "(prints file URIs with 0-based line and character).\n"
        "2. At that position (the LSP tool takes 1-based line and character): findReferences, goToDefinition, "
        "goToImplementation, incomingCalls/outgoingCalls or hover. Ask for several symbols in parallel.\n"
        "If the symbol is not C#, or you need a plain text match (a string, a comment, a non-C# file), send this same "
        "search again unchanged; a repeat is let through."
    )


def decide(hook_input, state_dir):
    tool = hook_input.get("tool_name")
    tool_input = hook_input.get("tool_input") or {}
    cwd = hook_input.get("cwd") or os.getcwd()
    if tool == "Grep":
        searches, key = [grep_search(tool_input)], json.dumps(tool_input, sort_keys=True)
    elif tool == "Bash":
        command = tool_input.get("command") or ""
        searches, key = bash_searches(command), command
    else:
        return None
    for pattern, filters, paths in searches:
        names = symbol_names(pattern)
        if names and in_csharp_scope(filters, paths, cwd):
            if seen_before(state_dir, hook_input.get("session_id") or "none", f"{tool}\0{key}"):
                return None
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason(names)}}
    return None


def main():
    state_dir = (sys.argv[1] if len(sys.argv) > 1 else "") or os.path.join(os.path.expanduser("~"), ".cache", "dotrush-cc")
    try:
        hook_input = json.load(sys.stdin)
        decision = decide(hook_input, state_dir)
    except Exception:  # a broken hook must never block a search
        return
    if decision:
        print(json.dumps(decision))


if __name__ == "__main__":
    main()
