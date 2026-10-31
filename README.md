# NxtHooks

One hook directory, five agent harnesses.

Write a hook once as a script that reads a JSON envelope on stdin. NxtHooks
runs it on Claude Code, Codex, OpenCode, pi and Hermes, translating each
harness's own event names and verdict shapes to and from one contract:

    exit 0   allow   (stdout `CTX:` lines become context injected into the turn)
    exit 2   block   (stderr is the reason the model is shown)

```console
$ pipx install nxthooks
$ nxthooks install
$ nxthooks list
NAME           TIER     EVENTS       MATCHERS                            POLICY
nxtlint        builtin  after_tool   Write,Edit,MultiEdit,NotebookEdit   advise
secret-scan    builtin  before_tool  Write,Edit,MultiEdit,NotebookEdit   enforce
snapshot-save  builtin  before_tool  Write,Edit,MultiEdit,NotebookEdit   advise
```

Three hooks ship with it: a Python linter that comments on files an agent just
wrote, a secret scanner that refuses a write carrying a credential, and an
undo snapshot of the working tree taken before each write. The package is
standard library only; Python 3.11 or newer.

## Events

`session_start`, `subagent_start`, `prompt_submit`, `before_tool`,
`after_tool`, `stop`, `session_end` -- canonical names, mapped per harness.
Not every harness provides every one; `nxthooks verify` prints the capability
matrix for the machine it runs on rather than a general claim.

## Layout

```
~/.config/nxt/hooks/
  config.toml            canonical policy/scope/harness config
  hooks.d/*.toml         THE DIRECTORY — one manifest per hook
~/.local/share/nxt/hooks/
  bin/cc-dispatch.sh     Claude Code transport (dumb pipe)
  bin/codex-dispatch.sh  Codex transport (dumb pipe)
  lib/                   installed runtime copy of this package
```

Manifests point at hook sources anywhere on disk (usually outside working
repos — a branch switch must never be able to delete an active guard).

## Writing a hook

```toml
# ~/.config/nxt/hooks/hooks.d/my-hook.toml
[[hook]]
name    = "my-hook"
events  = ["before_tool"]            # session_start | subagent_start | prompt_submit |
matchers = ["Bash"]                  #   before_tool | after_tool
source  = "~/hooks-src/my-hook.sh"   # configurable source location
policy  = "enforce"                  # enforce | advise | off
fail    = "closed"                   # open | closed (on crash/timeout)
timeout_ms = 5000
pi_preflight = true                  # before_tool only: block condition is
                                      #   knowable with no tool/args yet, so
                                      #   pi runs it at turn start instead
                                      #   of degrading to a post-hoc warning

[hook.harnesses.pi]
enabled = false                      # opt any harness out
```

Hook source contract (harness-agnostic):

| Signal | Meaning |
|---|---|
| stdin | normalized envelope JSON (`harness`, `event`, `cwd`, `tool`/`tool_name`, `args`/`tool_input`, `prompt`, …) |
| env | `NXTHOOKS_HARNESS`, `NXTHOOKS_EVENT`, `NXTHOOKS_TOOL`, `NXTHOOKS_CWD` |
| exit 0 | allow; stdout lines starting `CTX:` become context injections |
| exit 2 | block; stderr is the reason shown to the model |
| other | manifest fail mode applies (`open` → allow + note, `closed` → block) |

Claude Code-native scripts (emit their own `hookSpecificOutput`, read
`tool_name`) work **unmodified** — the envelope carries both vocabularies and
native payloads pass through verbatim.

Advisory context is stable-deduplicated and capped at 4,000 characters by
default (`NXTHOOKS_CONTEXT_MAX_CHARS`, minimum 256). The cap is a deterministic
character bound, not a tokenizer-specific token budget. Long advisory blocks
may retain only a prefix plus an omission marker; enforcement block reasons
bypass this cap and are never truncated by context projection.

## Tiers

`builtin < library < user`, with one deliberate inversion: a user manifest
overrides a builtin, but **cannot** replace a library hook. A library hook is
administered software and `hooks.d/` is an ordinary writable directory, so a
security hook whose implementation can be swapped by dropping a file beside
it would not be a security hook. Collisions are reported, never silently
resolved.

## Capability honesty

