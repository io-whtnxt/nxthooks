# Changelog

## Unreleased

- **Release contract for the NxtOS plugin pipeline (T1282).** The repo now
  carries a `[tool.nxt-release]` table, `scripts/release.sh` (a thin wrapper
  that runs the shared nxtos `scripts/plugin_release.py` with this repo as
  its parameter, replacing `release-build.sh`), and
  `tests/test_release_contract.py`. Releases publish as signed tarballs to
  `io-whtnxt/nxtos-releases`, not to PyPI (see the removed workflow).

- **The shared wrapper hardened (T2286).** `scripts/release.sh` now judges
  option tokens instead of the flattened argument string — a value containing
  `--repo`, or any word after `--`, no longer aborts a release — recognises the
  `--out=` spelling before filling in its default, and defaults `NXTOS_REPO` to
  `~/Coding/nxtos`, the MSM5 code root (the old default pointed at a retired
  machine). The argv it builds is pinned by behavioural tests in nxtos
  `tests/test_plugin_release_wrapper.py`; the sha256 pinned here is the
  byte-identity half of the same contract. `bins` must now equal the
  `[project.scripts]` keys, so a new console script cannot miss the bundle.

## 0.2.2

- **Uninstall takes back the plugin's agent documents.** NxtOS's delivery
  copies every `agents:` entry a manifest declares beside `plugin.yaml`; the
  pane's delivery removed only `plugin.yaml`, so a component that shipped an
  agent left that agent installed after the component was gone. `withdraw`
  now reads the declaration from the installed manifest and removes exactly
  those files, under the same confinement the host applied on the way in —
  `..`, absolute paths, symlinks escaping the plugin directory, and anything
  that is not a file are refused — then prunes the directories that emptied.
  A file the manifest never declared is never touched, and a declaration this
  package cannot read costs nothing but the declaration: the reason stays
  empty, because the Plugin SDK raises on a non-empty one.

## 0.2.1

- **Uninstall no longer drops foreign hook entries.** `strip_ours` treated any
  group without a `hooks` list (a Codex `{"command": ...}` entry) as emptied
  and deleted it. Only groups we actually removed a stanza from are dropped.
  The writer also keeps containers the file wrote on one line, so install then
  uninstall hands back the operator's original text. The C6
  `C6-foreign-remainder` acceptance is removed from `component.toml`.
- `nxtlint` and `nxthooks-pack` install as console scripts. NxtLint's CLI was
  reachable only as `python -m nxthooks.engines.nxtlint.cli`, and `nxtpack.json`
  names `nxthooks.pack` as its provider while no installed script of that name
  existed — the declaration is what a host runs by argv.
- The declaration is now checked against the package: its provider is an
  installed console script, every console-script target resolves to a callable,
  each operation's `field_args` name switches that operation's own subcommand
  defines — not merely switches some subcommand defines, which let an
  uninstall-only `--keep-runtime` pass as an `install` flag — and the install
  operation's harness choices are `INSTALLABLE_HARNESSES`. Ported from the
  NxtOS tree, which dropped them along with its copy of this package (9bd60614).

## 0.2.0

NxtHooks is its own project now. This repository is the source of truth; it
was previously generated from the NxtOS tree.

- **Standalone.** The package imports nothing from NxtOS and depends on the
  standard library only (PyYAML is used when present, to read a Hermes
  `config.yaml`). The modules it used to vendor under `nxthooks._vendor` are
  its own: `nxthooks.projection`, `nxthooks._paths`, and the engines under
  `nxthooks.engines` (`nxtgrep`, `nxtlint`, `snapshots`). A test imports the
  CLI, lifecycle and dispatcher with `nxtoskit` made unimportable.
- **Contract 1.0.** `nxthooks --contract` prints `name`, `version`,
  `contract`, `harnesses` and `events` as one JSON object. `contract` comes
  from `contractVersion` in `component.toml` and versions what a host may
  rely on, separately from the release number.
- **Keep-newer.** `nxthooks install` always replaces the runtime, so it is
  also the repair path. `--keep-newer` keeps a runtime whose `VERSION` is
  newer than the package, so a host bundling an older copy never downgrades
  a standalone install.
- **Atomic runtime swap.** The runtime is staged beside the live one and
  switched with a single rename. A hook firing during an install sees the old
  runtime or the new one, never a mix, and a failed switch restores the
  previous runtime.
- **Legacy path adoption.** NxtGrep and NxtLint configuration left at its
  older location is moved to the current one on first use, with a
  `.MIGRATED-TO` breadcrumb beside the old file. An existing file at the new
  location wins and the old copy is left in place. Installed hook shims from
  older layouts are recognised and upgraded; a modified one is left alone.
- **No more `_framework` copy.** The runtime no longer carries a partial copy
  of NxtOS under `lib/`; an install removes one left by an older version.
- A clean-HOME install writes nothing under `~/.nxtos`.
- `snapshot-save` ships as a third builtin hook: an undo snapshot of the
  working tree before each file write, kept in a shadow repository outside
  the project. It never refuses a write.
- When the runner is missing, every transport now points at
  `nxthooks doctor` instead of a command only NxtOS provides.

## 0.1.0

First release, generated from the NxtOS source tree.
