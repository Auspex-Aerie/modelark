"""Real catalog CAS; physical and committed-tree receipts remain synthetic here."""
import copy

import pytest

from modelark import publication_catalog as catalog, publication_locks, publication_store as store
from modelark.publication_policy import PublicationRefused
from modelark.catalog_write_context import WriteContextError
from modelark.core import db
from test_publication_lifecycle import con as publication_connection  # noqa: F401
from test_publication_lifecycle import MAP, OP, BATCH, FILE, prepare, advance


KEY = "SHA256-s100--" + "1" * 64


@pytest.fixture(name="con")
def _catalog_connection(request):
    return request.getfixturevalue("publication_connection")


def intended(scope, *, replica=True, source=None):
    before = catalog.capture(scope, repo_id="org/a", rfilename="model.safetensors", drive_label="d0",
                             source_drive=source)
    archive = {**before["key"], "stored_name": "model.safetensors", "stored_relpath": "model.safetensors",
               "orig_sha256": "1" * 64, "znn_sha256": None, "orig_bytes": 100, "stored_bytes": 100,
               "compressed": 0, "annex_key": KEY, "verified_at": "captured-ingestion-time",
               "orig_sha256_provenance": "hub_confirmed"}
    copy_row = {**before["key"], "annex_key": KEY, "present": 1,
                "verified_at": "captured-verification-time", "added_at": "captured-copy-time"} if replica else None
    return catalog.intended_pair(before, archived=archive, replicas=copy_row)


def ready(scope, **kw):
    prepare(scope)
    def persist(_):
        value = intended(scope, **kw)
        store.prepare_file(scope, operation_id=OP, batch_id=BATCH, file_id=FILE, intent={"catalog_pair": value})
        return value
    value = scope.write(persist)
    advance(scope, "LOCAL_VERIFIED")
    advance(scope, "TREE_VERIFIED")
    return value


def publish(scope):
    return advance(scope, "CATALOG_PUBLISHED", lambda c, intent: catalog.compare_and_swap(scope, intent["catalog_pair"]))


@pytest.mark.parametrize("replica", [True, False])
def test_atomic_complete_pair_and_explicit_absence(con, replica):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        value = ready(scope, replica=replica)
        publish(scope)
        assert con.execute("SELECT orig_sha256,annex_key,verified_at FROM archived").fetchone() == (
            "1" * 64, KEY, "captured-ingestion-time")
        assert con.execute("SELECT count(*) FROM replicas").fetchone()[0] == int(replica)
        assert con.execute("SELECT committed_revision FROM publication_files").fetchone()[0] == 6
        assert con.execute("SELECT planner_revision FROM planner_state").fetchone()[0] == 6
        assert value["before"]["archived"] is None


@pytest.mark.parametrize("tamper", [
    "UPDATE files SET quant='changed'",
    "INSERT INTO replicas(repo_id,rfilename,drive_label,annex_key,present) VALUES('org/a','model.safetensors','d0','other',0)",
    "INSERT INTO archived(repo_id,rfilename,drive_label,compressed) VALUES('org/a','model.safetensors','d0',0)",
])
def test_every_before_fact_and_absence_is_compared(con, tamper):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        ready(scope)
        # Even a rogue writer which neglected the planner revision cannot evade
        # the row-pair CAS by changing a non-key fact or an expected absent row.
        con.execute(tamper)
        with pytest.raises(PublicationRefused, match="PAIR_STALE"):
            publish(scope)
        assert con.execute("SELECT phase,committed_revision FROM publication_files").fetchone() == ("TREE_VERIFIED", None)
        assert con.execute("SELECT planner_revision FROM planner_state").fetchone()[0] == 5


