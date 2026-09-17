#!/usr/bin/env bash
# Collect and summarize bounded .NET CPU traces and managed-heap snapshots with DotRush's own
# diagnostics tools at the DotRush version this plugin pins.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/dotrush-install.sh"

usage() {
  cat <<'EOF'
Usage:
  dotrush-profile.sh tools
  dotrush-profile.sh ps [trace|gcdump] [--filter <text>]
  dotrush-profile.sh trace <pid> [duration] [output-dir] [trace-options]
  dotrush-profile.sh trace --launch [duration] [output-dir] [trace-options] -- <command> [args...]
  dotrush-profile.sh trace-report <trace.nettrace|trace.speedscope.json> [count] [thread-id]
  dotrush-profile.sh trace-diff <baseline-trace> <current-trace> [count] [baseline-thread-id current-thread-id]
  dotrush-profile.sh heap <pid> [output-dir]
  dotrush-profile.sh heap-report <snapshot.gcdump|snapshot.gcdump.json> [count]
  dotrush-profile.sh heap-diff <baseline.gcdump|.gcdump.json> <current.gcdump|.gcdump.json> [count]

Defaults:
  duration    00:00:30 (hh:mm:ss or dd:hh:mm:ss, hh 00-23, mm/ss 00-59;
              mm:ss and hh>23 are rejected, TimeSpan re-reads them as bigger units)
  output-dir  $DOTRUSH_PROFILE_OUTPUT_DIR, else $CLAUDE_PLUGIN_DATA/profiles,
              else ${XDG_CACHE_HOME:-~/.cache}/dotrush-cc/profiles
  count       30
  thread-id   all threads; the id from a report's thread table ranks that thread alone

Trace options (anywhere before --; each at most once):
  --profile <names>     dotnet-trace profiles, comma-separated: dotnet-common,
                        dotnet-sampled-thread-time (the default pair), gc-verbose, gc-collect,
                        database; dotnet-trace list-profiles names them all
  --providers <spec>    extra EventPipe providers in dotnet-trace's --providers syntax, no spaces
  --buffersize <MB>     in-memory buffer, 256 by default; raise it when events are dropped
  The report is built from the thread-time sampler, so dotnet-sampled-thread-time is added to
  any --profile that lacks it, and --providers alone keeps the default pair.

Comparing:
  trace-diff ranks functions by how much their share of their own capture's managed CPU moved,
  in percentage points, since two captures differ in length and thread count. If either capture
  has no sample tagged managed, both are compared by time on stack. Thread ids differ between
  processes, so a comparison of one thread names it in each capture.

Processes:
  ps lists attachable processes with elapsed time, main assembly and command line (its tail
  when long); --filter keeps rows whose name, path or command line contains <text>, ignoring case.

Launch:
  trace --launch starts <command> suspended and traces it from its first instruction until it
  exits or the duration ends, whichever comes first; at the duration it is killed. Its output
  goes to stderr and its exit code is printed as EXIT=. The command must itself be the .NET
  process doing the work: SDK commands such as `dotnet test` or `dotnet run` are refused, since
  the processes they start inherit the suspended diagnostic port and hang.

Tools:
  dotnet-trace and dotnet-gcdump are DotRush's own builds at the ref pinned in
  dotrush-version.json, installed into $CLAUDE_PLUGIN_DATA/diagnostics (else the user cache)
  exactly as the language server is: downloaded when the ref is a release that ships its
  bundles, built from source otherwise (git and a .NET SDK, a few minutes). They run as
  `dotnet <tool>.dll`. Heap commands need a build whose dotnet-gcdump has --format Json.
  `tools` shows the pin and what is installed without installing anything.

Overrides:
  DOTRUSH_REF              DotRush tag or full commit SHA instead of the pinned one
  DOTRUSH_REPO             GitHub owner/repository instead of the pinned one
  DOTRUSH_DIAGNOSTICS_DIR  ready directory holding dotnet-trace.dll and dotnet-gcdump.dll
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

# DotRush's fork adds --format Json to dotnet-gcdump; released builds before it lack the option.
# The help is captured rather than piped to grep -q, which would end the pipe early and fail it
# under pipefail. stdin is closed so the host cannot swallow a caller's `while read` input.
supports_gcdump_json() {
  local help
  help="$("$DOTRUSH_DOTNET" "$1/dotnet-gcdump.dll" collect --help </dev/null 2>/dev/null)" || true
  [[ "$help" == *"--format"* ]]
}

# Prints the directory to run the tools from: DOTRUSH_DIAGNOSTICS_DIR, else the pinned DotRush
# diagnostics, installed on first use exactly as the language server is. The tools are
# framework-dependent and run through the dotnet host.
resolve_bundle() {
  local need="${1:-}" dir
  if [[ -n "${DOTRUSH_DIAGNOSTICS_DIR:-}" ]]; then
    dir="$DOTRUSH_DIAGNOSTICS_DIR"
    dotrush_component_ready diagnostics "$dir" \
      || fail "DOTRUSH_DIAGNOSTICS_DIR holds no dotnet-trace.dll and dotnet-gcdump.dll: $dir"
  else
    dir="$(dotrush_data_dir)/diagnostics"
    dotrush_install diagnostics "$dir" || exit 1
  fi
  if [[ "$need" == json ]] && ! supports_gcdump_json "$dir"; then
    fail "the dotnet-gcdump in $dir has no --format Json; pin a DotRush version that has it"
  fi
  printf '%s\n' "$dir"
}

use_bundle() {
  DOTRUSH_DOTNET="$(dotrush_dotnet)" || exit 1
  DOTRUSH_BUNDLE="$(resolve_bundle "${1:-}")" || exit 1
}

run_tool() {
  local tool="$1"
  shift
  "$DOTRUSH_DOTNET" "$DOTRUSH_BUNDLE/$tool.dll" "$@"
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

# Both tools name converted files with Path.ChangeExtension, which replaces everything after the
# last dot of the file name and only appends when the name has no dot at all. Stripping a known
# suffix instead disagrees on any dotted stem.
change_extension() {
  local target="$1" extension="$2"
  local dir base
  dir="$(dirname "$target")"
  base="$(basename "$target")"
  base="${base%.*}"
  if [[ "$target" == */* ]]; then
    printf '%s/%s.%s\n' "$dir" "$base" "$extension"
  else
    printf '%s.%s\n' "$base" "$extension"
  fi
}

speedscope_report_path() {
  change_extension "$1" speedscope.json
}

# dotnet-gcdump keeps a name that already ends in .gcdump.json and otherwise changes the
# extension, so heap.gcdump gets its graph as heap.gcdump.json.
gcdump_json_path() {
  if [[ "$1" == *.gcdump.json ]]; then
    printf '%s\n' "$1"
  else
    change_extension "$1" gcdump.json
  fi
}

# Prints the absolute path of a snapshot's heap graph, converting a .gcdump that has none yet.
ensure_gcdump_json() {
  local input="$1" graph
  [[ -n "$input" ]] || fail "expected a .gcdump or .gcdump.json snapshot"
  [[ "$input" != *.heapstat.txt ]] \
    || fail "heap-stat text reports are no longer read; pass the .gcdump it was made from instead of $input"
  [[ -f "$input" ]] || fail "heap snapshot not found: $input"
  graph="$(gcdump_json_path "$input")"
  if [[ ! -f "$graph" ]]; then
    use_bundle json
    run_tool dotnet-gcdump convert "$input" --format Json >&2 || fail "dotnet-gcdump convert failed for $input"
    [[ -f "$graph" ]] || fail "dotnet-gcdump convert reported success but wrote no $graph"
  fi
  absolute_path "$graph"
}

# Prints the speedscope file for a .nettrace or .speedscope.json, converting a .nettrace that has none yet.
ensure_speedscope() {
  local trace_file="$1" speedscope_file
  [[ -f "$trace_file" ]] || fail "trace not found: $trace_file"
  if [[ "$trace_file" == *.speedscope.json ]]; then
    printf '%s\n' "$trace_file"
    return
  fi
  speedscope_file="$(speedscope_report_path "$trace_file")"
  if [[ ! -f "$speedscope_file" ]]; then
    use_bundle
    run_tool dotnet-trace convert "$trace_file" --format speedscope --output "$trace_file" >&2
    [[ -f "$speedscope_file" ]] || fail "dotnet-trace convert reported success but wrote no $speedscope_file"
  fi
  printf '%s\n' "$speedscope_file"
}

# The .nettrace beside a speedscope file, under the name `trace` gives its files; a speedscope file from
# elsewhere simply has no runtime line.
nettrace_for() {
  if [[ "$1" == *.speedscope.json ]]; then
    printf '%s\n' "${1%.speedscope.json}.nettrace"
  else
    printf '%s\n' "$1"
  fi
}

# The .nettrace, when there is one, names the target's runtime, which the report needs to explain a
# capture whose samples all read as unmanaged.
write_trace_report() {
  local speedscope_file="$1" count="$2" trace_file="${3:-}" thread="${4:-}"
  local args=("$speedscope_file" --limit "$count")
  [[ -n "$trace_file" && -f "$trace_file" ]] && args+=(--nettrace "$trace_file")
  [[ -n "$thread" ]] && args+=(--thread "$thread")
  python3 "$SCRIPT_DIR/summarize-speedscope.py" "${args[@]}"
}

# The runtime is launched suspended on a diagnostic port whose address it passes on in the
# environment, so any .NET process the command starts suspends too and waits for a resume that never
# comes. The SDK's own verbs all work that way; a .dll, `exec` or an app's own executable does not.
require_launch_command() {
  local program="$1" first="${2:-}"
  [[ "$(basename "$program")" == dotnet ]] || return 0
  [[ "$first" == exec || "$first" == *.dll ]] && return 0
  fail "cannot launch 'dotnet ${first}': the processes it starts would hang on the suspended diagnostic port; launch the app itself (dotnet <app.dll>, or its executable), or a test project that builds to an executable (Microsoft.Testing.Platform) with its filter"
}

SAMPLER_PROFILE="dotnet-sampled-thread-time"

# Splits `trace` arguments into trace_positional, the command after `--` (trace_command, with
# trace_command_given set when `--` was present) and collect_options for dotnet-trace. Options may
# sit anywhere before `--`. dotnet-trace drops its default profiles as soon as --profile or
# --providers is given, and the report is built from the thread-time sampler alone (without it the
# converter turns other events' stacks into untagged "CPU"), so the sampler is always kept; with only
# --providers the default profiles stay too. dotnet-trace itself rejects an unknown profile name, or
# one that is only for collect-linux, before it attaches or launches anything.
parse_trace_options() {
  local profile="" providers="" buffersize=""
  trace_positional=()
  trace_command=()
  trace_command_given=""
  collect_options=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --)
        trace_command_given=1
        shift
        trace_command=("$@")
        break
        ;;
      --profile|--providers|--buffersize)
        [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || fail "$1 needs a value"
        case "$1" in
          --profile)
            [[ -z "$profile" ]] || fail "--profile given twice; list the profiles comma-separated"
            [[ "$2" =~ ^[A-Za-z0-9-]+(,[A-Za-z0-9-]+)*$ ]] || fail "--profile takes comma-separated profile names, got '$2'"
            profile="$2"
            ;;
          --providers)
            [[ -z "$providers" ]] || fail "--providers given twice; list the providers comma-separated"
            [[ "$2" != *[[:space:]]* ]] || fail "--providers must not contain whitespace, got '$2'"
            providers="$2"
            ;;
          --buffersize)
            [[ -z "$buffersize" ]] || fail "--buffersize given twice"
            [[ "$2" =~ ^[1-9][0-9]{0,5}$ ]] || fail "--buffersize takes a size in MB, got '$2'"
            buffersize="$2"
            ;;
        esac
        shift 2
        ;;
      --launch)
        fail "--launch must come right after trace"
        ;;
      -*)
        fail "unknown trace option '$1'; expected --profile, --providers or --buffersize"
        ;;
      *)
        trace_positional+=("$1")
        shift
        ;;
    esac
  done
  if [[ -n "$profile$providers" ]]; then
    profile="${profile:-dotnet-common,$SAMPLER_PROFILE}"
    [[ ",$profile," == *",$SAMPLER_PROFILE,"* ]] || profile="$profile,$SAMPLER_PROFILE"
    collect_options+=(--profile "$profile")
  fi
  [[ -z "$providers" ]] || collect_options+=(--providers "$providers")
  [[ -z "$buffersize" ]] || collect_options+=(--buffersize "$buffersize")
  return 0
}

# Converts a captured trace and writes its report next to it, printing each path as it appears.
finish_trace() {
  local trace_file="$1" speedscope_file report_file
  speedscope_file="$(speedscope_report_path "$trace_file")"
  report_file="${trace_file%.nettrace}.top30.txt"

  run_tool dotnet-trace convert "$trace_file" --format speedscope --output "$trace_file" >&2
  # ConvertToFormat swallows its own failure and still exits 0, so check the file itself.
  [[ -f "$speedscope_file" ]] || fail "dotnet-trace convert reported success but wrote no $speedscope_file"
  echo "SPEEDSCOPE=$speedscope_file"

  if write_trace_report "$speedscope_file" 30 "$trace_file" > "$report_file"; then
    echo "REPORT=$report_file"
  else
    rm -f "$report_file"
    fail "report generation failed; the artifacts printed above are intact"
  fi
}

write_heap_report() {
  python3 "$SCRIPT_DIR/analyze-gcdump.py" report "$1" --limit "$2"
}

command_name="${1:-}"
case "$command_name" in
  tools)
    DOTRUSH_DOTNET="$(dotrush_dotnet)" || exit 1
    repo="$(dotrush_repo)" || exit 1
    ref="$(dotrush_ref)" || exit 1
    origin="$(dotrush_source "$repo" "$ref")" || exit 1
    echo "dotnet: $DOTRUSH_DOTNET"
    if [[ "$origin" == release ]]; then
      echo "pinned: $repo@$ref, installed from its release bundles"
    else
      echo "pinned: $repo@$ref, built from source (needs git and a .NET SDK, a few minutes per component)"
    fi
    for component in server diagnostics; do
      dir="$(dotrush_data_dir)/$component"
      [[ "$component" != server ]] || dir="${DOTRUSH_SERVER_DIR:-$dir}"
      if [[ "$component" == diagnostics && -n "${DOTRUSH_DIAGNOSTICS_DIR:-}" ]]; then
        dir="$DOTRUSH_DIAGNOSTICS_DIR"
        state="DOTRUSH_DIAGNOSTICS_DIR"
      elif dotrush_is_current "$component" "$dir" "$ref"; then
        state="installed"
      elif dotrush_component_ready "$component" "$dir"; then
        installed="$(dotrush_installed_ref "$dir")"
        state="installed at ${installed:-an unrecorded ref}, not the pin; the next use reinstalls it"
      else
        state="not installed; the next use installs it"
      fi
      if [[ "$component" == diagnostics ]] && dotrush_component_ready diagnostics "$dir"; then
        json="no"
        supports_gcdump_json "$dir" && json="yes"
        state="$state, gcdump-json=$json"
      fi
      echo "$component: $state ($dir)"
    done
    ;;

  ps)
    shift
    profiler_type="trace"
    if [[ $# -gt 0 && "$1" != --* ]]; then
      profiler_type="$1"
      shift
    fi
    case "$profiler_type" in
      trace) tool="dotnet-trace" ;;
      gcdump|heap) tool="dotnet-gcdump" ;;
      *) fail "unknown profiler type '$profiler_type'; expected trace or gcdump" ;;
    esac
    filter_args=()
    if [[ $# -gt 0 ]]; then
      [[ "$1" == --filter && $# -eq 2 && -n "$2" ]] || fail "ps takes only --filter <text> after the profiler type, got: $*"
      filter_args=(--filter "$2")
    fi
    use_bundle
    listing="$(run_tool "$tool" ps </dev/null)" || fail "$tool ps failed"
    python3 "$SCRIPT_DIR/list-dotnet-processes.py" ${filter_args[@]+"${filter_args[@]}"} <<<"$listing"
    ;;

  trace)
    shift
    launch=""
    if [[ "${1:-}" == --launch ]]; then
      launch=1
      shift
    fi
    parse_trace_options "$@"
    if [[ -n "$launch" ]]; then
      [[ -n "$trace_command_given" ]] || fail "trace --launch needs -- before the command to launch"
      (( ${#trace_command[@]} > 0 )) || fail "trace --launch needs a command after --"
      (( ${#trace_positional[@]} <= 2 )) || fail "trace --launch takes at most a duration and an output dir before --, got: ${trace_positional[*]}"
      set -- "${trace_command[@]}"
      require_launch_command "$@"
      duration="${trace_positional[0]:-00:00:30}"
      requested_dir="${trace_positional[1]:-}"
      label="launch"
    else
      [[ -z "$trace_command_given" ]] || fail "only trace --launch takes a command after --"
      (( ${#trace_positional[@]} <= 3 )) || fail "trace takes a pid, a duration and an output dir, got: ${trace_positional[*]}"
      pid="${trace_positional[0]:-}"
      duration="${trace_positional[1]:-00:00:30}"
      requested_dir="${trace_positional[2]:-}"
      require_pid "$pid"
      label="$pid"
    fi
    require_duration "$duration"
    destination="$(output_dir "$requested_dir")"
    mkdir -p "$destination"
    destination="$(cd "$destination" && pwd)"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    # $$ separates concurrent captures of the same PID within one second.
    trace_file="$destination/trace_${stamp}_${label}_$$.nettrace"
    use_bundle

    # Each artifact path is printed as soon as it exists: a later step failing must not discard
    # the pointer to a capture that already cost an attach.
    # The tools narrate to stdout; stdout here is the KEY=value contract the skills parse, so
    # their chatter goes to stderr alongside our own progress messages.
    if [[ -n "$launch" ]]; then
      # dotnet-trace exits with the child's code, so a failing command still leaves a trace; the
      # file, not the code, says whether the capture worked.
      status=0
      run_tool dotnet-trace collect --duration "$duration" --output "$trace_file" ${collect_options[@]+"${collect_options[@]}"} \
        --show-child-io -- "$@" </dev/null >&2 || status=$?
      [[ -f "$trace_file" ]] || fail "dotnet-trace wrote no $trace_file (exit $status)"
      echo "TRACE=$trace_file"
      echo "EXIT=$status"
    else
      run_tool dotnet-trace collect --process-id "$pid" --duration "$duration" --output "$trace_file" \
        ${collect_options[@]+"${collect_options[@]}"} >&2
      echo "TRACE=$trace_file"
    fi
    finish_trace "$trace_file"
    ;;

  trace-report)
    trace_file="${2:-}"
    count="${3:-30}"
    thread="${4:-}"
    [[ -f "$trace_file" ]] || fail "trace not found: $trace_file"
    require_count "$count"
    [[ -z "$thread" || "$thread" =~ ^[0-9]+$ ]] || fail "expected a numeric thread id, got '$thread'"
    speedscope_file="$(ensure_speedscope "$trace_file")" || exit 1
    write_trace_report "$speedscope_file" "$count" "$(nettrace_for "$trace_file")" "$thread"
    ;;

  trace-diff)
    baseline="${2:-}"
    current="${3:-}"
    count="${4:-30}"
    [[ -n "$baseline" && -n "$current" ]] || fail "trace-diff needs baseline and current traces"
    (( $# <= 6 )) || fail "trace-diff takes at most a count and two thread ids after the traces"
    require_count "$count"
    args=(--limit "$count")
    if (( $# > 4 )); then
      (( $# == 6 )) || fail "trace-diff needs a thread id for each capture: <baseline-thread-id> <current-thread-id>"
      [[ "$5" =~ ^[0-9]+$ && "$6" =~ ^[0-9]+$ ]] || fail "expected numeric thread ids, got '$5' and '$6'"
      args+=(--baseline-thread "$5" --thread "$6")
    fi
    baseline_speedscope="$(ensure_speedscope "$baseline")" || exit 1
    current_speedscope="$(ensure_speedscope "$current")" || exit 1
    baseline_nettrace="$(nettrace_for "$baseline")"
    current_nettrace="$(nettrace_for "$current")"
    [[ -f "$baseline_nettrace" ]] && args+=(--baseline-nettrace "$baseline_nettrace")
    [[ -f "$current_nettrace" ]] && args+=(--nettrace "$current_nettrace")
    python3 "$SCRIPT_DIR/summarize-speedscope.py" "$current_speedscope" --baseline "$baseline_speedscope" "${args[@]}"
    ;;

  heap)
    pid="${2:-}"
    require_pid "$pid"
    destination="$(output_dir "${3:-}")"
    mkdir -p "$destination"
    destination="$(cd "$destination" && pwd)"
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    # $$ separates concurrent captures of the same PID within one second.
    base="$destination/heap_${stamp}_${pid}_$$"
    dump="$base.gcdump"
    graph="$(gcdump_json_path "$dump")"
    report_file="$base.report.txt"
    use_bundle json

    # Printed before the report step: that collection already forced a full gen-2 GC on the
    # target, so its paths must survive a failure to summarize it.
    run_tool dotnet-gcdump collect --process-id "$pid" --output "$dump" --format Json >&2
    echo "GCDUMP=$dump"
    [[ -f "$graph" ]] || fail "dotnet-gcdump collect wrote no $graph; the .gcdump above is intact"
    echo "GCDUMP_JSON=$graph"

    if write_heap_report "$graph" 30 > "$report_file"; then
      echo "REPORT=$report_file"
    else
      rm -f "$report_file"
      fail "report generation failed; the artifacts printed above are intact"
    fi
    ;;

  heap-report)
    count="${3:-30}"
    require_count "$count"
    graph="$(ensure_gcdump_json "${2:-}")" || exit 1
    write_heap_report "$graph" "$count"
    ;;

  heap-diff)
    baseline="${2:-}"
    current="${3:-}"
    count="${4:-30}"
    [[ -n "$baseline" && -n "$current" ]] || fail "heap-diff needs baseline and current snapshots"
    require_count "$count"
    baseline="$(ensure_gcdump_json "$baseline")" || exit 1
    current="$(ensure_gcdump_json "$current")" || exit 1
    python3 "$SCRIPT_DIR/analyze-gcdump.py" diff "$baseline" "$current" --limit "$count"
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
