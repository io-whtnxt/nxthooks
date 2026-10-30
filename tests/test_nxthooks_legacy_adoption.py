"""NxtHooks adopts its engines' pre-N1 files, as `nxtoskit.paths` did.

An upgraded machine whose grep config still sat at `~/.config/nxt/nxtgrep/`
silently fell back to defaults once the engines resolved through
`nxthooks._paths`: a `policy = "off"` became enforce, and an operator's own
deny rules stopped applying.
"""

import pytest

from nxthooks import _paths as paths
from nxthooks.engines.nxtgrep import config as grep_config
from nxthooks.engines.nxtlint import install as lint_install

_ROOT_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
              "NXT_CONFIG_HOME", "NXT_DATA_HOME", "NXT_STATE_HOME", "NXT_CACHE_HOME",
              "NXTOS_STATE_HOME", "NXTGREP_CONFIG")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in _ROOT_VARS:
        monkeypatch.delenv(var, raising=False)
    return home


def test_the_legacy_grep_config_is_adopted_and_read(home):
    legacy = home / ".config" / "nxt" / "nxtgrep" / "config.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('policy = "off"\n')

    cfg = grep_config.load_config()

    assert cfg.policy == "off"
    target = home / ".config" / "nxt" / "grep" / "config.toml"
    assert target.read_text() == 'policy = "off"\n'
    assert not legacy.exists()
    assert (legacy.parent / "config.toml.MIGRATED-TO").read_text().strip() == str(target)


def test_the_legacy_pattern_pack_is_adopted(home):
    legacy = home / "Library" / "Application Support" / "NxtOS" / "nxtgrep" / "patterns.conf"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("my-rule = secret\n")

    pack = grep_config.default_user_patterns()

    assert pack == home / ".config" / "nxt" / "grep" / "patterns.conf"
    assert pack.read_text() == "my-rule = secret\n"
    assert not legacy.exists()


def test_a_target_that_exists_wins_and_the_legacy_copy_stays(home):
    legacy = home / ".config" / "nxt" / "nxtgrep" / "config.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('policy = "off"\n')
    target = home / ".config" / "nxt" / "grep" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text('policy = "advise"\n')

    assert grep_config.load_config().policy == "advise"
    assert legacy.read_text() == 'policy = "off"\n'
    assert target.read_text() == 'policy = "advise"\n'
    assert not (legacy.parent / "config.toml.MIGRATED-TO").exists()


def test_the_legacy_lint_shim_is_adopted_where_install_writes(home):
    legacy = home / ".nxtos" / "bin" / "nxtlint-hook.py"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(lint_install._PRE_T1281_SHIM.format(root="/old/checkout"))

    shim = lint_install.install_shim()

    assert shim == home / ".local" / "share" / "nxt" / "lint" / "bin" / "nxtlint-hook.py"
    assert not legacy.exists()
    assert "nxthooks.engines.nxtlint.hook" in shim.read_text()


def test_adoption_runs_at_most_once_per_process(home):
    legacy = home / ".config" / "nxt" / "nxtgrep" / "config.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("first\n")
    target = paths.resolve("grep", "config", "config.toml")
    target.unlink()
    legacy.write_text("second\n")

    paths.resolve("grep", "config", "config.toml")

    assert not target.exists()
    assert legacy.read_text() == "second\n"
