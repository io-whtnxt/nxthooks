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
import importlib
import json
import tomllib
from pathlib import Path

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


def test_the_operations_name_flags_the_cli_defines():
    """A renamed argument leaves the form offering a switch argparse then
    rejects, on a machine with no developer near it."""
    known = set()

    def collect(parser: argparse.ArgumentParser) -> None:
        for action in parser._actions:
            known.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for sub in action.choices.values():
                    collect(sub)

    collect(build_parser())

    offenders = [
        f"{service}.{operation}.{field} names {argv[0]}, which no parser defines"
        for service, operations in DECLARATION["operations"].items()
        for operation, spec in operations.items()
        for field, argv in spec.get("field_args", {}).items()
        if argv[0] not in known
    ]
    assert offenders == []


def test_the_harness_choices_are_the_ones_the_package_installs():
    """The operation offers only harnesses with verified install writers."""
    choices = DECLARATION["operations"]["hooks"]["install"]["field_specs"]["harness"]["choices"]
    assert choices == list(INSTALLABLE_HARNESSES)
