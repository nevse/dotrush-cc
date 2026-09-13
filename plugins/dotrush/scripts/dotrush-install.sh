#!/usr/bin/env bash
# dotrush-install.sh — sourced by install-dotrush.sh and dotrush-profile.sh. Installs a DotRush
# component, the language server or the diagnostics tools, at the ref pinned in dotrush-version.json.
#
# Both components are installed the same way and, for one ref, from the same place: downloaded when
# the ref is a release tag whose GitHub release ships a bundle of every component for this platform,
# built from source at that ref otherwise. A commit, or a release missing any bundle, is built — a
# downloaded server never sits beside diagnostics built from source.

DOTRUSH_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTRUSH_PIN_FILE="$DOTRUSH_LIB_DIR/../dotrush-version.json"
DOTRUSH_COMPONENTS="server diagnostics"

dotrush_fail() {
  echo "dotrush-install: $*" >&2
  exit 1
}

# Claude Code passes CLAUDE_PLUGIN_DATA to the language server through .lsp.json but does not set it
# for the Bash commands a skill runs. Those find the same directory from where the plugin is
# installed: <claude>/plugins/cache/<marketplace>/<plugin>/<version> keeps its data in
# <claude>/plugins/data/<plugin>-<marketplace>. Anywhere else, such as a checkout, uses the user cache.
dotrush_data_dir() {
  local root plugin marketplace
  if [[ -n "${CLAUDE_PLUGIN_DATA:-}" ]]; then
    printf '%s\n' "$CLAUDE_PLUGIN_DATA"
    return
  fi
  root="$(cd "$DOTRUSH_LIB_DIR/.." && pwd)"
  root="${root%/*}"
  plugin="${root##*/}"
  root="${root%/*}"
  marketplace="${root##*/}"
  root="${root%/*}"
  if [[ "${root##*/}" == cache && "${root%/*}" == */plugins ]]; then
    printf '%s/data/%s-%s\n' "${root%/*}" "$plugin" "$marketplace"
  else
    printf '%s\n' "${XDG_CACHE_HOME:-${HOME}/.cache}/dotrush-cc"
  fi
}

dotrush_pin_value() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$DOTRUSH_PIN_FILE" "$1" 2>/dev/null \
    || dotrush_fail "cannot read \"$1\" from $DOTRUSH_PIN_FILE"
}

# The DotRush tag or commit everything is installed from. A commit lets the plugin use DotRush work
# that is not released yet. The ref goes into URLs and install records, so it is held to the
# characters of a tag or a SHA.
dotrush_ref() {
  local ref="${DOTRUSH_REF:-}"
  if [[ -z "$ref" ]]; then
    ref="$(dotrush_pin_value ref)" || exit 1
  fi
  [[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || dotrush_fail "not a DotRush tag or commit SHA: '$ref'"
  printf '%s\n' "$ref"
}

dotrush_repo() {
  local repo="${DOTRUSH_REPO:-}"
  if [[ -z "$repo" ]]; then
    repo="$(dotrush_pin_value repository)" || exit 1
  fi
  [[ "$repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || dotrush_fail "not a GitHub owner/repository: '$repo'"
  printf '%s\n' "$repo"
}

dotrush_is_commit() {
  [[ "$1" =~ ^[0-9a-f]{40}$ ]]
}

dotrush_platform() {
  local os arch
  case "$(uname -s)" in
    Darwin) os="darwin" ;;
    Linux) os="linux" ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT) os="win32" ;;
    *) dotrush_fail "no DotRush build for OS '$(uname -s)'" ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch="arm64" ;;
    x86_64|amd64) arch="x64" ;;
    *) dotrush_fail "no DotRush build for architecture '$(uname -m)'" ;;
  esac
  printf '%s-%s\n' "$os" "$arch"
}

# The .NET runtime identifier of the platform the release bundles are named after.
dotrush_rid() {
  local platform
  platform="$(dotrush_platform)" || exit 1
  case "$platform" in
    darwin-*) printf 'osx-%s\n' "${platform#darwin-}" ;;
    win32-*) printf 'win-%s\n' "${platform#win32-}" ;;
    *) printf '%s\n' "$platform" ;;
  esac
}

