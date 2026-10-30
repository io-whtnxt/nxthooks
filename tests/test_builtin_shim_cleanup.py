import importlib
from pathlib import Path

import pytest

MODULES = ["nxthooks.engines.nxtgrep.install", "nxthooks.engines.nxtlint.install", "nxthooks.engines.snapshots.install"]


@pytest.mark.parametrize("module", MODULES)
def test_uninstall_removes_owned_shim_but_preserves_neighbors(tmp_path, module):
    installer = importlib.import_module(module)
    directory = tmp_path / "bin"
    target = installer.install_shim(directory)
    foreign = directory / "operator.py"
    foreign.write_text("keep")
    installer.install_shim(directory)
    installer.uninstall_shim(directory)
    assert not target.exists()
    assert foreign.read_text() == "keep"
    assert list(directory.iterdir()) == [foreign]


@pytest.mark.parametrize("module", MODULES)
def test_modified_or_unowned_shim_is_preserved(tmp_path, module):
    installer = importlib.import_module(module)
    directory = tmp_path / "bin"
    directory.mkdir()
    foreign = directory / installer.SHIM_NAME
    foreign.write_text("foreign")
    with pytest.raises(ValueError, match="unowned"):
        installer.install_shim(directory)
    installer.uninstall_shim(directory)
    assert foreign.read_text() == "foreign"
    foreign.unlink()
    target = installer.install_shim(directory)
    target.write_text("edited")
    installer.uninstall_shim(directory)
    assert target.read_text() == "edited"


@pytest.mark.parametrize("module", MODULES)
def test_symlinked_shim_directory_is_refused(tmp_path, module):
    installer = importlib.import_module(module)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        installer.install_shim(alias)
    with pytest.raises(ValueError, match="symlink"):
        installer.uninstall_shim(alias)
    assert not list(real.iterdir())


def test_cleanup_preserves_preexisting_empty_parent(tmp_path):
    from nxthooks import _shims as shims
    parent = tmp_path / "existing"
    parent.mkdir()
    target = parent / "created" / "shim.py"
    shims.install(target, "print('ok')")
    shims.uninstall(target)
    assert parent.is_dir()
    assert (parent / "created").is_dir()
    assert not list((parent / "created").iterdir())


def test_replaced_target_symlink_is_never_followed(tmp_path):
    from nxthooks import _shims as shims
    target = tmp_path / "shim.py"
    shims.install(target, "owned")
    target.unlink()
    foreign = tmp_path / "foreign"
    foreign.write_text("keep")
    target.symlink_to(foreign)
    with pytest.raises(ValueError, match="symlink"):
        shims.uninstall(target)
    assert foreign.read_text() == "keep"


def test_untrusted_directory_metadata_never_grants_deletion(tmp_path):
    import json
    from nxthooks import _shims as shims
    parent = tmp_path / "created"
    target = parent / "shim.py"
    shims.install(target, "owned")
    marker = parent / ".shim.py.nxtos-owned.json"
    record = json.loads(marker.read_text())
    record["created"] = [str(tmp_path), str(parent), "/unrelated"]
    marker.write_text(json.dumps(record))
    shims.uninstall(target)
    assert tmp_path.is_dir()
    assert parent.is_dir()
    assert not list(parent.iterdir())


def test_owned_shim_upgrade_converges_and_uninstalls(tmp_path):
    from nxthooks import _shims as shims
    target = tmp_path / "shim.py"
    shims.install(target, "old")
    shims.install(target, "new body")
    assert target.read_text() == "new body"
    shims.uninstall(target)
    assert not target.exists()



def test_hardlinked_ownership_record_is_refused(tmp_path):
    from nxthooks import _shims as shims
    target = tmp_path / "shim.py"
    shims.install(target, "owned")
    marker = tmp_path / ".shim.py.nxtos-owned.json"
    foreign = tmp_path / "foreign.json"
    foreign.hardlink_to(marker)
    original = foreign.read_bytes()
    with pytest.raises(ValueError, match="hard-linked"):
        shims.install(target, "upgrade")
    with pytest.raises(ValueError, match="hard-linked"):
        shims.uninstall(target)
    assert foreign.read_bytes() == original
    assert target.read_text() == "owned"



@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS system aliases")
def test_macos_system_temporary_alias_still_supports_owned_cleanup(tmp_path):
    from nxthooks import _shims as shims
    canonical = tmp_path.resolve()
    if not canonical.is_relative_to(Path("/private/var")):
        pytest.skip("temporary directory is outside macOS /var")
    alias = Path("/var") / canonical.relative_to("/private/var")
    target = shims.install(alias / "shim.py", "owned")
    assert target == canonical / "shim.py"
    shims.uninstall(alias / "shim.py")
    assert not target.exists()


@pytest.mark.parametrize("operation", ["upgrade", "uninstall"])
def test_parent_replacement_after_owned_check_never_mutates_foreign(tmp_path, monkeypatch, operation):
    from nxthooks import _shims as shims
    owned = tmp_path / "owned"
    target = owned / "shim.py"
    shims.install(target, "owned")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    victim = foreign / target.name
    victim.write_text("foreign")
    moved = tmp_path / "moved"
    matches = shims._matches
    replaced = False

    def replace_parent(*args):
        nonlocal replaced
        answer = matches(*args)
        if answer and not replaced:
            owned.rename(moved)
            owned.symlink_to(foreign, target_is_directory=True)
            replaced = True
        return answer

    monkeypatch.setattr(shims, "_matches", replace_parent)
    if operation == "upgrade":
        shims.install(target, "upgrade")
    else:
        shims.uninstall(target)
    assert replaced
    assert victim.read_text() == "foreign"


