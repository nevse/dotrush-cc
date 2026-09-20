#!/usr/bin/env bash
# Save the C# project or solution DotRush loads for this Claude session, and apply it live when the
# session's proxy is running. Paths travel as arguments, never inside shell or JSON source.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/dotrush-install.sh"

usage() {
  cat <<'EOF'
Usage:
  dotrush-pick-project.sh apply <absolute path> [--no-restore]
      save the project or solution to load, and send it to the running language server
  dotrush-pick-project.sh show
      print the saved choice, or nothing when there is none

--no-restore skips the NuGet restore DotRush runs before loading (restored checkouts, unreachable feeds).
EOF
}

# Writes one line to the proxy's FIFO without blocking when nothing reads it and without creating a file.
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

# Prints one JSON document built from argv: target <path> <restore>, config <target.json>, reload <workspace>.
build_json() {
  python3 - "$@" <<'EOF'
import json, pathlib, sys
kind = sys.argv[1]
if kind == "target":
    doc = {"projectOrSolutionFiles": [sys.argv[2]], "restoreProjectsBeforeLoading": sys.argv[3] == "true"}
elif kind == "config":
    with open(sys.argv[2]) as f:
        doc = {"method": "workspace/didChangeConfiguration",
               "params": {"settings": {"dotrush": {"roslyn": json.load(f)}}}}
else:
    doc = {"method": "dotrush/reloadWorkspace",
           "params": {"workspaceFolders": [{"uri": pathlib.Path(sys.argv[2]).as_uri(), "name": "ws"}]}}
print(json.dumps(doc))
EOF
}

command="${1:-}"
case "$command" in
  apply)
    path="${2:-}"
    restore=true
    [[ "${3:-}" == --no-restore ]] && restore=false
    [[ -z "${3:-}" || "${3:-}" == --no-restore ]] || { usage >&2; exit 2; }
    [[ "$path" == /* ]] || dotrush_fail "the project path must be absolute: $path"
    [[ -f "$path" ]] || dotrush_fail "no such project or solution file: $path"
    dir="$("$SCRIPT_DIR/dotrush-cli.sh" session --dir)"
    build_json target "$path" "$restore" > "$dir/target.json.tmp"
    mv "$dir/target.json.tmp" "$dir/target.json"
    echo "saved: $path (restore: $restore)"
    pid="$(cat "$dir/pid" 2>/dev/null || true)"
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null || [[ ! -p "$dir/inject.fifo" ]]; then
      # The proxy replays target.json when it starts the server, so nothing is lost.
      echo "not applied live: the language server for this session is not running yet; it loads this choice when it starts"
      exit 0
    fi
    inject "$dir/inject.fifo" "$(build_json config "$dir/target.json")"
    echo "applied: configuration sent"
    # A reload before the first load completes races it, so only an initialized server gets one.
    if [[ -f "$dir/load-completed" ]]; then
      workspace="$(cat "$dir/workspace.txt" 2>/dev/null || true)"
      [[ -n "$workspace" ]] || dotrush_fail "the session dir records no workspace: $dir"
      inject "$dir/inject.fifo" "$(build_json reload "$workspace")"
      echo "applied: workspace reload sent"
    fi
    ;;
  show)
    dir="$("$SCRIPT_DIR/dotrush-cli.sh" session --dir)"
    if [[ -f "$dir/target.json" ]]; then
      cat "$dir/target.json"
    fi
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
