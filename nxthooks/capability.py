"""One capability table, rendered twice.

`verify` prints an ASCII matrix; README.md carries a Markdown one. Both were
hand-written, and nothing read the README copy -- so it was the staler of the
two, still calling four harnesses "not adapted" days after their adapters had
shipped. Both renderings are projections of `ROWS` now, which retires that
drift class rather than testing for it.

Footnote markers live in the cells and are therefore shared. The footnote
*prose* is not: README's is long-form documentation and the docstring's is
terminal-terse, and forcing one into both would cost more than the drift.
"""
import textwrap

from . import CANONICAL_EVENTS, HARNESSES

MARKDOWN_HARNESSES = ("Claude Code", "Codex", "pi", "OpenCode", "Hermes", "Muse", "Pool")

#: Muse's settings.json hooks declare only SessionStart/UserPromptSubmit/
#: PreToolUse, and it reuses Claude Code's exact config shape and blocking
#: contract (verified against Muse Code 1.3.0; wired by hand, no installer transport), so
#: those three columns mirror Claude Code's. after_tool and stop/session_end
#: have no registered hook and no confirmed event support, so they're marked
#: unverified rather than guessed.
ROWS = (
    ("session_start inject",
     ("inject", "inject", "inject", "no event (declared gap)", "inject", "inject", "unsupported; CLI")),
    ("subagent_start inject",
     ("inject", "no event (declared gap)******", "no event (declared gap)******",
      "no event (declared gap)******", "no event (declared gap)******", "no event (declared gap)******",
      "unsupported; CLI")),
    ("prompt_submit inject",
     ("inject", "inject", "inject", "inject (advise-only)", "inject", "inject", "unsupported; CLI")),
    ("before_tool block",
     ("block", "block*", "block** / preflight***", "block (throw)", "block", "block", "unsupported; CLI")),
    ("after_tool observe",
     ("observe", "observe", "observe", "observe", "observe", "unverified", "unsupported; CLI")),
    ("stop / session_end",
     ("session_end", "wired****", "session_end*****", "session_end*****",
      "session_end*****", "unverified", "unsupported; CLI")),
)

CELLS = dict(ROWS)

#: Wide enough that no cell's longest single word has to be broken, narrow
#: enough that the whole table still fits a conventional terminal.
_CELL_WIDTH = 16


def _ascii_table() -> str:
    header = ("hook class", *HARNESSES)
    #: `break_on_hyphens` off: "(advise-only)" is one token, and splitting it
    #: across two rows of the table reads as two different verdicts.
    grid = [[[label], *(textwrap.wrap(cell, _CELL_WIDTH, break_on_hyphens=False)
                        for cell in values)]
            for label, values in ROWS]
    widths = [max(len(header[column]),
                  *(len(line) for row in grid for line in row[column]))
              for column in range(len(header))]

    lines = ["  ".join(name.ljust(width) for name, width in zip(header, widths)),
             "  ".join("-" * width for width in widths)]
    for row in grid:
        for depth in range(max(len(cell) for cell in row)):
            segments = (cell[depth] if depth < len(cell) else ""
                        for cell in row)
            lines.append("  ".join(segment.ljust(width)
                                   for segment, width in zip(segments, widths)))
    return "\n".join(line.rstrip() for line in lines)


def _backticked(label: str) -> str:
    return " ".join(f"`{word}`" if word in CANONICAL_EVENTS else word
                    for word in label.split())


def _markdown_table() -> str:
    lines = ["| Hook class | " + " | ".join(MARKDOWN_HARNESSES) + " |",
             "|" + "---|" * (len(MARKDOWN_HARNESSES) + 1)]
    for label, values in ROWS:
        lines.append("| " + " | ".join((_backticked(label), *values)) + " |")
    return "\n".join(lines)


MARKDOWN_TABLE = _markdown_table()

CAPABILITY_MATRIX = f"""\
capability matrix (effective enforcement):

{textwrap.indent(_ascii_table(), "  ")}

  * Codex PreToolUse covers Bash, apply_patch, MCP, and most local function
    tools. Hosted and specialized tool paths can bypass it, so it is a strong
    guardrail rather than a complete security boundary.
  ** pi blocks for real through `tool_call`, whose handler runs before the
    tool and returns {{block, reason}}. This was verified live against a
    refusing hook; `tool_execution_end` remains the after_tool observer.
  *** A pi_preflight hook has a block condition knowable without tool/args;
    pi dispatches it at turn start and turns a block verdict into an
    interactive stay-or-switch-style confirmation instead of a warning.
  **** Codex maps stop to Stop and session_end to SessionEnd. SessionEnd is
    advisory and can be delayed until the thread is actually closed/idle.
  ***** session_end is the canonical event; these harnesses reach it under
    their own names. pi dispatches from session_shutdown, OpenCode from the
    session.deleted event, hermes from on_session_end. All three are
    observers: no verdict they return can stop a session from closing.
  ****** Only Claude Code starts subagents inside the session and offers a
    hook for it (SubagentStart). Every other harness's subagents start as a
    fresh session (Codex exec, pi and OpenCode subagents launched by
    nxtagents, Hermes delegate), so session_start already covers them. Muse
    declares only SessionStart/UserPromptSubmit/PreToolUse.

  Manual fallback where no effective pre-tool guard exists: run `nxtgit check`
  before the first edit and at each mutation point; run `nxtgit guard` after
  route advice. AGENTS.md remains advisory, never claimed enforcement.

  hermes maps onto its own vocabulary, not matching names:
    session_start  a registered system prompt section, re-evaluated per new
                   session. NOT on_session_start, whose return is discarded --
                   injecting there would be a silent no-op.
    prompt_submit  pre_llm_call. Context merges into the current turn's user
                   message. It cannot veto the call, so a block verdict
                   degrades to an in-context warning.
    before_tool    pre_tool_call, a real pre-execution veto. Hermes also
                   offers an approve directive escalating to its human gate,
                   which no other harness has; unused for now.
    after_tool     post_tool_call, return discarded.
    session_end    on_session_end, return discarded.
  Its transport is Python loaded into the agent process, not a subprocess
  invoked with JSON on stdin, and plugins are opt-in per profile: installing
  activates nothing until the profile's config.yaml lists it in
  plugins.enabled."""
