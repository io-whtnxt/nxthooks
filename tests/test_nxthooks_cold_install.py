"""Install into a HOME that has never seen NxtHooks and assert the result.

Every existing NxtHooks test runs against an already-importable package on a
machine that was installed once, long ago, and hand-corrected since. The five
defects fixed in fca6b8e0 were all invisible there for exactly that reason.
This is the one test that installs cold and checks what a stranger's machine
would actually see.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _cold(tmp_path, *args, cwd=REPO, safe_entry=False):
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "home" / ".config")
    env["XDG_DATA_HOME"] = str(tmp_path / "home" / ".local" / "share")
    env.pop("NXTHOOKS_ROOT", None)
    env.pop("NXTHOOKS_POLICY", None)
    # Relocating HOME also hides Python's *user* site-packages, where this
    # machine happens to keep jsonschema -- a dependency cmd_install acquired
    # when it started importing nxtoskit.plugins.delivery. A stranger
    # installing the wheel gets it in the same site-packages as everything
    # else, so losing it here tests pip's install location rather than hook
    # wiring, which is what this file is about.
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in sys.path if p] + [env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # A cold machine has none of these harnesses. The sandbox relocates HOME
    # but inherits PATH, so `detect` finds the developer's own codex, pi and
    # opencode binaries and `verify` then reports gaps for harnesses this
    # machine was never supposed to have. Presence is exactly what these tests
    # seed, so PATH must not decide it.
    #
    # Safe to narrow: every subprocess here is launched by absolute
    # interpreter path, and the only PATH lookup inside install is `git`, for
    # the source-provenance line, which degrades to a bare path.
    env["PATH"] = os.pathsep.join(["/usr/bin", "/bin"])
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, *(["-P"] if safe_entry else []), "-m", "nxthooks", *args],
        capture_output=True, text=True, cwd=str(cwd), env=env,
        timeout=120, check=False,
    )


def _installed_lib(tmp_path) -> Path:
    return tmp_path / "home" / ".local" / "share" / "nxt" / "hooks" / "lib"


def _dispatch(tmp_path, payload: str) -> subprocess.CompletedProcess:
    """Run a real dispatch through the copy `install` wrote, never the repo."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "home" / ".config")
    env["XDG_DATA_HOME"] = str(tmp_path / "home" / ".local" / "share")
    env["PATH"] = os.pathsep.join(["/usr/bin", "/bin"])
    env["PYTHONPATH"] = os.pathsep.join([p for p in sys.path if p])
    return subprocess.run(
        [sys.executable, "-m", "nxthooks", "dispatch",
         "--harness", "claude-code", "--out", "json"],
        input=payload, capture_output=True, text=True,
        cwd=str(_installed_lib(tmp_path)), env=env, timeout=120, check=False,
    )


def test_a_cold_install_finds_its_own_hooks(tmp_path):
    """Every assertion here must be reachable only through install.

    The first version ran install, then ran `list` from the repo checkout and
    looked for `nxtlint` — but `list` reads BUILTIN_DIR, which is package-
    relative, so it passed with install deleted entirely. It also never
    checked install's exit code, which is how a permanently-unclearable
    `GAPS FOUND` verdict shipped unnoticed. So: assert the exit code, assert
    the runtime landed on disk, and run `list` through the *installed* copy.
    """
    install = _cold(tmp_path, "install", "--harnesses", "claude-code")
    assert install.returncode == 0, install.stdout + install.stderr

    builtin = _installed_lib(tmp_path) / "nxthooks" / "builtin" / "nxtlint.toml"
    assert builtin.is_file(), f"install did not put the builtin tier at {builtin}"

    proc = _cold(tmp_path, "list", cwd=_installed_lib(tmp_path))
    assert proc.returncode == 0, proc.stderr
    names = {line.split()[0] for line in proc.stdout.splitlines()[1:] if line.strip()}
    assert "nxtlint" in names


def test_no_builtin_hook_names_a_source_that_cannot_exist():
    """Four seeded manifests named retired .sh guards for weeks. Nothing
    failed, because this machine's hand-corrected copies masked them."""
    from nxthooks.registry import builtin_manifests

    bad = [e["name"] for e in builtin_manifests()
           if e["source"].endswith((".sh", ".bash"))]
    assert bad == []


def test_install_seeds_config_where_the_dispatcher_reads_it(tmp_path):
    _cold(tmp_path, "install", "--harnesses", "claude-code")
    cfg = tmp_path / "home" / ".config" / "nxt" / "hooks"

    assert cfg.is_dir(), "install wrote config somewhere the dispatcher will not look"


