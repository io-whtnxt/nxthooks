"""The NxtHooks registry has one deterministic three-tier resolution model."""

import textwrap

import pytest

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


REFUSED_MATCHERS = (
    '[[hook]]\nname = "broken"\nevents = ["after_tool"]\n'
    'source = "/x"\nmatchers = ["Write", 42]\n'
)


def test_one_walk_returns_the_entries_and_the_refusals_with_their_tiers(
    tmp_path, monkeypatch
):
    """`resolve` walked the tiers and `skipped_manifests` walked them again to
    ask why hooks were missing, so every refusal was said twice on stderr and
    neither copy said which tier it came from (T2337, review d0cf8532).
    """
    builtin = tmp_path / "builtin"
    library = tmp_path / "library" / "hooks.d"
    user = tmp_path / "user"
    _write(builtin, "built", "built.sh")
    _write(library, "good", "good.sh")
    _write(user, "ours", "ours.sh")
    library.mkdir(parents=True, exist_ok=True)
    (library / "broken.toml").write_text(REFUSED_MATCHERS, encoding="utf-8")
    monkeypatch.setattr(registry, "BUILTIN_DIR", builtin)

    walked = []
    load_dir = registry._load_dir

    def counting(directory, tier):
        walked.append((directory, tier))
        return load_dir(directory, tier)

    monkeypatch.setattr(registry, "_load_dir", counting)
    result = registry.load_tiers(library=library.parent, hooks_d=user)

    assert walked == [(builtin, "builtin"), (library, "library"), (user, "user")]
    assert {entry["name"] for entry in result.entries} == {"built", "good", "ours"}
    assert [(r.tier, r.path.name) for r in result.refused] == [("library", "broken.toml")]
    assert "matchers" in result.refused[0].detail


def test_the_builtin_tier_is_walked_too(tmp_path, monkeypatch):
    """Neither of the two walkers passed over the tier that ships with the
    package, so a builtin manifest that stopped loading was the one refusal no
    report could see, on the machine that had it."""
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "rotten.toml").write_text('[[hook]]\nname = 7\n', encoding="utf-8")
    monkeypatch.setattr(registry, "BUILTIN_DIR", builtin)

    result = registry.load_tiers(library=tmp_path / "no-library",
                                 hooks_d=tmp_path / "no-user")

    assert [(r.tier, r.path.name) for r in result.refused] == [("builtin", "rotten.toml")]


@pytest.mark.parametrize("body", [
    pytest.param('[hook]\nname = "single"\nevents = ["after_tool"]\nsource = "/x"\n',
                 id="hook-is-one-table"),
    pytest.param('hook = "just-a-string"\n', id="hook-is-a-string"),
    pytest.param('hook = ["name", "events"]\n', id="hook-array-of-strings"),
])
def test_a_hook_section_that_is_not_an_array_of_tables_is_reported_not_raised(
    tmp_path, monkeypatch, body
):
    """`[hook]` iterates as its keys and a bare string as its characters, so
    both reached `_validate` as a string and raised AttributeError out of the
    loader: one mistyped manifest cost the operator the whole report."""
    user = tmp_path / "user"
    _write(user, "fine", "fine.sh")
    (user / "mistyped.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(registry, "BUILTIN_DIR", tmp_path / "no-builtin")

    result = registry.load_tiers(library=tmp_path / "no-library", hooks_d=user)

    assert [(r.tier, r.path.name) for r in result.refused] == [("user", "mistyped.toml")]
    assert {entry["name"] for entry in result.entries} == {"fine"}


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
