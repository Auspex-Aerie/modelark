"""Storage/authority integration under real disposable locks and SQLite transactions.

IO proof payloads here are explicitly synthetic: these tests exercise record
binding/phase/CAS/closure, not the still-unfinished production IO proof factories.
"""
from contextlib import contextmanager
import os
import sqlite3
import subprocess
import sys

import pytest

import _pr09_gate1_fixtures as fixtures
from modelark import drive_fence, drive_mutation, publication_locks, publication_store as store
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.proposal import graph_write
from modelark.publication_policy import PublicationRefused

LIBRARY = "11111111-1111-4111-8111-111111111111"
MAP = "22222222-2222-4222-8222-222222222222"
OP = "33333333-3333-4333-8333-333333333333"
BATCH = "44444444-4444-4444-8444-444444444444"
FILE = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    source = fixtures.mem_con()
    fixtures.seed_plan_selection(source, repos=("org/a",))
    for number, label in enumerate(("d0", "d1")):
        annex = f"66666666-6666-4666-8666-66666666666{number}"
        fingerprint = identity_fingerprint_v1(fs_uuid=label + "-fs", annex_uuid=annex,
                                               serial=None, filesystem_capacity_bytes=10**12)
        source.execute("UPDATE drives SET annex_uuid=?,identity_fingerprint=?,write_generation=2 WHERE drive_label=?",
                       [annex, fingerprint, label])
        source.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                       "VALUES(?,1,2,'fixture dirty generation')", [label])
    source.execute("PRAGMA user_version=8")
    target = sqlite3.connect(tmp_path / "catalog.sqlite", isolation_level=None)
    source.backup(target)
    source.close()
    target.execute("PRAGMA foreign_keys=ON")
    graph_write(target, lambda c: store._install_schema(c, library_id=LIBRARY, map_uuid=MAP))
    yield target
    target.close()


def prepare(scope):
    return scope.write(lambda _: store.prepare_operation(
        scope, operation_id=OP, kind="fill", profile_digest="a" * 64,
        batch_files={BATCH: [FILE]}, before_state={"fixture": "no actual archive"}))


def file_intent(scope):
    return scope.write(lambda _: store.prepare_file(
        scope, operation_id=OP, batch_id=BATCH, file_id=FILE, intent={"fixture": "captured row pair"}))


def advance(scope, phase, callback=None):
    return scope.write(lambda _: store.advance_file(
        scope, operation_id=OP, file_id=FILE, phase=phase,
        proof={"fixture": phase}, catalog_cas=callback))


def propagate(scope):
    return scope.write(lambda _: store.propagate_batch(
        scope, operation_id=OP, batch_id=BATCH, map_proof={"fixture": "map receipt"}))


def observations(scope):
    return {label: drive_mutation.Observation(True, 1000, identity.filesystem_capacity_bytes,
                                             identity.identity_fingerprint, "fixture identity", "fixture fence")
            for label, identity in scope.identities.items()}


def close(scope, values=None):
    return scope.write(lambda _: store.close_operation(
        scope, operation_id=OP, observations=observations(scope) if values is None else values,
        inventory_proofs={label: {"fixture": "complete inventory"} for label in scope.identities}, now="fixture time"))


def published(scope):
    prepare(scope)
    file_intent(scope)
    advance(scope, "LOCAL_VERIFIED")
    advance(scope, "TREE_VERIFIED")
    advance(scope, "CATALOG_PUBLISHED", lambda con, intent: None)


def test_full_record_lifecycle_never_closes_from_per_file_or_batch_success(con):
    with publication_locks.hold(con, ["d0", "d1"], map_uuid=MAP) as scope:
        published(scope)
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
            store.require_clear(con, ["d0"])
        with pytest.raises(PublicationRefused, match="BATCHES_INCOMPLETE"):
            close(scope)
        propagate(scope)
        with pytest.raises(drive_mutation.DriveMutationRefused, match="MAINTENANCE_REQUIRED"):
            drive_mutation._publish_anchor_locked(con, "d0", 1, 1, observations(scope)["d0"], "now")
        close(scope)
        store.require_clear(con)
        assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=2").fetchone()[0] == 2
        assert con.execute("SELECT created_revision,closed_revision,last_revision FROM publication_operations").fetchone() == (2, 8, 8)
        assert con.execute("SELECT committed_revision FROM publication_files").fetchone()[0] == 6
        assert con.execute("SELECT committed_revision FROM publication_batches").fetchone()[0] == 7
        with pytest.raises(PublicationRefused, match="NOT_ACTIVE"):
            close(scope)


