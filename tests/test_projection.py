"""The projection library, exercised only against a sandbox HOME.

Every test here builds a fake home directory and points the harness table at
it. Nothing reads or writes the operator's real `~/.claude`, `~/.codex`,
`~/.pi`, `~/.config/opencode` or `~/.hermes`, and
`test_no_test_here_can_reach_the_operators_own_config` is the assertion that
keeps it that way.

`wire` and `unwire` are not library verbs -- components own those -- so the
helpers at the top of this module play the component's part. They are also the
worked example of the intended call shape: detect, then edit under the lock,
then append to the ledger.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nxthooks.projection import (
    ALTERED,
    INTACT,
    MISSING,
    ConcurrentModification,
    Detection,
    NotDetected,
    detect,
    detect_json_style,
    envelope,
    ledger,
    orphans,
    write,
)
from nxthooks.projection.verify import verify
from nxthooks.projection.envelope import (
    FORM_ENV_PREFIX,
    LEGACY_ENV_PREFIX,
    Marker,
    command_payload,
    digest,
    stamp_command,
)

COMPONENT = "hooks"
VERSION = "0.3.0"

#: What a stranger already had in their settings file. Reproduced from the
#: live shapes on this machine: an NLM hook using the same env-assignment
#: idiom, a bare `printf`, and a permissions block that key-orders around ours.
FOREIGN_SETTINGS = {
    "permissions": {"allow": ["Bash(echo:*)"], "deny": []},
    "model": "opus",
    "hooks": {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": 'printf "hello"'}]},
        ],
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{
                    "type": "command",
                    "command": "NLM_HOOK_MODE=live '/opt/nlm/bin/node' /opt/nlm/hook.js",
                }],
            },
        ],
    },
    "note": "café naïve",
}


# --------------------------------------------------------------- the harness


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    (root / ".claude").mkdir(parents=True)
    settings(root).write_text(
        json.dumps(FOREIGN_SETTINGS, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    """The dispatcher a wired stanza depends on."""
    script = tmp_path / "runtime" / "bin" / "cc-dispatch.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return script


def settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def no_binaries(_name: str) -> str | None:
    """A PATH with no harness on it, so detection is decided by the sandbox."""
    return None


def detected(home: Path) -> Detection:
    return detect("claude-code", home=home, env={}, path_lookup=no_binaries)


# ------------------------------------------------- the component's own verbs


def wire(home: Path, runtime: Path, *, event: str = "SessionStart",
         arg: str = "session_start", ledger_file: Path | None = None) -> ledger.Record:
    """What a component does: detect, edit under the lock, record."""
    detection = detected(home)
    command = stamp_command(f'sh "{runtime}" {arg}', COMPONENT, VERSION)
    box = {}

    def mutate(doc):
        groups = doc.setdefault("hooks", {}).setdefault(event, [])
        groups.append({"hooks": [{"type": "command", "command": command}]})
        box["slot"] = f"/hooks/{event}/{len(groups) - 1}/hooks/0"
        return doc

    outcome = write.edit_json(
        settings(home), detection=detection, mutate=mutate, strip=strip_ours
    )
    record = ledger.Record(
        op=ledger.WIRE, component=COMPONENT, version=VERSION,
        harness="claude-code", file=str(settings(home)), slot=box["slot"],
        marker=str(envelope.parse_command_marker(command)), form=FORM_ENV_PREFIX,
        own_sha256=digest(command_payload(command)),
        foreign_sha256_before=outcome.foreign_before,
        foreign_sha256_after=outcome.foreign_after,
        runtime=str(runtime),
    )
    ledger.append(record, path=ledger_file or ledger_path(home))
    return record


def unwire(home: Path, record: ledger.Record, *,
           ledger_file: Path | None = None) -> None:
    detection = detected(home)

    def mutate(doc):
        for pointer, command in list(orphans.walk_commands(doc)):
            marker = envelope.parse_command_marker(command)
            if marker is not None and str(marker) == record.marker:
                orphans.prune(doc, pointer)
        return doc

    outcome = write.edit_json(
        settings(home), detection=detection, mutate=mutate, strip=strip_ours
    )
    ledger.append(
        ledger.Record(
            op=ledger.UNWIRE, component=record.component, version=record.version,
            harness=record.harness, file=record.file, slot=record.slot,
            marker=record.marker, form=record.form,
            foreign_sha256_before=outcome.foreign_before,
            foreign_sha256_after=outcome.foreign_after,
        ),
        path=ledger_file or ledger_path(home),
    )


def strip_ours(doc):
    """The foreign remainder: the document with every Nxt stanza removed."""
    clone = json.loads(json.dumps(doc))
    pointers = [
        pointer for pointer, command in orphans.walk_commands(clone)
        if envelope.parse_command_marker(command) is not None
        or envelope.is_legacy_command(command)
    ]
    for pointer in sorted(pointers, reverse=True):
        orphans.prune(clone, pointer)
    return clone


def ledger_path(home: Path) -> Path:
    return home / ".local" / "state" / "nxt" / ledger.LEDGER_NAME


# ------------------------------------------------------------------ envelope


def test_the_digest_excludes_the_marker_it_is_embedded_in():
    """The marker lives inside the string it describes. A digest that covered
    itself could not be recomputed, so re-stamping would never converge and
    `verify` would report altered for every slot forever."""
    once = stamp_command('sh "/r/bin/d.sh" before_tool', COMPONENT, VERSION)
    twice = stamp_command(once, COMPONENT, VERSION)

    assert once == twice
    assert command_payload(once) == 'sh "/r/bin/d.sh" before_tool'
    assert envelope.parse_command_marker(once).covers(command_payload(once))


def test_a_marker_parses_from_both_spellings_and_round_trips():
    marker = envelope.build(COMPONENT, VERSION, "payload")
    assert envelope.parse_marker(str(marker)) == marker
    assert envelope.parse_marker(f"{COMPONENT}/{VERSION}/{marker.sha12}") == marker
    assert marker.env_assignment == f"NXT={COMPONENT}/{VERSION}/{marker.sha12}"


def test_the_legacy_prefix_is_recognised_but_never_verifiable():
    """NxtHooks 0.1/0.2 wrote a marker with no component, version or hash.
    Unwire has to know it; verify must not pretend it can check it."""
    command = f'{LEGACY_ENV_PREFIX} sh "/r/bin/d.sh" session_start'

    assert envelope.is_legacy_command(command)
    assert envelope.parse_command_marker(command) is None
    assert command_payload(command) == 'sh "/r/bin/d.sh" session_start'


def test_a_first_line_marker_uses_the_right_comment_syntax():
    marker = envelope.build(COMPONENT, VERSION, "body")
    assert envelope.comment_line(marker, ".ts").startswith("// nxt:")
    assert envelope.comment_line(marker, ".py").startswith("# nxt:")
    with pytest.raises(envelope.MarkerError):
        envelope.comment_line(marker, ".sqlite")


# -------------------------------------------------------------------- detect


def test_a_directory_alone_is_not_a_harness(tmp_path: Path):
    """`copytree` and `mkdir(parents=True)` manufacture a config root. A probe
    that accepts one lets NxtOS wire a harness it invented."""
    root = tmp_path / "home"
    (root / ".pi" / "agent" / "extensions").mkdir(parents=True)

    result = detect("pi", home=root, env={}, path_lookup=no_binaries)

    assert result.present is False
    assert "none of the harness's own files" in result.reason


def test_the_harnesss_own_file_is_what_makes_it_present(home: Path):
    result = detected(home)

    assert result.present is True
    assert result.paths == (settings(home),)


def test_a_binary_on_path_is_enough_without_any_config(tmp_path: Path):
    result = detect(
        "codex", home=tmp_path / "empty", env={},
        path_lookup=lambda name: "/usr/local/bin/codex",
    )

    assert result.present is True
    assert "on PATH" in result.reason


def test_an_env_override_relocates_the_whole_root(tmp_path: Path):
    """The seam that makes a sandbox and a packaged install one code path."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "hooks.json").parent.mkdir(parents=True)
    (elsewhere / "hooks.json").write_text("{}", encoding="utf-8")

    result = detect(
        "codex", home=tmp_path / "home", env={"CODEX_HOME": str(elsewhere)},
        path_lookup=no_binaries,
    )

    assert result.present is True
    assert result.root == elsewhere


