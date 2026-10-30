"""The one way NxtHooks (and NxtOS, which re-exports this) runs git.

A workspace is untrusted: it can arrive with its own `.git/config`, and git
runs programs that config names (fsmonitor, hooks, filter and merge drivers,
textconv, an ssh command, a pager, an editor, a credential helper). Every git
child therefore gets the same treatment from `run_git`:

* list argv, no shell, a minimal environment (`git_env`), no stdin unless the
  caller supplies bytes, its own process group so a timeout kills every
  descendant, and a byte cap on stdout and stderr;
* fixed configuration overrides in the environment (`GIT_CONFIG_COUNT`), which
  outrank every config file, switching off every config key that names a
  program and has a fixed name (`FIXED_OVERRIDES`). The user's global and system
  config stay in effect; only what a repository can add is overruled;
* `--no-optional-locks` on anything that does not write, and `--no-ext-diff
  --no-textconv` on diff, show and log;
* hooks off (`core.hooksPath=/dev/null`) unless the caller passes `hooks=True`.
  The only such caller is a commit the person explicitly asked to run hooks for.

Keys with an arbitrary name (`filter.X.clean`, `diff.X.textconv`,
`merge.X.driver`) cannot be switched off by a fixed override. Before a command
that could run one, the repository's own config is read with the same hardened
environment (`local_program_keys`). Reads (status, diff, show, log, ls-files,
blame, grep) then get a per-name override that blanks each one found. Commands
that would change the repository or the index (`PROGRAM_RUNNING_COMMANDS`) are
refused outright: blanking a clean filter would stage different content than
the repository intends. An `include` directive hides keys from that scan, so it
refuses both classes.

Where a workspace path arrives in a request, pass `root` and `contain` (a
`(root, path) -> resolved path | None`); the cwd and every -C/--git-dir/--work-tree
value must pass it. This module is stdlib-only; the desktop's policy is injected: `workspace_policy.run_git_in_workspace` binds
`contain=resolve_within`.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import time
import re
import shutil
import signal
import subprocess
import threading
from typing import Callable, NamedTuple


DEFAULT_MAX_OUTPUT = 32 * 1024 * 1024
STDERR_CAP = 256 * 1024
CONFIG_SCAN_CAP = 4 * 1024 * 1024

# Config keys with a fixed name that make git run something. Empty value turns
# the feature off; `core.hooksPath` is added separately because `hooks=True`
# leaves it alone.
FIXED_OVERRIDES = (
    ("core.fsmonitor", "false"),
    ("core.sshCommand", "ssh"),
    ("core.pager", "cat"),
    ("core.editor", "true"),
    ("core.askPass", ""),
    ("sequence.editor", "true"),
    ("diff.external", ""),
    ("credential.helper", ""),
    ("protocol.ext.allow", "never"),
    ("gpg.program", "gpg"),
    ("gpg.ssh.program", "ssh-keygen"),
    ("gpg.x509.program", "gpgsm"),
    ("commit.gpgSign", "false"),
    ("log.showSignature", "false"),
    ("core.gitProxy", ""),
    # A submodule is a repository with its own config, which the scan of the
    # superproject never sees: never descend into one.
    ("diff.ignoreSubmodules", "all"),
    ("submodule.recurse", "false"),
    ("status.submoduleSummary", "false"),
    ("fetch.recurseSubmodules", "no"),
    ("push.recurseSubmodules", "no"),
)
HOOKS_OFF = ("core.hooksPath", "/dev/null")

# Config key shapes git runs programs for, whatever the section's name:
# (section, variables). `filter.X.required` is blanked beside them so a blanked
# filter is not then reported as a failure.
NAMED_PROGRAM_KEYS = (
    ("filter", ("clean", "smudge", "process")),
    ("diff", ("textconv", "command")),
    ("merge", ("driver",)),
    # Programs git starts to talk to a remote: local-transport upload/receive
    # packs and a `git-remote-<vcs>` helper. Git keeps the first of several
    # values, so a later blank cannot override them: commands that reach a
    # remote refuse instead.
    ("remote", ("uploadpack", "receivepack", "uploadarchive", "vcs")),
)
# Local config that pulls in more config, which the scan cannot see through.
OPAQUE_SECTIONS = ("include", "includeif")

# Commands that change the repository, the index or the work tree, or that
# merge: filter, merge-driver and hook-adjacent code runs in all of them.
PROGRAM_RUNNING_COMMANDS = frozenset({
    "add", "am", "apply", "checkout", "cherry-pick", "clone", "commit", "merge",
    "merge-tree", "pull", "rebase", "reset", "restore", "revert", "stash",
    "switch", "worktree", "commit-tree", "update-ref", "read-tree",
})
# Of those, the ones that merge content and so can run a `merge.X.driver`. A
# clean or smudge filter matters to every one of them; a textconv or external
# diff driver matters to none (they only run when something is shown).
MERGING_COMMANDS = frozenset({
    "am", "apply", "checkout", "cherry-pick", "merge", "merge-tree", "pull",
    "rebase", "revert", "stash", "switch",
})
# Read-only commands that still consult filters, textconv or diff drivers.
PROGRAM_READING_COMMANDS = frozenset({"blame", "diff", "grep", "log", "ls-files", "show", "status"})
# Commands that talk to a remote and so start the programs a `remote.X.*` key names.
NETWORK_COMMANDS = frozenset({"fetch", "push", "ls-remote"})
REMOTE_REACHING_COMMANDS = NETWORK_COMMANDS | {"clone", "pull"}
REFUSED_SECTIONS_BY_COMMAND = {
    command: (
        *(("filter", "merge") if command in MERGING_COMMANDS else ("filter",)
          if command in PROGRAM_RUNNING_COMMANDS else ()),
        *(("remote",) if command in REMOTE_REACHING_COMMANDS else ()),
    )
    for command in PROGRAM_RUNNING_COMMANDS | NETWORK_COMMANDS
}
NO_EXTERNAL_DIFF_COMMANDS = frozenset({"diff", "log", "show"})
# Commands that take --ignore-submodules (checked with `git <cmd> -h` and the
# diff options): they never open a submodule's own repository.
IGNORE_SUBMODULES_COMMANDS = frozenset({"status", "diff", "log", "show"})
UNSUPPORTED_COMMANDS = frozenset({"submodule"})

# What the person running NxtOS set for their own git: where the config files
# are and who commits. A repository cannot set any of these.
_PASSTHROUGH = (
    "HOME", "LANG", "TMPDIR", "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
)
_NAMED_KEY = re.compile(r"^([^.]+)\.(.+)\.([^.]+)$")


class GitResult(NamedTuple):
    """outcome is ok, failed (non-zero exit), missing (no git), timeout,
    overflow (output past the cap) or refused (`refusal` says why)."""
    outcome: str
    code: "int | None"
    stdout: bytes
    stderr: bytes
    refusal: str = ""

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def error_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


def refusal_message(key: str, operation: str) -> str:
    return (f"This repository's git config defines {key}, which can run programs. "
            f"NxtOS will not run {operation} here.")


def _absolute_path(source: "dict[str, str]") -> str:
    entries = [e for e in source.get("PATH", "").split(os.pathsep) if os.path.isabs(e)]
    return os.pathsep.join(entries)


def git_env(*, hooks: bool = False, read_only: bool = True, extra: "tuple[tuple[str, str], ...]" = (),
            source: "dict[str, str] | None" = None) -> dict[str, str]:
    """The child's whole environment. `extra` is more (key, value) config
    overrides, applied after the fixed ones."""
    source = os.environ if source is None else source
    env = {name: source[name] for name in _PASSTHROUGH if source.get(name)}
    env["PATH"] = _absolute_path(source)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    pairs = list(FIXED_OVERRIDES) + ([] if hooks else [HOOKS_OFF]) + list(extra)
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


class _Outcome(NamedTuple):
    code: "int | None"
    stdout: bytes
    stderr: bytes
    timed_out: bool
    overflow: bool


_COMMIT_PREPARATION = threading.local()


@contextmanager
def commit_preparation_budget(deadline, cancelled):
    """Bound this preparation worker's Git children, including snapshot packing."""
    _COMMIT_PREPARATION.budget = (deadline, cancelled)
    try:
        yield
    finally:
        del _COMMIT_PREPARATION.budget