Effective enforcement is what each harness can actually do. The matrix is
printed by `verify`; missing blocking points are labeled unavailable and use
the explicit manual fallback rather than silently pretending.

<!-- Generated from `ROWS` in nxthooks/capability.py, which `verify` also renders
     as the ASCII matrix. Regenerate, never hand-edit:
     python3 -c "from nxthooks.capability import MARKDOWN_TABLE; print(MARKDOWN_TABLE)" -->

| Hook class | Claude Code | Codex | pi | OpenCode | Hermes | Muse | Pool |
|---|---|---|---|---|---|---|---|
| `session_start` inject | inject | inject | inject | no event (declared gap) | inject | inject | unsupported; CLI |
| `subagent_start` inject | inject | no event (declared gap)****** | no event (declared gap)****** | no event (declared gap)****** | no event (declared gap)****** | no event (declared gap)****** | unsupported; CLI |
| `prompt_submit` inject | inject | inject | inject | inject (advise-only) | inject | inject | unsupported; CLI |
| `before_tool` block | block | block* | block** / preflight*** | block (throw) | block | block | unsupported; CLI |
| `after_tool` observe | observe | observe | observe | observe | observe | unverified | unsupported; CLI |
| `stop` / `session_end` | session_end | wired**** | session_end***** | session_end***** | session_end***** | unverified | unsupported; CLI |

\* Codex covers Bash, `apply_patch`, MCP, and most local function tools, but
hosted and specialized tool paths can bypass the default hook path. It is a
guardrail, not a complete security boundary.

\*\* pi blocks for real through `tool_call`, which fires before execution and
returns `{ block, reason }`. This path was verified live against a refusing
hook; `tool_execution_end` remains the `after_tool` observer.

\*\*\* A `before_tool` hook marked `pi_preflight` can run at turn start when
its block condition needs no specific tool/args; pi then turns a block verdict
into an interactive stay-or-switch confirmation. Hooks that need real args,
such as `secret-scan`, use the real `tool_call` gate.

\*\*\*\* Codex maps `session_end` to `SessionEnd`, which is advisory and may be
delayed until the thread actually closes or becomes idle.

\*\*\*\*\* `session_end` is the canonical event; these harnesses reach it under
their own names: pi from `session_shutdown`, OpenCode from the
`session.deleted` event, Hermes from `on_session_end`. All three are observers,
so no verdict they return can stop a session from closing.

\*\*\*\*\*\* Only Claude Code starts subagents inside the session and offers a hook
for it (`SubagentStart`). Every other harness's subagents start as a fresh
session (Codex `exec`, pi and OpenCode subagents launched by nxtagents, Hermes
delegate), so `session_start` already covers them. Muse declares only
SessionStart, UserPromptSubmit and PreToolUse.

Where a harness lacks an effective pre-tool guard, run `nxtgit check` before
the first edit and each mutation point, and `nxtgit guard` after route advice.
Workspace instructions remain advisory on those harnesses.

## Lifecycle

```sh
nxthooks install [--keep-newer]   # probe harnesses, install (or repair) runtime, wire, verify
nxthooks verify      # re-check every path; print matrix; non-zero on silent gaps
nxthooks doctor      # verify + synthetic dispatch self-tests
nxthooks list        # show the directory
nxthooks uninstall [--keep-runtime] [--purge-config]
```

Two more commands install beside it. `nxtlint --staged` gates the git-staged
Python files the way the `nxtlint` hook advises on them, and `nxthooks-pack`
is the NxtPack configuration provider a host drives over stdin — the one
`nxtpack.json` names as its provider.

`install` always replaces the runtime, so it doubles as the repair path.
`--keep-newer` keeps one whose `VERSION` is newer than the package, so a host
that bundles an older NxtHooks never downgrades a standalone install. The
runtime is staged beside the old one and switched with one atomic rename, so
a hook firing mid-install sees the old runtime or the new one, never half of
each.

**Presence, never config.** `install` wires a harness when
`nxthooks.projection.detect` finds it: its binary on PATH, or its config root
holding one of *its own* files. A directory is never enough, because
`~/.pi/agent/extensions` and `~/.config/opencode/plugins` are directories
NxtHooks creates. The `[harnesses]` block in `config.toml` records what the
last install detected; it decides nothing. `--harnesses <name>` overrides,
wires, and says so: it is the operator asserting a harness is there, and the
report labels it `not detected` rather than pretending otherwise.