def test_no_sentinel_is_a_directory_nxtos_creates_itself(tmp_path: Path):
    """`~/.pi/agent/extensions` and `~/.config/opencode/plugins` are made by
    NxtHooks' own `copytree`. A sentinel on one lets NxtOS detect the harness
    it manufactured two minutes ago, which is the bug detect exists to end."""
    from nxthooks.projection.detect import HARNESSES

    nxtos_creates = (".pi/agent/extensions", ".config/opencode/plugins",
                     ".hermes/plugins", ".claude/hooks")
    root = tmp_path / "home"
    for created in nxtos_creates:
        (root / created).mkdir(parents=True, exist_ok=True)

    for spec in HARNESSES.values():
        for sentinel in spec.sentinels:
            assert sentinel not in nxtos_creates, f"{spec.id}: {sentinel}"
        assert detect(
            spec.id, home=root, env={}, path_lookup=no_binaries
        ).present is False, spec.id


def test_every_harness_records_what_it_was_verified_against():
    """A harness whose writer has never been driven must not silently be
    treated as safe for an in-file key."""
    from nxthooks.projection.detect import HARNESSES

    for spec in HARNESSES.values():
        assert spec.tolerates_unknown_keys is False, spec.id
        assert spec.notes, spec.id


# --------------------------------------------------------------------- write