dotrush_dotnet() {
  if [[ -n "${DOTRUSH_DOTNET:-}" ]]; then
    printf '%s\n' "$DOTRUSH_DOTNET"
  elif command -v dotnet >/dev/null 2>&1; then
    command -v dotnet
  elif [[ -n "${DOTNET_ROOT:-}" && -x "$DOTNET_ROOT/dotnet" ]]; then
    printf '%s\n' "$DOTNET_ROOT/dotnet"
  else
    dotrush_fail "a .NET runtime is required: no dotnet on PATH or in DOTNET_ROOT"
  fi
}

dotrush_bundle_url() {
  local component="$1" repo="$2" ref="$3" name platform
  case "$component" in
    server) name="Server" ;;
    diagnostics) name="Diagnostics" ;;
    *) dotrush_fail "unknown DotRush component '$component'" ;;
  esac
  platform="$(dotrush_platform)" || exit 1
  printf 'https://github.com/%s/releases/download/%s/DotRush.Bundle.%s_%s.zip\n' "$repo" "$ref" "$name" "$platform"
}

# Prints where every component at this ref comes from: "release" or "build". Only a missing bundle
# (HTTP 4xx) means build; failing to reach GitHub at all is an error, not a reason to build.
dotrush_source() {
  local repo="$1" ref="$2" component url status
  if dotrush_is_commit "$ref"; then
    echo "build"
    return
  fi
  command -v curl >/dev/null 2>&1 || dotrush_fail "curl is required to install DotRush"
  for component in $DOTRUSH_COMPONENTS; do
    url="$(dotrush_bundle_url "$component" "$repo" "$ref")" || exit 1
    curl -fsIL --retry 2 -o /dev/null "$url" 2>/dev/null && status=0 || status=$?
    case "$status" in
      0) ;;
      22) echo "build"; return ;;
      *) dotrush_fail "cannot check $url (curl exit $status)" ;;
    esac
  done
  echo "release"
}

dotrush_component_ready() {
  case "$1" in
    server) [[ -f "$2/DotRush" || -f "$2/DotRush.exe" ]] ;;
    diagnostics) [[ -f "$2/dotnet-trace.dll" && -f "$2/dotnet-gcdump.dll" ]] ;;
    *) return 1 ;;
  esac
}

dotrush_installed_ref() {
  cat "$1/.dotrush-ref" 2>/dev/null || true
}

dotrush_is_current() {
  dotrush_component_ready "$1" "$2" && [[ "$(dotrush_installed_ref "$2")" == "$3" ]]
}

dotrush_fetch_release() {
  local component="$1" repo="$2" ref="$3" into="$4" url
  command -v unzip >/dev/null 2>&1 || dotrush_fail "unzip is required to install DotRush"
  url="$(dotrush_bundle_url "$component" "$repo" "$ref")" || exit 1
  echo "dotrush-install: downloading ${url##*/} from release $ref" >&2
  curl -fsSL --retry 3 -o "$into.zip" "$url" && unzip -q "$into.zip" -d "$into" >&2
}

# Builds a component into $5 as DotRush's own build does. Both checkouts are shallow, and the
# submodule the component needs is fetched directly by the commit the ref pins, which took a third of
# the time `git submodule update --depth 1` did.
dotrush_build_steps() {
  local component="$1" repo="$2" ref="$3" work="$4" out="$5"
  local source="$4/source" submodule commit url tool rid dotnet
  dotnet="$(dotrush_dotnet)" || return 1
  case "$component" in
    server) submodule="src/DotRush.LanguageServer.Framework" ;;
    diagnostics) submodule="src/DotRush.Debugging.Diagnostics" ;;
  esac
  git init -q "$source" || return 1
  git -C "$source" fetch -q --depth 1 "https://github.com/$repo.git" "$ref" || return 1
  git -C "$source" checkout -q FETCH_HEAD || return 1
  commit="$(git -C "$source" rev-parse "HEAD:$submodule")" || return 1
  url="$(git -C "$source" config -f .gitmodules "submodule.$submodule.url")" || return 1
  git init -q "$source/$submodule" || return 1
  git -C "$source/$submodule" fetch -q --depth 1 "$url" "$commit" || return 1
  git -C "$source/$submodule" checkout -q FETCH_HEAD || return 1
  case "$component" in
    server)
      # The release's server bundle is this project published for one platform, framework-dependent,
      # with its native DotRush launcher, plus the default config DotRush's repack step writes.
      rid="$(dotrush_rid)" || return 1
      "$dotnet" publish "$source/src/DotRush.Roslyn.Server/DotRush.Roslyn.Server.csproj" -c Release \
        -r "$rid" --self-contained false -p:UseAppHost=true -o "$out" </dev/null || return 1
      printf '%s\n' '{' '    "dotrush": {' '        "roslyn": { }' '    }' '}' > "$out/_dotrush.config.json"
      ;;
    diagnostics)
      for tool in dotnet-trace dotnet-gcdump; do
        "$dotnet" publish "$source/$submodule/src/Tools/$tool/$tool.csproj" -c Release \
          -p:SatelliteResourceLanguages=en -o "$out" </dev/null || return 1
      done
      ;;
  esac
}

