"""Serialize native lifecycle operations without placing locks in removable state.

Two constraints pull in opposite directions and both are load-bearing.

The lock cannot live in `state_dir`, because `uninstall` removes it -- an
operation holding a lock on a file it is deleting keeps a valid descriptor,
but the next operation creates a fresh file and gets no exclusion from it, so
the guarantee quietly stops holding at exactly the moment two uninstalls could
overlap.

It also cannot live in the system temp directory, which is where this started.
`/tmp` is world-writable on a shared host, so any other local user can create
the path first -- the name is fully derivable from a uid and a home directory
-- and hold `LOCK_EX` on it indefinitely. Every `install`, `uninstall` and
`doctor` from then on prints "another operation is active" and exits 1, with
nothing to point at. `O_NOFOLLOW` does not help: the squatted path is a
regular file, not a symlink.

The cache directory is neither. It is under the operator's own home, nothing
in `uninstall` touches it, and it is the one NxtOS role whose contents are
defined as disposable, which is what a lock file is.
"""

from __future__ import annotations

import fcntl
import functools
import os
from pathlib import Path


def lock_path(host) -> Path:
    """One lock per install root, not per user.

    The root is what an operation mutates, and `[install].root` is
    configurable, so two roots on one machine are two independent operations
    and must not block each other.
    """
    return host.cache_dir / "lifecycle.lock"


def _discard(path: Path, home: Path) -> None:
    """Take the lock file and any directory it needed with it.

    `uninstall` promises the machine is left as it was found, and
    `_remove_locks` already applies that to `projection.write`'s own sidecars:
    ours, empty, and residue like any other if kept. A lock file that outlived
    the install it guarded is the same thing.

    Cleaning up after releasing rather than while holding leaves one narrow
    window -- a second operation can acquire between the release and the
    unlink, and a third would then create a fresh file and not contend with
    it. That needs two lifecycle operations starting within microseconds of
    each other, against a leftover file that was otherwise certain.
    """
    path.unlink(missing_ok=True)
    for parent in path.parents:
        if parent == home or not parent.is_relative_to(home):
            return
        try:
            parent.rmdir()
        except OSError:
            return


def exclusive_lifecycle(operation):
    """Refuse rather than queue. A lifecycle operation runs for minutes and
    rewrites harness settings files; a second one waiting silently behind it
    looks identical to one that has hung."""

    @functools.wraps(operation)
    def run(args, host=None) -> int:
        from .lifecycle import Host

        host = host or Host.local()
        path = lock_path(host)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        held = False
        try:
            with os.fdopen(fd, "a") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print("Another native Hooks lifecycle operation is active; "
                          "retry after it finishes.")
                    return 1
                held = True
                return operation(args, host)
        finally:
            # Only the holder cleans up. A refused caller unlinking would
            # delete the file out from under the operation that is running.
            if held:
                _discard(path, host.home)

    return run