def _spawn(argv: list[str], cwd: str, env: dict[str, str], stdin: bytes,
           timeout: float, max_output: int) -> _Outcome:
    budget = getattr(_COMMIT_PREPARATION, "budget", None)
    if budget:
        deadline, cancelled = budget
        timeout = min(timeout, deadline - time.monotonic())
        if cancelled() or timeout <= 0:
            return _Outcome(None, b"", b"Commit preparation cancelled or timed out.", True, False)
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, shell=False,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    chunks = {"out": bytearray(), "err": bytearray()}
    overflow = threading.Event()

    def kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    def drain(stream, key: str, cap: int) -> None:
        buffer = chunks[key]
        while block := stream.read1(8192):
            buffer.extend(block)
            if len(buffer) > cap:
                overflow.set()
                kill()
                return

    def feed() -> None:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except OSError:
            pass

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, "out", max_output), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, "err", STDERR_CAP), daemon=True),
    ]
    if stdin:
        threads.append(threading.Thread(target=feed, daemon=True))
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        if budget:
            end = time.monotonic() + timeout
            while proc.poll() is None:
                if cancelled() or time.monotonic() >= end:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    proc.wait(timeout=min(.05, max(.001, end - time.monotonic())))
                except subprocess.TimeoutExpired:
                    continue
        else:
            proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill()
        proc.wait()
    for thread in threads:
        thread.join(timeout=2.0)
    if any(thread.is_alive() for thread in threads):
        kill()  # a descendant that outlived git is holding the pipes open
        for thread in threads:
            thread.join(timeout=2.0)
    for stream in (proc.stdout, proc.stderr):
        stream.close()
    return _Outcome(proc.returncode, bytes(chunks["out"]), bytes(chunks["err"]),
                    timed_out, overflow.is_set())


