"""Mutation refuses fact drift even when the saved null-serial hash still matches."""
from contextlib import contextmanager

import pytest

from modelark import drive_fence, drive_mutation as dm
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.drive_identity import FenceIdentity
from test_drive_mutation_envelope import _catalog, _proven_drive, _observer, _reconciler


def seed(con, label="drive-00"):
    fp = identity_fingerprint_v1(fs_uuid="fs-a", annex_uuid="annex-a", serial=None,
                                  filesystem_capacity_bytes=1000)
    _proven_drive(con, label, fp=fp)
    return FenceIdentity("fs-a", "annex-a", "serial-a", 1000, 1, fp)


@pytest.mark.parametrize("field", ["serial", "fs_uuid", "annex_uuid"])
@pytest.mark.parametrize("phase", ["after_lock", "before_dirty", "during_body", "before_anchor"])
def test_fact_drift_cannot_dirty_or_publish_unheld_identity(tmp_path, monkeypatch, field, phase):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    with _catalog(tmp_path) as con:
        seed(con)

        def change():
            con.execute(f"UPDATE drives SET {field}='changed'")

        original_lock = drive_fence.hold_drives_sorted

        @contextmanager
        def lock(*args, **kwargs):
            with original_lock(*args, **kwargs) as handles:
                if phase == "after_lock":
                    change()
                yield handles

        monkeypatch.setattr(drive_fence, "hold_drives_sorted", lock)
        original_immediate = dm._immediate
        commits = 0

        def immediate(*args, **kwargs):
            nonlocal commits
            commits += 1
            if (phase == "before_dirty" and commits == 1
                    or phase == "before_anchor" and commits == 2):
                change()
            return original_immediate(*args, **kwargs)

        monkeypatch.setattr(dm, "_immediate", immediate)
        with pytest.raises(dm.DriveMutationRefused, match="DRIVE_IDENTITY_UNPROVEN"):
            with dm.drive_mutation(con, ["drive-00"], "alias-cas", observe=_observer(con),
                                   reconcile=_reconciler(con), now="2026-09-09"):
                if phase in {"after_lock", "before_dirty"}:
                    pytest.fail("mutation body must not start")
                if phase == "during_body":
                    change()
        expected_dirty = int(phase in {"during_body", "before_anchor"})
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == expected_dirty
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_multiple_labels_sharing_identity_take_one_global_alias_set(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    with _catalog(tmp_path) as con:
        facts = seed(con, "z")
        seed(con, "a")
        original = drive_fence.hold_drives_sorted
        seen = []

        @contextmanager
        def capture(keys, **kwargs):
            seen.append(tuple(keys))
            with original(keys, **kwargs) as handles:
                assert len(handles) == 2
                yield handles

        monkeypatch.setattr(drive_fence, "hold_drives_sorted", capture)
        with dm.drive_mutation(con, iter(["z", "a", "z"]), "duplicate-aliases",
                               observe=_observer(con), reconcile=_reconciler(con),
                               now="2026-09-09", blocking=False):
            pass
        assert seen == [facts.lock_keys()]
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 2