def test_a_write_without_a_preceding_detect_is_refused(home: Path):
    """MANDATORY. 'Detect before any write' enforced by convention is enforced
    by nobody; the function will not run without the token."""
    with pytest.raises(NotDetected) as caught:
        write.edit_json(settings(home), detection=None, mutate=lambda doc: doc)

    assert "no detection was performed" in str(caught.value)
    assert json.loads(settings(home).read_text()) == FOREIGN_SETTINGS


def test_a_negative_detection_does_not_authorise_a_write(tmp_path: Path):
    root = tmp_path / "home"
    (root / ".claude").mkdir(parents=True)
    absent = detect("claude-code", home=root, env={}, path_lookup=no_binaries)

    with pytest.raises(NotDetected):
        write.edit_json(settings(root), detection=absent, mutate=lambda doc: doc)

    assert not settings(root).exists()


def test_a_detection_for_one_harness_does_not_licence_another(home: Path, tmp_path: Path):
    codex = tmp_path / "home" / ".codex" / "hooks.json"
    codex.parent.mkdir(parents=True)
    codex.write_text("{}", encoding="utf-8")

    with pytest.raises(NotDetected) as caught:
        write.edit_json(codex, detection=detected(home), mutate=lambda doc: doc)

    assert "does not contain it" in str(caught.value)


def test_the_foreign_remainder_survives_wire_and_unwire_byte_for_byte(
    home: Path, runtime: Path
):
    """The C6 bar, for the one writer in the family that can meet it: NxtOS
    was the last writer, so the bytes must come back identical."""
    before = settings(home).read_bytes()

    record = wire(home, runtime)
    assert settings(home).read_bytes() != before
    unwire(home, record)

    assert settings(home).read_bytes() == before


def test_a_strangers_indentation_comes_back_unchanged(home: Path, runtime: Path):
    settings(home).write_text(
        json.dumps(FOREIGN_SETTINGS, indent=4, ensure_ascii=False), encoding="utf-8"
    )

    wire(home, runtime)
    text = settings(home).read_text(encoding="utf-8")

    assert '\n    "model"' in text
    assert '\n  "model"' not in text
    assert not text.endswith("\n")