dotrush_install_locked() {
  local component="$1" target="$2" repo="$3" ref="$4" force="$5" origin staging log
  # Another session may have installed this ref while this one waited for the lock.
  [[ "$force" == force ]] || ! dotrush_is_current "$component" "$target" "$ref" || return 0
  origin="$(dotrush_source "$repo" "$ref")" || exit 1
  staging="$(mktemp -d "$target.new.XXXXXX")"
  if [[ "$origin" == release ]]; then
    dotrush_fetch_release "$component" "$repo" "$ref" "$staging" \
      || dotrush_fail "downloading the DotRush $component bundle for $ref failed"
  else
    command -v git >/dev/null 2>&1 || dotrush_fail "building DotRush from source needs git"
    [[ -n "$("$(dotrush_dotnet)" --list-sdks 2>/dev/null)" ]] || dotrush_fail "building DotRush from source needs a .NET SDK"
    echo "dotrush-install: DotRush $ref has no release bundles for this platform; building the $component from source (a few minutes, once per pinned ref)" >&2
    log="$(dirname "$target")/$component-build.log"
    if ! dotrush_build_steps "$component" "$repo" "$ref" "$staging.work" "$staging" > "$log" 2>&1 \
      || ! dotrush_component_ready "$component" "$staging"; then
      tail -n 20 "$log" >&2
      dotrush_fail "building the DotRush $component from $repo@$ref failed; full log: $log"
    fi
    rm -f "$log"
  fi
  dotrush_component_ready "$component" "$staging" || dotrush_fail "the DotRush $component for $ref is missing its files"
  [[ ! -f "$staging/DotRush" ]] || chmod +x "$staging/DotRush"
  printf '%s\n' "$ref" > "$staging/.dotrush-ref"
  printf '%s\n' "$origin" > "$staging/.dotrush-source"
  rm -rf "$target"
  [[ ! -e "$target" ]] || dotrush_fail "could not remove the previous DotRush $component at $target (is it still running?)"
  mv "$staging" "$target"
  # 0.4.0 installed NuGet diagnostics tools beside the data directory's diagnostics.
  [[ "$component" != diagnostics ]] || rm -rf "$(dirname "$target")/diagnostics-tools"
  echo "dotrush-install: DotRush $component $ref ($origin) installed in $target" >&2
}

# Installs a component at the pinned ref into $2 unless it is already there; "force" as $3 reinstalls.
# Sessions share the directory, so the install holds $2.lock, which records its PID: a lock left by a
# killed process is taken over. The result is prepared beside $2 and swapped in whole, so nothing of a
# previous ref survives, and a failed install leaves the previous one in place.
dotrush_install() {
  local component="$1" target="$2" force="${3:-}" repo ref lock holder waited=0 status
  repo="$(dotrush_repo)" || exit 1
  ref="$(dotrush_ref)" || exit 1
  [[ "$force" == force ]] || ! dotrush_is_current "$component" "$target" "$ref" || return 0
  lock="$target.lock"
  mkdir -p "$(dirname "$target")"
  until mkdir "$lock" 2>/dev/null; do
    holder="$(cat "$lock/pid" 2>/dev/null || true)"
    if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
      rm -rf "$lock"
      continue
    fi
    (( waited < 1800 )) || dotrush_fail "timed out waiting for another session to release $lock"
    (( waited > 0 )) || echo "dotrush-install: waiting for another session installing the DotRush $component" >&2
    waited=$((waited + 1))
    sleep 1
  done
  echo "$$" > "$lock/pid"
  # dotrush_fail exits from inside the install, and the trap cleans up on that path too.
  trap "rm -rf $(printf '%q' "$lock") $(printf '%q' "$target").new.*" EXIT
  dotrush_install_locked "$component" "$target" "$repo" "$ref" "$force" && status=0 || status=$?
  trap - EXIT
  rm -rf "$lock" "$target".new.*
  return "$status"
}