def test_tampered_parent_list_cannot_delete_preexisting_empty_ancestor(tmp_path):
    import json
    from nxthooks import _shims as shims
    existing = tmp_path / "existing"
    existing.mkdir()
    target = existing / "created" / "shim.py"
    shims.install(target, "owned")
    marker = target.with_name(".shim.py.nxtos-owned.json")
    record = json.loads(marker.read_text())
    record.setdefault("created", []).append(str(existing))
    marker.write_text(json.dumps(record))
    shims.uninstall(target)
    assert existing.is_dir()



@pytest.mark.parametrize("operation", ["upgrade", "uninstall"])
def test_target_replacement_after_owned_check_preserves_foreign(tmp_path, monkeypatch, operation):
    from nxthooks import _shims as shims
    target = tmp_path / "shim.py"
    shims.install(target, "owned")
    matches = shims._matches
    replaced = False

    def replace_target(*args):
        nonlocal replaced
        answer = matches(*args)
        if answer and not replaced:
            target.unlink()
            target.write_text("foreign")
            replaced = True
        return answer

    monkeypatch.setattr(shims, "_matches", replace_target)
    try:
        if operation == "upgrade":
            shims.install(target, "upgrade")
        else:
            shims.uninstall(target)
    except ValueError as exc:
        assert "concurrent" in str(exc)
    assert replaced
    assert target.read_text() == "foreign"


def test_directory_replaced_between_mkdir_and_open_is_never_claimed(tmp_path, monkeypatch):
    from nxthooks import _shims as shims
    parent = tmp_path / "owned"
    foreign = tmp_path / "foreign-empty"
    foreign.mkdir()
    foreign_inode = foreign.stat().st_ino
    moved = tmp_path / "moved"
    mkdir = shims.os.mkdir
    replaced = False

    def replace_created_directory(path, *args, **kwargs):
        nonlocal replaced
        result = mkdir(path, *args, **kwargs)
        if path == "owned" and not replaced:
            parent.rename(moved)
            foreign.rename(parent)
            replaced = True
        return result

    with monkeypatch.context() as patch:
        patch.setattr(shims.os, "mkdir", replace_created_directory)
        shims.install(parent / "shim.py", "owned")
    shims.uninstall(parent / "shim.py")
    assert replaced
    assert parent.is_dir()
    assert parent.stat().st_ino == foreign_inode
    assert moved.is_dir()


# The exact shim bodies main's installers (be75c219) wrote, with no ownership
# record. Spelled out here rather than imported so a template edit cannot make
# an upgrade from them silently stop working.
_MAIN_SHIMS = {
    "nxthooks.engines.nxtgrep.install": ("NxtGrep hook shim", "nxtgrep/hook.py", "nxtgrep.install", "nxtgrep.hook"),
    "nxthooks.engines.nxtlint.install": ("NxtLint hook shim", "nxtlint/hook.py", "nxtlint.install", "nxtlint.hook"),
    "nxthooks.engines.snapshots.install": ("Snapshot hook shim", "snapshots/hook.py", "snapshots.install", "snapshots.hook"),
}


def _main_shim(module, root):
    title, source, installer, hook = _MAIN_SHIMS[module]
    return ('#!/usr/bin/env python3\n'
            f'"""{title} (installed copy; canonical source in the nxtos repo at\n'
            f'nxtoskit/{source}). Regenerate with `python3 -m nxtoskit.{installer}`."""\n'
            'import sys\n'
            f'sys.path.insert(0, {root!r})\n'
            f'from nxtoskit.{hook} import main\n'
            'sys.exit(main())\n')


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("root", ["/opt/old-nxtos/lib/python3.12/site-packages", None])
def test_main_installer_output_is_adopted_reprovisioned_and_uninstalled(tmp_path, module, root):
    installer = importlib.import_module(module)
    directory = tmp_path / "bin"
    directory.mkdir()
    legacy = directory / installer.SHIM_NAME
    root = root or str(installer.package_root(Path(installer.__file__)))
    legacy.write_text(_main_shim(module, root), encoding="utf-8")
    legacy.chmod(0o755)
    target = installer.install_shim(directory)
    current = installer._SHIM.format(root=str(installer.package_root(Path(installer.__file__))))
    assert target.read_text() == current
    installer.uninstall_shim(directory)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("module", MODULES)
def test_modified_main_installer_output_is_still_refused(tmp_path, module):
    installer = importlib.import_module(module)
    directory = tmp_path / "bin"
    directory.mkdir()
    legacy = directory / installer.SHIM_NAME
    edited = _main_shim(module, "/opt/old") + "print('operator edit')\n"
    legacy.write_text(edited)
    with pytest.raises(ValueError, match="unowned"):
        installer.install_shim(directory)
    installer.uninstall_shim(directory)
    assert legacy.read_text() == edited
    assert list(directory.iterdir()) == [legacy]


def test_a_symlink_planted_after_the_marker_leaves_no_marker_behind(tmp_path, monkeypatch):
    # The fresh install creates its ownership marker before the shim; a symlink
    # planted in between used to leave that marker, empty, and every later
    # install then refused the shim as altered.
    from nxthooks import _shims as shims
    target = tmp_path / "shim.py"
    foreign = tmp_path / "foreign.py"
    foreign.write_text("foreign")
    save = shims._save

    def plant_then_save(stream, record):
        save(stream, record)
        if record == {"sha256": None} and not target.is_symlink():
            target.symlink_to(foreign)

    monkeypatch.setattr(shims, "_save", plant_then_save)
    with pytest.raises(ValueError, match="Refusing symlink in shim path"):
        shims.install(target, "owned", prior="old {root!r}")
    monkeypatch.undo()
    assert foreign.read_text() == "foreign"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["foreign.py", "shim.py"]
    target.unlink()
    shims.install(target, "owned")
    assert target.read_text() == "owned"