def test_a_cold_install_provisions_its_builtin_hook(tmp_path):
    proc = _cold(tmp_path, "install", "--harnesses", "claude-code")
    # NxtLint own component dir. It used to be ~/.nxtos/bin, which belongs to
    # `git` -- see COMPONENT_IDS in nxtoskit.paths.
    shim = (tmp_path / "home" / ".local" / "share" / "nxt"
            / "lint" / "bin" / "nxtlint-hook.py")

    assert proc.returncode == 0, proc.stderr
    assert shim.is_file(), "install left its own builtin's source missing"


def test_a_cold_install_reports_no_gaps(tmp_path):
    """The phase's headline claim, asserted rather than reasoned.

    `verify` is pinned to cwd=_installed_lib(tmp_path) for the same reason
    as test_the_stranger_experience's: left at the default cwd=REPO it
    would import the intact source package instead of the copy install
    actually wrote, so a broken installed copy could report zero GAPs here
    while genuinely having some -- the exact way this claim went unverified
    once before.
    """
    _cold(tmp_path, "install", "--harnesses", "claude-code")
    proc = _cold(tmp_path, "verify", cwd=_installed_lib(tmp_path))

    assert "GAPS FOUND" not in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stdout


def _cc_settings(tmp_path) -> dict:
    import json

    return json.loads((tmp_path / "home" / ".claude" / "settings.json").read_text())


def test_a_cold_install_wires_only_the_events_that_have_hooks(tmp_path):
    """Two builtins, two events. SessionStart, UserPromptSubmit and PreToolUse
    used to be wired unconditionally, spending `sh -> python -> import -> exit`
    per session, prompt and tool call to discover they had nothing to run --
    and `verify` called all four `[ok]`.

    The expected set tracks the builtin tier rather than naming events
    literally: `nxtlint` is after_tool, `secret-scan` is before_tool, and
    hardcoding the answer here would have to be re-edited by hand every time
    the tier changes, which is how it comes to disagree with reality.
    """
    install = _cold(tmp_path, "install", "--harnesses", "claude-code")
    assert install.returncode == 0, install.stdout + install.stderr

    from nxthooks.registry import builtin_manifests

    canonical = {ev for e in builtin_manifests() for ev in e["events"]}
    expected = {"before_tool": "PreToolUse", "after_tool": "PostToolUse",
                "session_start": "SessionStart",
                "prompt_submit": "UserPromptSubmit"}
    assert set(_cc_settings(tmp_path)["hooks"]) == {expected[e] for e in canonical}

    verify = _cold(tmp_path, "verify", cwd=_installed_lib(tmp_path))
    assert "no live hook declares this event" in verify.stdout, verify.stdout
    assert "claude-code wiring (after_tool)" in verify.stdout
    assert "claude-code wiring (before_tool)" in verify.stdout


def test_the_stranger_experience(tmp_path):
    """Install into an empty HOME, then use it. No NxtOS, no config, no
    pre-existing anything. This is the phase's deliverable, stated once.

    `install` runs with the default cwd=REPO on purpose: nothing has been
    installed yet, so the only package that can exist to do the installing
    is the source tree, and REPO is exactly where install_runtime's own
    `_pkg_dir()` (`Path(__file__).resolve().parent`) points regardless of
    cwd -- pinning cwd here would not change what gets copied.

    `verify` and `list` are pinned to cwd=_installed_lib(tmp_path), the same
    fix `test_a_cold_install_finds_its_own_hooks` already uses and for the
    same reason: `registry.BUILTIN_DIR` resolves off the imported package's
    `__file__`, and `python -m nxthooks` resolves that import off cwd. Left
    at the default cwd=REPO, both subprocesses would import the intact
    source package instead of the copy install_runtime actually wrote, so
    a corrupted or incomplete installed copy could never turn this red --
    exactly the hole a `copytree` that silently drops `*.toml` would hide
    in.
    """
    install = _cold(tmp_path, "install", "--harnesses", "claude-code")
    assert install.returncode == 0, install.stdout + install.stderr

    verify = _cold(tmp_path, "verify", cwd=_installed_lib(tmp_path))
    assert verify.returncode == 0, verify.stdout
    assert "GAPS FOUND" not in verify.stdout

    listing = _cold(tmp_path, "list", cwd=_installed_lib(tmp_path))
    names = {l.split()[0] for l in listing.stdout.splitlines()[1:] if l.strip()}
    assert names == {"nxtlint", "secret-scan", "snapshot-save"}, listing.stdout


# ------------------------------------------- every builtin, not just the first

