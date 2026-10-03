#!/usr/bin/env bash
# Run DotRush's whole-solution compiler analysis in this Claude session's language server and
# report the diagnostics it publishes, which the proxy mirrors into the session's diagnostics.json.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/dotrush-install.sh"
DOTRUSH_PROG=dotrush-diagnostics

usage() {
  cat <<'EOF'
Usage:
  dotrush-diagnostics.sh solution [count] [--hints]   analyze the loaded solution, wait for the results, report them
  dotrush-diagnostics.sh report [count] [--hints]     report the diagnostics DotRush last published, analyzing nothing
  dotrush-diagnostics.sh where              print this session's DotRush runtime dir and its state

Defaults:
  count    50 diagnostics listed, errors first
  --hints  also list hint-severity diagnostics, which are otherwise only counted

Environment:
  DOTRUSH_DIAGNOSTICS_TIMEOUT  seconds to wait for the first result (default 300)
  DOTRUSH_DIAGNOSTICS_QUIET    seconds without new results that end the analysis (default 2)

Exit status 3 from `solution` means nothing was published in time: the solution has no compiler
diagnostics, or the analysis is still running or was cancelled by an edit.
EOF
}

# This session's runtime dir, found by the CLI, which prints why when there is none and exits 1.
session_dir() {
  "$SCRIPT_DIR/dotrush-cli.sh" session --dir
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
    || dotrush_fail "DotRush has not finished loading the workspace in this session, so its code analysis is not running; wait for the load and retry (where shows load: completed when it is done)"
  # DotRush also completes a load that found nothing to load, and a chosen project can fail to load. The last load's
  # count is what is loaded now; without it the total stands in, and a missing total is a proxy that predates it.
  if [[ "$(cat "$dir/last-load-projects" 2>/dev/null || true)" == 0 ]]; then
    dotrush_fail "DotRush loaded no project in its last load; dotrush-diagnostics.sh report shows what DotRush reported about it (such as a failed restore), or choose another project with dotrush-pick-project"
  fi
  [[ -f "$dir/last-load-projects" || "$(cat "$dir/projects-loaded" 2>/dev/null || true)" != "0" ]] \
    || dotrush_fail "DotRush loaded no project in this session: none was chosen, and the workspace has no single solution or project; choose one with dotrush-pick-project"
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
    count="" hints=""
    for arg in "${@:2}"; do
      if [[ "$arg" == --hints ]]; then
        hints=--hints
      elif [[ -z "$count" ]]; then
        count="$arg"
      else
        dotrush_fail "unexpected argument: $arg (usage: $command [count] [--hints])"
      fi
    done
    count="${count:-50}"
    [[ "$count" =~ ^[1-9][0-9]*$ ]] || dotrush_fail "count must be a positive integer: $count"
    dir="$(session_dir)"
    workspace="$(cat "$dir/workspace.txt" 2>/dev/null || true)"
    if [[ "$command" == report ]]; then
      [[ -f "$dir/diagnostics.json" ]] \
        || dotrush_fail "no diagnostics.json in $dir; the proxy predates diagnostics capture"
      exec python3 "$SCRIPT_DIR/summarize-diagnostics.py" "$dir/diagnostics.json" --root "$workspace" --count "$count" $hints
    fi
    require_live_proxy "$dir"
    baseline="$(publishes "$dir/diagnostics.json")"
    inject "$dir/inject.fifo" '{"method":"dotrush/solutionDiagnostics","params":{}}'
    exec python3 "$SCRIPT_DIR/summarize-diagnostics.py" "$dir/diagnostics.json" --root "$workspace" --count "$count" $hints \
      --after "$baseline" --timeout "${DOTRUSH_DIAGNOSTICS_TIMEOUT:-300}" --quiet "${DOTRUSH_DIAGNOSTICS_QUIET:-2}"
    ;;
  where)
    exec "$SCRIPT_DIR/dotrush-cli.sh" session
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
