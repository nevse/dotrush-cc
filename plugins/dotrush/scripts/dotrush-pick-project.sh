#!/usr/bin/env bash
# Save the C# project or solution DotRush loads for this Claude session, and apply it live when the
# session's proxy is running. Paths travel as arguments, never inside shell or JSON source.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/dotrush-install.sh"
DOTRUSH_PROG=dotrush-pick-project

usage() {
  cat <<'EOF'
Usage:
  dotrush-pick-project.sh apply <absolute path> [--no-restore]
      save the project or solution to load, and send it to the running language server
  dotrush-pick-project.sh wait
      keep waiting for the load the last apply started
  dotrush-pick-project.sh show
      print the saved choice, or nothing when there is none

--no-restore skips the NuGet restore DotRush runs before loading (restored checkouts, unreachable feeds).
apply and wait return once DotRush finishes the load, or exit 3 after DOTRUSH_PICK_PROJECT_TIMEOUT seconds
(default 90) with the load still running.
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

send_reload() {
  local workspace
  workspace="$(cat "$1/workspace.txt" 2>/dev/null || true)"
  [[ -n "$workspace" ]] || dotrush_fail "the session dir records no workspace: $1"
  inject "$1/inject.fifo" "$(build_json reload "$workspace")"
}

# Records in pick-wait the proxy pid, both load counts as they are now, and whether a reload is still owed.
record_wait() {
  echo "$2 $(cat "$1/workspace-loads") $(cat "$1/projects-loaded" 2>/dev/null || echo 0) $3" > "$1/pick-wait"
}

# Waits until the proxy counts a workspace load past the one pick-wait recorded. A reload apply could not send
# yet goes out once the first load completes: that load started before apply's configuration arrived, so it
# loaded the previous choice, and only a reload loads this one.
wait_for_load() {
  local dir="$1" pid loads projects base_pid base_loads base_projects owed
  read -r base_pid base_loads base_projects owed 2>/dev/null < "$dir/pick-wait" \
    || dotrush_fail "no load to wait for in this session; run apply first"
  local timeout="${DOTRUSH_PICK_PROJECT_TIMEOUT:-90}" waited=0
  while :; do
    pid="$(cat "$dir/pid" 2>/dev/null || true)"
    [[ "$pid" == "$base_pid" ]] && kill -0 "$pid" 2>/dev/null \
      || dotrush_fail "the language server for this session stopped or restarted while loading; a restarted one loads the saved choice itself, so check again with an LSP documentSymbol"
    if [[ "$owed" == reload && -f "$dir/load-completed" ]]; then
      # The first load's progress ends before loadCompleted, so these counts already include it.
      record_wait "$dir" "$pid" none
      read -r base_pid base_loads base_projects owed < "$dir/pick-wait"
      send_reload "$dir"
      echo "applied: workspace reload sent (the first load finished with the previous choice)"
    fi
    loads="$(cat "$dir/workspace-loads" 2>/dev/null || echo 0)"
    if [[ "$owed" != reload ]] && (( loads > base_loads )); then
      projects=$(( $(cat "$dir/projects-loaded" 2>/dev/null || echo "$base_projects") - base_projects ))
      rm -f "$dir/pick-wait"
      (( projects > 0 )) \
        || dotrush_fail "DotRush finished the load without loading a project; look for errors in $dir/proxy.log"
      echo "loaded: $projects project$( (( projects == 1 )) || echo s)"
      return
    fi
    if (( waited >= timeout )); then
      echo "still loading after ${waited}s: run dotrush-pick-project.sh wait to keep waiting"
      exit 3
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
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
    # A reload before the first load completes races it, so only an initialized server gets one now. The
    # counts are taken before anything is sent, so the load this apply starts is the one that moves them.
    completed=false
    [[ -f "$dir/load-completed" ]] && completed=true
    tracked=false
    if [[ -f "$dir/workspace-loads" ]]; then
      record_wait "$dir" "$pid" "$( [[ "$completed" == true ]] && echo none || echo reload )"
      tracked=true
    fi
    inject "$dir/inject.fifo" "$(build_json config "$dir/target.json")"
    echo "applied: configuration sent"
    if [[ "$completed" == true ]]; then
      send_reload "$dir"
      echo "applied: workspace reload sent"
    fi
    if [[ "$tracked" == false ]]; then
      echo "not waited: the running DotRush proxy is older and does not count loads; restart Claude Code to load the updated plugin"
      exit 0
    fi
    wait_for_load "$dir"
    ;;
  wait)
    [[ -z "${2:-}" ]] || { usage >&2; exit 2; }
    wait_for_load "$("$SCRIPT_DIR/dotrush-cli.sh" session --dir)"
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
