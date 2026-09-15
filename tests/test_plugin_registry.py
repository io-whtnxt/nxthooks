import shutil
import subprocess
from pathlib import Path
import pytest
from nxthooks._vendor.plugins.preferences import set_plugin_disabled
from nxthooks._vendor.plugins.registry import scan, plugins_dir

VALID = """
id: {pid}
displayName: {name}
kind: cli
version: 1.0.0
schemaVersion: 1
cli:
  entrypoint: nxtos-{pid}
  commands: [{{ name: run, argv: ["--go"], outputShape: text }}]
"""

def _make(home: Path, pid: str, name: str, body: str | None = None) -> None:
    d = plugins_dir(home) / pid
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(body if body is not None else VALID.format(pid=pid, name=name))

def test_plugins_dir_is_the_runtime_root(tmp_path: Path):
    assert plugins_dir(tmp_path) == tmp_path / ".local/share/nxt/os/plugins"

#: What the host package itself carries. `wiki`, `nxtskills` and `nxthooks`
#: are deliberately absent: their manifests travel inside their own components
#: and arrive through those components' `install` (N4-c-1), so a machine that
#: has never installed them has no row for them and this scan must not invent
#: one.
_BUILTIN_IDS = {
    "seo", "model-registry", "topology", "config-drift",
    "claims", "hygiene",
}

@pytest.mark.skip(reason="not applicable to the extracted package: needs nxtoskit/plugins/builtin/")
def test_scan_missing_user_dir_still_lists_builtins(tmp_path: Path):
    recs = {r.id: r for r in scan(tmp_path)}
    assert _BUILTIN_IDS <= set(recs)
    assert all(recs[pid].enabled for pid in _BUILTIN_IDS)

def test_no_component_delivered_plugin_is_in_the_host_package(tmp_path: Path):
    """The pane a component installs must not also ship with the host.

    Both roots would hold the same id, `scan` would resolve it to whichever
    copy the user root had, and the two would drift silently."""
    assert {r.id for r in scan(tmp_path)}.isdisjoint({"wiki", "nxtskills", "nxthooks"})


@pytest.mark.skip(reason="not applicable to the extracted package: NxtSkills does not ship here")
def test_nxtskills_exposes_safe_lifecycle_commands():
    """Read verbs only, checked where the manifest now lives.

    `sync` and `detach` change harness directories; this pane offers one Run
    control with no confirmation step, so it exposes neither."""
    from nxthooks._vendor.plugins.manifest import load_manifest

    manifest = Path(__file__).resolve().parents[1] / "nxtskills" / "plugin.yaml"
    record = load_manifest(manifest)
    assert record.enabled, record.reason
    assert [command["name"] for command in record.data["cli"]["commands"]] == [
        "status", "plan", "doctor",
    ]

def test_scan_returns_sorted_records(tmp_path: Path):
    _make(tmp_path, "zeta", "Zeta"); _make(tmp_path, "alpha", "Alpha")
    names = [r.display_name for r in scan(tmp_path)]
    assert names.index("Alpha") < names.index("Zeta")

def test_broken_plugin_appears_disabled_not_omitted(tmp_path: Path):
    _make(tmp_path, "good", "Good")
    _make(tmp_path, "broken", "Broken", body="id: broken\nkind: cli\nschemaVersion: 1\n")
    recs = {r.id: r for r in scan(tmp_path)}
    assert {"good", "broken"} <= set(recs)
    assert recs["broken"].enabled is False and recs["broken"].reason

def test_user_plugin_with_builtin_id_replaces_it(tmp_path: Path):
    _make(tmp_path, "seo", "Overridden SEO", body=(
        "id: seo\ndisplayName: Overridden SEO\nkind: cli\nversion: 9.9.9\n"
        "schemaVersion: 1\ncli:\n  entrypoint: nxtos-seo\n"
        "  commands: [{ name: run, argv: [\"--go\"], outputShape: text }]\n"
    ))
    recs = [r for r in scan(tmp_path) if r.id == "seo"]
    assert len(recs) == 1
    assert recs[0].display_name == "Overridden SEO"
    assert recs[0].version == "9.9.9"

