#!/usr/bin/env bash
# dotrush-cli.sh — runs DotRushCli, the plugin's C# command-line tool, building it on first use.
#
# The build lands in <data>/cli/<hash of the tool's sources>/, so sessions running different plugin
# versions each keep their own build and never rebuild over each other. The build runs from tools/,
# whose empty Directory.* files keep MSBuild from importing anything from the user's parent
# directories, and where the user repo's global.json does not apply.
#
# Environment:
#   DOTRUSH_CLI_DIR  run DotRushCli.dll from this directory and build nothing
#   DOTRUSH_DOTNET   the dotnet host to build and run with (default: dotnet on PATH)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/dotrush-install.sh"

cli_fail() {
  echo "dotrush-cli: $*" >&2
  exit 1
}

# sha256 over the sorted list of files under tools/ and their contents, leaving out bin/ and obj/.
cli_source_hash() {
  (
    cd "$1"
    find . -type d \( -name bin -o -name obj \) -prune -o -type f -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 shasum -a 256
  ) | shasum -a 256 | cut -d ' ' -f 1
}

cli_build_locked() {
  local tools="$1" data="$2" hash="$3" dotnet="$4"
  local target="$data/cli/$hash" staging="$data/cli/$hash.new.$$" log="$data/cli-build.log"
  # Another session may have built these sources while this one waited for the lock.
  [[ ! -f "$target/DotRushCli.dll" ]] || return 0
  echo "dotrush-cli: building the DotRush CLI (once per plugin version)" >&2
  if ! (cd "$tools" && "$dotnet" build DotRushCli/DotRushCli.csproj -c Release --nologo \
          --artifacts-path "$data/cli-artifacts/$hash" -o "$staging") </dev/null >"$log" 2>&1 \
      || [[ ! -f "$staging/DotRushCli.dll" ]]; then
    rm -rf "$staging"
    tail -n 20 "$log" >&2
    cli_fail "building the DotRush CLI failed; full log: $log"
  fi
  rm -rf "$target"
  mv "$staging" "$target"
  rm -f "$log"
  # Builds of other sources, such as earlier plugin versions, that nothing rebuilt for a day.
  find "$data/cli" "$data/cli-artifacts" -mindepth 1 -maxdepth 1 ! -name "$hash" -mmin +1440 \
    -exec rm -rf {} + 2>/dev/null || true
}

# Sessions share the data dir, so the build holds <data>/cli.lock, which records its PID: a lock left
# by a killed process is taken over, as dotrush_install does.
cli_build() {
  local tools="$1" data="$2" hash="$3" dotnet="$4" lock="$2/cli.lock" holder waited=0
  mkdir -p "$data/cli"
  until mkdir "$lock" 2>/dev/null; do
    holder="$(cat "$lock/pid" 2>/dev/null || true)"
    if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
      rm -rf "$lock"
      continue
    fi
    (( waited < 600 )) || cli_fail "timed out waiting for another session to release $lock"
    (( waited > 0 )) || echo "dotrush-cli: waiting for another session building the DotRush CLI" >&2
    waited=$((waited + 1))
    sleep 1
  done
  echo "$$" > "$lock/pid"
  # cli_fail exits from inside the build, and the trap cleans up on that path too.
  trap "rm -rf $(printf '%q' "$lock") $(printf '%q' "$data/cli/$hash.new.$$")" EXIT
  cli_build_locked "$tools" "$data" "$hash" "$dotnet"
  trap - EXIT
  rm -rf "$lock"
}

plugin="$(cd "$SCRIPT_DIR/.." && pwd)"
tools="$plugin/tools"
data="$(dotrush_data_dir)"
dotnet="$(dotrush_dotnet)"

if [[ -n "${DOTRUSH_CLI_DIR:-}" ]]; then
  dir="$DOTRUSH_CLI_DIR"
else
  hash="$(cli_source_hash "$tools")"
  [[ "$hash" =~ ^[0-9a-f]{64}$ ]] || cli_fail "cannot hash the CLI sources in $tools"
  dir="$data/cli/$hash"
  [[ -f "$dir/DotRushCli.dll" ]] || cli_build "$tools" "$data" "$hash" "$dotnet"
fi

export DOTRUSH_DATA_DIR="$data"
exec "$dotnet" "$dir/DotRushCli.dll" "$@"