def _find_git(env: dict[str, str]) -> "str | None":
    return shutil.which("git", path=env["PATH"])


# Global options git reads before the subcommand. Anything else is refused
# rather than guessed at: a wrongly-parsed option would let the next token be
# taken for the subcommand and skip the config scan.
_VALUE_OPTIONS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
    "--config-env", "--attr-source",
})
_FLAG_OPTIONS = frozenset({
    "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks", "--no-advice",
    "--bare", "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
    "--icase-pathspecs", "-p", "--paginate", "-P", "--no-pager",
})
_PATH_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree"})
_CONFIG_OPTIONS = frozenset({"-c", "--config-env"})


class UnsupportedOption(ValueError):
    pass


def _split_globals(args: "list[str]") -> "tuple[list[tuple[str, str | None]], str, list[str]]":
    """([(option, value-or-None)], subcommand, rest) of a git argv without the
    leading `git`. Raises UnsupportedOption for any leading option not in the
    two tables above (`--exec-path`, `--html-path`, `--version`, an unknown one)."""
    options: list[tuple[str, str | None]] = []
    index = 0
    while index < len(args) and args[index].startswith("-"):
        token = args[index]
        name, equals, inline = token.partition("=")
        if token in _FLAG_OPTIONS:
            options.append((token, None))
        elif name in _VALUE_OPTIONS and equals and name != "-C" and name != "-c":
            options.append((name, inline))
        elif token in _VALUE_OPTIONS:
            if index + 1 >= len(args):
                raise UnsupportedOption(token)
            options.append((token, args[index + 1]))
            index += 1
        else:
            raise UnsupportedOption(token)
        index += 1
    if index >= len(args):
        raise ValueError("run_git: no git subcommand in the arguments")
    return options, args[index], list(args[index + 1:])


def _option_argv(options: "list[tuple[str, str | None]]") -> "list[str]":
    argv: list[str] = []
    for name, value in options:
        argv.append(name)
        if value is not None:
            argv.append(value)
    return argv


def _fixed_keys(hooks: bool) -> "frozenset[str]":
    keys = {key.lower() for key, _ in FIXED_OVERRIDES}
    if not hooks:
        keys.add(HOOKS_OFF[0].lower())
    return frozenset(keys)


