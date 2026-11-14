#!/usr/bin/env bash
# Build half of the Nxt plugin release pipeline (T1282): version from the single
# version source, sdist + wheel built twice with identical digests, the wheel
# byte-checked against the committed tree, a provenance record. Builds the
# committed HEAD; never tags, pushes or uploads. The logic is shared and lives in
# nxtos (scripts/plugin_release_build.py); set NXTOS_REPO if nxtos is not at the
# default checkout.
#
#   scripts/release-build.sh [--out DIR] [--allow-dirty] [--source-date-epoch N]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED="${NXTOS_REPO:-$HOME/Documents/Coding Projects/nxtos}/scripts/plugin_release_build.py"
[ -f "$SHARED" ] || { echo "error: $SHARED not found; set NXTOS_REPO to an nxtos checkout" >&2; exit 1; }
OUT="$REPO/dist/release"
if [ "${1:-}" = "--out" ]; then OUT="$2"; shift 2; fi
exec python3 -I "$SHARED" build --repo "$REPO" --repo-name nxthooks --out "$OUT" "$@"
