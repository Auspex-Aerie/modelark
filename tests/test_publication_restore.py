"""Publication-aware restore admission is fenced, fresh and always read-only."""
from contextlib import contextmanager

import pytest

from modelark import drive_fence, drive_mutation, publication_locks, restore
from test_publication_lifecycle import con as publication_connection  # noqa: F401
from test_publication_lifecycle import MAP, observations, prepare


@pytest.fixture(name="con")
def _catalog_connection(request):
    return request.getfixturevalue("publication_connection")


def rows(con):
    for label in ("d0", "d1"):
        con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,compressed,stored_name,stored_relpath) "
                    "VALUES('org/a','model.safetensors',?,0,'model.safetensors','model.safetensors')", [label])
    return restore._rows(con, "org/a")["model.safetensors"]


def clean(con, label):
    with publication_locks.hold(con, [label], map_uuid=MAP) as scope:
        observation = observations(scope)[label]
        scope.write(lambda c: drive_mutation._publish_anchor_locked(c, label, 1, 2, observation, "test observation"))


def test_pending_copy_cannot_be_read_or_retrieved_but_unrelated_clean_copy_can(con):
    first, second = rows(con)
    clean(con, "d1")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        prepare(scope)
    with pytest.raises(restore.RestoreError, match="MAINTENANCE_REQUIRED"):
        with restore._publication_source(con, "org/a", first):
            pytest.fail("blocked copy reached the payload reader")
    total = con.total_changes
    with restore._publication_source(con, "org/a", second) as may_mutate:
        assert may_mutate is False
    assert con.total_changes == total and not con.in_transaction


def test_clean_v9_restore_does_not_acquire_controller_or_map_after_drive(con, monkeypatch):
    first, _ = rows(con)
    clean(con, "d0")
    def forbidden(*args, **kwargs):
        pytest.fail("reader attempted reverse controller/map lock acquisition")
    monkeypatch.setattr(drive_fence, "hold_controller", forbidden)
    monkeypatch.setattr(drive_fence, "hold_map", forbidden)
    with restore._publication_source(con, "org/a", first) as may_mutate:
        assert may_mutate is False


def test_restore_rechecks_full_consumed_row_in_fresh_snapshot(con):
    first, _ = rows(con)
    clean(con, "d0")
    con.execute("UPDATE archived SET stored_relpath='changed' WHERE drive_label='d0'")
    with pytest.raises(restore.RestoreError, match="SOURCE_ROW_CHANGED"):
        with restore._publication_source(con, "org/a", first):
            pytest.fail("stale row reached payload reader")


def test_restore_holds_physical_fence_for_entire_reader_context(con, monkeypatch):
    first, _ = rows(con)
    clean(con, "d0")
    active = []
    original = drive_fence.hold_drives_sorted
    @contextmanager
    def hold(*args, **kwargs):
        with original(*args, **kwargs) as result:
            active.append(True)
            try:
                yield result
            finally:
                active.pop()
    monkeypatch.setattr(drive_fence, "hold_drives_sorted", hold)
    with restore._publication_source(con, "org/a", first):
        assert active == [True]
    assert active == []
