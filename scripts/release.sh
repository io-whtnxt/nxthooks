#!/usr/bin/env bash
# release.sh: the one plugin release wrapper (T1282). Byte-identical in every Nxt plugin repo; the source of
# truth is nxtos scripts/plugin-release-wrapper.sh. It only fills in this repo's path; all logic lives in nxtos
# scripts/plugin_release.py. Stages: bump | candidate | hosted | publish (see `--help` there).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED="${NXTOS_REPO:-$HOME/Documents/Coding Projects/nxtos}/scripts/plugin_release.py"
usage() { echo "usage: scripts/release.sh bump|candidate|hosted|publish [args...]" >&2; }
[ -f "$SHARED" ] || { echo "error: $SHARED not found; set NXTOS_REPO to an nxtos checkout" >&2; exit 1; }
STAGE="${1:-}"
case "$STAGE" in
  -h|--help) usage; exec python3 -I "$SHARED" --help ;;
  bump|publish) shift; exec python3 -I "$SHARED" "$STAGE" --repo "$REPO" "$@" ;;
  candidate) shift
    case " $* " in *" --out "*) ;; *) set -- --out "$REPO/dist/release" "$@" ;; esac
    exec python3 -I "$SHARED" candidate --repo "$REPO" "$@" ;;
  hosted) shift; exec python3 -I "$SHARED" hosted "$@" ;;
  *) usage; exit 2 ;;
esac
