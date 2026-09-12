#!/usr/bin/env bash
# Collect and summarize bounded .NET CPU traces and managed-heap snapshots.
# The diagnostic tools are installed lazily outside the user's repository.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage:
  dotrush-profile.sh tools
  dotrush-profile.sh ps [trace|gcdump]
  dotrush-profile.sh trace <pid> [duration] [output-dir]
  dotrush-profile.sh trace-report <trace.nettrace> [count]
  dotrush-profile.sh heap <pid> [output-dir]
  dotrush-profile.sh heap-report <snapshot.gcdump>
  dotrush-profile.sh heap-diff <baseline.gcdump|heapstat.txt> <current.gcdump|heapstat.txt> [count]

Defaults:
  duration    00:00:30 (hh:mm:ss or dd:hh:mm:ss, hh 00-23, mm/ss 00-59;
              mm:ss and hh>23 are rejected, TimeSpan re-reads them as bigger units)
  output-dir  $DOTRUSH_PROFILE_OUTPUT_DIR, else $CLAUDE_PLUGIN_DATA/profiles,
              else ${XDG_CACHE_HOME:-~/.cache}/dotrush-cc/profiles
  count       30

Overrides:
  DOTRUSH_TRACE_TOOL       path to dotnet-trace
  DOTRUSH_GCDUMP_TOOL      path to dotnet-gcdump
  DOTRUSH_DIAGNOSTICS_DIR  directory for lazily installed tools
EOF
}

fail() {
  echo "dotrush-profile: $*" >&2
  exit 1
}

require_pid() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]] || fail "expected a positive numeric PID, got '$1'"
}

require_count() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]] || fail "expected a positive count, got '$1'"
}

