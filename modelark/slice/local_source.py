"""Explicit, read-only archive attachments yielding original bytes without retrieval.

Compose with FencedSources: its archive fence must span this context manager.
Only direct ``<mount>/modelark/.git`` archives are supported. The sole allowed
symlink is a final annex pointer whose normalized target is a sealed key under
the pinned archive's object directory; the target is then opened independently
with the same no-symlink/no-mount-crossing confinement as ordinary files.
"""
from __future__ import annotations

import configparser
from contextlib import ExitStack, contextmanager
import errno
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat

from modelark.capacity_evidence import identity_fingerprint_v1
from . import domain as d
from .decoding import original_stream
from .paths import canonical_attachment
from .io_errors import classify_io
from .transaction import TransferRefusal


def _identity(evidence):
    return tuple(getattr(evidence, key) for key in
                 ("fs_uuid", "serial", "total_bytes", "device_id", "mount_id", "mount_path"))


def _translate_io(exc, label):
    if isinstance(exc, FileNotFoundError):
        return TransferRefusal("SOURCE_MISSING", label)
    code = "SOURCE_PATH_UNSAFE" if exc.errno in (errno.ELOOP, errno.EXDEV) else "SOURCE_READ_FAILED"
    return TransferRefusal(code, f"{label}: {exc}")


def _translate_confinement(exc):
    if exc.code == "WAITING_DESTINATION":
        return TransferRefusal("SOURCE_MISSING", exc.detail)
    if exc.code == "DESTINATION_CHANGED":
        return TransferRefusal("SOURCE_CHANGED", exc.detail)
    # The shared hardware observer speaks destination-side eligibility codes.
    # Its inability to prove a source attachment must stay a source refusal so
    # the engine can consider other sealed archive copies, never blame USB output.
    if exc.code.startswith("DESTINATION_") or exc.code == "FILESYSTEM_UNSUPPORTED":
        return TransferRefusal("SOURCE_IDENTITY_UNPROVEN", exc.detail)
    if exc.code == "PATH_UNSAFE":
        return TransferRefusal("SOURCE_PATH_UNSAFE", exc.detail)
    return exc


class _SourceErrors:
    def __init__(self, stream, label, check=None):
        self.stream, self.label, self.check = stream, label, check

    def read(self, size=-1):
        try:
            return self.stream.read(size)
        except OSError as exc:
            raise _translate_confinement(classify_io(
                exc, self.check, _translate_io(exc, self.label))) from exc
        except TransferRefusal as exc:
            translated = _translate_confinement(exc)
            if translated is exc:
                raise
            raise translated from exc


class _SourceStack(ExitStack):
    """Translate only source-owned cleanup, without masking a consumer exception."""
    def __init__(self, label):
        super().__init__()
        self.label = label

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        except OSError as cleanup:
            if exc_type is not None:
                return False  # Preserve the original failure already being unwound.
            # The retained tree may already be closed: no fresh absence inference.
            raise _translate_confinement(classify_io(
                cleanup, None, _translate_io(cleanup, self.label))) from cleanup


