"""PR-08 / #39-A complete graph-writer revision closure (tests-first, A3).

Public supported entry points must bump planner_revision. If complete closure is too large,
stop and propose PR-08A — do not weaken these pins.
"""
from __future__ import annotations

import importlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from modelark.core import db


def _setup_catalog(tmp_path: Path):
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    # Also rebind portal data module if present.
    try:
        from modelark.web import data
        if hasattr(data, "_con"):
            data._con = None
    except Exception:
        pass
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] >= 5, (
        "v5 planner_state required (expected Gate-1 red until migration)")
    if con.execute("SELECT count(*) FROM planner_state").fetchone()[0] == 0:
        raise AssertionError("planner_state singleton missing after connect")
    return con


def _rev(con) -> int:
    return int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])


def _require_bump(con, label, fn):
    before = _rev(con)
    fn()
    after = _rev(con)
    assert after == before + 1, (
        f"{label} must bump planner_revision {before}→{before + 1}; got {after}")


def _seed(con):
    con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT OR IGNORE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16',?)", ["1" * 64])
    con.execute(
        "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
        "write_authority,filesystem_capacity_bytes) "
        "VALUES('d0',1000,900,'primary',0,'active','enabled',1,0,?,'dedicated_local',1000)",
        ["a" * 64])
    from modelark import plan
    if plan.get(con, "ark") is None:
        plan.create(con, "ark", name="Ark")
    if "d0" not in plan.plan_drive_labels(con, "ark"):
        plan.add_drive(con, "ark", "d0")
    plan.set_active(con, "ark")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")


def _inv_names():
    prop = importlib.import_module("modelark.proposal")
    inv = getattr(prop, "GRAPH_AFFECTING_WRITERS", None) or getattr(
        prop, "graph_affecting_writers", None)
    assert inv is not None, "export GRAPH_AFFECTING_WRITERS"
    return {str(x) for x in (inv.keys() if isinstance(inv, dict) else inv)}


def test_inventory_lists_every_required_supported_writer():
    names = _inv_names()
    required = [
        "selection_api.finalize", "selection_api.clear", "selection_api.toggle",
        "selection_api.bulk",
        "db.replace_files", "cli.cmd_protect", "plan.add_drive", "plan.remove_drive",
        "plan.set_active", "plan.bootstrap", "plan.set_capacity_mode",
        "record_archived", "remove_archived",
        "drive_mutation.begin_generation", "drive_mutation.publish_clean_anchor",
        "drive_bootstrap.reconcile_drive", "hash_repair.repair_hashes",
        "set_drive_lifecycle", "set_drive_eligibility", "set_drive_role",
    ]
    missing = [s for s in required if not any(s in n for n in names)]
    assert not missing, f"inventory incomplete; missing {missing}; have={sorted(names)}"


def test_plan_membership_capacity_bootstrap_active(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import plan
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility) VALUES('d1',1000,900,'replica',0,'active','enabled')")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "plan.add_drive", lambda: plan.add_drive(con, "ark", "d1"))
    _require_bump(con, "plan.remove_drive", lambda: plan.remove_drive(con, "ark", "d1"))
    _require_bump(con, "plan.set_capacity_mode",
                  lambda: plan.set_capacity_mode(con, "ark", "compression_aware"))
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility) VALUES('d2',1000,900,'primary',0,'active','enabled')")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "plan.bootstrap", lambda: plan.bootstrap(con, "ark"))
    plan.create(con, "other", name="Other")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    before = _rev(con)
    plan.set_active(con, "other")
    assert _rev(con) == before + 1
    before = _rev(con)
    plan.set_active(con, "other")
    assert _rev(con) == before, "no-op set_active must not bump"
    con.close()


