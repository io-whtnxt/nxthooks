#!/bin/sh
# NxtHooks transport for Claude Code — a dumb pipe.
#
# Managed by `nxthooks install`; edits are overwritten on upgrade.
# The envelope passes through untouched; the dispatcher owns selection,
# policy, execution, and native-protocol emission (exit 2 blocks with
# stderr as the reason, per Claude Code's PreToolUse convention).
#
# Fail-quiet. A stanza can outlive its runtime — `pip uninstall`, a moved
# install root, a half-finished upgrade — and a transport that then exits
# non-zero turns every single turn into a hook error the operator cannot act
# on. It exits 0 instead, and says so on stderr at most once an hour so the
# silence is not total. Once an hour, not once per event: PreToolUse fires on
# every tool call, and a line per call is noise the reader learns to skip.
RUNNER="{{ROOT}}/lib/run_nxthooks.py"
if [ ! -f "$RUNNER" ]; then
    stamp="{{STATE}}/runner-missing.stamp"
    if [ ! -e "$stamp" ] || [ -n "$(find "$stamp" -mmin +60 2>/dev/null)" ]; then
        mkdir -p "{{STATE}}" 2>/dev/null
        printf '[nxthooks] runner missing at %s; hooks are not running. Run: nxtos doctor\n' \
            "$RUNNER" >&2
        : > "$stamp" 2>/dev/null
    fi
    exit 0
fi
exec "{{PYTHON}}" "$RUNNER" dispatch --harness claude-code --out native "$@"