def test_a_tab_indented_file_stays_tab_indented(home: Path, runtime: Path):
    settings(home).write_text(
        json.dumps(FOREIGN_SETTINGS, indent="\t", ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    wire(home, runtime)

    assert '\n\t"model"' in settings(home).read_text(encoding="utf-8")


def test_a_strangers_accented_text_is_not_rewritten_into_escapes(
    home: Path, runtime: Path
):
    """Claude Code's own writer emits UTF-8; `json.dumps` defaults to
    `ensure_ascii=True`, which is what NxtHooks calls today. On any settings
    file holding an accented character that turns a wire into a rewrite of
    someone else's prose."""
    wire(home, runtime)
    text = settings(home).read_text(encoding="utf-8")

    assert "café naïve" in text
    assert "\\u00e9" not in text


def test_a_file_that_was_pure_ascii_stays_escaped_the_way_it_was(
    home: Path, runtime: Path
):
    """The mirror of the rule above: style is detected, not preferred."""
    settings(home).write_text(
        json.dumps(FOREIGN_SETTINGS, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    wire(home, runtime)
    text = settings(home).read_text(encoding="utf-8")

    assert "\\u00e9" in text
    assert "café" not in text


def test_style_detection_reads_the_file_rather_than_a_default():
    assert detect_json_style('{\n    "a": 1\n}\n') == write.JsonStyle(4, True, True)
    assert detect_json_style('{\n\t"a": "é"\n}') == write.JsonStyle("\t", False, False)
    assert detect_json_style("") == write.JsonStyle(2, False, True)


def test_a_foreign_writer_landing_mid_edit_is_retried(home: Path, runtime: Path):
    """The lock only serialises Nxt writers; the harness does not take it. The
    mutation callback runs between the two reads, which is where a real
    foreign write lands."""
    landed = []

    def mutate(doc):
        if not landed:
            landed.append(True)
            doc_now = json.loads(settings(home).read_text(encoding="utf-8"))
            doc_now["model"] = "sonnet"
            settings(home).write_text(
                json.dumps(doc_now, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        doc.setdefault("hooks", {}).setdefault("Stop", []).append({"hooks": []})
        return doc

    outcome = write.edit_json(settings(home), detection=detected(home), mutate=mutate)

    assert outcome.retries == 1
    assert json.loads(settings(home).read_text())["model"] == "sonnet"
    assert "Stop" in json.loads(settings(home).read_text())["hooks"]


def test_a_second_collision_fails_with_the_file_named(home: Path):
    def always_collide(doc):
        current = json.loads(settings(home).read_text(encoding="utf-8"))
        current.setdefault("churn", []).append(1)
        settings(home).write_text(
            json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return doc

    with pytest.raises(ConcurrentModification) as caught:
        write.edit_json(
            settings(home), detection=detected(home), mutate=always_collide
        )

    assert str(settings(home)) in str(caught.value)


def test_an_unparsable_settings_file_is_refused_not_replaced(home: Path):
    settings(home).write_text('{"hooks": ', encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        write.edit_json(settings(home), detection=detected(home), mutate=lambda d: d)

    assert "refusing to overwrite" in str(caught.value)
    assert settings(home).read_text() == '{"hooks": '


def test_the_lock_refuses_a_planted_symlink(home: Path, tmp_path: Path):
    victim = tmp_path / "victim"
    victim.write_text("precious", encoding="utf-8")
    lock = settings(home).with_name(settings(home).name + ".nxt.lock")
    lock.symlink_to(victim)

    with pytest.raises(ValueError):
        with write.locked(settings(home)):
            pass

    assert victim.read_text() == "precious"


# -------------------------------------------------------------------- ledger


def test_the_ledger_folds_to_the_current_wire_state(home: Path, runtime: Path):
    first = wire(home, runtime, event="SessionStart", arg="session_start")
    second = wire(home, runtime, event="PreToolUse", arg="before_tool")
    unwire(home, first)

    live = ledger.live_slots(COMPONENT, path=ledger_path(home))

    assert [r.slot for r in live] == [second.slot]


def observe(record: ledger.Record, home: Path) -> None:
    ledger.append(
        ledger.Record(
            op=ledger.VERIFY, component=record.component, version=record.version,
            harness=record.harness, file=record.file, slot=record.slot,
        ),
        path=ledger_path(home),
    )


def test_an_observation_changes_neither_direction_of_the_wire_state(
    home: Path, runtime: Path
):
    """`verify` writes a record too. It must not unwire a live slot by being
    the last word on it, and it must not resurrect a dead one."""
    live = wire(home, runtime, event="SessionStart")
    dead = wire(home, runtime, event="PreToolUse", arg="before_tool")
    unwire(home, dead)
    observe(live, home)
    observe(dead, home)

    assert [r.slot for r in ledger.live_slots(COMPONENT, path=ledger_path(home))] \
        == [live.slot]


def test_a_torn_line_is_reported_rather_than_silently_dropped(home: Path, runtime: Path):
    wire(home, runtime)
    with ledger_path(home).open("a", encoding="utf-8") as handle:
        handle.write('{"op": "wire", "component": "hoo')

    records, warnings = ledger.read(path=ledger_path(home))

    assert len(records) == 1
    assert any("is not JSON" in w for w in warnings)


def test_a_record_from_a_newer_nxtos_is_kept_and_flagged(home: Path):
    ledger_path(home).parent.mkdir(parents=True, exist_ok=True)
    with ledger_path(home).open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "op": "wire", "component": COMPONENT, "version": VERSION,
            "harness": "claude-code", "file": "/x", "slot": "/y",
            "enforcement": "block",
        }) + "\n")

    records, warnings = ledger.read(path=ledger_path(home))

    assert len(records) == 1
    assert any("unknown fields" in w for w in warnings)


def test_two_components_on_one_slot_are_named_together(home: Path, runtime: Path):
    record = wire(home, runtime)
    ledger.append(
        ledger.Record(
            op=ledger.WIRE, component="skills", version="0.3.0",
            harness="claude-code", file=record.file, slot="/hooks/Stop/0/hooks/0",
        ),
        path=ledger_path(home),
    )

    same = ledger.conflicting_slot(
        "skills", record.file, record.slot, path=ledger_path(home)
    )
    disjoint = ledger.conflicting_slot(
        "skills", record.file, "/hooks/Stop/0/hooks/0", path=ledger_path(home)
    )

    assert same is not None and same.component == COMPONENT
    assert disjoint is None


def test_the_ledger_answers_whether_the_harness_rewrote_its_own_file(
    home: Path, runtime: Path
):
    """Step three of the write protocol. A file NxtOS has never written cannot
    have drifted from a state NxtOS never recorded, so no record is not a
    move -- otherwise every first wire reports the harness as hostile."""
    fresh = write.foreign_digest({"a": 1}, json_like=True)
    assert ledger.foreign_moved(settings(home), fresh, path=ledger_path(home)) is False

    wire(home, runtime)
    settled = ledger.last_for_file(
        settings(home), path=ledger_path(home)
    ).foreign_sha256_after

    assert ledger.foreign_moved(
        settings(home), settled, path=ledger_path(home)
    ) is False
    assert ledger.foreign_moved(
        settings(home), fresh, path=ledger_path(home)
    ) is True


def test_a_record_whose_form_nothing_can_dispatch_on_is_refused(home: Path):
    """`verify` picks its check from the form. A slot recorded with a form the
    reader does not know is a slot it would have to guess about."""
    with pytest.raises(ValueError) as caught:
        ledger.append(
            ledger.Record(
                op=ledger.WIRE, component=COMPONENT, version=VERSION,
                harness="claude-code", file="/x", slot="/y", form="handwave",
            ),
            path=ledger_path(home),
        )

    assert "unknown projection form" in str(caught.value)
    assert not ledger_path(home).exists()


def test_the_ledger_is_family_level_not_per_component(tmp_path: Path):
    """Conflict detection needs one place. A per-component ledger is exactly
    as blind to a second writer as no ledger at all."""
    resolved = ledger.ledger_path(tmp_path, {})

    assert resolved == tmp_path / ".local" / "state" / "nxt" / "projections.jsonl"
    assert COMPONENT not in resolved.parts


# -------------------------------------------------------------------- verify


def test_verify_reports_intact_and_exits_zero(home: Path, runtime: Path):
    wire(home, runtime)

    report = verify(COMPONENT, home=home, path=ledger_path(home))

    assert [s.state for s in report.slots] == [INTACT]
    assert report.exit_code == 0


def test_verify_reports_altered_for_a_foreign_modified_stanza(home: Path, runtime: Path):
    """MANDATORY. A human edits the command and leaves the marker alone.
    Presence is not health: this must not read as intact."""
    record = wire(home, runtime)
    doc = json.loads(settings(home).read_text(encoding="utf-8"))
    entry = orphans.resolve_pointer(doc, record.slot)
    entry["command"] = entry["command"].replace("session_start", "prompt_submit")
    settings(home).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    report = verify(COMPONENT, home=home, path=ledger_path(home))

    assert [s.state for s in report.slots] == [ALTERED]
    assert report.exit_code == 1


def test_verify_reports_missing_when_the_harness_deleted_the_stanza(
    home: Path, runtime: Path
):
    record = wire(home, runtime)
    doc = json.loads(settings(home).read_text(encoding="utf-8"))
    orphans.prune(doc, record.slot)
    settings(home).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    report = verify(COMPONENT, home=home, path=ledger_path(home))

    assert [s.state for s in report.slots] == [MISSING]
    assert report.exit_code == 1


def test_verify_reports_altered_when_only_the_marker_was_stripped(
    home: Path, runtime: Path
):
    """Three states, not two: the stanza is still running, so it is not
    missing, and it is no longer ours to prove, so it is not intact."""
    record = wire(home, runtime)
    doc = json.loads(settings(home).read_text(encoding="utf-8"))
    entry = orphans.resolve_pointer(doc, record.slot)
    entry["command"] = command_payload(entry["command"])
    settings(home).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    report = verify(COMPONENT, home=home, path=ledger_path(home))

    assert report.slots[0].state == ALTERED
    assert "marker is gone" in report.slots[0].detail


def test_a_harness_reformat_alone_does_not_report_a_foreign_change(
    home: Path, runtime: Path
):
    """Claude Code reorders top-level keys and normalises whitespace when it
    saves. A raw-byte remainder hash flips on that, so `foreign_changed` would
    be true after almost every harness save and would stop meaning anything.
    The digest is over a canonical form, so only content counts."""
    wire(home, runtime)
    doc = json.loads(settings(home).read_text(encoding="utf-8"))
    reordered = {key: doc[key] for key in reversed(list(doc))}
    settings(home).write_text(
        json.dumps(reordered, indent=4, ensure_ascii=False), encoding="utf-8"
    )

    report = verify(COMPONENT, home=home, path=ledger_path(home))

    assert report.foreign_changed == {str(settings(home)): False}
    assert report.slots[0].state == INTACT


def test_a_harness_editing_its_own_content_does_report_a_foreign_change(
    home: Path, runtime: Path
):
    wire(home, runtime)
    doc = json.loads(settings(home).read_text(encoding="utf-8"))
    doc["permissions"]["allow"].append("Bash(git:*)")
    settings(home).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    report = verify(COMPONENT, home=home, path=ledger_path(home))

    assert report.foreign_changed == {str(settings(home)): True}
    assert report.slots[0].state == INTACT


def test_a_ledger_only_slot_in_a_toml_file_still_gets_all_three_states(
    tmp_path: Path
):
    """Codex's `mcp_servers` and Hermes' `config.yaml` have no verified inert
    slot, so the ledger is all there is. That must not cost them the tri-state:
    a slot nothing can answer for is a slot that reads healthy by default."""
    root = tmp_path / "home"
    config = root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.nxt]\ncommand = "echo"\n', encoding="utf-8")
    value = {"command": "echo"}
    ledger.append(
        ledger.Record(
            op=ledger.WIRE, component="mcp", version=VERSION, harness="codex",
            file=str(config), slot="/mcp_servers/nxt",
            form=envelope.FORM_LEDGER_ONLY,
            own_sha256=digest(write.canonical_json(value)),
        ),
        path=ledger_path(root),
    )

    assert verify("mcp", home=root, path=ledger_path(root)).slots[0].state == INTACT

    config.write_text('[mcp_servers.nxt]\ncommand = "cat"\n', encoding="utf-8")
    assert verify("mcp", home=root, path=ledger_path(root)).slots[0].state == ALTERED

    config.write_text("model = 'gpt-5'\n", encoding="utf-8")
    assert verify("mcp", home=root, path=ledger_path(root)).slots[0].state == MISSING


def test_verify_reports_a_projection_that_outlived_its_runtime(
    home: Path, runtime: Path
):
    wire(home, runtime)
    runtime.unlink()

    report = verify(COMPONENT, home=home, path=ledger_path(home))

    assert report.slots[0].runtime_present is False
    assert report.slots[0].state == INTACT


def test_an_unreadable_ledger_is_operational_not_drift(home: Path):
    ledger_path(home).parent.mkdir(parents=True, exist_ok=True)
    ledger_path(home).write_text("not json at all\n", encoding="utf-8")

    report = verify(COMPONENT, home=home, path=ledger_path(home))

    assert report.exit_code == 2


# ------------------------------------------------------------------- orphans


def test_unwire_orphans_strips_a_stanza_with_no_component_and_no_ledger(
    home: Path, runtime: Path
):
    """MANDATORY. The case that matters is the one where the component is
    already gone: nothing can be imported, and the ledger may have gone with
    it. The grammar in the file is all there is."""
    before = settings(home).read_bytes()
    wire(home, runtime)
    runtime.unlink()
    ledger_path(home).unlink()

    removed = orphans.strip_orphans(home=home, env={}, path_lookup=no_binaries)

    assert [o.removed for o in removed] == [True]
    assert settings(home).read_bytes() == before


def test_a_live_projection_is_not_swept(home: Path, runtime: Path):
    wire(home, runtime)
    marked = settings(home).read_bytes()

    assert orphans.find_orphans(home=home, env={}) == ()
    assert orphans.strip_orphans(home=home, env={}, path_lookup=no_binaries) == ()
    assert settings(home).read_bytes() == marked


def test_a_foreign_stanza_using_the_same_idiom_is_never_swept(
    home: Path, runtime: Path
):
    """The NLM hook on this machine writes `NLM_HOOK_MODE=live '<gone>' ...`
    with the same env-assignment idiom. Sweeping by 'looks like a prefix'
    would delete somebody else's hook."""
    wire(home, runtime)
    runtime.unlink()

    orphans.strip_orphans(home=home, env={}, path_lookup=no_binaries)
    doc = json.loads(settings(home).read_text(encoding="utf-8"))
    commands = [c for _, c in orphans.walk_commands(doc)]

    assert any("NLM_HOOK_MODE=live" in c for c in commands)


def test_the_legacy_marker_is_swept_too(home: Path, tmp_path: Path):
    """`unwire` recognises `NXTHOOKS_MANAGED=1` through 0.4, or an upgrade
    strands every stanza the old writer left."""
    doc = json.loads(settings(home).read_text(encoding="utf-8"))
    doc["hooks"]["SessionStart"].append({"hooks": [{
        "type": "command",
        "command": f'{LEGACY_ENV_PREFIX} sh "{tmp_path}/gone/bin/cc-dispatch.sh" x',
    }]})
    settings(home).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    found = orphans.find_orphans(home=home, env={})
    orphans.strip_orphans(home=home, env={}, path_lookup=no_binaries)

    assert [f.legacy for f in found] == [True]
    assert "NXTHOOKS_MANAGED" not in settings(home).read_text(encoding="utf-8")


def pi_extension(root: Path, runtime: Path) -> Path:
    """An owned directory in pi's extension root, marked the NxtSkills way."""
    (root / ".pi").mkdir(parents=True, exist_ok=True)
    (root / ".pi" / "settings.json").write_text("{}", encoding="utf-8")
    extension = root / ".pi" / "agent" / "extensions" / "nxthooks"
    extension.mkdir(parents=True)
    (extension / "index.js").write_text("//\n", encoding="utf-8")
    marker = envelope.build(COMPONENT, VERSION, "src")
    (extension / envelope.MARKER_FILE_NAME).write_text(
        envelope.marker_file_body(marker, digest("src"), runtime=str(runtime)),
        encoding="utf-8",
    )
    return extension


def test_an_owned_directory_whose_runtime_is_gone_is_removed(
    tmp_path: Path, runtime: Path
):
    """Without the runtime line in the marker file, the only path an owned
    directory names is itself, which exists by definition -- so a pi extension
    whose dispatcher was deleted could never be seen as an orphan at all."""
    root = tmp_path / "home"
    extension = pi_extension(root, runtime)
    assert orphans.find_orphans(home=root, env={}) == ()

    runtime.unlink()
    removed = orphans.strip_orphans(home=root, env={}, path_lookup=no_binaries)

    assert [o.removed for o in removed] == [True]
    assert not extension.exists()
    assert extension.parent.exists()


def test_an_owned_file_is_swept_by_its_first_line_marker(
    tmp_path: Path, runtime: Path
):
    root = tmp_path / "home"
    plugins = root / ".config" / "opencode" / "plugins"
    plugins.mkdir(parents=True)
    (root / ".config" / "opencode" / "opencode.jsonc").write_text(
        "// comments\n{}\n", encoding="utf-8"
    )
    body = f'\nexport default () => run("{runtime}");\n'
    marker = envelope.build(COMPONENT, VERSION, body)
    plugin = plugins / "nxthooks.ts"
    plugin.write_text(
        envelope.comment_line(marker, ".ts") + body, encoding="utf-8"
    )
    assert orphans.find_orphans(home=root, env={}) == ()

    runtime.unlink()
    orphans.strip_orphans(home=root, env={}, path_lookup=no_binaries)

    assert not plugin.exists()
    assert (root / ".config" / "opencode" / "opencode.jsonc").exists()


def test_the_ledger_rebuilds_from_the_markers_in_the_files(home: Path, runtime: Path):
    """Recoverable rather than sacred: a deleted ledger is degraded, not lost."""
    record = wire(home, runtime)
    ledger_path(home).unlink()

    recovered = ledger.rebuild(home=home, env={}, path=ledger_path(home))

    assert [r.marker for r in recovered] == [record.marker]
    assert [r.own_sha256 for r in recovered] == [record.own_sha256]
    assert ledger.live_slots(COMPONENT, path=ledger_path(home))[0].slot == record.slot


def test_a_record_stamps_the_shape_that_wrote_it(home: Path, runtime: Path):
    """N7: a projection embeds an absolute runtime path, so which shape wrote
    a slot is the difference between a stanza pointing at a venv and one
    pointing at whatever branch a checkout has out."""
    from nxthooks import _paths as paths

    assert wire(home, runtime).shape == paths.install_shape()


def test_a_rebuilt_record_claims_no_shape(home: Path, runtime: Path):
    """The marker in the file carries no shape, so a rebuild cannot know one.

    Stamping the shape running the rebuild would turn "we do not know" into a
    claim indistinguishable from knowledge, and `verify`'s
    `slot written by dev shape` finding would then be manufactured by the
    recovery rather than observed.
    """
    wire(home, runtime)
    ledger_path(home).unlink()
    recovered = ledger.rebuild(home=home, env={}, path=ledger_path(home))
    assert [r.shape for r in recovered] == [""]


# ----------------------------------------------------------------- the shape


def test_the_library_imports_no_component():
    """NxtHooks owns this library now, so the rule runs the other way: it may
    not reach up into NxtOS or a sibling component. That is what lets NxtHooks
    ship without them."""
    import ast

    package = Path(__file__).resolve().parent.parent / "nxthooks" / "projection"
    forbidden = {"nxtoskit", "nxtskills", "nxtmcp", "nxtwiki"}
    for module in sorted(package.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden, f"{module.name}: {name}"


def test_no_test_here_can_reach_the_operators_own_config(home: Path, runtime: Path):
    """Every fixture is home-relative and every call passes `home=`. A default
    that fell through to `Path.home()` would write into the machine running
    the suite."""
    wire(home, runtime)

    assert ledger_path(home).is_relative_to(home)
    for spec_id in ("claude-code", "codex", "pi", "opencode", "hermes"):
        result = detect(spec_id, home=home, env={}, path_lookup=no_binaries)
        assert result.root.is_relative_to(home)