@pytest.mark.skip(reason="not applicable to the extracted package: needs nxtoskit/plugins/builtin/")
def test_deleting_user_plugins_dir_leaves_builtins_working(tmp_path: Path):
    _make(tmp_path, "seo", "Overridden SEO", body=(
        "id: seo\ndisplayName: Overridden SEO\nkind: cli\nversion: 9.9.9\n"
        "schemaVersion: 1\ncli:\n  entrypoint: nxtos-seo\n"
        "  commands: [{ name: run, argv: [\"--go\"], outputShape: text }]\n"
    ))
    assert any(r.id == "seo" and r.display_name == "Overridden SEO" for r in scan(tmp_path))

    shutil.rmtree(plugins_dir(tmp_path))

    recs = {r.id: r for r in scan(tmp_path)}
    assert _BUILTIN_IDS <= set(recs)
    assert recs["seo"].display_name == "SEO Measurement"

def test_operator_disabled_plugin_is_listed_but_not_live(tmp_path: Path):
    # Listed, because a plugin that vanishes when turned off cannot be turned
    # back on from the listing that hides it.
    _make(tmp_path, "good", "Good")
    set_plugin_disabled(tmp_path, "good", True)
    rec = next(r for r in scan(tmp_path) if r.id == "good")
    assert rec.enabled is False
    assert rec.disabled_by_operator is True

def test_operator_disabled_plugin_is_not_reported_as_broken(tmp_path: Path):
    _make(tmp_path, "good", "Good")
    set_plugin_disabled(tmp_path, "good", True)
    rec = next(r for r in scan(tmp_path) if r.id == "good")
    assert rec.reason == ""
    assert rec.manifest_ok is True

def test_a_refused_manifest_keeps_its_reason_even_when_also_turned_off(tmp_path: Path):
    # A plugin that will not load is not off, it is broken. Restating it as
    # "you turned this off" sends the operator looking for the wrong fix.
    _make(tmp_path, "broken", "Broken", body="id: broken\nkind: cli\nschemaVersion: 1\n")
    set_plugin_disabled(tmp_path, "broken", True)
    rec = next(r for r in scan(tmp_path) if r.id == "broken")
    assert rec.disabled_by_operator is False
    assert rec.reason
    assert rec.manifest_ok is False

@pytest.mark.skip(reason="not applicable to the extracted package: needs nxtoskit/plugins/builtin/")
def test_a_builtin_can_be_turned_off_too(tmp_path: Path):
    set_plugin_disabled(tmp_path, "seo", True)
    rec = next(r for r in scan(tmp_path) if r.id == "seo")
    assert rec.enabled is False and rec.disabled_by_operator is True

def test_a_turned_off_plugin_keeps_its_declarations_for_the_listing(tmp_path: Path):
    _make(tmp_path, "good", "Good")
    set_plugin_disabled(tmp_path, "good", True)
    rec = next(r for r in scan(tmp_path) if r.id == "good")
    assert rec.data["cli"]["commands"][0]["name"] == "run"

def test_nothing_is_turned_off_by_default(tmp_path: Path):
    _make(tmp_path, "good", "Good")
    assert all(not r.disabled_by_operator for r in scan(tmp_path))

def test_library_plugins_resolve_and_win_over_user(tmp_path: Path):
    library, home = tmp_path / "lib", tmp_path / "home"
    _make(library, "nocodb", "Library NocoDB")
    _make(home, "nocodb", "Deployment NocoDB")
    records = {r.id: r for r in scan(home, library=library)}
    assert records["nocodb"].display_name == "Library NocoDB"

def test_a_deployment_may_still_disable_a_library_plugin(tmp_path: Path):
    """Disabling only ever reduces, so it cannot escalate past a grant."""
    library, home = tmp_path / "lib", tmp_path / "home"
    _make(library, "nocodb", "Library NocoDB")
    set_plugin_disabled(home, "nocodb", True)
    records = {r.id: r for r in scan(home, library=library)}
    assert records["nocodb"].enabled is False

def test_scan_spawns_no_subprocess(tmp_path: Path, monkeypatch):
    _make(tmp_path, "seo", "SEO")
    def boom(*a, **k):
        raise AssertionError("registry must not execute anything")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    scan(tmp_path)
