"""What NxtHooks tells a host about itself, and how install treats a newer runtime.

The host side of the contract (the accepted range, the doctor line) lives with
the host that checks it, not here."""

import json

import pytest

from nxthooks import __version__, cli, contract, lifecycle



def test_contract_flag_prints_json_a_host_can_check(capsys):
    assert cli.main(["--contract"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["name"] == "nxthooks" and answer["version"] == __version__
    assert answer["contract"] == contract.contract_version() == "1.0"
    assert "claude-code" in answer["harnesses"] and "before_tool" in answer["events"]


@pytest.mark.parametrize("text, key", [("0.2.0", (0, 2, 0)), ("0.10.2", (0, 10, 2)), ("1.0rc1", (1, 0)), ("x", ())])
def test_version_key(text, key):
    assert contract.version_key(text) == key
    assert contract.version_key("0.10.0") > contract.version_key("0.9.9")


def _forge_newer_runtime(host_):
    args = type("A", (), {"harnesses": None, "source": None, "keep_newer": False})()
    assert lifecycle.cmd_install(args, host_) == 0
    version_file = host_.root() / "VERSION"
    version_file.write_text("999\n")
    marker = host_.root() / "lib" / "nxthooks" / "cli.py"
    marker.write_text("# forged copy\n")
    return version_file, marker


def test_install_overwrites_a_newer_runtime_by_default(hooks_machine, capsys):
    """`nxthooks install` is the repair path: a VERSION file alone, which the
    same UID can write, must not let a tampered runtime survive it."""
    host_ = hooks_machine("claude-code")
    version_file, marker = _forge_newer_runtime(host_)
    capsys.readouterr()

    args = type("A", (), {"harnesses": None, "source": None, "keep_newer": False})()
    assert lifecycle.cmd_install(args, host_) == 0

    assert "runtime installed" in capsys.readouterr().out
    assert marker.read_text() != "# forged copy\n"
    assert version_file.read_text() == __version__ + "\n"


def test_keep_newer_keeps_a_newer_runtime(hooks_machine, capsys):
    host_ = hooks_machine("claude-code")
    version_file, marker = _forge_newer_runtime(host_)
    capsys.readouterr()

    args = type("A", (), {"harnesses": None, "source": None, "keep_newer": True})()
    assert lifecycle.cmd_install(args, host_) == 0

    assert "runtime kept" in capsys.readouterr().out
    assert marker.read_text() == "# forged copy\n"
    assert version_file.read_text() == "999\n"


def test_the_cli_takes_keep_newer():
    parsed = cli.build_parser().parse_args(["install", "--keep-newer"])
    assert parsed.keep_newer is True
    assert cli.build_parser().parse_args(["install"]).keep_newer is False
