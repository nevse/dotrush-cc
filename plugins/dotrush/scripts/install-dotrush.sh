#!/usr/bin/env bash
#
# install-dotrush.sh — install a DotRush component at the ref pinned in dotrush-version.json.
# Idempotent: an install already at the pinned ref is left alone.
#
# Usage:
#   install-dotrush.sh [server|diagnostics] [TARGET_DIR] [--force]
#
# The component defaults to server. TARGET_DIR defaults to $DOTRUSH_SERVER_DIR for the server, else
# <data>/<component>, where <data> is $CLAUDE_PLUGIN_DATA or ~/.cache/dotrush-cc.
#
# Env (all optional):
#   DOTRUSH_REF    DotRush release tag or full commit SHA (default: "ref" in dotrush-version.json)
#   DOTRUSH_REPO   GitHub owner/repository (default: "repository" in dotrush-version.json)
#
# A release tag whose GitHub release ships the LanguageServer and Diagnostics bundles is
# downloaded (needs curl and unzip); any other ref is built from source (needs git and a .NET SDK).
# The logic lives in dotrush-install.sh, which the profiling helper shares.
#
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dotrush-install.sh"

component="server"
target=""
force=""
for arg in "$@"; do
  case "$arg" in
    --force) force="force" ;;
    server|diagnostics) component="$arg" ;;
    *) target="$arg" ;;
  esac
done
if [[ -z "$target" ]]; then
  if [[ "$component" == server && -n "${DOTRUSH_SERVER_DIR:-}" ]]; then
    target="$DOTRUSH_SERVER_DIR"
  else
    target="$(dotrush_data_dir)/$component"
  fi
fi

dotrush_install "$component" "$target" "$force"
echo "install-dotrush: DotRush $component $(dotrush_installed_ref "$target")($(cat "$target/.dotrush-source" 2>/dev/null)) at $target"
