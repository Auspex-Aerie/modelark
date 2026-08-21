"""INC-034 Gate-1 contracts — dest-main no-clobber at the publish syscall.

Contracts only. Production is unchanged, so c01 must stay red until Gate 2
replaces ``os.replace`` with an atomic no-clobber primitive.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from modelark.core import db
from test_dec053_054_gate2_remediation import _rehearse_ok


_SIDECARS = ("-wal", "-shm", "-journal")
_SENTINEL = b"inc034-rival-main-sentinel"


def test_c01_late_rival_main_at_publish_syscall_is_refused_and_preserved(tmp_path):
    """Rival dest main injected at replace/link must be refused and kept.

    A remigrate-time inject is not this pin: a second exists() would green
    that hook. Inject at the publish syscall so only no-clobber greens.
    """
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    injected = {"yes": False}
    real_replace = os.replace
    real_link = os.link
    real_fd = db._link_fd_no_clobber

    def _maybe_inject(dst):
        if Path(dst) == dest_catalog:
            dest_catalog.write_bytes(_SENTINEL)
            injected["yes"] = True

    def replace_hook(src, dst, *args, **kwargs):
        _maybe_inject(dst)
        return real_replace(src, dst, *args, **kwargs)

    def link_hook(src, dst, *args, **kwargs):
        _maybe_inject(dst)
        return real_link(src, dst, *args, **kwargs)

    def fd_hook(fd, dest_cat, *args, **kwargs):
        _maybe_inject(dest_cat)
        return real_fd(fd, dest_cat)

    error = None
    published = None
    with mock.patch.object(db.os, "replace", side_effect=replace_hook), mock.patch.object(
        db.os, "link", side_effect=link_hook
    ), mock.patch.object(db, "_link_fd_no_clobber", side_effect=fd_hook):
        try:
            published = db.publish_provenance_migration(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True
            )
        except RuntimeError as exc:
            error = exc

    assert injected["yes"] is True, "contract must inject at the publish syscall"
    assert error is not None, (
        "late dest main at os.replace/os.link must be refused, "
        f"got published={published!r}"
    )
    assert published is None or published.get("status") != "ok"
    assert dest_catalog.is_file(), "sentinel dest main must remain"
    assert dest_catalog.read_bytes() == _SENTINEL, (
        "publish must not clobber the just-injected dest main"
    )
    for suffix in _SIDECARS:
        side = dest_catalog.with_name(dest_catalog.name + suffix)
        assert not side.exists(), f"c01 must not create dest sidecar {side.name}"
