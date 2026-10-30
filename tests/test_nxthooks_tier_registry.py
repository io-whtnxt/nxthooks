"""The NxtHooks registry has one deterministic three-tier resolution model."""

import textwrap

from nxthooks import manifest, registry
from nxthooks.lifecycle import Host


def _write(directory, name, source):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(
        textwrap.dedent(
            f"""\
            [[hook]]
            name = "{name}"
            events = ["after_tool"]
            source = "{source}"
            """
        ),
        encoding="utf-8",
    )


def test_builtin_library_user_precedence_and_provenance(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    library = tmp_path / "library" / "hooks.d"
    user = tmp_path / "user"
    _write(builtin, "same", "builtin.sh")
    _write(builtin, "builtin-only", "builtin-only.sh")
    _write(library, "same", "library.sh")
    _write(library, "library-only", "library-only.sh")
    _write(user, "same", "user.sh")
    _write(user, "user-only", "user-only.sh")
    monkeypatch.setattr(registry, "BUILTIN_DIR", builtin)

    entries, collisions = registry.resolve(library=library.parent, hooks_d=user)
    by_name = {entry["name"]: entry for entry in entries}

    assert by_name["same"]["source"] == "library.sh"
    assert by_name["same"]["tier"] == "library"
    assert by_name["builtin-only"]["tier"] == "builtin"
    assert by_name["library-only"]["tier"] == "library"
    assert by_name["user-only"]["tier"] == "user"
    assert collisions == (
        registry.Collision("same", "user", "library", str(user / "same.toml")),
    )


def test_load_manifests_uses_the_resolver_and_accepts_a_sandbox(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    _write(builtin, "built", "built.sh")
    _write(user, "user", "user.sh")
    monkeypatch.setattr(registry, "BUILTIN_DIR", builtin)

    from nxthooks import manifest

    entries = manifest.load_manifests(hooks_d=user)

    assert {entry["name"] for entry in entries} == {"built", "user"}
    assert {entry["tier"] for entry in entries} == {"builtin", "user"}


def test_host_resolution_uses_the_host_data_root_for_library_manifests(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    _write(builtin, "built", "built.sh")
    monkeypatch.setattr(registry, "BUILTIN_DIR", builtin)
    host = Host(
        home=tmp_path / "home",
        env={
            "NXT_CONFIG_HOME": str(tmp_path / "config"),
            "NXT_DATA_HOME": str(tmp_path / "data"),
        },
    )
    _write(host.library_root / "hooks.d", "fleet", "fleet.sh")
    _write(host.hooks_d, "user", "user.sh")

    entries = manifest.load_manifests(host.hooks_d, library=host.library_root)

    assert {entry["name"] for entry in entries} == {"built", "fleet", "user"}
    assert next(entry for entry in entries if entry["name"] == "fleet")["tier"] == "library"
