"""The table is generated now, but the claims in its cells are still written by
hand. A cell that claims an enforcement nobody installed is the exact shape that
shipped a dead title bar in 0.3.13: correct-looking everywhere except where it
runs."""
from pathlib import Path

from nxthooks.capability import CAPABILITY_MATRIX, CELLS, MARKDOWN_TABLE
from nxthooks.lifecycle import _CODEX_PLAN, _cc_plan

HARNESSES = ("claude-code", "codex", "pi", "opencode", "hermes")
NXTHOOKS = Path(__file__).resolve().parents[1] / "nxthooks"
README = NXTHOOKS.parent / "README.md"
ADAPTERS = NXTHOOKS / "adapters"
CLOSE_ROW = "stop / session_end"
NATIVE_CLOSE_EVENT = {
    "pi": ('pi.on("session_shutdown"', ADAPTERS / "pi-extension" / "index.ts"),
    "opencode": ('"session.deleted"', ADAPTERS / "opencode-plugin" / "nxthooks.ts"),
    "hermes": ('register_hook("on_session_end"', ADAPTERS / "hermes-plugin" / "__init__.py"),
}
ALL_CC_GROUPS = {
    "session_start": [None],
    "prompt_submit": [None],
    "before_tool": [None],
    "session_end": [None],
}


def test_the_claude_code_cell_matches_what_cc_plan_installs():
    _, plan = _cc_plan(Path("/tmp"), ALL_CC_GROUPS)
    installed = {event for _, _, event, _ in plan}
    cell = CELLS[CLOSE_ROW][0]
    if "not adapted" in cell:
        assert "session_end" not in installed and "stop" not in installed
    else:
        assert "session_end" in installed


def test_the_codex_cell_matches_what_codex_plan_installs():
    installed = {event for _, event, _, _ in _CODEX_PLAN}
    cell = CELLS[CLOSE_ROW][1]
    assert ("not adapted" in cell) == ("session_end" not in installed)


def test_claude_code_installs_session_end():
    _, plan = _cc_plan(Path("/tmp"), ALL_CC_GROUPS)
    assert "session_end" in {event for _, _, event, _ in plan}


def test_the_matrix_no_longer_calls_claude_code_unadapted():
    assert "not adapted" not in CELLS[CLOSE_ROW][0]


def test_readme_carries_the_generated_table_verbatim():
    """README used to hold a second hand-written copy that nothing read, so it
    stayed on 'not adapted' for four harnesses whose adapters had already
    shipped. Every cell is guarded now, not just the close row."""
    assert MARKDOWN_TABLE in README.read_text(encoding="utf-8"), (
        "README's table has drifted from nxthooks/capability.py. Replace it with:"
        f"\n\n{MARKDOWN_TABLE}\n"
    )


def test_every_footnote_marker_in_a_cell_has_prose_in_both_copies():
    """The markers are shared because they live in the cells; the prose behind
    them is written twice on purpose. A marker pointing at a footnote only one
    copy defines is the drift the shared cells cannot catch."""
    readme = README.read_text(encoding="utf-8")
    markers = {"*" * (len(cell) - len(cell.rstrip("*")))
               for cells in CELLS.values() for cell in cells}
    for marker in markers - {""}:
        assert f"\n  {marker} " in CAPABILITY_MATRIX, marker
        assert "\n" + "\\*" * len(marker) + " " in readme, marker


def test_every_harness_claimed_adapted_registers_a_close_event():
    cells = dict(zip(HARNESSES, CELLS[CLOSE_ROW]))
    for harness, (needle, adapter) in NATIVE_CLOSE_EVENT.items():
        claimed = "not adapted" not in cells[harness]
        assert claimed == (needle in adapter.read_text(encoding="utf-8")), harness