def test_replace_files_bumps_on_actual_change(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    # Change size so replace is not a canonical no-op.
    rows = [{"rfilename": "model.safetensors", "size_bytes": 101, "format": "safetensors",
             "quant": "bf16", "sha256": "2" * 64}]
    _require_bump(con, "db.replace_files (changed row)", lambda: db.replace_files(con, "org/m", rows))
    # Identical re-apply may proven_noop (must not require bump).
    before = _rev(con)
    db.replace_files(con, "org/m", rows)
    assert _rev(con) in (before, before + 1)  # allow either; if bumps on identical, still ok
    # Prefer proven_noop for identical: document expectation
    con.close()


def test_cmd_protect_bumps(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    con.execute("UPDATE models SET numcopies=1 WHERE repo_id='org/m'")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    con.close()
    from modelark import cli
    before = _rev(db.connect())
    db.connect().close()
    cli.cmd_protect(SimpleNamespace(repo=["org/m"], numcopies=2))
    after = _rev(db.connect())
    db.connect().close()
    assert after == before + 1
    con = db.connect()
    assert con.execute("SELECT numcopies FROM models WHERE repo_id='org/m'").fetchone()[0] == 2
    con.close()


def test_selection_api_finalize_clear_toggle_bulk(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    con.execute("INSERT OR IGNORE INTO selection(repo_id) VALUES('org/m')")
    con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES('org/a',1)")
    con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES('org/b',1)")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    con.close()

    from modelark.web import data, selection_api
    data._con = None  # force reconnect to tmp catalog via db.connect

    def rev():
        c = db.connect()
        try:
            return _rev(c)
        finally:
            c.close()

    b = rev()
    selection_api.finalize()
    assert rev() == b + 1, "selection_api.finalize must bump"

    b = rev()
    selection_api.toggle("org/a", True)  # add is unguarded; still graph-affecting
    assert rev() == b + 1, "selection_api.toggle(on) must bump"

    b = rev()
    selection_api.toggle("org/a", False)  # deselect
    assert rev() == b + 1, "selection_api.toggle(off)/deselect must bump"

    b = rev()
    selection_api.bulk(["org/a", "org/b"], True)
    assert rev() == b + 1, "selection_api.bulk(on) must bump"

    b = rev()
    selection_api.bulk(["org/a", "org/b"], False)
    assert rev() == b + 1, "selection_api.bulk(off) must bump"

    b = rev()
    selection_api.clear()
    assert rev() == b + 1, "selection_api.clear must bump"


def test_dirty_generation_and_clean_anchor_publish_bump(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import drive_mutation as dm
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "drive_mutation.begin_generation",
                  lambda: dm.begin_generation(con, "d0", "test-op"))
    # Publish clean anchor for current generation.
    gen = con.execute(
        "SELECT write_generation FROM drives WHERE drive_label='d0'").fetchone()[0]
    obs = SimpleNamespace(
        free_bytes=800, filesystem_capacity=1000, fingerprint="a" * 64,
        identity_proven=True, identity_proof="p", fence_proof="p")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "drive_mutation.publish_clean_anchor",
                  lambda: dm.publish_clean_anchor(con, "d0", 1, gen, obs, "now"))
    con.close()


def test_drive_bootstrap_reconcile_bumps_on_success(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import drive_bootstrap as dbp
    prop = importlib.import_module("modelark.proposal")
    # Prefer a test seam that applies a catalog identity refresh without physical mount.
    apply = getattr(dbp, "apply_identity_refresh", None) or getattr(
        prop, "drive_bootstrap_identity_write", None)
    assert apply is not None or hasattr(dbp, "reconcile_drive"), (
        "drive_bootstrap must expose a catalog-writing path that bumps revision")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    if apply is not None:
        _require_bump(con, "drive_bootstrap identity write",
                      lambda: apply(con, "d0", identity_epoch=2, fingerprint="b" * 64))
    else:
        # reconcile_drive without mount may refuse — require it still routes through graph_write
        # when it mutates; for Gate-1 pin a wrapper that production must provide for tests:
        assert hasattr(prop, "bumping_drive_bootstrap_for_tests") or hasattr(
            dbp, "catalog_only_reconcile_for_tests"), (
            "provide catalog-only bootstrap mutator for revision proof, or apply_identity_refresh")
        fn = getattr(prop, "bumping_drive_bootstrap_for_tests", None) or \
            dbp.catalog_only_reconcile_for_tests
        _require_bump(con, "drive_bootstrap catalog-only", lambda: fn(con, "d0"))
    con.close()


def test_hash_repair_apply_bumps_when_repairs_land(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import hash_repair
    # Seed archived without orig_sha256 so repair has work when apply=True and evidence exists.
    # Without archive bytes, repair may no-op — require production test seam for forced repair write.
    prop = importlib.import_module("modelark.proposal")
    force = getattr(prop, "apply_hash_repair_for_tests", None) or getattr(
        hash_repair, "apply_one_for_tests", None)
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    if force is not None:
        _require_bump(con, "hash_repair apply",
                      lambda: force(con, "org/m", "model.safetensors", "d0", "9" * 64))
    else:
        # Public repair_hashes(apply=True) must bump when report["repairs"] non-empty.
        # Pin inventory + require apply path uses graph_write; simulate non-empty by stubbing audit.
        with mock.patch.object(hash_repair, "audit_hashes", return_value={
            "errors": [],
            "repairs": [{
                "repo_id": "org/m", "rfilename": "model.safetensors",
                "drive_label": "d0", "sha256": "9" * 64,
            }],
        }):
            try:
                before = _rev(con)
                hash_repair.repair_hashes(con, ["org/m"], apply=True)
                assert _rev(con) == before + 1, "repair_hashes(apply=True) with repairs must bump"
            except Exception as exc:
                raise AssertionError(
                    f"hash_repair.repair_hashes(apply=True) must bump when repairs land: {exc}"
                ) from exc
    con.close()


def test_all_drive_axis_mutators_bump(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    prop = importlib.import_module("modelark.proposal")
    for name, args in (
        ("set_drive_eligibility", ("d0", "excluded")),
        ("set_drive_lifecycle", ("d0", "lost")),
        ("set_drive_role", ("d0", "replica")),
    ):
        fn = getattr(prop, name, None)
        assert fn is not None, f"public {name} required for axis writes (Gate-1 red)"
        # re-seed active drive between axis flips as needed
        con.execute(
            "UPDATE drives SET lifecycle='active', eligibility='enabled', role='primary' "
            "WHERE drive_label='d0'")
        con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
        _require_bump(con, name, lambda f=fn, a=args: f(con, *a))
    con.close()


def test_archived_record_and_remove_bump(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    prop = importlib.import_module("modelark.proposal")
    record = getattr(prop, "record_archived", None) or getattr(prop, "upsert_archived", None)
    remove = getattr(prop, "remove_archived", None) or getattr(prop, "delete_archived", None)
    assert record is not None, "record_archived/upsert_archived required"
    assert remove is not None, "remove_archived/delete_archived required (not optional)"
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "record_archived", lambda: record(
        con, repo_id="org/m", rfilename="model.safetensors", drive_label="d0",
        orig_bytes=100, stored_bytes=100, compressed=0))
    _require_bump(con, "remove_archived", lambda: remove(
        con, repo_id="org/m", rfilename="model.safetensors", drive_label="d0"))
    con.close()


def test_proven_noop_does_not_bump(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    prop = importlib.import_module("modelark.proposal")
    gw = getattr(prop, "graph_write", None)
    assert gw is not None

    class Result:
        proven_noop = True
        value = None

    before = _rev(con)
    gw(con, lambda c: Result())
    assert _rev(con) == before
    con.close()


def main():
    import inspect
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                fn(Path(tempfile.mkdtemp(prefix="mark-rev-")))
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, type(exc).__name__, str(exc)[:240]))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("Gate-1 tests-only: revision-writer contracts EXPECTED RED until PR-08 production.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
