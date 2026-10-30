"""NxtGrep — pattern-pack gate, shared by every surface that needs it.

One engine, one config authority:

  nxthooks/engines/nxtgrep/config.py   reads ~/.config/nxt/nxtgrep/config.json
                               (schema: nxtos-nxtgrep-config.schema.json, in NxtOS)
  nxthooks/engines/nxtgrep/engine.py   pack loading + payload scanning, pure functions

The builtin hook (nxthooks/engines/nxtgrep/hook.py) is a thin stdin/stdout
wrapper around this package. The Claude Code / OpenCode / Pi adapters are equally
thin: they translate their harness's native event into a payload dict, call
engine.scan(), and translate the verdict back. None of them carry policy — a pack
edit here applies to every harness at once.
"""

__version__ = "0.2.0"
