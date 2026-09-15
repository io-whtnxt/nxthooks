"""`hooks_d` must be resolved per call, not frozen at import.

A module-level `HOOKS_D = _hooks_d()` is correct in a one-shot hook
subprocess and wrong in every long-lived process: the installer, the desktop
app, and the test suite all change the config home after `nxthooks.manifest`
is already imported. Frozen, `load_manifests()` reads whichever hooks.d
happened to exist when the first import ran.
"""

import importlib

from nxthooks import manifest


def test_hooks_d_follows_the_config_home_after_import(tmp_path, monkeypatch):
    importlib.reload(manifest)  # simulate the import happening first

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "later"))
    assert str(tmp_path / "later") in str(manifest.hooks_d_default())


def test_two_config_homes_resolve_to_two_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "a"))
    first = manifest.hooks_d_default()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "b"))
    assert manifest.hooks_d_default() != first


def test_no_module_level_cache_survives(tmp_path, monkeypatch):
    assert not hasattr(manifest, "HOOKS_D"), (
        "a module-level HOOKS_D is the cache this module must not have"
    )
