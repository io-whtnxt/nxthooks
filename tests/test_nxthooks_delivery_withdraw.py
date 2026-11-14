"""`delivery.withdraw` takes back what delivery wrote, and nothing else.

The manifest is not the only file a plugin directory holds. NxtOS's delivery
(T1874) copies every `agents:` entry beside the manifest it validated, so a
withdrawal that recognised one filename left a plugin agent resolvable on a
machine whose component was uninstalled. These tests pin the other side of the
rule too: the confinement the host applied when writing is the confinement
applied when removing, and a file the manifest never named is never touched.
"""

from __future__ import annotations

import json
import sys

import pytest

from nxthooks import delivery


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def plugin_dir(home):
    directory = delivery.plugins_dir(home, {}) / "demo"
    directory.mkdir(parents=True)
    return directory


def _write_manifest(plugin_dir, body: str) -> None:
    (plugin_dir / "plugin.yaml").write_text(body, encoding="utf-8")


def _seed(plugin_dir, relative: str, text: str = "doc\n"):
    path = plugin_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest_bodies():
    """Shapes a real manifest may arrive in, read by each reader's own rules."""
    return [
        ("block", "id: demo\nagents:\n  - agents/one.md\n  - agents/two.md\n"),
        ("column-zero", "id: demo\nagents:\n- agents/one.md\n"),
        ("quoted", "id: demo\nagents:\n  - 'agents/one two.md'\n"),
        ("absent", "id: demo\nkind: cli\n"),
        ("empty", "id: demo\nagents:\nkind: cli\n"),
        ("flow", "id: demo\nagents: [agents/one.md]\n"),
        ("escaped-double-quote", 'id: demo\nagents:\n  - "agents\\tone.md"\n'),
        ("anchored-null", "id: demo\nagents: &a\n"),
        ("mapping-item", "id: demo\nagents:\n  - name: one.md\n"),
        ("folded-item", "id: demo\nagents:\n  - agents/one.md\n    folded.md\n"),
        ("duplicate-key", "id: demo\nagents:\n  - agents/one.md\nagents:\n  - agents/two.md\n"),
    ]


class TestRemovesWhatWasDeclared:
    def test_declared_documents_go_and_the_empty_agents_directory_with_them(
        self, home, plugin_dir
    ):
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents/one.md\n  - agents/two.md\n")
        _seed(plugin_dir, "agents/one.md")
        _seed(plugin_dir, "agents/two.md")

        removed, problem = delivery.withdraw("demo", home, {})

        assert problem == ""
        assert removed == plugin_dir / "plugin.yaml"
        assert not (plugin_dir / "agents" / "one.md").exists()
        assert not (plugin_dir / "agents" / "two.md").exists()
        # The directory the delivery created came back out with its contents.
        assert not (plugin_dir / "agents").exists()
        assert not plugin_dir.exists()

    def test_a_nested_agents_directory_is_pruned_only_to_the_plugin_directory(
        self, home, plugin_dir
    ):
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents/deep/nested/one.md\n")
        _seed(plugin_dir, "agents/deep/nested/one.md")
        root = delivery.plugins_dir(home, {})

        delivery.withdraw("demo", home, {})

        assert not (plugin_dir / "agents").exists()
        # The plugin root itself is the host's, never this function's to prune.
        assert root.is_dir()

    def test_an_entry_written_through_itself_is_still_confined(self, home, plugin_dir):
        """`agents/../agents/one.md` names a file inside the directory, so it
        goes -- and pruning stops at the plugin directory instead of walking
        above it through the `..` the entry was written with."""
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents/../agents/one.md\n")
        _seed(plugin_dir, "agents/one.md")
        root = delivery.plugins_dir(home, {})

        delivery.withdraw("demo", home, {})

        assert not (plugin_dir / "agents").exists()
        assert not plugin_dir.exists()
        assert root.is_dir()

    def test_manifest_without_agents_behaves_exactly_as_before(self, home, plugin_dir):
        _write_manifest(plugin_dir, "id: demo\nkind: cli\n")

        removed, problem = delivery.withdraw("demo", home, {})

        assert (removed, problem) == (plugin_dir / "plugin.yaml", "")
        assert not plugin_dir.exists()

    def test_a_declared_document_that_is_not_there_is_not_a_failure(self, home, plugin_dir):
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents/gone.md\n")

        removed, problem = delivery.withdraw("demo", home, {})

        assert (removed, problem) == (plugin_dir / "plugin.yaml", "")

    def test_a_manifest_that_declares_itself_is_removed_once(self, home, plugin_dir):
        """The host confined `agents:` by "file inside the plugin directory", so
        `plugin.yaml` itself qualifies and would be unlinked as an agent. Twice
        is a FileNotFound the Plugin SDK turns into an exception, so the
        manifest is withdrawn by the withdrawal, not by the declaration."""
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - plugin.yaml\n")

        removed, problem = delivery.withdraw("demo", home, {})

        assert (removed, problem) == (plugin_dir / "plugin.yaml", "")
        assert not plugin_dir.exists()

    def test_withdrawing_twice_is_quiet(self, home, plugin_dir):
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents/one.md\n")
        _seed(plugin_dir, "agents/one.md")
        delivery.withdraw("demo", home, {})

        removed, problem = delivery.withdraw("demo", home, {})

        assert (removed, problem) == (None, "")