def test_second_write_failure_rolls_back_first_pair_write_and_marker(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        ready(scope)
        con.execute("CREATE TRIGGER corrupt_copy AFTER INSERT ON replicas BEGIN UPDATE replicas SET present=0; END")
        with pytest.raises(PublicationRefused, match="POSTCONDITION_FAILED"):
            publish(scope)
        assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM replicas").fetchone()[0] == 0
        assert con.execute("SELECT phase,last_revision FROM publication_files,publication_operations").fetchone() == (
            "TREE_VERIFIED", 5)


def test_preserves_existing_timestamps_and_other_drive_copy(con):
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,compressed,verified_at) "
                "VALUES('org/a','model.safetensors','d0',0,'historical-ingestion')")
    con.execute("INSERT INTO replicas(repo_id,rfilename,drive_label,present,added_at) "
                "VALUES('org/a','model.safetensors','d0',0,'historical-copy')")
    con.execute("INSERT INTO replicas(repo_id,rfilename,drive_label,annex_key,present) "
                "VALUES('org/a','model.safetensors','d1','other',0)")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        prepare(scope)
        def persist(_):
            value = intended(scope)
            value["after"]["archived"]["verified_at"] = value["before"]["archived"]["verified_at"]
            value["after"]["replicas"]["added_at"] = value["before"]["replicas"]["added_at"]
            store.prepare_file(scope, operation_id=OP, batch_id=BATCH, file_id=FILE, intent={"catalog_pair": value})
        scope.write(persist)
        advance(scope, "LOCAL_VERIFIED")
        advance(scope, "TREE_VERIFIED")
        publish(scope)
        assert con.execute("SELECT verified_at FROM archived").fetchone()[0] == "historical-ingestion"
        assert con.execute("SELECT added_at FROM replicas WHERE drive_label='d0'").fetchone()[0] == "historical-copy"
        assert con.execute("SELECT annex_key,present FROM replicas WHERE drive_label='d1'").fetchone() == ("other", 0)


def test_replica_source_whole_pair_bound_and_not_modified(con):
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,compressed) VALUES('org/a','model.safetensors','d1',0)")
    with publication_locks.hold(con, ["d0", "d1"], map_uuid=MAP) as scope:
        ready(scope, source="d1")
        con.execute("UPDATE archived SET orig_sha256_provenance='legacy_unknown' WHERE drive_label='d1'")
        with pytest.raises(PublicationRefused, match="PAIR_STALE"):
            publish(scope)
        assert con.execute("SELECT count(*) FROM archived WHERE drive_label='d0'").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    lambda value: value["after"]["archived"].update(drive_label="d1"),
    lambda value: value["after"]["replicas"].update(annex_key="other"),
    lambda value: value["after"]["archived"].pop("orig_sha256_provenance"),
    lambda value: value["after"]["archived"].update(stored_relpath="../escape"),
])
def test_mixed_identity_incomplete_or_unsafe_pair_refused(con, change):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        value = scope.write(lambda _: intended(scope))
        modified = copy.deepcopy(value)
        change(modified)
        with pytest.raises(PublicationRefused):
            catalog.intended_pair(modified["before"], **modified["after"])
        assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 0


def test_pair_cas_cannot_be_called_outside_bound_file_transition(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        value = ready(scope)
        with pytest.raises(PublicationRefused, match="TRANSITION_REQUIRED"):
            scope.write(lambda _: catalog.compare_and_swap(scope, value))
        different = copy.deepcopy(value)
        different["after"]["archived"]["verified_at"] = "not frozen"
        with pytest.raises(PublicationRefused, match="INTENT_MISMATCH"):
            advance(scope, "CATALOG_PUBLISHED", lambda c, intent: catalog.compare_and_swap(scope, different))
        assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 0
        publish(scope)


def test_raw_transaction_or_extended_schema_not_implicitly_adopted(con):
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        con.execute("BEGIN IMMEDIATE")
        with pytest.raises(WriteContextError, match="AUTHORITY_MISSING"):
            intended(scope)
        con.execute("ROLLBACK")
        con.execute("ALTER TABLE files ADD COLUMN new_evidence TEXT")
        with pytest.raises(PublicationRefused, match="SCHEMA_UNQUALIFIED"):
            scope.write(lambda _: intended(scope))


def test_historical_alter_column_order_still_binds_every_named_fact(con):
    # Tiny synthetic table only: reproduce the real legacy ALTER migration,
    # instead of using the current CREATE TABLE column order for every fixture.
    con.execute("DROP TABLE archived")
    con.execute(db._canonical_table_sql("archived", "archived", exclude=("stored_relpath", "orig_sha256_provenance")))
    migration = next(stmt for stmt in db._MIGRATIONS if stmt == "ALTER TABLE archived ADD COLUMN stored_relpath VARCHAR")
    con.execute(migration)
    con.execute("ALTER TABLE archived ADD COLUMN orig_sha256_provenance VARCHAR")
    order = [row[1] for row in con.execute("PRAGMA table_info(archived)")]
    assert order.index("stored_relpath") > order.index("verified_at")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        ready(scope)
        publish(scope)
        assert con.execute("SELECT stored_relpath,orig_sha256,annex_key FROM archived").fetchone() == (
            "model.safetensors", "1" * 64, KEY)
        assert con.execute("SELECT annex_key,present FROM replicas").fetchone() == (KEY, 1)
