"""NxtHooks must resolve the config home per operation, not at import."""

import importlib

from nxthooks import manifest, registry


def test_hooks_d_follows_the_config_home_after_import(tmp_path, monkeypatch):
    importlib.reload(manifest)
    # A reload re-creates ManifestError; registry imported the old class and
    # would no longer catch what _validate raises for every later test.
    importlib.reload(registry)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "later"))

    assert manifest.hooks_d_default() == tmp_path / "later" / "nxt" / "hooks" / "hooks.d"


def test_two_config_homes_resolve_to_two_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "a"))
    first = manifest.hooks_d_default()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "b"))

    assert manifest.hooks_d_default() != first


def test_no_module_level_cache_survives():
    assert not hasattr(manifest, "HOOKS_D")
