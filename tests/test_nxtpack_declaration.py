"""Whether nxtpack.json still describes this package.

NxtPack reads the declaration and never the code behind it: it runs the
provider by argv, builds the operator's form from `field_specs`, and spells
the flags itself in `field_args`. So the declaration can name a module nobody
installs, a flag argparse rejects, or a harness `install` refuses -- and
nothing in the package notices until someone at a keyboard types it. These are
the checks that make it break in CI instead, ported from the NxtOS tree, which
dropped them along with its in-tree copy of this package (9bd60614).
"""
import argparse
import contextlib
import copy
import importlib
import io
import json
import tomllib
from pathlib import Path

import pytest

from nxthooks import INSTALLABLE_HARNESSES
from nxthooks.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
DECLARATION = json.loads((ROOT / "nxthooks" / "nxtpack.json").read_text(encoding="utf-8"))


def _manifest():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _console_script_targets():
    return {target.split(":")[0] for target in _manifest()["project"]["scripts"].values()}


def test_the_declaration_ships_as_package_data():
    """On disk it is a file in the checkout; in a wheel it is whatever is declared."""
    patterns = _manifest()["tool"]["setuptools"]["package-data"]["nxthooks"]
    assert "nxtpack.json" in patterns
    assert (ROOT / "nxthooks" / "nxtpack.json").is_file()


def test_the_provider_is_a_console_script_the_install_ships():
    """The provider is a module path NxtPack resolves to an installed script."""
    assert DECLARATION["configuration"]["provider"] in _console_script_targets()


def test_every_console_script_resolves_to_a_callable():
    """A typo in a script target is an AttributeError on an installed machine."""
    failures = []
    for name, target in _manifest()["project"]["scripts"].items():
        module, _, attribute = target.partition(":")
        try:
            callable_ = getattr(importlib.import_module(module), attribute)
        except (ImportError, AttributeError) as exc:
            failures.append(f"{name} = {target}: {exc}")
            continue
        if not callable(callable_):
            failures.append(f"{name} = {target} is not callable")
    assert failures == []


def _switches(parser: argparse.ArgumentParser) -> set[str]:
    """The option strings one parser accepts."""
    return {option for action in parser._actions for option in action.option_strings}


def _switches_by_subcommand(parser: argparse.ArgumentParser) -> dict[str, set[str]]:
    """{subcommand: the switches it defines}, walking every subparser.

    Nothing is merged between siblings or up to the root. argparse hands the
    rest of argv to the chosen subcommand's parser, so a switch defined only
    somewhere else is a usage error there, not a spelling of something valid.
    """
    by_subcommand: dict[str, set[str]] = {}

    def walk(current: argparse.ArgumentParser) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, sub in action.choices.items():
                by_subcommand[name] = _switches(sub)
                walk(sub)

    walk(parser)
    return by_subcommand


def _flag_offenders(declaration: dict, parser: argparse.ArgumentParser) -> list[str]:
    """Every `field_args` switch the parser which will read it rejects.

    An operation's `args[0]` picks that parser: the subcommand of the same
    name, or the root itself when `args[0]` is already a switch (`--version`).
    """
    by_subcommand = _switches_by_subcommand(parser)
    root = _switches(parser)
    offenders = []
    for service, operations in declaration["operations"].items():
        for operation, spec in operations.items():
            selector = spec["args"][0]
            allowed = root if selector.startswith("-") else by_subcommand.get(selector)
            for field, argv in spec.get("field_args", {}).items():
                if allowed is None:
                    offenders.append(
                        f"{service}.{operation}.{field} names {argv[0]}, but the CLI has"
                        f" no {selector} subcommand to read it"
                    )
                elif argv[0] not in allowed:
                    offenders.append(
                        f"{service}.{operation}.{field} names {argv[0]},"
                        f" which `nxthooks {selector}` does not define"
                    )
    return offenders


def test_the_operations_name_flags_the_cli_defines():
    """A renamed argument leaves the form offering a switch argparse then
    rejects, on a machine with no developer near it."""
    assert _flag_offenders(DECLARATION, build_parser()) == []


def test_a_switch_borrowed_from_another_subcommand_fails_the_check():
    """The hole one shared flag set leaves: `--keep-runtime` is defined by this
    CLI, just not for `install`. Only checking each operation against its own
    subparser notices the form offers a switch that command refuses."""
    for service, operation, field, argv in (
        ("hooks", "install", "harness", ["--keep-runtime", "{field:harness}"]),
        ("hooks", "uninstall", "keep_runtime", ["--harnesses"]),
    ):
        declaration = copy.deepcopy(DECLARATION)
        declaration["operations"][service][operation]["field_args"][field] = argv
        assert _flag_offenders(declaration, build_parser()) == [
            f"{service}.{operation}.{field} names {argv[0]},"
            f" which `nxthooks {operation}` does not define"
        ]


def test_a_borrowed_switch_is_real_and_still_rejected_by_the_parser():
    """Why the check above is not vacuous: each switch is one this CLI defines,
    and argparse still answers it with a usage error on the other subcommand."""
    parser = build_parser()
    defined = _switches(parser) | {
        option for options in _switches_by_subcommand(parser).values() for option in options
    }
    for command, flag in (("install", "--keep-runtime"), ("uninstall", "--harnesses")):
        assert flag in defined
        with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as refused:
            parser.parse_args([command, flag])
        assert refused.value.code == 2


def test_the_harness_choices_are_the_ones_the_package_installs():
    """The operation offers only harnesses with verified install writers."""
    choices = DECLARATION["operations"]["hooks"]["install"]["field_specs"]["harness"]["choices"]
    assert choices == list(INSTALLABLE_HARNESSES)
