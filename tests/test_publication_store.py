"""Publication-floor migration and shared guards, disposable catalogs only."""
import sqlite3

import pytest

import _pr09_gate1_fixtures as fixtures
from modelark import drive_bootstrap, drive_mutation, publication_store as store
from modelark.catalog_versions import MAX_SUPPORTED_CATALOG_VERSION
from modelark.proposal import graph_write, preview_pure, Refusal
from modelark.publication_policy import PublicationRefused


LIBRARY = "11111111-1111-4111-8111-111111111111"
MAP = "22222222-2222-4222-8222-222222222222"
DRIVE = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def con():
    connection = fixtures.mem_con()
    connection.execute("PRAGMA user_version=8")
    yield connection
    connection.close()


def install(con):
    graph_write(con, lambda c: store._install_schema(c, library_id=LIBRARY, map_uuid=MAP))


def install_v9(con):
    def apply(c):
        for statement in store.DDL_V9:
            c.execute(statement)
        c.execute("INSERT INTO publication_library VALUES(1,?,?,?)", [LIBRARY, MAP, store.PROTOCOL])
        c.execute("PRAGMA user_version=9")
    graph_write(con, apply)


def pending(con, label="d0", operation="fixture-op"):
    # Direct fixture construction only. Production prepare/closure must use the
    # authority-bound publisher, not these deliberately minimal synthetic rows.
    con.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,'PREPARED',1,NULL,NULL,1)",
                [operation, LIBRARY, "fill", "{}", store.digest({})])
    con.execute("INSERT INTO publication_participants VALUES(?,?,?,1,1,?,NULL,NULL)",
                [operation, label, DRIVE, "a" * 64])


@pytest.mark.parametrize("registered", [True, False])
def test_returning_clone_obligation_blocks_only_affected_tree_admission(con, registered):
    install(con)
    # Synthetic completed operation isolates the clone guard; this is not a
    # conversion implementation or a valid coordinator closure receipt.
    con.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,'CLOSED',1,2,'{}',2)",
                ["old-conversion", LIBRARY, "maintenance", "{}", store.digest({})])
    if registered:
        con.execute("INSERT INTO drives(drive_label,annex_uuid) VALUES('returning',?)", [DRIVE])
    con.execute("INSERT INTO publication_clone_obligations VALUES(?,?,1,2,'PENDING',NULL,NULL)",
                [DRIVE, "old-conversion"])
    before = tuple(con.iterdump())
    with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
        store.require_clear(con, tree_change=True)
    if registered:
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
            store.require_clear(con, ["returning"], tree_change=True)
    else:
        store.require_clear(con, ["returning"], tree_change=True)
    store.require_clear(con, ["unrelated"], tree_change=True)
    # Clone tree-policy obligations do not claim that compatible old bytes are bad.
    store.require_clear(con)
    store.require_clear(con, ["returning"])
    assert tuple(con.iterdump()) == before
    con.execute("UPDATE publication_clone_obligations SET state='CLOSED',receipt_json='{}'")
    store.require_clear(con, tree_change=True)


@pytest.mark.parametrize("version", [7, 8])
def test_explicit_additive_floor_preserves_history_and_bumps_once(con, version):
    con.execute(f"PRAGMA user_version={version}")
    con.execute("INSERT INTO models(repo_id) VALUES('org/preserved')")
    old = con.execute("SELECT * FROM models").fetchall()
    revision = con.execute("SELECT planner_revision FROM planner_state").fetchone()[0]
    install(con)
    assert store.library(con) == (LIBRARY, MAP)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 10
    assert con.execute("SELECT * FROM models").fetchall() == old
    assert con.execute("SELECT planner_revision FROM planner_state").fetchone()[0] == revision + 1
    store.require_clear(con)
    # Reader admission is integrated; this is not a conversion implementation.
    assert MAX_SUPPORTED_CATALOG_VERSION == 10


def test_qualified_v9_opens_and_explicit_v10_migration_preserves_action_history(con):
    install_v9(con)
    pending(con)
    con.execute(
        "INSERT INTO publication_actions(operation_id,action_id,kind,intent_json,intent_digest,status) "
        "VALUES('fixture-op','old-action','annex_add','{}',?,'PREPARED')", ["a" * 64])
    before_action = con.execute("SELECT * FROM publication_actions").fetchone()
    before_revision = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert store.library(con) == (LIBRARY, MAP)

    graph_write(con, lambda c: store._install_schema(c, library_id=LIBRARY, map_uuid=MAP))

    assert con.execute("PRAGMA user_version").fetchone() == (10,)
    assert store.library(con) == (LIBRARY, MAP)
    assert con.execute("SELECT * FROM publication_actions").fetchone() == before_action
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0] == before_revision + 1
    con.execute(
        "INSERT INTO publication_actions(operation_id,action_id,kind,intent_json,intent_digest,status) "
        "VALUES('fixture-op','retirement-action','source_retirement','{}',?,'PREPARED')", ["b" * 64])


def test_v9_refuses_maintenance_before_reading_caller_workset(con):
    install_v9(con)

    class Scope:
        connection = con

    with pytest.raises(PublicationRefused, match="PUBLICATION_SCHEMA_UPGRADE_REQUIRED"):
        store.prepare_maintenance_operation(
            Scope(), operation_id="not-even-a-uuid", profile_digest="invalid",
            batch_files={}, before_state={})