# dotnet-trace binds --duration with TimeSpan.Parse, which reinterprets any field that runs past
# its range. Three fields are the minimum, because "00:30" parses as hh:mm — 30 minutes, not 30
# seconds. Hours are capped at 23 for the same reason: "24:00:00" parses as d:hh:mm, i.e. 24 days.
# The optional leading field is the day component dotnet-trace's own help documents.
require_duration() {
  [[ "$1" =~ ^([0-9]{1,3}:)?([01]?[0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$ ]] || \
    fail "duration must use hh:mm:ss or dd:hh:mm:ss with hh 00-23 and mm/ss 00-59, got '$1' (mm:ss is rejected: dotnet-trace reads it as hh:mm; for 24 hours or more use the day field, e.g. 01:00:00:00)"
  # An all-zero duration is TimeSpan.Zero, and CollectCommand arms its stop timer only when the
  # duration differs from the default — so it would collect unbounded, which is the opposite of
  # what this check exists to guarantee.
  [[ -n "${1//[0:]/}" ]] || fail "duration must be greater than zero, got '$1'"
}

diagnostics_dir() {
  if [[ -n "${DOTRUSH_DIAGNOSTICS_DIR:-}" ]]; then
    printf '%s\n' "$DOTRUSH_DIAGNOSTICS_DIR"
  elif [[ -n "${CLAUDE_PLUGIN_DATA:-}" ]]; then
    printf '%s\n' "$CLAUDE_PLUGIN_DATA/diagnostics-tools"
  else
    local cache_root="${XDG_CACHE_HOME:-${HOME}/.cache}"
    printf '%s\n' "$cache_root/dotrush-cc/diagnostics-tools"
  fi
}

resolve_tool() {
  local name="$1"
  local override="$2"
  local configured="${!override:-}"
  local tool_dir
  tool_dir="$(diagnostics_dir)"

  if [[ -n "$configured" ]]; then
    [[ -x "$configured" ]] || fail "$override points to a non-executable file: $configured"
    printf '%s\n' "$configured"
    return
  fi

  if command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
    return
  fi

  local candidate
  for candidate in "$tool_dir/$name" "$tool_dir/$name.exe"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  command -v dotnet >/dev/null 2>&1 || fail "dotnet SDK is required"
  mkdir -p "$tool_dir"
  echo "dotrush-profile: installing $name into $tool_dir" >&2
  # Installed from the tool directory, not the caller's: the .NET CLI resolves NuGet settings
  # from the working directory up, so running this inside a repository would let its own
  # NuGet.Config decide which feed these packages come from. User- and machine-level config
  # (corporate mirrors, proxies) still applies.
  ( cd "$tool_dir" && dotnet tool install "$name" --tool-path "$tool_dir" ) >&2

  for candidate in "$tool_dir/$name" "$tool_dir/$name.exe"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  fail "$name installation completed but no executable was found in $tool_dir"
}

output_dir() {
  # Never default into $PWD: skills run from the user's repository, and the plugin
  # promises to write nothing there.
  if [[ -n "${1:-}" ]]; then
    printf '%s\n' "$1"
  elif [[ -n "${DOTRUSH_PROFILE_OUTPUT_DIR:-}" ]]; then
    printf '%s\n' "$DOTRUSH_PROFILE_OUTPUT_DIR"
  elif [[ -n "${CLAUDE_PLUGIN_DATA:-}" ]]; then
    printf '%s\n' "$CLAUDE_PLUGIN_DATA/profiles"
  else
    local cache_root="${XDG_CACHE_HOME:-${HOME}/.cache}"
    printf '%s\n' "$cache_root/dotrush-cc/profiles"
  fi
}

absolute_path() {
  local value="$1"
  local dir
  dir="$(cd "$(dirname "$value")" && pwd)"
  printf '%s/%s\n' "$dir" "$(basename "$value")"
}

heap_report_path() {
  local dump="$1"
  printf '%s.heapstat.txt\n' "${dump%.gcdump}"
}

# dotnet-trace names the converted file with Path.ChangeExtension(--output, "speedscope.json"),
# which replaces everything after the last dot of the file name and only appends when the name
# has no dot at all. Stripping a ".nettrace" suffix instead disagrees on any dotted stem.
speedscope_report_path() {
  local target="$1"
  local dir base
  dir="$(dirname "$target")"
  base="$(basename "$target")"
  base="${base%.*}"
  if [[ "$target" == */* ]]; then
    printf '%s/%s.speedscope.json\n' "$dir" "$base"
  else
    printf '%s.speedscope.json\n' "$base"
  fi
}

create_heap_report() {
  local dump="$1"
  local report
  local gcdump_tool
  [[ -f "$dump" ]] || fail "heap snapshot not found: $dump"
  report="$(heap_report_path "$dump")"
  gcdump_tool="$(resolve_tool dotnet-gcdump DOTRUSH_GCDUMP_TOOL)"
  if ! "$gcdump_tool" report "$dump" > "$report"; then
    rm -f "$report"
    fail "dotnet-gcdump report failed for $dump"
  fi
  absolute_path "$report"
}

write_trace_report() {
  local speedscope_file="$1"
  local count="$2"
  python3 "$SCRIPT_DIR/summarize-speedscope.py" "$speedscope_file" --limit "$count"
}

command_name="${1:-}"
case "$command_name" in
  tools)
    trace_tool="$(resolve_tool dotnet-trace DOTRUSH_TRACE_TOOL)"
    gcdump_tool="$(resolve_tool dotnet-gcdump DOTRUSH_GCDUMP_TOOL)"
    echo "dotnet-trace: $trace_tool"
    "$trace_tool" --version
    echo "dotnet-gcdump: $gcdump_tool"
    "$gcdump_tool" --version
    ;;

  ps)
    profiler_type="${2:-trace}"
    case "$profiler_type" in
      trace)
        tool="$(resolve_tool dotnet-trace DOTRUSH_TRACE_TOOL)"
        ;;
      gcdump|heap)
        tool="$(resolve_tool dotnet-gcdump DOTRUSH_GCDUMP_TOOL)"
        ;;
      *)
        fail "unknown profiler type '$profiler_type'; expected trace or gcdump"
        ;;
    esac
    "$tool" ps
    ;;

  trace)
    pid="${2:-}"
    duration="${3:-00:00:30}"
    require_pid "$pid"
    require_duration "$duration"
    destination="$(output_dir "${4:-}")"
    mkdir -p "$destination"
    destination="$(cd "$destination" && pwd)"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    # $$ separates concurrent captures of the same PID within one second.
    base="$destination/trace_${stamp}_${pid}_$$"
    trace_file="$base.nettrace"
    speedscope_file="$(speedscope_report_path "$trace_file")"
    report_file="$base.top30.txt"
    trace_tool="$(resolve_tool dotnet-trace DOTRUSH_TRACE_TOOL)"

    # Each artifact path is printed as soon as it exists: a later step failing must not discard
    # the pointer to a capture that already cost an attach.
    "$trace_tool" collect --process-id "$pid" --duration "$duration" --output "$trace_file"
    echo "TRACE=$trace_file"

    "$trace_tool" convert "$trace_file" --format speedscope --output "$trace_file"
    # ConvertToFormat swallows its own failure and still exits 0, so check the file itself.
    [[ -f "$speedscope_file" ]] || fail "dotnet-trace convert reported success but wrote no $speedscope_file"
    echo "SPEEDSCOPE=$speedscope_file"

    if write_trace_report "$speedscope_file" 30 > "$report_file"; then
      echo "REPORT=$report_file"
    else
      rm -f "$report_file"
      fail "report generation failed; the artifacts printed above are intact"
    fi
    ;;

  trace-report)
    trace_file="${2:-}"
    count="${3:-30}"
    [[ -f "$trace_file" ]] || fail "trace not found: $trace_file"
    require_count "$count"
    if [[ "$trace_file" == *.speedscope.json ]]; then
      speedscope_file="$trace_file"
    else
      trace_tool="$(resolve_tool dotnet-trace DOTRUSH_TRACE_TOOL)"
      speedscope_file="$(speedscope_report_path "$trace_file")"
      if [[ ! -f "$speedscope_file" ]]; then
        "$trace_tool" convert "$trace_file" --format speedscope --output "$trace_file"
        [[ -f "$speedscope_file" ]] || fail "dotnet-trace convert reported success but wrote no $speedscope_file"
      fi
    fi
    write_trace_report "$speedscope_file" "$count"
    ;;

  heap)
    pid="${2:-}"
    require_pid "$pid"
    destination="$(output_dir "${3:-}")"
    mkdir -p "$destination"
    destination="$(cd "$destination" && pwd)"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    # $$ separates concurrent captures of the same PID within one second.
    dump="$destination/heap_${stamp}_${pid}_$$.gcdump"
    gcdump_tool="$(resolve_tool dotnet-gcdump DOTRUSH_GCDUMP_TOOL)"

    # Printed before the report step: that collection already forced a full gen-2 GC on the
    # target, so its path must survive a failure to summarize it.
    "$gcdump_tool" collect --process-id "$pid" --output "$dump"
    echo "GCDUMP=$dump"

    report="$(create_heap_report "$dump")"
    echo "REPORT=$report"
    ;;

  heap-report)
    dump="${2:-}"
    create_heap_report "$dump"
    ;;

  heap-diff)
    baseline="${2:-}"
    current="${3:-}"
    count="${4:-30}"
    [[ -n "$baseline" && -n "$current" ]] || fail "heap-diff needs baseline and current files"
    require_count "$count"
    if [[ "$baseline" == *.gcdump ]]; then
      baseline="$(create_heap_report "$baseline")"
    fi
    if [[ "$current" == *.gcdump ]]; then
      current="$(create_heap_report "$current")"
    fi
    [[ -f "$baseline" ]] || fail "baseline report not found: $baseline"
    [[ -f "$current" ]] || fail "current report not found: $current"
    python3 "$SCRIPT_DIR/compare-heapstats.py" "$baseline" "$current" --limit "$count"
    ;;

  -h|--help|help)
    usage
    ;;

  *)
    usage >&2
    [[ -n "$command_name" ]] && echo "dotrush-profile: unknown command '$command_name'" >&2
    exit 2
    ;;
esac
