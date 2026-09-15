"""`install` must not destroy a hook implementation it does not own.

`install_runtime` rmtree's `lib/nxthooks` before copying the package in.
Nothing *should* keep a hook source in there, but `nlm-session-start.py` did
-- as its only copy -- so every reinstall deleted a live hook with no warning
and no backup, and `verify` then reported a gap whose printed advice was to
run the very command that caused it.

This is the signature failure of this component: the gap looks like a missing
file, so the fix looks like reinstalling, so the operator reruns the thing
that ate it.
"""

import pytest

from nxthooks import lifecycle


def _install(host):
    return lifecycle.install_runtime(host, {"install": {"root": str(host.home / "rt")}})


@pytest.fixture
def installed_with_a_foreign_source(hooks_machine, monkeypatch, tmp_path):
    """A machine where a live manifest points at a file inside the runtime."""
    host = hooks_machine()
    root = _install(host)

    foreign = root / "lib" / "nxthooks" / "adapters" / "nlm-session-start.py"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("#!/usr/bin/env python3\nprint('irreplaceable')\n")

    monkeypatch.setattr(
        lifecycle,
        "load_manifests",
        lambda *a, **k: [{"name": "nlm-session-start", "source": str(foreign)}],
    )
    return host, foreign


def test_a_reinstall_does_not_delete_it(installed_with_a_foreign_source):
    host, foreign = installed_with_a_foreign_source

    _install(host)

    assert foreign.is_file(), "reinstall destroyed a live hook implementation"
    assert "irreplaceable" in foreign.read_text()


def test_the_rescued_file_stays_executable(installed_with_a_foreign_source):
    host, foreign = installed_with_a_foreign_source

    _install(host)

    assert foreign.stat().st_mode & 0o111, "rescued source came back non-executable"


def test_a_source_outside_the_runtime_is_left_alone(hooks_machine, monkeypatch):
    """The rescue reads files to copy them back. It must not reach outside
    `lib`, or install starts rewriting sources it was never replacing."""
    host = hooks_machine()
    outside = host.home / "bin" / "guard.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("original\n")
    before = outside.stat().st_mtime_ns

    monkeypatch.setattr(
        lifecycle,
        "load_manifests",
        lambda *a, **k: [{"name": "guard", "source": str(outside)}],
    )
    _install(host)

    assert outside.read_text() == "original\n"
    assert outside.stat().st_mtime_ns == before, "install rewrote an untouched source"