def test_every_builtin_declares_what_provisions_its_source():
    """A builtin whose source nothing writes is a hook that GAPs on a cold
    machine. That was true of `secret-scan` for as long as it existed: the
    provisioner named exactly one module, `nxthooks.engines.nxtlint.install`, so
    NxtGrep shipped its pattern pack, its hook and its installer and a
    stranger still got no secret scanning.
    """
    from nxthooks.registry import builtin_manifests

    missing = [e["name"] for e in builtin_manifests() if not e.get("provision")]
    assert missing == [], (
        f"builtin hook(s) {missing} name a source with nothing to write it"
    )


def test_a_cold_install_provisions_every_builtin_not_just_the_first(tmp_path):
    proc = _cold(tmp_path, "install", "--harnesses", "claude-code")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    from nxthooks.registry import builtin_manifests

    for entry in builtin_manifests():
        source = entry["source"]
        assert source.startswith("~/"), source
        landed = tmp_path / "home" / source[2:]
        assert landed.is_file(), (
            f"install left builtin '{entry['name']}' pointing at a file "
            f"nothing wrote: {source}"
        )


def test_a_cold_install_refuses_a_known_secret(tmp_path):
    """The behavioural claim, not the file-on-disk one.

    A stranger who installs this gets a security layer or does not. Asserted
    through a real dispatch against the *installed* copy, because that is the
    only thing a stranger runs.
    """
    install = _cold(tmp_path, "install", "--harnesses", "claude-code")
    assert install.returncode == 0, install.stdout + install.stderr

    key = "AKIA" + "IOSFODNN7EXAMPLE"
    payload = json.dumps({
        "version": 1, "harness": "claude-code", "event": "before_tool",
        "cwd": str(tmp_path), "session_id": "s", "tool": "Write",
        "tool_input": {"file_path": str(tmp_path / "x.py"),
                       "content": f"aws_key = {key}"},
    })
    proc = _dispatch(tmp_path, payload)
    outcome = json.loads(proc.stdout)
    assert outcome["verdict"] == "block", (
        "a cold install did not refuse a known-format credential; "
        f"outcome was {outcome}"
    )


def test_a_cold_install_still_allows_clean_content(tmp_path):
    """The other half: a scanner that blocks everything is not a scanner."""
    _cold(tmp_path, "install", "--harnesses", "claude-code")

    payload = json.dumps({
        "version": 1, "harness": "claude-code", "event": "before_tool",
        "cwd": str(tmp_path), "session_id": "s", "tool": "Write",
        "tool_input": {"file_path": str(tmp_path / "x.py"),
                       "content": "print(1)"},
    })
    outcome = json.loads(_dispatch(tmp_path, payload).stdout)
    assert outcome["verdict"] == "allow", outcome


def test_provisioning_survives_a_cwd_that_shadows_the_package(tmp_path):
    """`python -m` prepends the working directory to sys.path.

    So a cwd holding any directory named `nxthooks` or `nxtoskit` shadows the
    real one, and provisioning fails with a ModuleNotFoundError naming a
    module that is installed and importable from everywhere else. Running
    `nxthooks install` from a checkout of its own source is enough -- which is
    exactly how it was found, and it means the failure is likeliest for the
    people most likely to hit it.
    """
    # A `nxtoskit` that exists but is the wrong one -- an empty package with
    # none of the writers in it. Shadowing `nxthooks` itself is not the case
    # under test: `python -m nxthooks` could not start at all then, whereas
    # the installed console script would be unaffected.
    decoy = tmp_path / "decoy"
    (decoy / "nxtoskit").mkdir(parents=True)
    (decoy / "nxtoskit" / "__init__.py").write_text("")

    # Model the installed console entry; the provisioning child must still
    # defend itself against this cwd. Do not rely on editable-import fallback.
    proc = _cold(tmp_path, "install", "--harnesses", "claude-code", cwd=decoy, safe_entry=True)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FAILED to provision" not in proc.stdout, proc.stdout
    from nxthooks.registry import builtin_manifests

    for entry in builtin_manifests():
        landed = tmp_path / "home" / entry["source"][2:]
        assert landed.is_file(), f"{entry['name']} not provisioned from a shadowing cwd"


def test_cold_list_observes_a_corrupted_installed_copy(tmp_path):
    proc = _cold(tmp_path, "install", "--harnesses", "claude-code")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    installed = _installed_lib(tmp_path) / "nxthooks/builtin/nxtlint.toml"
    assert installed.is_file()
    installed.unlink()
    assert (REPO / "nxthooks/builtin/nxtlint.toml").is_file()
    listing = _cold(tmp_path, "list", cwd=_installed_lib(tmp_path))
    assert listing.returncode == 0, listing.stderr
    names = {line.split()[0] for line in listing.stdout.splitlines()[1:] if line.strip()}
    assert "nxtlint" not in names
    assert "secret-scan" in names
