"""A component's pane arrives with the component and leaves with it.

Until N4-c-1 these manifests sat in `nxthooks._vendor/plugins/builtin/`, so the row
appeared in the roster because the *host* package was importable. That said
nothing about whether the component behind it was on the machine, and an
uninstall left the row behind. These tests are the statement of the new rule:
`install` puts the manifest in the operator's plugin root, `uninstall` takes it
out, and the registry finds it there like any third-party plugin.
"""
import types
from pathlib import Path

import pytest

from nxthooks._vendor.plugins.delivery import deliver, withdraw
from nxthooks._vendor.plugins.registry import BUILTIN_DIR, plugins_dir, scan

REPO_ROOT = Path(__file__).resolve().parents[1]

COMPONENT_MANIFESTS = {
    "nxthooks": "nxthooks",
    "nxtskills": "nxtskills",
    "nxtwiki": "wiki",
}


@pytest.mark.parametrize("package,plugin_id", sorted(COMPONENT_MANIFESTS.items()))
@pytest.mark.skip(reason="not applicable to the extracted package: asserts the NxtOS host package's contents")
def test_a_component_manifest_is_not_also_in_the_host_package(package, plugin_id):
    assert (REPO_ROOT / package / "plugin.yaml").is_file()
    assert not (BUILTIN_DIR / plugin_id).exists()


@pytest.mark.parametrize("package,plugin_id", sorted(COMPONENT_MANIFESTS.items()))
@pytest.mark.skip(reason="not applicable to the extracted package: needs nxtoskit's builtin plugin manifests")
def test_delivering_a_component_manifest_puts_it_in_the_roster(tmp_path, package, plugin_id):
    written, problem = deliver(REPO_ROOT / package / "plugin.yaml", tmp_path)
    assert problem == ""
    assert written == plugins_dir(tmp_path) / plugin_id / "plugin.yaml"
    record = next(r for r in scan(tmp_path) if r.id == plugin_id)
    assert record.enabled, record.reason


@pytest.mark.skip(reason="not applicable to the extracted package: needs nxtoskit's builtin plugin manifests")
def test_withdrawing_takes_the_row_out_again(tmp_path):
    deliver(REPO_ROOT / "nxtwiki" / "plugin.yaml", tmp_path)
    removed, problem = withdraw("wiki", tmp_path)
    assert problem == ""
    assert removed is not None
    assert not any(r.id == "wiki" for r in scan(tmp_path))


def test_withdrawing_what_was_never_delivered_is_not_an_error(tmp_path):
    """Uninstall runs on machines that never installed, and on machines that
    already uninstalled. Neither is a fault worth reporting."""
    assert withdraw("wiki", tmp_path) == (None, "")


@pytest.mark.skip(reason="not applicable to the extracted package: needs nxtoskit's builtin plugin manifests")
def test_delivery_is_idempotent(tmp_path):
    source = REPO_ROOT / "nxtwiki" / "plugin.yaml"
    first, _ = deliver(source, tmp_path)
    second, problem = deliver(source, tmp_path)
    assert problem == "" and first == second
    assert second.read_bytes() == source.read_bytes()


def test_a_component_never_installs_a_manifest_the_host_would_refuse(tmp_path):
    """The install says why, at the moment someone is watching.

    The alternative is a greyed-out row with a schema message in it, found
    later by whoever next opens the pane."""
    source = tmp_path / "plugin.yaml"
    source.write_text("id: broken\nkind: cli\nschemaVersion: 1\n", encoding="utf-8")
    written, problem = deliver(source, tmp_path / "home")
    assert written is None
    assert "schema" in problem
    assert not plugins_dir(tmp_path / "home").exists()


@pytest.mark.skip(reason="not applicable to the extracted package: needs nxtoskit's builtin plugin manifests")
def test_withdrawal_removes_the_manifest_not_the_neighbours(tmp_path):
    """A plugin directory can hold more than its manifest -- agent documents,
    for one -- and a component that shipped only the manifest has no business
    deleting whatever else came to live beside it."""
    deliver(REPO_ROOT / "nxtwiki" / "plugin.yaml", tmp_path)
    stranger = plugins_dir(tmp_path) / "wiki" / "notes.md"
    stranger.write_text("mine\n", encoding="utf-8")
    withdraw("wiki", tmp_path)
    assert stranger.is_file()


# --- the round trip through each component's own verbs ----------------------

@pytest.mark.skip(reason="not applicable to the extracted package: NxtWiki does not ship here")
def test_nxtwiki_install_delivers_its_pane_and_uninstall_removes_it(tmp_path):
    from nxtwiki import lifecycle

    lifecycle.install(tmp_path)
    assert any(r.id == "wiki" for r in scan(tmp_path))
    lifecycle.uninstall(tmp_path)
    assert not any(r.id == "wiki" for r in scan(tmp_path))


def test_nxthooks_install_delivers_its_pane(tmp_path, monkeypatch):
    from nxthooks import lifecycle

    monkeypatch.setenv("HOME", str(tmp_path))
    host = lifecycle.Host(tmp_path, {"HOME": str(tmp_path), "PATH": ""})
    args = types.SimpleNamespace(harnesses=[], config=None, json=False)
    lifecycle.cmd_install(args, host)
    assert any(r.id == "nxthooks" for r in scan(tmp_path))


@pytest.mark.skip(reason="not applicable to the extracted package: NxtSkills does not ship here")
def test_nxtskills_install_delivers_its_pane_and_uninstall_removes_it(tmp_path):
    from nxtskills.cli import main

    assert main(["--home", str(tmp_path), "install"]) in (0, 1)
    assert any(r.id == "nxtskills" for r in scan(tmp_path))
    assert main(["--home", str(tmp_path), "uninstall"]) in (0, 1)
    assert not any(r.id == "nxtskills" for r in scan(tmp_path))
