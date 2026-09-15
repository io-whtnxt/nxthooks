"""NxtHooks command-line interface.

    python3 -m nxthooks <command>

  dispatch   run all hooks matching a harness+event (used by transports)
  install    probe harnesses, install runtime, wire marked stanzas, verify
  uninstall  remove exactly the marked wiring; never touches foreign hooks
  verify     re-check every wiring path; print the capability matrix
  doctor     verify + synthetic dispatch self-tests
  list       show the hook directory
"""

from __future__ import annotations

import argparse
import sys

from . import HARNESSES, __version__
from . import dispatch as dispatch_mod
from . import lifecycle
from .platform import config_dir, data_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nxthooks", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    dispatch_mod.register(sub)

    p = sub.add_parser("install", help="install runtime + wire all present harnesses")
    p.add_argument("--harnesses", nargs="*", choices=HARNESSES, help="restrict to these harnesses")
    p.add_argument("--from", dest="source", metavar="PATH",
                   help="package directory to install from; defaults to the "
                        "imported nxthooks, which follows sys.path and so the "
                        "working directory")
    p.set_defaults(func=lifecycle.cmd_install)

    p = sub.add_parser("uninstall", help="remove NxtHooks wiring and artifacts")
    p.add_argument("--keep-runtime", action="store_true",
                   help=f"leave {data_dir()} in place")
    p.add_argument("--purge-config", action="store_true",
                   help=f"also remove {config_dir()}")
    p.set_defaults(func=lifecycle.cmd_uninstall)

    p = sub.add_parser("verify", help="check every wired path resolves")
    p.set_defaults(func=lifecycle.cmd_verify)

    p = sub.add_parser("doctor", help="verify + dispatch self-tests")
    p.set_defaults(func=lifecycle.cmd_doctor)

    p = sub.add_parser("list", help="show hooks registered in the directory")
    p.set_defaults(func=lifecycle.cmd_list)

    return parser


#: Reserved. Every transport reads it as "block this tool call", so nothing
#: but a real hook veto may return it -- see tests/test_nxthooks_exit_codes.py.
BLOCK_EXIT = 2


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on any usage error, and 2 is this protocol's block
        # signal. Left alone, an unknown --harness or a flag a newer transport
        # passes to an older dispatcher vetoes the user's tool call -- the
        # exact inversion of `fail = "open"`.
        #
        # --help is a SystemExit(0) and passes through untouched.
        code = exc.code if isinstance(exc.code, int) else 1
        return 1 if code == BLOCK_EXIT else code

    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 -- this is the one external boundary
        # A traceback out of a CLI is a stack dump where an operator needed a
        # sentence, and on a transport that reads exit status it is also an
        # unhandled non-zero nobody chose. One line, exit 1 -- never 2, which
        # would read as a tool-call veto.
        print(f"nxthooks {args.command} failed: {exc}", file=sys.stderr)
        return 1
