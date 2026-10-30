"""Is this harness actually here, and what has it been verified to tolerate.

Two questions, one table, because they have the same failure mode: answering
from a plan rather than from the machine. NxtHooks' `cmd_install` wires every
harness its config enables, and `copytree` creates missing parents, so a fresh
install on a box that has only Claude Code manufactures a pi extension
directory, an OpenCode plugin file and a Hermes plugin tree -- and then
`verify` reports all three fine, because it is declared, never sniffed.

`detect` is the probe the harness itself would agree with: its binary on PATH,
or its config root present *and holding one of the harness's own files*. Not
merely a directory, which NxtOS may have created itself two minutes ago.

`Detection` is also the token `write.py` demands before it will touch a file.
That is deliberate: "detect before any write" enforced by convention is
enforced by nobody. A caller cannot fabricate one by accident, and one for the
wrong harness or a stale one is refused.

**What each harness was verified to do with an unknown key, on this machine.**
The uniformity plan lists this as prescribed-but-unverified and forbids the
`_nxt` sibling key until a harness is recorded as tolerating one. Two harnesses
were driven through a real write in a sandbox HOME and both came back hostile:

- Claude Code 2.1.263 (`claude auto-mode reset -y`, `CLAUDE_CONFIG_DIR` at a
  sandbox): unknown *top-level* keys and non-ASCII survive; an unknown key
  *inside a hook entry object* is silently deleted. The harness reserialises
  the whole file through its own typed model, and reorders top-level keys.
  Its writer emits exactly `json.dumps(doc, indent=2, ensure_ascii=False)`
  plus a trailing newline -- which is also why `write.py` must not use
  Python's default `ensure_ascii=True`, or every wire mangles a stranger's
  accented text into escapes.
- Codex 0.153.4 (`codex mcp add`, `CODEX_HOME` at a sandbox): an unrelated
  unknown TOML table survives, and so does a top-of-file comment; an unknown
  key inside `[mcp_servers.<name>]` is deleted, and *a comment line attached
  to an `[mcp_servers.*]` table is deleted too*. That last one is a negative
  result against the plan's own proposal of a `# nxt:` comment above the key
  for round-trip writers: for this region of this file it does not survive.
  `codex mcp list` read all of it without complaint, so the hostility is in
  the writer, not the reader.
- OpenCode 1.18.25 read a config carrying comments, an unknown top-level key
  and an unknown key inside an `mcp` entry without complaint. Its write path
  (`opencode mcp add`) is interactive and was not driven, so its writer is
  unverified. Note the config on this machine is `opencode.jsonc`, not the
  `opencode.json` the plan names, and it does not parse as strict JSON: 21
  comment lines. A JSON round-trip writer would delete all of them.
- Hermes 0.20.6 loads `config.yaml` with `yaml.safe_load` into plain dicts and
  reads keys with `.get`; no schema rejects extras, and its own home probe is
  `("config.yaml", ".env", "state.db")` -- which is where this table's Hermes
  sentinels come from. Read from the installed source, not driven.
- pi: not probed. Its NxtHooks surface is a directory NxtOS owns outright, so
  the marker-file form applies and the key-tolerance question does not arise
  for it. Its `settings.json` writer is unverified.

`ledger_only` in the table below is the consequence: a harness stays
ledger-only for name-keyed JSON and for foreign TOML/YAML until its *writer*
has been driven and recorded here. Nothing in this repository may set it from
a reading of documentation.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .envelope import (
    FORM_COMMENT_LINE,
    FORM_ENV_PREFIX,
    FORM_LEDGER_ONLY,
    FORM_MARKER_FILE,
)


class UnknownHarness(KeyError):
    """A harness id outside the adapter table."""


@dataclass(frozen=True)
class Harness:
    """One harness NxtOS may project into.

    `sentinels` are home-relative and are files the harness itself writes.
    A *directory* must never appear here: `~/.pi/agent/extensions` and
    `~/.config/opencode/plugins` are directories NxtHooks creates itself with
    `copytree`, so a sentinel on one would let NxtOS detect the harness it
    just manufactured. `root` is
    home-relative too; `home_env` names an environment variable that relocates
    the whole root, which is how a sandbox test and a packaged install become
    the same code path rather than two.
    """

    id: str
    binaries: tuple[str, ...]
    root: str
    sentinels: tuple[str, ...]
    home_env: str = ""
    #: form per projection surface, keyed by a short surface name
    forms: dict[str, str] = field(default_factory=dict)
    #: Home-relative globs of the files this harness loads that can carry a
    #: marker. Harness knowledge, not component knowledge: these are where the
    #: harness reads from, which is why `orphans.py` can sweep them without
    #: importing anything that wrote to them.
    marker_globs: tuple[str, ...] = ()
    #: exact harness build the notes above were taken against, or "" for none
    verified_against: str = ""
    #: True only where a *write* by the harness has been observed preserving
    #: an unknown key in the region NxtOS writes into. Nothing sets this yet.
    tolerates_unknown_keys: bool = False
    notes: str = ""

    def root_path(self, home: Path, env: dict | None = None) -> Path:
        environ = os.environ if env is None else env
        override = (environ.get(self.home_env) or "").strip() if self.home_env else ""
        if override:
            return Path(override).expanduser()
        return home / self.root

    def sentinel_paths(self, home: Path, env: dict | None = None) -> tuple[Path, ...]:
        root = self.root_path(home, env)
        base = home / self.root
        out = []
        for sentinel in self.sentinels:
            candidate = home / sentinel
            try:
                out.append(root / candidate.relative_to(base))
            except ValueError:
                # A sentinel outside the root, e.g. `~/.claude.json`. It does
                # not move with an override of the root.
                out.append(candidate)
        return tuple(out)


HARNESSES: dict[str, Harness] = {
    "claude-code": Harness(
        id="claude-code",
        binaries=("claude",),
        root=".claude",
        sentinels=(".claude/settings.json", ".claude.json"),
        home_env="CLAUDE_CONFIG_DIR",
        forms={"hooks": FORM_ENV_PREFIX, "mcp": FORM_LEDGER_ONLY},
        marker_globs=(".claude/settings.json",),
        verified_against="claude-code 2.1.263",
        tolerates_unknown_keys=False,
        notes=(
            "deletes an unknown key inside a hook entry; preserves an "
            "env-assignment prefix on the command string"
        ),
    ),
    "codex": Harness(
        id="codex",
        binaries=("codex",),
        root=".codex",
        sentinels=(".codex/config.toml", ".codex/hooks.json", ".codex/auth.json"),
        home_env="CODEX_HOME",
        forms={"hooks": FORM_ENV_PREFIX, "mcp": FORM_LEDGER_ONLY},
        marker_globs=(".codex/hooks.json",),
        verified_against="codex-cli 0.153.4",
        tolerates_unknown_keys=False,
        notes=(
            "deletes an unknown key inside [mcp_servers.*] and deletes a "
            "comment line attached to that table; preserves an unrelated "
            "unknown table and the top-of-file comment"
        ),
    ),
    "pi": Harness(
        id="pi",
        binaries=("pi",),
        root=".pi",
        sentinels=(".pi/settings.json",),
        forms={"extension": FORM_MARKER_FILE, "mcp": FORM_LEDGER_ONLY},
        marker_globs=(".pi/agent/extensions/*/.nxt-managed",),
        verified_against="",
        notes="writer not probed; extension directory is owned outright",
    ),
    "opencode": Harness(
        id="opencode",
        binaries=("opencode",),
        root=".config/opencode",
        sentinels=(
            ".config/opencode/opencode.json",
            ".config/opencode/opencode.jsonc",
        ),
        forms={"plugin": FORM_COMMENT_LINE, "mcp": FORM_LEDGER_ONLY},
        marker_globs=(".config/opencode/plugins/*.ts",),
        verified_against="opencode 1.18.25",
        notes=(
            "config on this machine is opencode.jsonc and is not strict JSON; "
            "a JSON round-trip writer would delete its comments. Reader "
            "tolerant; writer interactive and not driven"
        ),
    ),
    "hermes": Harness(
        id="hermes",
        binaries=("hermes",),
        root=".hermes",
        sentinels=(".hermes/config.yaml", ".hermes/.env", ".hermes/state.db"),
        home_env="HERMES_HOME",
        forms={"plugin": FORM_MARKER_FILE, "mcp": FORM_LEDGER_ONLY},
        marker_globs=(".hermes/plugins/*/.nxt-managed",),
        verified_against="hermes-agent 0.20.6",
        notes=(
            "sentinels are the harness's own home markers; config.yaml is "
            "read as a plain dict, so extras are ignored rather than rejected"
        ),
    ),
}


@dataclass(frozen=True)
class Detection:
    """The answer to 'is this harness here', and the token `write.py` wants.

    `paths` is every sentinel that actually existed, so a report can say what
    convinced it rather than only that something did.
    """

    harness: str
    present: bool
    reason: str
    root: Path
    paths: tuple[Path, ...] = ()
    on_path: str = ""

    def owns(self, path: Path) -> bool:
        """Is `path` inside what this detection actually looked at?

        A detection for Claude Code does not license a write to Codex's
        config, and this is what stops one caller's `Detection` being passed
        to the wrong `edit_json` and quietly authorising it.
        """
        target = Path(path).expanduser()
        if target in self.paths:
            return True
        try:
            target.relative_to(self.root)
        except ValueError:
            return False
        return True


def harness(harness_id: str) -> Harness:
    try:
        return HARNESSES[harness_id]
    except KeyError:
        raise UnknownHarness(
            f"{harness_id!r} is not a known harness; known ids are "
            f"{', '.join(sorted(HARNESSES))}"
        ) from None


def detect(
    harness_id: str,
    *,
    home: Path | str | None = None,
    env: dict | None = None,
    path_lookup=None,
) -> Detection:
    """Present if the binary is on PATH, or the root holds one of its files.

    `path_lookup` exists so a sandbox can answer the PATH half without the
    test machine's real PATH deciding it. Everything else about the probe is
    the filesystem.
    """
    spec = harness(harness_id)
    root = Path.home() if home is None else Path(home)
    config_root = spec.root_path(root, env)
    lookup = shutil.which if path_lookup is None else path_lookup

    found = tuple(p for p in spec.sentinel_paths(root, env) if p.exists())
    binary = ""
    for name in spec.binaries:
        resolved = lookup(name)
        if resolved:
            binary = str(resolved)
            break

    if binary:
        return Detection(
            harness=harness_id, present=True,
            reason=f"{spec.binaries[0]} on PATH at {binary}",
            root=config_root, paths=found, on_path=binary,
        )
    if found:
        names = ", ".join(p.name for p in found)
        return Detection(
            harness=harness_id, present=True,
            reason=f"config root {config_root} holds {names}",
            root=config_root, paths=found,
        )
    if config_root.is_dir():
        return Detection(
            harness=harness_id, present=False,
            reason=(
                f"config root {config_root} exists but holds none of the "
                f"harness's own files; a directory alone is not a harness"
            ),
            root=config_root,
        )
    return Detection(
        harness=harness_id, present=False,
        reason=f"no binary on PATH and no config root at {config_root}",
        root=config_root,
    )

