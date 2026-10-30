#!/bin/sh
# NxtHooks transport for Codex — a dumb pipe.
#
# Managed by `nxthooks install`; edits are overwritten on upgrade. Codex sends
# its native hook envelope on stdin. The dispatcher owns normalization,
# selection, and Codex-native output (including exit 2 for a PreToolUse veto).
#
# Fail-quiet, for the same reason as cc-dispatch.sh: a stanza outliving its
# runtime must not turn every turn into an error, and must not be silent
# either. One stderr line per hour, then exit 0.
RUNNER="{{ROOT}}/lib/run_nxthooks.py"
if [ ! -f "$RUNNER" ]; then
    stamp="{{STATE}}/runner-missing.stamp"
    if [ ! -e "$stamp" ] || [ -n "$(find "$stamp" -mmin +60 2>/dev/null)" ]; then
        mkdir -p "{{STATE}}" 2>/dev/null
        printf '[nxthooks] runner missing at %s; hooks are not running. Run: nxthooks doctor\n' \
            "$RUNNER" >&2
        : > "$stamp" 2>/dev/null
    fi
    exit 0
fi
exec "{{PYTHON}}" "$RUNNER" dispatch --harness codex --out native "$@"
