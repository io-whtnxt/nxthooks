"""What `nxthooks install` is allowed to put in the operator's hook store.

Nothing, as of N2-c. The seven manifests this file used to pin named sources
under `~/.nxtos/bin/` that no wheel installs, so on a stranger's machine all
seven resolved to a file that did not exist.
"""

from nxthooks import lifecycle


def test_a_component_seeds_no_manifest_it_does_not_ship():
    """The rule, stated as the thing it forbids.

    A manifest's `source` is a path to an implementation. Seeding one a
    component does not ship produces a hook that can never run, a `verify`
    reporting a gap its own printed advice cannot repair, and -- until the
    dispatcher stopped counting them -- a `doctor` reporting the guard as
    having run.
    """
    assert lifecycle.PILOT_MANIFESTS == {}


def test_seeding_creates_the_store_without_filling_it(hooks_machine):
    host = hooks_machine()

    seeded = lifecycle.seed_manifests(host)

    assert seeded == []
    assert host.hooks_d.is_dir()
    assert list(host.hooks_d.iterdir()) == []


def test_seeding_never_overwrites_a_manifest_the_operator_has(hooks_machine):
    """The mechanism survives the emptying: whatever a component seeds later,
    an existing manifest is the operator's, not ours to reset."""
    host = hooks_machine()
    host.hooks_d.mkdir(parents=True)
    (host.hooks_d / "secret-scan.toml").write_text("# already here\n")

    lifecycle.PILOT_MANIFESTS["secret-scan.toml"] = "# ours\n"
    try:
        assert lifecycle.seed_manifests(host) == []
    finally:
        del lifecycle.PILOT_MANIFESTS["secret-scan.toml"]

    assert (host.hooks_d / "secret-scan.toml").read_text() == "# already here\n"