def test_failed_second_anchor_rolls_back_first_anchor_and_closure(con):
    with publication_locks.hold(con, ["d0", "d1"], map_uuid=MAP) as scope:
        published(scope)
        propagate(scope)
        values = observations(scope)
        values["d1"] = drive_mutation.Observation(False, 1000, 10**12, "wrong", "", "")
        with pytest.raises(drive_mutation.DriveMutationRefused):
            close(scope, values)
        assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=2").fetchone()[0] == 0
        assert con.execute("SELECT state,last_revision FROM publication_operations").fetchone() == ("PREPARED", 7)
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
            store.require_clear(con)
        close(scope)


def test_io_scope_checks_current_binding_without_a_write_or_open_transaction(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        prepare(scope)
        before = (store._revision(con), con.total_changes)
        scope.require_io()
        assert not con.in_transaction
        assert (store._revision(con), con.total_changes) == before
        graph_write(con, lambda _: None)  # Unrelated graph write advances the revision.
        with pytest.raises(PublicationRefused, match="OPERATION_STALE"):
            scope.require_io()
        assert not con.in_transaction
    with pytest.raises(PublicationRefused, match="AUTHORITY_MISSING"):
        scope.require_io()


def test_io_scope_cannot_run_in_or_rollback_callers_transaction(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        prepare(scope)
        con.execute("BEGIN")
        try:
            with pytest.raises(PublicationRefused, match="IO_TRANSACTION_ACTIVE"):
                scope.require_io()
            assert con.in_transaction
        finally:
            con.rollback()
        with pytest.raises(PublicationRefused, match="IO_TRANSACTION_ACTIVE"):
            scope.write(lambda _: scope.require_io())
        assert not con.in_transaction
        scope.require_io()


def test_scope_participants_and_authority_fields_cannot_be_rewritten(con):
    with publication_locks.hold(con, ["d0", "d1"], map_uuid=MAP) as scope:
        with pytest.raises(TypeError):
            del scope.identities["d1"]
        with pytest.raises(AttributeError):
            scope.identities = {}
        with pytest.raises(AttributeError):
            scope.writer = None
        with pytest.raises(AttributeError):
            scope._authority.library = (LIBRARY, LIBRARY)
        assert set(scope.identities) == {"d0", "d1"}
        scope.require_io()


@pytest.mark.parametrize("tamper", [
    "UPDATE publication_files SET committed_revision=0",
    "UPDATE publication_files SET committed_revision=999999",
    "UPDATE publication_batches SET intent_digest='wrong'",
])
def test_closure_rechecks_batch_intent_and_file_revision_markers(con, tamper):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        published(scope)
        propagate(scope)
        con.execute(tamper)
        with pytest.raises(PublicationRefused):
            close(scope)
        assert con.execute("SELECT state FROM publication_operations").fetchone()[0] == "PREPARED"
        assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE generation=2").fetchone()[0] == 0


def test_catalog_cas_error_rolls_back_data_phase_and_revision(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        prepare(scope)
        file_intent(scope)
        advance(scope, "LOCAL_VERIFIED")
        advance(scope, "TREE_VERIFIED")

        def bad(c, intent):
            c.execute("INSERT INTO models(repo_id) VALUES('fixture/rollback')")
            raise OSError("injected catalog CAS failure")

        with pytest.raises(OSError):
            advance(scope, "CATALOG_PUBLISHED", bad)
        assert not con.execute("SELECT 1 FROM models WHERE repo_id='fixture/rollback'").fetchone()
        assert con.execute("SELECT phase,committed_revision FROM publication_files").fetchone() == ("TREE_VERIFIED", None)
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone()[0] == 5
        advance(scope, "CATALOG_PUBLISHED", lambda c, intent: None)


@pytest.mark.parametrize("phase", ["TREE_VERIFIED", "CATALOG_PUBLISHED", "CLOSED", "PROPAGATED"])
def test_no_phase_skipping(con, phase):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        prepare(scope)
        file_intent(scope)
        with pytest.raises(PublicationRefused):
            advance(scope, phase, lambda c, intent: None)


def test_bound_revision_change_refuses_continuation_without_clearing_obligation(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        prepare(scope)
        graph_write(con, lambda _: None)  # unrelated catalog write, not implicit adoption
        with pytest.raises(PublicationRefused, match="STALE"):
            file_intent(scope)
        store.require_clear(con, ["d1"])
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
            store.require_clear(con, ["d0"])


def test_participant_cannot_detach_and_become_optional(con):
    with publication_locks.hold(con, ["d0", "d1"], map_uuid=MAP) as scope:
        prepare(scope)
        con.execute("UPDATE drives SET identity_epoch=2 WHERE drive_label='d1'")
        with pytest.raises(PublicationRefused, match="IDENTITY_CHANGED"):
            file_intent(scope)
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
            store.require_clear(con, ["d0"])


def test_scope_cannot_be_used_after_exit_or_on_another_connection(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        other = sqlite3.connect(":memory:", isolation_level=None)
        try:
            with pytest.raises(PublicationRefused, match="CONNECTION_MISMATCH"):
                scope.require_transaction(other)
        finally:
            other.close()
    with pytest.raises(PublicationRefused, match="FENCE_AUTHORITY_MISSING"):
        prepare(scope)


def test_locks_refuse_inside_transaction_before_any_lock_acquisition(con, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("lock waited inside SQLite transaction")
    monkeypatch.setattr(drive_fence, "hold_controller", forbidden)
    con.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(PublicationRefused, match="LOCK_ORDER_TRANSACTION_ACTIVE"):
            with publication_locks.hold(con, ["d0"], map_uuid=MAP):
                pass
    finally:
        con.execute("ROLLBACK")


def test_lock_order_and_all_child_descriptors(con, monkeypatch):
    calls = []
    for name in ("hold_controller", "hold_map", "hold_drives_sorted"):
        original = getattr(drive_fence, name)

        @contextmanager
        def wrapped(*args, _name=name, _original=original, **kwargs):
            assert not con.in_transaction
            calls.append(_name)
            with _original(*args, **kwargs) as result:
                yield result

        monkeypatch.setattr(drive_fence, name, wrapped)
    with publication_locks.hold(con, ["d1", "d0"], map_uuid=MAP) as scope:
        assert len(scope.child_fence_fds) == 4
        prepare(scope)
    assert calls == ["hold_controller", "hold_map", "hold_drives_sorted"]


def test_map_and_controller_remain_fenced_in_inherited_child(con):
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    child = None
    try:
        with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
            child = subprocess.Popen(
                [sys.executable, "-c", "import os,sys; os.write(int(sys.argv[1]),b'1'); os.read(int(sys.argv[2]),1)",
                 str(ready_w), str(release_r)], pass_fds=(*scope.child_fence_fds, ready_w, release_r))
            os.close(ready_w)
            ready_w = None
            assert os.read(ready_r, 1) == b"1"
        with pytest.raises(drive_fence.FenceUnavailable):
            with drive_fence.hold_map(MAP, blocking=False):
                pass
        catalog = con.execute("PRAGMA database_list").fetchone()[2]
        with pytest.raises(drive_fence.FenceUnavailable):
            with drive_fence.hold_controller(catalog, blocking=False):
                pass
    finally:
        os.write(release_w, b"1")
        for fd in (ready_r, ready_w, release_r, release_w):
            if fd is not None:
                os.close(fd)
        if child is not None:
            child.wait()
    assert child.returncode == 0
    with drive_fence.hold_map(MAP, blocking=False):
        pass


def test_conversion_and_unselected_files_are_disabled(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="CONVERSION_DISABLED"):
            graph_write(con, lambda _: store.prepare_operation(
                scope, operation_id=OP, kind="maintenance", profile_digest="a" * 64,
                batch_files={BATCH: [FILE]}, before_state={}))
        prepare(scope)
        with pytest.raises(PublicationRefused, match="FILE_UNSELECTED"):
            graph_write(con, lambda _: store.prepare_file(scope, operation_id=OP, batch_id=BATCH,
                                                        file_id=MAP, intent={}))


def test_scoped_progress_write_advances_operation_revision_without_faking_a_file_phase(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        prepare(scope)
        scope.write(lambda c: c.execute("INSERT INTO models(repo_id) VALUES('fixture/progress')"))
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone()[0] == 3
        assert con.execute("SELECT count(*) FROM publication_files").fetchone()[0] == 0
        file_intent(scope)
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone()[0] == 4


def test_session_adapter_keeps_original_dirty_owner_and_one_revision_per_step(con):
    con.execute("INSERT INTO placement_proposals(proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
                "mutation_kind,serializer_version) VALUES('fixture-proposal','ark',1,'approved',?,'adopt_current','1')",
                ["a" * 64])
    con.execute("INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,controller_identity,"
                "worker_identity,state,bound_planner_revision,fencing_token) "
                "VALUES('fixture-session','ark','fixture-proposal','fixture','fixture','running',1,2)")
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code,"
                "owner_session_id,owner_fencing_token) VALUES('d0',1,3,'fill','fixture-session',2)")
    con.execute("UPDATE drives SET write_generation=3 WHERE drive_label='d0'")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP, session_id="fixture-session", fencing_token=2) as scope:
        published(scope)
        scope.write(lambda c: c.execute("INSERT INTO models(repo_id) VALUES('fixture/progress')"))
        assert con.execute("SELECT bound_planner_revision FROM execution_sessions").fetchone()[0] == 7
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone()[0] == 7
        from modelark.proposal import GraphResult
        assert scope.write(lambda _: GraphResult(proven_noop=True)).proven_noop
        scope.require_io()
        assert con.execute("SELECT bound_planner_revision FROM execution_sessions").fetchone()[0] == 7
        assert con.execute("SELECT last_revision FROM publication_operations").fetchone()[0] == 7
        propagate(scope)
        close(scope)
        assert con.execute("SELECT bound_planner_revision FROM execution_sessions").fetchone()[0] == 9
        assert con.execute("SELECT closed_revision FROM publication_operations").fetchone()[0] == 9
        assert con.execute("SELECT owner_session_id,owner_fencing_token FROM drive_dirty_generations "
                           "WHERE drive_label='d0' AND generation=3").fetchone() == ("fixture-session", 2)


def test_reopen_requires_exact_durable_operation_and_participants(con):
    with publication_locks.hold(con, ["d0", "d1"], map_uuid=MAP) as scope:
        prepare(scope)
    path = con.execute("PRAGMA database_list").fetchone()[2]
    resumed = sqlite3.connect(path, isolation_level=None)
    try:
        with publication_locks.hold(resumed, ["d0"], map_uuid=MAP, operation_id=OP) as partial:
            with pytest.raises(PublicationRefused, match="BINDING_CHANGED"):
                file_intent(partial)
        with publication_locks.hold(resumed, ["d0", "d1"], map_uuid=MAP, operation_id=OP) as exact:
            file_intent(exact)
    finally:
        resumed.close()


def test_changed_proof_with_recomputed_hash_does_not_match_its_successor(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        published(scope)
        import json
        raw = con.execute("SELECT local_proof_json FROM publication_files").fetchone()[0]
        changed = json.loads(raw)
        changed["proof"] = {"fixture": "different evidence"}
        con.execute("UPDATE publication_files SET local_proof_json=?,local_proof_digest=?",
                    [store.canonical(changed), store.digest(changed)])
        with pytest.raises(PublicationRefused, match="PROOF_CHAIN_CHANGED"):
            propagate(scope)


def test_failed_prepare_restores_memory_binding_and_database(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        def fail(c):
            store.prepare_operation(scope, operation_id=OP, kind="fill", profile_digest="a" * 64,
                                    batch_files={BATCH: [FILE]}, before_state={})
            raise OSError("injected post-prepare failure")
        with pytest.raises(OSError):
            scope.write(fail)
        assert scope.operation_id is None
        assert not con.execute("SELECT 1 FROM publication_operations").fetchone()
        prepare(scope)