class TestKeepsWhatWasNotDeclared:
    def test_an_undeclared_neighbour_survives_and_holds_the_directory_open(
        self, home, plugin_dir
    ):
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents/one.md\n")
        _seed(plugin_dir, "agents/one.md")
        _seed(plugin_dir, "agents/operator-note.md", "written by a person\n")
        _seed(plugin_dir, "data/state.json", "{}\n")

        delivery.withdraw("demo", home, {})

        assert not (plugin_dir / "agents" / "one.md").exists()
        assert (plugin_dir / "agents" / "operator-note.md").read_text() == "written by a person\n"
        assert (plugin_dir / "data" / "state.json").exists()
        assert plugin_dir.exists()

    def test_a_declared_directory_is_not_recursed_into(self, home, plugin_dir):
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents\n")
        _seed(plugin_dir, "agents/one.md")

        removed, problem = delivery.withdraw("demo", home, {})

        assert (removed, problem) == (plugin_dir / "plugin.yaml", "")
        assert (plugin_dir / "agents" / "one.md").exists()
        # The manifest still came out; only the undeclared contents stay.
        assert not (plugin_dir / "plugin.yaml").exists()

    def test_a_declared_symlink_costs_its_link_and_never_its_target(self, home, plugin_dir, tmp_path):
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents/link.md\n")
        _seed(plugin_dir, "agents/real.md", "inside\n")
        (plugin_dir / "agents" / "link.md").symlink_to(plugin_dir / "agents" / "real.md")

        delivery.withdraw("demo", home, {})

        assert not (plugin_dir / "agents" / "link.md").exists()
        assert (plugin_dir / "agents" / "real.md").read_text() == "inside\n"