def _annex_uuid(tree):
    fd = tree.open(".git/config", os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as config:
        info = os.fstat(config.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1 << 20:
            raise TransferRefusal("SOURCE_IDENTITY_UNPROVEN", "invalid archive git config")
        data = config.read((1 << 20) + 1)
    if len(data) > 1 << 20:
        raise TransferRefusal("SOURCE_IDENTITY_UNPROVEN", "oversize archive git config")
    parser = configparser.RawConfigParser(strict=True)
    try:
        parser.read_string(data.decode("utf-8"))
        value = parser.get("annex", "uuid").strip()
    except (UnicodeError, configparser.Error) as exc:
        raise TransferRefusal("SOURCE_IDENTITY_UNPROVEN", "missing unambiguous annex UUID") from exc
    if not value:
        raise TransferRefusal("SOURCE_IDENTITY_UNPROVEN", "missing annex UUID")
    return value


def _open_content(tree, candidate):
    stored = candidate.copy.stored_relpath
    repo = candidate.copy.repo_id
    if not d._path(stored) or not d._path(repo) or len(PurePosixPath(repo).parts) != 2:
        raise TransferRefusal("SOURCE_PATH_UNSAFE", f"{repo}/{stored}")
    # Catalog stored_relpath is repository-relative, exactly as written by
    # fetch and consumed by restore. Root-level names must never shadow it.
    relative = f"{repo}/{stored}"
    with tree.parent(relative) as (parent, name):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(name, dir_fd=parent)
            if target.startswith("/") or "\\" in target or "\0" in target:
                raise TransferRefusal("SOURCE_PATH_UNSAFE", "absolute or malformed annex link")
            normalized = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
            key = candidate.copy.annex_key
            parts = PurePosixPath(normalized).parts
            if (not d._path(normalized) or not isinstance(key, str) or not d._ANNEX.fullmatch(key)
                    or len(parts) != 7 or parts[:3] != (".git", "annex", "objects")
                    or parts[-2:] != (key, key)):
                raise TransferRefusal("SOURCE_PATH_UNSAFE", "annex target differs from sealed key")
            relative = normalized
        elif not stat.S_ISREG(info.st_mode):
            raise TransferRefusal("SOURCE_PATH_UNSAFE", "source is not a regular file")
    fd = tree.open(relative, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TransferRefusal("SOURCE_PATH_UNSAFE", "source is not a regular file")
        if info.st_size != candidate.copy.stored_bytes:
            raise TransferRefusal("SOURCE_CHANGED", "stored size differs from sealed evidence")
        return fd
    except BaseException:
        os.close(fd)
        raise


class LocalArchiveReader:
    """Internal reader with full per-artifact and lightweight per-read attachment proofs."""

    def __init__(self, attachments, *, observer, max_decode_bytes=64 << 20):
        self.attachments = {label: canonical_attachment(path)
                            for label, path in attachments.items()}
        self.observer = observer
        self.max_decode_bytes = max_decode_bytes

    @contextmanager
    def open(self, candidate):
        from .linux import BoundTree
        label = candidate.drive.drive_label
        if label not in self.attachments:
            raise TransferRefusal("WAITING_SOURCE", f"attach archive {label}")
        path = self.attachments[label]
        with _SourceStack(label) as stack:
            tree = observed = None

            def recheck():
                if tree is not None and observed is not None:
                    self.observer.recheck_attachment(tree, observed)

            try:
                observed = self.observer.observe(path)
                expected = _identity(observed)
                if path != Path(observed.mount_path) / "modelark":
                    raise TransferRefusal("SOURCE_PATH_UNSAFE", "archive must be <mount>/modelark")

                tree = stack.enter_context(BoundTree(path))
                if tree.mount_id != expected[4]:
                    raise TransferRefusal("SOURCE_CHANGED", "observer and source descriptor attachments differ")
                drive = candidate.drive
                annex = _annex_uuid(tree)
                from modelark.serial_identity import serial_for_identity, SerialIdentityUnproven
                try:
                    identity_serial = serial_for_identity(drive.serial, expected[1])
                except SerialIdentityUnproven as exc:
                    raise TransferRefusal("SOURCE_IDENTITY_UNPROVEN", label) from exc
                fingerprint = identity_fingerprint_v1(
                    fs_uuid=expected[0], annex_uuid=annex, serial=identity_serial,
                    filesystem_capacity_bytes=expected[2])
                if ((expected[0], expected[2]) != (drive.fs_uuid, drive.filesystem_capacity_bytes)
                        or (bool(drive.serial) and expected[1] != drive.serial)
                        or annex != drive.annex_uuid or fingerprint != drive.identity_fingerprint):
                    raise TransferRefusal("SOURCE_CHANGED", label)
                stream = stack.enter_context(os.fdopen(_open_content(tree, candidate), "rb"))
                confirmed = self.observer.observe(path)
                if _identity(confirmed) != expected:
                    raise TransferRefusal("SOURCE_CHANGED", label)

                def check():
                    # The archive fence spans this context. Full inventory and
                    # annex identity are checked at artifact-open boundaries;
                    # each read retains kernel attachment/backing-device proof
                    # without spawning a complete inventory for every chunk.
                    self.observer.check_attachment(tree, confirmed)

                check()
                decoded = original_stream(stream, compressed=candidate.copy.compressed,
                                          expected_bytes=candidate.copy.orig_bytes,
                                          max_decode_bytes=self.max_decode_bytes, check=check)
            except OSError as exc:
                raise _translate_confinement(classify_io(exc, recheck, _translate_io(exc, label))) from exc
            except TransferRefusal as exc:
                translated = _translate_confinement(exc)
                if translated is exc:
                    raise
                raise translated from exc
            # Consumer exceptions (particularly destination ENOSPC/EIO) must
            # never be attributed to the source by this context manager.
            yield _SourceErrors(decoded, label, recheck)
