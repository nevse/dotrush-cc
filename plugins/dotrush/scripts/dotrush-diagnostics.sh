#!/usr/bin/env bash
# Run DotRush's whole-solution compiler analysis in this Claude session's language server and
# report the diagnostics it publishes, which the proxy mirrors into the session's diagnostics.json.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/dotrush-install.sh"

usage() {
  cat <<'EOF'
Usage:
  dotrush-diagnostics.sh solution [count]   analyze the loaded solution, wait for the results, report them
  dotrush-diagnostics.sh report [count]     report the diagnostics DotRush last published, analyzing nothing
  dotrush-diagnostics.sh where              print this session's DotRush runtime dir and its state

Defaults:
  count  50 diagnostics listed, errors first

Environment:
  DOTRUSH_DIAGNOSTICS_TIMEOUT  seconds to wait for the first result (default 300)
  DOTRUSH_DIAGNOSTICS_QUIET    seconds without new results that end the analysis (default 2)

Exit status 3 from `solution` means nothing was published in time: the solution has no compiler
diagnostics, or the analysis is still running or was cancelled by an edit.
EOF
}

# This session's runtime dir: the one recording our session id, else (no session id) our workspace.
session_dir() {
  local ws hit="" sid="${DOTRUSH_SESSION_ID:-${AGTERM_SESSION_ID:-}}"
  ws="$(dotrush_data_dir)/ws"
  if [[ -n "$sid" ]]; then
    hit="$(grep -lFx -- "$sid" "$ws"/sess-*/session.txt 2>/dev/null | head -1 || true)"
  fi
  if [[ -z "$hit" ]]; then
    hit="$(grep -lFx -- "${CLAUDE_PROJECT_DIR:-$PWD}" "$ws"/*/workspace.txt 2>/dev/null | head -1 || true)"
  fi
  [[ -n "$hit" ]] || dotrush_fail "no DotRush language server has started in this session (looked in $ws); run any C# LSP operation first"
  dirname "$hit"
}

# Fails unless the proxy that owns dir is running and captures diagnostics.
require_live_proxy() {
  local dir="$1" pid
  pid="$(cat "$dir/pid" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null \
    || dotrush_fail "the DotRush language server for this session is not running; run any C# LSP operation to start it"
  [[ -f "$dir/diagnostics.json" ]] \
    || dotrush_fail "the running DotRush proxy predates diagnostics capture; restart Claude Code to load the updated plugin"
  # DotRush starts its analysis worker only when its first project load completes; before that a
  # request would sit in its queue until the timeout.
  [[ -f "$dir/load-completed" ]] \
    || dotrush_fail "DotRush has not finished loading a project in this session, so its code analysis is not running; choose one with dotrush-pick-project, or wait for the load to finish and retry"
}

publishes() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("publishes", 0))' "$1"
}

# Writes one line to the proxy's FIFO without blocking when nothing reads it.
inject() {
  python3 - "$1" "$2" <<'EOF'
import os, sys
try:
    fd = os.open(sys.argv[1], os.O_WRONLY | os.O_NONBLOCK)
except OSError as e:
    sys.exit(f"dotrush: cannot write to {sys.argv[1]}: {e}")
os.write(fd, (sys.argv[2] + "\n").encode())
os.close(fd)
EOF
}

command="${1:-}"
case "$command" in
  solution|report)
    count="${2:-50}"
    [[ "$count" =~ ^[1-9][0-9]*$ ]] || dotrush_fail "count must be a positive integer: $count"
    dir="$(session_dir)"
    workspace="$(cat "$dir/workspace.txt" 2>/dev/null || true)"
    if [[ "$command" == report ]]; then
      [[ -f "$dir/diagnostics.json" ]] \
        || dotrush_fail "no diagnostics.json in $dir; the proxy predates diagnostics capture"
      exec python3 "$SCRIPT_DIR/summarize-diagnostics.py" "$dir/diagnostics.json" --root "$workspace" --count "$count"
    fi
    require_live_proxy "$dir"
    if [[ ! -f "$dir/target.json" && ! -f "$workspace/dotrush.config.json" ]]; then
      echo "dotrush: no project chosen for this session; DotRush analyzes only what it loaded on its own" >&2
    fi
    baseline="$(publishes "$dir/diagnostics.json")"
    inject "$dir/inject.fifo" '{"method":"dotrush/solutionDiagnostics","params":{}}'
    exec python3 "$SCRIPT_DIR/summarize-diagnostics.py" "$dir/diagnostics.json" --root "$workspace" --count "$count" \
      --after "$baseline" --timeout "${DOTRUSH_DIAGNOSTICS_TIMEOUT:-300}" --quiet "${DOTRUSH_DIAGNOSTICS_QUIET:-2}"
    ;;
  where)
    dir="$(session_dir)"
    echo "dir: $dir"
    echo "workspace: $(cat "$dir/workspace.txt" 2>/dev/null || echo unknown)"
    pid="$(cat "$dir/pid" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then echo "proxy: running (pid $pid)"; else echo "proxy: not running"; fi
    if [[ -f "$dir/load-completed" ]]; then echo "load: completed"; else echo "load: not completed (no project loaded yet, or still loading)"; fi
    if [[ -f "$dir/target.json" ]]; then echo "target: $(cat "$dir/target.json")"; else echo "target: none chosen"; fi
    if [[ -f "$dir/diagnostics.json" ]]; then echo "publishes: $(publishes "$dir/diagnostics.json")"; else echo "publishes: no capture (older proxy)"; fi
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