def _weakened_key(options: "list[tuple[str, str | None]]", hooks: bool) -> "str | None":
    """The first `-c`/`--config-env` key that would set one of our fixed keys."""
    fixed = _fixed_keys(hooks)
    for name, value in options:
        if name in _CONFIG_OPTIONS and value is not None:
            key = value.split("=", 1)[0]
            if key.lower() in fixed:
                return key
    return None


def _escaping_path(options: "list[tuple[str, str | None]]", where: str,
                   root: "str | os.PathLike", contain: "Callable") -> "str | None":
    """The first -C/--git-dir/--work-tree value that resolves outside `root`."""
    for name, value in options:
        if name in _PATH_OPTIONS and value is not None:
            if contain(root, os.path.join(where, value)) is None:
                return value
    return None


_NO_WORKTREE_CONFIG = b"worktreeConfig"
_NOT_A_REPOSITORY = (b"inside a git repository", b"not a git repository", b"not in a git directory")


def local_program_keys(cwd: str, git: str, global_options: "list[str]" = (),
                       timeout: float = 10.0) -> "list[str] | None":
    """Every key in the repository's own config (common and per-worktree) that
    git would run a program for, plus any include directive. `[]` when `cwd` is
    not in a repository at all (there is no config to run anything from).
    None when the config exists but could not be read: timeout, over the scan
    cap, or git failing for any other reason. Callers must fail closed on None."""
    env = git_env()
    keys: list[str] = []
    for scope in ("--local", "--worktree"):
        done = _spawn([git, "--no-optional-locks", *global_options, "config", scope, "--list", "-z"],
                      cwd, env, b"", timeout, CONFIG_SCAN_CAP)
        if done.timed_out or done.overflow:
            return None
        if done.code != 0:
            if done.code == 128 and any(m in done.stderr for m in _NOT_A_REPOSITORY):
                return []
            if scope == "--worktree" and done.code == 128 and _NO_WORKTREE_CONFIG in done.stderr:
                continue  # linked worktree, extension off: git reads no per-worktree file
            return None
        for record in done.stdout.split(b"\0"):
            key = record.split(b"\n", 1)[0].decode("utf-8", errors="replace")
            if key:
                keys.append(key)
    return [k for k in dict.fromkeys(keys) if _is_program_key(k)]


def _split(key: str) -> "tuple[str, str, str] | None":
    match = _NAMED_KEY.match(key)
    return (match.group(1).lower(), match.group(2), match.group(3).lower()) if match else None


def _is_program_key(key: str) -> bool:
    lowered = key.lower()
    if lowered.split(".", 1)[0] in OPAQUE_SECTIONS:
        return True
    parts = _split(key)
    return parts is not None and any(
        parts[0] == section and parts[2] in variables for section, variables in NAMED_PROGRAM_KEYS)


def _blocking(found: "list[str]", subcommand: str) -> "str | None":
    """The first key that makes `subcommand` unsafe to run, or None. An include
    blocks everything that scans (it hides keys); the rest block only the
    commands that would run them and cannot have them blanked (a change to the
    repository or index)."""
    for key in found:
        if key.lower().split(".", 1)[0] in OPAQUE_SECTIONS:
            return key
    for key in found:
        parts = _split(key)
        if parts and parts[0] in REFUSED_SECTIONS_BY_COMMAND.get(subcommand, ()):
            return key
    return None


def _neutralizing(keys: "list[str]") -> "tuple[tuple[str, str], ...]":
    pairs: list[tuple[str, str]] = []
    for key in keys:
        parts = _split(key)
        if parts is None:
            continue
        pairs.append((key, ""))
        if parts[0] == "filter":
            pairs.append((f"filter.{parts[1]}.required", "false"))
    return tuple(pairs)