**Wiring rules.** Claude Code and Codex get stanzas in
`~/.claude/settings.json` and `~/.codex/hooks.json` whose command strings
carry a marker, `NXT=hooks/<version>/<sha12> sh "<root>/bin/..."` — the one
inert slot both harnesses were measured to leave alone. Codex requires
`/hooks` review/trust after a new or changed definition. pi, OpenCode and
Hermes get transports NxtHooks owns outright, stamped with a `.nxt-managed`
marker file (directories) or a first-line `// nxt:` comment (the OpenCode
plugin). Unmanaged hooks are never touched, and re-running `install` always
converges to exactly the current stanza set.

**Ownership is a ledger slot with a hash**, not "starts with the marker":
`~/.local/state/nxt/projections.jsonl`, family-level and append-only, one
record per wire and unwire. `verify` reports each slot `intact`, `altered` or
`missing`, plus whether the harness has rewritten its own file since NxtHooks
last wrote it. The roots a previous install wired come out of the ledger's
`runtime` field, which is what a root migration or a partial harness install
needs in order to remove prior stanzas without matching another tool's
generic marker. An older `~/.config/nxt/hooks/installed-roots` is still read;
nothing writes one.

**Uninstall is complete.** Every ledger slot is unwired, every owned
directory and marker file removed, the runtime root deleted, the config kept
unless `--purge-config`, and every directory `install` had to create removed
if it is still empty. What NxtHooks did not create is never a candidate:
`~/.config/nxt/hooks/created-paths` is the record, and emptiness is the
guard.

**The transports are fail-quiet.** A stanza can outlive its runtime. Each
dispatcher checks the runner exists, prints one line to stderr at most once an
hour naming `nxthooks doctor`, and exits 0 rather than turning every turn into a
hook error. The pi, OpenCode and Hermes adapters still fail open — refusing
every turn because a dispatcher moved would be worse than the gap — but they
say so once per session in the surface the hooks themselves use, instead of
resolving to a silent allow.

**Seeded manifests: none.** A component may seed a hook whose implementation
it ships. NxtHooks ships none, so `hooks.d/` arrives empty. Seeding a manifest
whose `source` is not on the machine produces a hook that can never run, a
`verify` gap its own printed advice cannot repair, and — until the dispatcher
stopped counting them — a `doctor` reporting the guard as having run. A hook
whose source is absent is now reported separately from the hooks that ran.

## Contract for hosts

A program that bundles or drives NxtHooks checks what it found with
`nxthooks --contract`, which prints one JSON object:

```console
$ nxthooks --contract
{"name": "nxthooks", "version": "0.2.0", "contract": "1.0", "harnesses": [...], "events": [...]}
```

`contract` is the version of what a host may rely on, not of the release: the
major changes only when a host that worked would stop working (a verb removed,
a flag renamed, the shape of this output), the minor when something is added.
It lives in `nxthooks/component.toml` as `contractVersion`. A copy that does
not understand `--contract` predates the contract.

## Status

Beta. What has actually been observed, rather than what the design intends:

| Harness | Dispatch observed | Blocking verified |
|---|---|---|
| Claude Code | all tool events, live | live |
| Codex | all tool events, live | live |
| OpenCode | all tool events, live | live |
| pi | all tool events, live | live |
| Hermes | `session_start`, `prompt_submit` only | through its own dispatcher, not a live turn |

The Hermes caveat is worth stating plainly. Its transport was driven through
`hermes_cli.plugins._dispatch_pre_tool_call_hooks` on hermes-agent 0.21.1,
which returned the block message, and its `session_start` and `prompt_submit`
hooks were seen firing in a real session. But no Hermes turn has yet reached
a tool call with these hooks installed, so `before_tool` and `after_tool`
there are verified one layer below the harness rather than end to end. Treat
Hermes tool hooks as working-by-construction until someone reports otherwise.

`nxthooks verify` prints the capability matrix for the machine it runs on,
which is the claim that matters to you rather than any of the above.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e . pytest ruff build
.venv/bin/pytest -q
```

`ruff` is optional at runtime; NxtLint is silent without it. Installing it is
what makes the lint hook's tests run rather than skip.

## Licence

Apache-2.0. See LICENSE and NOTICE.