class TestConfinement:
    """The host refused these on the way in; removal must not follow them out."""

    def test_traversal_and_absolute_entries_are_refused(self, home, plugin_dir, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("someone else's\n", encoding="utf-8")
        neighbour = delivery.plugins_dir(home, {}) / "other.md"
        neighbour.write_text("another plugin's\n", encoding="utf-8")
        _write_manifest(
            plugin_dir,
            f"id: demo\nagents:\n  - ../other.md\n  - {outside}\n  - ../{outside.name}\n",
        )

        removed, problem = delivery.withdraw("demo", home, {})

        assert (removed, problem) == (plugin_dir / "plugin.yaml", "")
        assert outside.read_text() == "someone else's\n"
        assert neighbour.read_text() == "another plugin's\n"

    def test_a_symlink_out_of_the_plugin_directory_is_refused(self, home, plugin_dir, tmp_path):
        outside = tmp_path / "vault.md"
        outside.write_text("operator's own\n", encoding="utf-8")
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - agents/peek.md\n")
        (plugin_dir / "agents").mkdir()
        (plugin_dir / "agents" / "peek.md").symlink_to(outside)

        delivery.withdraw("demo", home, {})

        assert outside.read_text() == "operator's own\n"
        # Keeping the link is the conservative outcome: it is outside the
        # declaration's confinement, so removal stops rather than guesses.
        assert (plugin_dir / "agents" / "peek.md").is_symlink()

    def test_a_symlinked_plugin_directory_declares_nothing_removable(self, home, tmp_path):
        real = tmp_path / "real-plugin"
        real.mkdir()
        (real / "agent.md").write_text("keep\n", encoding="utf-8")
        link = delivery.plugins_dir(home, {}) / "demo"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        (real / "plugin.yaml").write_text("id: demo\nagents:\n  - agent.md\n", encoding="utf-8")

        delivery.withdraw("demo", home, {})

        assert (real / "agent.md").read_text() == "keep\n"


class TestUnreadableDeclarationIsNotAnError:
    """The Plugin SDK raises on a non-empty reason, so only a failed unlink may say so."""

    @pytest.mark.parametrize(
        "body",
        [
            "\t- not: [valid\n",                       # malformed YAML
            json.dumps({"id": "demo", "agents": "not-a-list"}),
            json.dumps({"id": "demo", "agents": [1, 2]}),
        ],
        ids=["malformed-yaml", "agents-is-a-string", "agents-is-not-strings"],
    )
    def test_the_manifest_still_comes_and_the_neighbour_stays(self, home, plugin_dir, body):
        _write_manifest(plugin_dir, body)
        _seed(plugin_dir, "agents/one.md")

        removed, problem = delivery.withdraw("demo", home, {})

        assert problem == ""
        assert removed == plugin_dir / "plugin.yaml"
        assert (plugin_dir / "agents" / "one.md").exists()

    def test_a_manifest_that_is_not_text_is_understood_as_declaring_nothing(
        self, home, plugin_dir
    ):
        (plugin_dir / "plugin.yaml").write_bytes(b"\xff\xfe\x00")
        _seed(plugin_dir, "agents/one.md")

        removed, problem = delivery.withdraw("demo", home, {})

        assert problem == ""
        assert removed == plugin_dir / "plugin.yaml"
        assert (plugin_dir / "agents" / "one.md").exists()


class TestWithoutPyYAML:
    """`nxthooks uninstall` runs from a package that does not require PyYAML.

    The documents it is taking back were written by the host, which does, so
    the fallback reader has to cope with the common shape -- and refuse
    anything it would have to guess at, which costs a stale document rather
    than a wrong deletion.
    """

    @pytest.fixture
    def no_yaml(self, monkeypatch):
        # A None entry in sys.modules makes `import yaml` raise ImportError.
        monkeypatch.setitem(sys.modules, "yaml", None)

    def test_a_block_sequence_is_read(self, home, plugin_dir, no_yaml):
        _write_manifest(
            plugin_dir,
            "# a comment\nid: demo\nagents:\n  - agents/one.md\n  - agents/two.md\nkind: cli\n",
        )
        _seed(plugin_dir, "agents/one.md")
        _seed(plugin_dir, "agents/two.md")

        _, problem = delivery.withdraw("demo", home, {})

        assert problem == ""
        assert not (plugin_dir / "agents").exists()

    def test_a_sequence_at_column_zero_is_read(self, home, plugin_dir, no_yaml):
        _write_manifest(plugin_dir, "id: demo\nagents:\n- agents/one.md\n")
        _seed(plugin_dir, "agents/one.md")

        delivery.withdraw("demo", home, {})

        assert not (plugin_dir / "agents" / "one.md").exists()

    def test_a_quoted_item_is_read(self, home, plugin_dir, no_yaml):
        _write_manifest(plugin_dir, "id: demo\nagents:\n  - 'agents/one two.md'\n")
        _seed(plugin_dir, "agents/one two.md")

        delivery.withdraw("demo", home, {})

        assert not (plugin_dir / "agents" / "one two.md").exists()

    @pytest.mark.parametrize(
        "body",
        [
            "id: demo\nagents: [agents/one.md]\n",
            'id: demo\nagents:\n  - "agents\\tone.md"\n',
            "id: demo\nagents: &a\n",
            "id: demo\nagents:\n  - name: one.md\n",
            "id: demo\nagents:\n  - agents/one.md\n    folded.md\n",
            "id: demo\nagents:\n  - agents/one.md\nagents:\n  - agents/two.md\n",
        ],
        ids=["flow", "escaped-double-quote", "anchored", "mapping-item",
             "folded-item", "duplicate-key"],
    )
    def test_what_it_must_guess_at_it_leaves_alone(self, home, plugin_dir, no_yaml, body):
        _write_manifest(plugin_dir, body)
        _seed(plugin_dir, "agents/one.md")
        _seed(plugin_dir, "agents/two.md")

        removed, problem = delivery.withdraw("demo", home, {})

        assert problem == ""
        assert removed == plugin_dir / "plugin.yaml"
        assert (plugin_dir / "agents" / "one.md").exists()
        assert (plugin_dir / "agents" / "two.md").exists()

    @pytest.mark.parametrize(
        "body",
        [body for _, body in _manifest_bodies()],
        ids=[name for name, _ in _manifest_bodies()],
    )

    def test_the_fallback_never_names_what_pyyaml_would_not(self, plugin_dir, monkeypatch, body):
        """The two readers may differ in reach, never in verdict.

        PyYAML reads a flow sequence the fallback refuses, so the fallback can
        leave a declared document in place. The other direction is the one that
        costs something: an entry the fallback names must be the entry PyYAML
        names, or which copy of the interpreter runs `uninstall` would decide
        which files survive.
        """
        _write_manifest(plugin_dir, body)
        manifest = plugin_dir / "plugin.yaml"
        with_pyyaml = delivery._agents_entries(manifest)
        monkeypatch.setitem(sys.modules, "yaml", None)
        without = delivery._agents_entries(manifest)

        if without is None:
            return  # refused outright: it names nothing, so it deletes nothing
        assert without == with_pyyaml

    def test_a_manifest_that_declares_nothing_is_understood(self, home, plugin_dir, no_yaml):
        _write_manifest(plugin_dir, "id: demo\ncli:\n  commands:\n    - name: list\n")

        removed, problem = delivery.withdraw("demo", home, {})

        assert (removed, problem) == (plugin_dir / "plugin.yaml", "")
        assert not plugin_dir.exists()
