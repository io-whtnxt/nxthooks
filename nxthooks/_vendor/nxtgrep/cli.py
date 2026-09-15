"""NxtGrep CLI — the standalone core harness adapters call.

Mirrors ~/.nxtos/bin/nxtgit's shape: one stdlib-only entrypoint, subcommands,
no NxtOS runtime dependency. Adapters (Claude Code hook wrapper, OpenCode
plugin) translate their native event into a {tool_name, tool_input} payload on
stdin and interpret the reply JSON; none of them carry policy.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from .config import load_config
from .engine import Pack, load_pack, scan

# The builtin pack ships beside this package when installed standalone
# (<pkg>/patterns/default.conf) and in repo layout lives over in the hook tree.
# Env override wins, per the spec's precedence chain.
def _builtin_pack_path() -> Path:
    override = os.environ.get("NXTGREP_BUILTIN_PACK")
    if override:
        return Path(override)
    # In-package, always. It used to live in the NxtOS hook tree, with this
    # as a repo-layout fallback -- which meant the pack shipped in one place
    # and was read from another, and a wheel carried neither. Moving it here
    # is what lets nxtgrep ship at all.
    return Path(__file__).resolve().parent / "patterns" / "default.conf"


BUILTIN_PACK = _builtin_pack_path()


def cmd_scan() -> int:
    """Read {tool_name, tool_input} JSON on stdin, emit verdict JSON on stdout.

    Exit codes: 0 = continue/advise, 3 = refuse, 4 = config or engine error.
    Adapter mapping is deliberate — 2 stays reserved for the Claude Code
    convention (exit 2 = block), which its wrapper derives from the verdict
    rather than the exit code, so one binary serves every harness.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        print(json.dumps({"decision": "refuse", "reason": f"NxtGrep could not parse input: {exc}"}))
        return 4
    tool_name = payload.get("tool_name", "?") if isinstance(payload, dict) else "?"
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None

    try:
        config = load_config()
    except ValueError as exc:
        print(json.dumps({"decision": "refuse", "reason": f"NxtGrep: {exc}"}))
        return 4
    if config.policy == "off":
        print(json.dumps({"decision": "continue"}))
        return 0

    packs: list[Pack] = []
    if config.builtin_enabled:
        pack, error = load_pack(BUILTIN_PACK)
        if error:
            print(json.dumps({"decision": "refuse", "reason": f"NxtGrep: {error}"}))
            return 4
        if pack is not None and pack.rules:
            packs.append(pack)
    for path in config.pack_files():
        pack, error = load_pack(path)
        if error:
            print(json.dumps({"decision": "refuse", "reason": f"NxtGrep: {error}"}))
            return 4
        if pack is not None and pack.rules:
            packs.append(pack)
    if not packs:
        print(json.dumps({"decision": "continue"}))
        return 0

    hits, truncated = scan(
        tool_input, packs,
        max_bytes=config.max_bytes, max_reported_hits=config.max_reported_hits,
    )
    if hits:
        detail = "; ".join(f"{h.rule_source} at {h.location}: …{h.snippet}…" for h in hits)
        more = " (report truncated)" if truncated else ""
        print(json.dumps({
            "decision": "refuse" if config.policy == "enforce" else "continue",
            "reason": f"NxtGrep: pattern hit(s){more} in {tool_name}. Hits: {detail}",
        }))
        return 3 if config.policy == "enforce" else 0
    if truncated:
        print(json.dumps({"decision": "continue"}))
        return 0
    print(json.dumps({"decision": "continue"}))
    return 0


def cmd_selftest() -> int:
    """Internal assertions over the engine, in the nxtgit selftest spirit."""
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        pack_file = Path(td) / "p.conf"
        pack_file.write_text("# c\nSECRET_[A-Z]+_VALUE\n", encoding="utf-8")
        pack, err = load_pack(pack_file)
        assert err is None and pack is not None and len(pack.rules) == 1

        bad = Path(td) / "bad.conf"
        bad.write_text("(unclosed\n", encoding="utf-8")
        _, err = load_pack(bad)
        assert err is not None and "bad regex" in err

        hits, trunc = scan(
            {"content": "the SECRET_API_VALUE here"},
            [pack],
            max_bytes=1000, max_reported_hits=5,
        )
        assert len(hits) == 1 and hits[0].location == "tool_input.content"
        assert trunc is False

        hits2, trunc2 = scan({"content": "x" * 500}, [pack], max_bytes=100, max_reported_hits=5)
        assert trunc2 is True and hits2 == []
    print("selftest ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="nxtgrep")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="scan a {tool_name, tool_input} payload from stdin")
    sub.add_parser("selftest", help="internal assertions")
    args = parser.parse_args()
    if args.cmd == "scan":
        return cmd_scan()
    return cmd_selftest()


if __name__ == "__main__":
    sys.exit(main())
