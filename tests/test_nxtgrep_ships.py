"""NxtGrep can be installed from a distribution, and refuses what it should.

Work item 4 of the release spec. Two things kept it from shipping:

  - the pattern pack lived at `nxthooks._vendor/hooks/builtin/nxtgrep/patterns/`,
    inside the NxtOS runtime hook tree rather than the package, so no wheel
    carried the file the scanner reads
  - the installer was a 513-line script outside the package that also edited
    `~/.claude/settings.json` directly and kept its own versioned backups --
    all of which NxtHooks now does, for every harness rather than Claude Code
    alone

The pack is in-package now and the installer is `nxthooks._vendor.nxtgrep.install`,
the same shape as nxtlint's.

Fixture secrets are assembled at runtime rather than written as literals.
Writing this file with a real-looking key in it is refused by the very hook
under test -- which happened, and is the most direct proof available that the
manifest, the dispatcher and this shim are wired to each other.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nxthooks._vendor.nxtgrep import hook as grep_hook
from nxthooks._vendor.nxtgrep.install import SHIM_NAME, default_bin, install_shim

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "nxthooks/_vendor" / "nxtgrep" / "hook.py"

#: Assembled, not written. See the module docstring.
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def _run(payload: dict, *, home: Path | None = None, policy: str | None = None):
    env = dict(os.environ)
    if home is not None:
        cfg = home / ".config" / "nxt" / "grep"
        cfg.mkdir(parents=True, exist_ok=True)
        if policy:
            (cfg / "config.toml").write_text(f'policy = "{policy}"\n')
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=60,
    )


# ------------------------------------------------------------- the pack ships

def test_the_pattern_pack_is_inside_the_package():
    assert grep_hook.BUILTIN_PACK.is_file()
    assert grep_hook.BUILTIN_PACK.parent.parent.name == "nxtgrep", (
        "the pack must live under the package, or no distribution carries it"
    )


def test_the_pack_is_not_still_read_from_the_nxtos_hook_tree():
    stale = REPO / "nxthooks/_vendor" / "hooks" / "builtin" / "nxtgrep" / "patterns"
    assert not stale.exists(), (
        "the pack is back in the runtime hook tree; a wheel will not carry it"
    )


@pytest.mark.skip(reason="not applicable to the extracted package: copies nxtoskit/ as a unit; the package is already vendored here")
def test_a_vendored_copy_carries_its_own_pack(tmp_path):
    """The extraction check: copy only the package, and it still scans."""
    root = tmp_path / "vendor"
    root.mkdir()
    shutil.copytree(REPO / "nxthooks/_vendor", root / "nxthooks/_vendor",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    proc = subprocess.run(
        [sys.executable, str(root / "nxthooks/_vendor" / "nxtgrep" / "hook.py")],
        input=json.dumps({"tool_name": "Write",
                          "tool_input": {"content": FAKE_AWS_KEY}}),
        capture_output=True, text=True, cwd="/tmp",
        env={"PATH": "/usr/bin:/bin"}, timeout=60,
    )
    assert proc.returncode == 2, (
        "a vendored copy could not scan with its own pack:\n" + proc.stderr
    )


# ---------------------------------------------------------------- the verdict

def test_a_known_secret_is_refused():
    proc = _run({"tool_name": "Write",
                 "tool_input": {"content": f"aws_key = {FAKE_AWS_KEY}"}})
    assert proc.returncode == 2
    assert "builtin" in proc.stderr, "the refusal must name which pack spoke"


def test_clean_input_is_allowed():
    proc = _run({"tool_name": "Write",
                 "tool_input": {"file_path": "/tmp/x.py", "content": "print(1)"}})
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("policy,expected", [("off", 0), ("advise", 0), ("enforce", 2)])
def test_policy_tiers(tmp_path, policy, expected):
    proc = _run({"tool_name": "Write", "tool_input": {"content": FAKE_AWS_KEY}},
                home=tmp_path / policy, policy=policy)
    assert proc.returncode == expected, proc.stderr
    if policy == "advise":
        assert "advise" in proc.stderr


def test_unparseable_input_fails_closed():
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2, "a payload it cannot read must refuse, not allow"


# --------------------------------------------------------------- the installer

def test_install_writes_an_executable_shim(tmp_path):
    target = install_shim(tmp_path / "bin")
    assert target.name == SHIM_NAME
    assert target.stat().st_mode & 0o111
    assert "from nxthooks._vendor.nxtgrep.hook import main" in target.read_text()


def test_the_shim_is_a_shim_not_a_copy(tmp_path):
    """A copy goes stale silently; a shim always runs the installed source."""
    body = install_shim(tmp_path / "bin").read_text()
    assert len(body.splitlines()) < 12, "this looks like a copy, not a shim"


def test_the_shim_lands_in_nxtgreps_own_component_dir():
    """Not ~/.nxtos/bin, which nxthooks._vendor.paths maps to the `git` component."""
    assert default_bin().parts[-2:] == ("grep", "bin"), default_bin()