def test_migration_rollback_restores_floor_schema_and_revision(con):
    before = con.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()

    def fail(c):
        store._install_schema(c, library_id=LIBRARY, map_uuid=MAP)
        raise OSError("injected failure")

    with pytest.raises(OSError):
        graph_write(con, fail)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 8
    assert con.execute("SELECT planner_revision FROM planner_state").fetchone()[0] == 0
    assert con.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before


def test_raw_transaction_cannot_authorize_migration(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="AUTHORITY_MISSING"):
            store._install_schema(con, library_id=LIBRARY, map_uuid=MAP)
        assert con.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        con.execute("ROLLBACK")


def test_old_empty_catalog_has_no_obligation_but_floor_downgrade_refuses(con):
    store.require_clear(con)
    install(con)
    con.execute("PRAGMA user_version=8")
    with pytest.raises(PublicationRefused, match="FLOOR_INVALID"):
        store.require_clear(con)


@pytest.mark.parametrize("corruption", [
    "DROP TABLE publication_files", "DELETE FROM publication_library",
    "UPDATE publication_library SET map_uuid='unknown'", "PRAGMA user_version=11",
])
def test_partial_unknown_or_unbound_store_is_not_empty_success(con, corruption):
    install(con)
    con.execute(corruption)
    with pytest.raises(PublicationRefused):
        store.require_clear(con, ["d0"])


def test_selected_obligation_blocks_source_and_ordinary_closure(con):
    install(con)
    pending(con)
    for labels in (None, ["d0"], ["d0", "d1"]):
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED") as error:
            store.require_clear(con, labels)
        assert error.value.evidence["operation_ids"] == ["fixture-op"]
    store.require_clear(con, ["d1"])
    # Fail before looking up drive facts or calling inventory. Ordinary recovery
    # cannot supply a token/boolean to bypass the common guard.
    with pytest.raises(drive_mutation.DriveMutationRefused, match="MAINTENANCE_REQUIRED"):
        drive_mutation._publish_anchor_locked(con, "d0", 1, 1, None, "now")
    with pytest.raises(drive_mutation.DriveMutationRefused, match="MAINTENANCE_REQUIRED"):
        drive_mutation._advance_one(con, "d0", "test")
    with pytest.raises(drive_mutation.DriveMutationRefused, match="MAINTENANCE_REQUIRED"):
        drive_bootstrap.reconcile_drive(con, "d0", now="now", dedicated=True)
    with pytest.raises(Refusal, match="MAINTENANCE_REQUIRED"):
        preview_pure(con)
    assert con.execute("SELECT state FROM publication_operations").fetchone()[0] == "PREPARED"


def test_map_only_registration_blocks_clean_anchor_and_ordinary_recovery(con):
    install(con)
    con.execute("INSERT INTO publication_operations VALUES(?,?,?,?,?,'PREPARED',1,NULL,NULL,1)",
                ["reg-op", LIBRARY, "registration", "{}", store.digest({})])
    for labels in (None, ["d0"], ["d1"]):
        with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED") as error:
            store.require_clear(con, labels)
        assert error.value.evidence["operation_ids"] == ["reg-op"]
    with pytest.raises(drive_mutation.DriveMutationRefused, match="MAINTENANCE_REQUIRED"):
        drive_mutation._publish_anchor_locked(con, "d0", 1, 1, None, "now")
    with pytest.raises(drive_mutation.DriveMutationRefused, match="MAINTENANCE_REQUIRED"):
        drive_bootstrap.reconcile_drive(con, "d0", now="now", dedicated=True)
    with pytest.raises(Refusal, match="MAINTENANCE_REQUIRED"):
        preview_pure(con)


def test_missing_participant_is_a_corrupt_record_not_zero_work(con):
    install(con)
    pending(con)
    con.execute("DELETE FROM publication_participants")
    with pytest.raises(PublicationRefused, match="RECORD_UNPROVEN"):
        store.require_clear(con, ["d0"])


def test_query_only_guard_does_not_mutate_or_create_schema(con):
    install(con)
    pending(con)
    total = con.total_changes
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    with pytest.raises(PublicationRefused, match="MAINTENANCE_REQUIRED"):
        store.require_clear(con, ["d0"])
    assert con.total_changes == total
    assert con.in_transaction
    con.execute("ROLLBACK")


def test_catalog_io_error_is_not_permission(con):
    con.close()
    with pytest.raises(PublicationRefused, match="SCHEMA_UNPROVEN"):
        store.require_clear(con)


def test_empty_v9_table_names_without_contract_are_rejected():
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA user_version=9")
        for name in store.TABLES:
            con.execute(f"CREATE TABLE {name}(meaningless TEXT)")
        with pytest.raises(PublicationRefused):
            store.require_clear(con)
    finally:
        con.close()


@pytest.mark.parametrize("corruption", [
    "ALTER TABLE publication_files ADD COLUMN unbound_fact TEXT",
    "DROP INDEX publication_open_operations",
    "DROP INDEX publication_participant_drive",
])
def test_schema_definition_drift_is_not_an_empty_guard_success(con, corruption):
    install(con)
    con.execute(corruption)
    with pytest.raises(PublicationRefused, match="SCHEMA_UNQUALIFIED"):
        store.require_clear(con)