def run_git(args: "list[str]", cwd: "str | os.PathLike", *, timeout: float,
            max_output: int = DEFAULT_MAX_OUTPUT, hooks: bool = False,
            stdin: bytes = b"", root: "str | os.PathLike | None" = None,
            contain: "Callable | None" = None, isolated: bool = False) -> GitResult:
    """Run `git <args>` in `cwd` under the policy in the module docstring.

    The subcommand may follow the global options in `_VALUE_OPTIONS`/`_FLAG_OPTIONS`;
    any other leading option is refused. `root`, when given with `contain`, is the workspace the
    cwd and every -C/--git-dir/--work-tree value must be inside. A caller `-c` or
    `--config-env` for one of our fixed keys is refused. Never raises for a git or OS failure; the outcome says."""
    try:
        options, subcommand, rest = _split_globals(list(args))
    except UnsupportedOption as exc:
        return GitResult("refused", None, b"", b"", f"unsupported git option {exc}")
    where = str(cwd)
    if root is not None:
        if contain is None:
            raise ValueError("run_git: root needs a contain function")
        contained = contain(root, where)
        if contained is None:
            return GitResult("refused", None, b"", b"", "This folder is outside the workspace.")
        where = str(contained)
        escaping = _escaping_path(options, where, root, contain)
        if escaping is not None:
            return GitResult("refused", None, b"", b"", f"The git path {escaping} is outside the workspace.")
    if subcommand in UNSUPPORTED_COMMANDS:
        return GitResult("refused", None, b"", b"", f"unsupported git option {subcommand}")
    for token in rest:
        if token == "--":
            break
        if token == "--recursive" or token.startswith("--recurse-submodules"):
            return GitResult("refused", None, b"", b"", f"unsupported git option {token}")
    weakened = _weakened_key(options, hooks)
    if weakened is not None:
        return GitResult("refused", None, b"", b"", f"NxtOS will not let a caller set {weakened} for git.")
    global_options = _option_argv(options)
    mutating = subcommand in PROGRAM_RUNNING_COMMANDS
    env = git_env(hooks=hooks, read_only=not mutating)
    if isolated:
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    git = _find_git(env)
    if git is None:
        return GitResult("missing", None, b"", b"")
    argv = [git] if mutating else [git, "--no-optional-locks"]
    try:
        if mutating or subcommand in PROGRAM_READING_COMMANDS or subcommand in NETWORK_COMMANDS:
            found = local_program_keys(where, git, global_options)
            if found is None:
                return GitResult("refused", None, b"", b"",
                                 f"NxtOS could not read this repository's git config, so it will not run git {subcommand} here.")
            blocking = _blocking(found, subcommand)
            if blocking:
                return GitResult("refused", None, b"", b"",
                                 refusal_message(blocking, f"git {subcommand}"))
            if found:
                env = git_env(hooks=hooks, read_only=not mutating, extra=_neutralizing(found))
        if isolated:
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        if subcommand in NO_EXTERNAL_DIFF_COMMANDS:
            rest = ["--no-ext-diff", "--no-textconv", *rest]
        if subcommand in IGNORE_SUBMODULES_COMMANDS:
            rest = ["--ignore-submodules=all", *rest]
        done = _spawn([*argv, *global_options, subcommand, *rest], where, env, stdin, timeout, max_output)
    except FileNotFoundError:
        return GitResult("missing", None, b"", b"")
    except OSError:
        return GitResult("failed", None, b"", b"")
    if done.timed_out:
        return GitResult("timeout", None, done.stdout, done.stderr)
    if done.overflow:
        return GitResult("overflow", done.code, done.stdout, done.stderr)
    return GitResult("ok" if done.code == 0 else "failed", done.code, done.stdout, done.stderr)


def run_argv(argv: "list[str]", cwd: "str | os.PathLike", *, timeout: float = 20,
             stderr_on_failure: bool = True) -> "tuple[int, str]":
    """`(exit code, text)` for an injectable-runner seam whose argv still leads
    with "git" (claims, worktrees, hygiene). A refusal is `(1, <why>)`. A git
    that is missing or too slow raises as `subprocess.run` did, because those
    callers already handle it that way."""
    if not argv or argv[0] != "git":
        raise ValueError("run_argv: argv must lead with git")
    done = run_git(argv[1:], cwd, timeout=timeout)
    if done.outcome == "missing":
        raise FileNotFoundError("git")
    if done.outcome == "timeout":
        raise subprocess.TimeoutExpired(argv, timeout)
    if done.outcome == "refused":
        return 1, done.refusal
    code = done.code if done.code is not None else 1
    if code != 0 and stderr_on_failure:
        return code, done.error_text or done.text
    return code, done.text
