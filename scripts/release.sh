#!/usr/bin/env bash
# Nxt plugin release pipeline (T1282). The logic lives in nxtos and runs with
# this repo as its parameter; this wrapper adds only that parameter. Stages:
# bump | candidate | hosted | publish. With no stage the shared script runs
# candidate then hosted and prints the exact publish command. Set NXTOS_REPO
# if nxtos is not at the default checkout.
#
#   scripts/release.sh [stage] [args...]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED="${NXTOS_REPO:-$HOME/Documents/Coding Projects/nxtos}/scripts/plugin_release.py"
[ -f "$SHARED" ] || { echo "error: $SHARED not found; set NXTOS_REPO to an nxtos checkout" >&2; exit 1; }
STAGE="${1:-}"
case "$STAGE" in
  bump|candidate) shift; exec python3 -I "$SHARED" "$STAGE" --repo "$REPO" "$@" ;;
  hosted|publish) exec python3 -I "$SHARED" "$@" ;;
  "") exec python3 -I "$SHARED" --repo "$REPO" ;;
  *) echo "error: unknown stage '$STAGE' (bump|candidate|hosted|publish)" >&2; exit 2 ;;
esac
