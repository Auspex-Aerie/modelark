"""INC-035 Gate-1 contracts — staging pathname swap at publish.

Contracts only. Production is unchanged, so c01 stays red until Gate 2
fd-links the validated inode instead of ``os.link(staging_path, dest)``.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from modelark.core import db
from test_dec053_054_gate2_remediation import _rehearse_ok


_SENTINEL = b"inc035-staging-pathname-sentinel"


def test_c01_staging_pathname_swap_at_link_must_not_publish_sentinel(tmp_path):
    """Rival inode on the staging *name* at publish must not become dest bytes."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    staging = dest / ".catalog.sqlite.publish-staging"
    injected = {"yes": False, "after_link": None}
    real_link = os.link

    def link_hook(src, dst, *args, **kwargs):
        dst_p = Path(dst)
        if dst_p.resolve() == dest_catalog.resolve() and staging.exists():
            rival = tmp_path / "inc035-rival"
            rival.write_bytes(_SENTINEL)
            os.replace(str(rival), str(staging))
            injected["yes"] = True
        out = real_link(src, dst, *args, **kwargs)
        if dst_p.resolve() == dest_catalog.resolve() and dest_catalog.is_file():
            injected["after_link"] = dest_catalog.read_bytes()
        return out

    error = None
    published = None
    with mock.patch.object(db.os, "link", side_effect=link_hook):
        try:
            published = db.publish_provenance_migration(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True
            )
        except RuntimeError as exc:
            error = exc

    assert injected["yes"] is True, (
        "contract must swap the staging pathname at the publish link syscall"
    )
    assert injected["after_link"] != _SENTINEL, (
        "publish must not install the swapped staging pathname as dest at link; "
        f"after_link={injected['after_link']!r} error={error!r} published={published!r}"
    )
    if published is not None and published.get("status") == "ok":
        assert dest_catalog.is_file()
        assert dest_catalog.read_bytes() != _SENTINEL
