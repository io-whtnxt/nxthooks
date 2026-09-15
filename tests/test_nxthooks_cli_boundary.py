"""The one external boundary: nxthooks/cli.py's main().

install_runtime raises FileNotFoundError on a missing packaged artifact,
_provision_builtin_hooks's subprocess.run(timeout=30) raises TimeoutExpired,
and running install with cwd inside the installed lib makes install_runtime
rmtree its own source out from under the interpreter -- three distinct
uncaught tracebacks, all with an internal path and no remedy, all through the
same subcommand dispatch. Rule 8 (code-quality.md): fail loud at boundaries,
silent inside -- one handler in main(), not one per internal helper.
"""

import subprocess

import pytest

import nxthooks.cli as cli
import nxthooks.lifecycle as lifecycle


def test_main_reports_and_exits_nonzero_on_an_uncaught_exception(monkeypatch, capsys):
    monkeypatch.setattr(
        lifecycle, "cmd_install", lambda args: (_ for _ in ()).throw(FileNotFoundError("adapters/run_nxthooks.py"))
    )

    code = cli.main(["install"])

    assert code == 1
    err = capsys.readouterr().err
    assert "nxthooks install failed" in err
    assert "run_nxthooks.py" in err


def test_main_reports_a_subprocess_timeout(monkeypatch, capsys):
    def _raise(args):
        raise subprocess.TimeoutExpired(cmd=["python3", "-m", "nxthooks._vendor.nxtlint.install"], timeout=30)

    monkeypatch.setattr(lifecycle, "cmd_install", _raise)

    code = cli.main(["install"])

    assert code == 1
    assert "nxthooks install failed" in capsys.readouterr().err


def test_a_clean_command_is_unaffected(monkeypatch):
    monkeypatch.setattr(lifecycle, "cmd_list", lambda args: 0)

    assert cli.main(["list"]) == 0
