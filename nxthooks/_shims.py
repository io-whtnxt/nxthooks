"""Descriptor-bound builtin shim files; directory ownership is never inferred."""
from __future__ import annotations

import ast
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path


def _canonical(target: Path) -> Path:
    if ".." in target.parts:
        raise ValueError("Shim paths cannot contain parent traversal")
    target = target.absolute()
    if sys.platform == "darwin":
        for alias in (Path("/var"), Path("/tmp")):
            canonical = Path("/private") / alias.name
            if target.is_relative_to(alias) and alias.is_symlink() and alias.resolve() == canonical:
                target = canonical / target.relative_to(alias)
    return target


def _identity(info) -> list[int]:
    return [info.st_dev, info.st_ino]


def _open(directory: int, name: str, flags: int = os.O_RDONLY):
    try:
        descriptor = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"Refusing symlink in shim path: {name}") from None
        raise
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(f"Refusing nonregular or hard-linked shim artifact: {name}")
    return os.fdopen(descriptor, "r+b" if flags & os.O_RDWR else "rb")


def _save(stream, record: dict) -> None:
    stream.seek(0)
    stream.write(json.dumps(record).encode())
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


@contextmanager
def _parent(target: Path, create: bool):
    opened = [(Path(target.anchor), os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY))]
    try:
        for name in target.parent.parts[1:]:
            path, parent = opened[-1]
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(name, 0o755, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(f"Refusing symlink in shim path: {path / name}") from None
                raise
            opened.append((path / name, child))
        yield opened
    finally:
        for _, descriptor in reversed(opened):
            os.close(descriptor)


@contextmanager
def _record(directory: int, name: str, create: bool):
    fresh = False
    try:
        stream = _open(directory, name, os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0))
        fresh = create
    except FileExistsError:
        stream = _open(directory, name, os.O_RDWR)
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    with stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(os.fstat(stream.fileno())):
            raise ValueError("Shim ownership changed while waiting for its lock")
        yield stream, fresh


def _matches(stream, record: dict) -> bool:
    info = os.fstat(stream.fileno())
    stream.seek(0)
    return (info.st_nlink == 1 and record.get("file") == _identity(info)
            and hashlib.sha256(stream.read()).hexdigest() == record.get("sha256"))


def _remove_verified(directory: int, name: str, stream) -> bool:
    """Capture the current directory entry, then verify before deleting it."""
    expected = os.fstat(stream.fileno())
    stream.seek(0)
    digest = hashlib.sha256(stream.read()).digest()
    quarantine = ".nxtos-remove-" + uuid.uuid4().hex
    try:
        os.rename(name, quarantine, src_dir_fd=directory, dst_dir_fd=directory)
    except FileNotFoundError:
        return False
    try:
        with _open(directory, quarantine) as captured:
            matched = (_identity(os.fstat(captured.fileno())) == _identity(expected)
                       and hashlib.sha256(captured.read()).digest() == digest)
    except ValueError:
        matched = False
    if not matched:
        try:
            # Exclusive hard-link restoration cannot clobber a newer entry.
            os.link(quarantine, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            raise ValueError(f"Concurrent shim replacement preserved at {quarantine}") from None
        os.unlink(quarantine, dir_fd=directory)
        return False
    os.unlink(quarantine, dir_fd=directory)
    return True


def _prior_output(data: bytes, template: str) -> bool:
    """True when `data` is exactly `template.format(root=...)` for some root string."""
    head, _, tail = template.partition("{root!r}")
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return False
    if len(text) < len(head) + len(tail) or not (text.startswith(head) and text.endswith(tail)):
        return False
    try:
        root = ast.literal_eval(text[len(head):len(text) - len(tail)])
    except (ValueError, SyntaxError):
        return False
    return isinstance(root, str) and template.format(root=root) == text


def _adoptable(directory: int, name: str, prior: str | None) -> dict | None:
    """The ownership record for a marker-less shim a prior NxtOS installer wrote, else None."""
    if prior is None:
        return None
    with _open(directory, name) as existing:
        data = existing.read()
        if not _prior_output(data, prior):
            return None
        return {"file": _identity(os.fstat(existing.fileno())), "sha256": hashlib.sha256(data).hexdigest()}


def install(target: Path, body: str, prior: str | None = None) -> Path:
    """Write `body` at `target` under an ownership record.

    `prior` is the `{root!r}` template earlier NxtOS installers wrote with no
    ownership record; a marker-less target is adopted only when its bytes are
    exactly that template for some root. Anything else is refused.
    """
    target = _canonical(target)
    marker = "." + target.name + ".nxtos-owned.json"
    digest = hashlib.sha256(body.encode()).hexdigest()
    with _parent(target, create=True) as opened:
        directory = opened[-1][1]
        try:
            os.stat(marker, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.stat(target.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if _adoptable(directory, target.name, prior) is None:
                    raise ValueError(f"Refusing unowned shim: {target}")
        with _record(directory, marker, create=True) as (ownership, fresh):
            written = False
            if fresh:
                _save(ownership, {"sha256": None})
                try:
                    output = _open(directory, target.name, os.O_RDWR | os.O_CREAT | os.O_EXCL)
                except FileExistsError:
                    try:
                        record = _adoptable(directory, target.name, prior)
                    except ValueError:
                        # A symlink planted after the marker: drop the marker this
                        # install created, or every later install refuses the shim.
                        _remove_verified(directory, marker, ownership)
                        raise
                    if record is None:
                        _remove_verified(directory, marker, ownership)
                        raise ValueError(f"Refusing unowned shim: {target}") from None
                else:
                    with output:
                        output.write(body.encode())
                        output.flush()
                        os.fchmod(output.fileno(), 0o755)
                        record = {"sha256": digest, "file": _identity(os.fstat(output.fileno()))}
                    written = True
            else:
                record = json.load(ownership)
            if not written:
                with _open(directory, target.name, os.O_RDWR) as output:
                    if not _matches(output, record):
                        raise ValueError(f"Refusing altered shim: {target}")
                    if record["sha256"] != digest:
                        if not _remove_verified(directory, target.name, output):
                            raise ValueError(f"Refusing concurrent shim replacement: {target}")
                        with _open(directory, target.name, os.O_RDWR | os.O_CREAT | os.O_EXCL) as replacement:
                            replacement.write(body.encode())
                            replacement.flush()
                            os.fchmod(replacement.fileno(), 0o755)
                            record["file"] = _identity(os.fstat(replacement.fileno()))
            record["sha256"] = digest
            _save(ownership, record)
    return target


def uninstall(target: Path) -> None:
    target = _canonical(target)
    marker = "." + target.name + ".nxtos-owned.json"
    with _parent(target, create=False) as opened:
        if opened is None:
            return
        directory = opened[-1][1]
        with _record(directory, marker, create=False) as entry:
            if entry is None:
                return
            ownership, _ = entry
            record = json.load(ownership)
            try:
                output = _open(directory, target.name)
            except FileNotFoundError:
                output = None
            if output is not None:
                with output:
                    if not _matches(output, record) or not _remove_verified(directory, target.name, output):
                        return
            if not _remove_verified(directory, marker, ownership):
                return
