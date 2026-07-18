#!/usr/bin/env bash
#
# install-dotrush.sh — download the DotRush Roslyn language server (official release
# bundle) for the current platform into a target directory. Idempotent.
#
# Usage:
#   install-dotrush.sh [TARGET_DIR] [--force]
#
# Env (all optional):
#   DOTRUSH_SERVER_DIR   target dir (default: ./server next to this script's plugin,
#                        or $1 if given)
#   DOTRUSH_RELEASE      release tag to fetch (default: 2026.07)
#   DOTRUSH_REPO         GitHub repo (default: JaneySprings/DotRush)
#
set -euo pipefail

RELEASE="${DOTRUSH_RELEASE:-2026.07}"
REPO="${DOTRUSH_REPO:-JaneySprings/DotRush}"

FORCE=0
TARGET=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    *) TARGET="$arg" ;;
  esac
done
TARGET="${TARGET:-${DOTRUSH_SERVER_DIR:-}}"
if [ -z "$TARGET" ]; then
  # default: <plugin-root>/../data/server relative to this script
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  TARGET="$here/../server"
fi

# ---- detect platform ---------------------------------------------------------
uname_s="$(uname -s)"
case "$uname_s" in
  Darwin) os="darwin" ;;
  Linux)  os="linux" ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT) os="win32" ;;
  *) echo "install-dotrush: unsupported OS '$uname_s'" >&2; exit 2 ;;
esac
uname_m="$(uname -m)"
case "$uname_m" in
  arm64|aarch64) arch="arm64" ;;
  x86_64|amd64)  arch="x64" ;;
  *) echo "install-dotrush: unsupported arch '$uname_m'" >&2; exit 2 ;;
esac

exe="DotRush"; [ "$os" = "win32" ] && exe="DotRush.exe"
asset="DotRush.Bundle.Server_${os}-${arch}.zip"
url="https://github.com/${REPO}/releases/download/${RELEASE}/${asset}"

# ---- idempotence -------------------------------------------------------------
if [ -x "$TARGET/$exe" ] && [ "$FORCE" -ne 1 ]; then
  echo "install-dotrush: server already present at $TARGET/$exe (use --force to reinstall)"
  exit 0
fi

command -v curl >/dev/null 2>&1 || { echo "install-dotrush: 'curl' is required" >&2; exit 3; }
command -v unzip >/dev/null 2>&1 || { echo "install-dotrush: 'unzip' is required" >&2; exit 3; }

echo "install-dotrush: platform=${os}-${arch} release=${RELEASE}"
echo "install-dotrush: downloading ${asset} ..."

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if ! curl -fSL --retry 3 -o "$tmp/$asset" "$url"; then
  echo "install-dotrush: download failed: $url" >&2
  echo "  (check the release tag exists; set DOTRUSH_RELEASE to override)" >&2
  exit 4
fi

mkdir -p "$TARGET"
echo "install-dotrush: extracting into $TARGET ..."
unzip -oq "$tmp/$asset" -d "$TARGET"
[ -f "$TARGET/$exe" ] && chmod +x "$TARGET/$exe" 2>/dev/null || true

if [ ! -e "$TARGET/$exe" ]; then
  echo "install-dotrush: expected '$exe' in $TARGET after extraction, not found" >&2
  echo "  archive top-level contents:" >&2
  ls "$TARGET" | head -10 >&2
  exit 5
fi

echo "install-dotrush: done -> $TARGET/$exe"
