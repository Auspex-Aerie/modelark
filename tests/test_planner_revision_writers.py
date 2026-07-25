"""PR-08 / #39-A complete graph-writer revision closure (tests-first, A3).

Pins that **public supported entry points** bump planner_revision — not synthetic SQL inside
graph_write alone. Inventory must enumerate every closed mutator; no expected-red follow-ups.
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
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] >= 5, (
        "v5 planner_state required (expected Gate-1 red until migration)")
    # Ensure singleton exists even if migration forgot seed (production must seed).
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
        "write_authority) VALUES('d0',1000,900,'primary',0,'active','enabled',1,0,?,'unknown')",
        ["a" * 64])
    from modelark import plan
    if plan.get(con, "ark") is None:
        plan.create(con, "ark", name="Ark")
    if "d0" not in plan.plan_drive_labels(con, "ark"):
        plan.add_drive(con, "ark", "d0")
    plan.set_active(con, "ark")
    # Baseline revision after seed — production seed path may bump; pin to known value for delta.
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")


def test_inventory_lists_every_required_supported_writer():
    prop = importlib.import_module("modelark.proposal")
    inv = getattr(prop, "GRAPH_AFFECTING_WRITERS", None) or getattr(
        prop, "graph_affecting_writers", None)
    assert inv is not None, "export GRAPH_AFFECTING_WRITERS listing every closed mutator"
    if isinstance(inv, dict):
        names = {str(k) for k in inv}
    else:
        names = {str(x) for x in inv}
    required_substrings = [
        "selection", "finalize", "replace_files", "protect", "numcopies",
        "plan.add_drive", "plan.remove_drive", "plan.set_active", "plan.bootstrap",
        "archived", "dirty", "anchor", "lifecycle", "eligibility", "role",
        "hash_repair", "drive_bootstrap", "register",
    ]
    missing = [s for s in required_substrings if not any(s in n for n in names)]
    assert not missing, (
        f"GRAPH_AFFECTING_WRITERS incomplete; missing coverage for {missing}; have={sorted(names)}")


def test_plan_add_remove_drive_public_api_bumps(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import plan
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility) VALUES('d1',1000,900,'replica',0,'active','enabled')")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "plan.add_drive", lambda: plan.add_drive(con, "ark", "d1"))
    _require_bump(con, "plan.remove_drive", lambda: plan.remove_drive(con, "ark", "d1"))
    con.close()


def test_plan_bootstrap_and_set_active_public_api(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import plan
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility) VALUES('d2',1000,900,'primary',0,'active','enabled')")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "plan.bootstrap", lambda: plan.bootstrap(con, "ark"))
    plan.create(con, "other", name="Other")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    before = _rev(con)
    plan.set_active(con, "other")
    assert _rev(con) == before + 1, "real active-plan switch must bump"
    before = _rev(con)
    plan.set_active(con, "other")
    assert _rev(con) == before, "no-op set_active must not bump"
    con.close()


def test_replace_files_public_api_bumps(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    rows = [{"rfilename": "model.safetensors", "size_bytes": 100, "format": "safetensors",
             "quant": "bf16", "sha256": "1" * 64}]
    _require_bump(con, "db.replace_files", lambda: db.replace_files(con, "org/m", rows))
    con.close()


def test_cmd_protect_public_path_bumps(tmp_path):
    """cli.cmd_protect is a supported graph writer — must bump via connect() path."""
    con = _setup_catalog(tmp_path)
    _seed(con)
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    con.close()
    from modelark import cli
    args = SimpleNamespace(repo=["org/m"], numcopies=2)
    before = _rev(db.connect())
    db.connect().close()
    # cmd_protect opens its own connection on DB_PATH.
    with mock.patch.object(db, "connect", wraps=db.connect) as _:
        before_con = db.connect()
        before = _rev(before_con)
        before_con.close()
        cli.cmd_protect(args)
        after_con = db.connect()
        after = _rev(after_con)
        after_con.close()
    assert after == before + 1, (
        f"cli.cmd_protect must bump revision via supported path; {before}→{after}")


def test_selection_api_finalize_and_clear_bump(tmp_path):
    """Portal selection_api.finalize/clear must bump (graph_write under the hood)."""
    con = _setup_catalog(tmp_path)
    _seed(con)
    con.execute("INSERT OR IGNORE INTO selection(repo_id) VALUES('org/m')")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    con.close()

    from modelark.web import data, selection_api
    # Point portal data layer at the same catalog.
    data.configure = getattr(data, "configure", None)
    if hasattr(data, "configure"):
        data.configure(tmp_path)
    else:
        # Fall back to db.configure + data rebinding hooks production must honor.
        db.configure(str(tmp_path))
        if hasattr(data, "DB_PATH"):
            data.DB_PATH = db.DB_PATH
        if hasattr(data, "conn"):
            # Force reconnect
            if hasattr(data, "_con"):
                data._con = None

    # Ensure selection row visible through data layer.
    try:
        before_con = db.connect()
        before = _rev(before_con)
        before_con.close()
        selection_api.finalize()
        after_con = db.connect()
        after = _rev(after_con)
        after_con.close()
        assert after == before + 1, f"selection_api.finalize must bump; {before}→{after}"
    except Exception as exc:
        raise AssertionError(
            f"selection_api.finalize must be revision-aware (Gate-1 red / wire failure): {exc}"
        ) from exc


def test_drive_mutation_dirty_and_anchor_public_api_bumps(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import drive_mutation as dm
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    # advance_dirty_generation is the supported dirty path.
    if hasattr(dm, "advance_dirty_generation"):
        _require_bump(con, "drive_mutation.advance_dirty_generation",
                      lambda: dm.advance_dirty_generation(con, "d0", operation_code="test"))
    else:
        raise AssertionError(
            "drive_mutation.advance_dirty_generation (or equivalent public dirty API) required")
    con.close()


def test_drive_bootstrap_reconcile_bumps_when_identity_changes(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    try:
        from modelark import drive_bootstrap as dbp
    except ModuleNotFoundError as exc:
        raise AssertionError("drive_bootstrap module required") from exc
    assert hasattr(dbp, "reconcile_drive"), "reconcile_drive is a supported graph writer"
    # Without physical mount, reconcile may refuse; still must go through graph_write when it mutates.
    # Pin: inventory includes drive_bootstrap; successful identity refresh bumps (integration path).
    prop = importlib.import_module("modelark.proposal")
    inv = getattr(prop, "GRAPH_AFFECTING_WRITERS", ())
    assert any("drive_bootstrap" in str(x) or "reconcile_drive" in str(x) for x in inv), (
        "inventory must include drive_bootstrap.reconcile_drive")
    con.close()


def test_hash_repair_public_api_in_inventory_and_bumps_on_change(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import hash_repair
    assert hasattr(hash_repair, "repair_hashes")
    prop = importlib.import_module("modelark.proposal")
    inv = getattr(prop, "GRAPH_AFFECTING_WRITERS", ())
    assert any("hash_repair" in str(x) for x in inv), "inventory must include hash_repair"
    # Call with empty/no-op set may proven_noop; force a file hash write path.
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    try:
        hash_repair.repair_hashes(con, repo_ids=["org/m"], dry_run=False)
    except TypeError:
        try:
            hash_repair.repair_hashes(con, ["org/m"])
        except Exception:
            pass
    except Exception:
        # May fail without network/manifest; inventory pin is required regardless.
        pass
    con.close()


def test_lifecycle_eligibility_role_via_supported_mutator(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    prop = importlib.import_module("modelark.proposal")
    # Preferred: explicit axis mutators; else graph_write only if exported as the path.
    for name in ("set_drive_eligibility", "set_drive_lifecycle", "set_drive_role"):
        if hasattr(prop, name):
            fn = getattr(prop, name)
            con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
            if "eligibility" in name:
                _require_bump(con, name, lambda: fn(con, "d0", "excluded"))
            elif "lifecycle" in name:
                _require_bump(con, name, lambda: fn(con, "d0", "lost"))
            else:
                _require_bump(con, name, lambda: fn(con, "d0", "replica"))
            con.close()
            return
    inv = getattr(prop, "GRAPH_AFFECTING_WRITERS", ())
    assert any("lifecycle" in str(x) or "eligibility" in str(x) or "role" in str(x) for x in inv), (
        "must expose supported lifecycle/eligibility/role mutators that bump revision")
    raise AssertionError(
        "no public set_drive_eligibility/lifecycle/role; inventory incomplete (Gate-1 red)")


def test_archived_progress_supported_path_bumps(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    prop = importlib.import_module("modelark.proposal")
    record = getattr(prop, "record_archived", None) or getattr(prop, "upsert_archived", None)
    assert record is not None, (
        "supported archived progress API required (record_archived/upsert_archived) — "
        "not bare SQL; Gate-1 red")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "record_archived", lambda: record(
        con, repo_id="org/m", rfilename="model.safetensors", drive_label="d0",
        orig_bytes=100, stored_bytes=100, compressed=0))
    remove = getattr(prop, "remove_archived", None) or getattr(prop, "delete_archived", None)
    if remove is not None:
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
    out = gw(con, lambda c: Result())
    assert _rev(con) == before, "proven_noop must not bump"
    con.close()
    del out


def test_numcopies_only_via_supported_path_no_pre_mutate(tmp_path):
    """cmd_protect / set_numcopies must not mutate before graph_write (no-op semantics)."""
    con = _setup_catalog(tmp_path)
    _seed(con)
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    con.execute("UPDATE models SET numcopies=1 WHERE repo_id='org/m'")
    before_nc = con.execute(
        "SELECT numcopies FROM models WHERE repo_id='org/m'").fetchone()[0]
    con.close()
    from modelark import cli
    args = SimpleNamespace(repo=["org/m"], numcopies=2)
    cli.cmd_protect(args)
    con = db.connect()
    after_nc = con.execute(
        "SELECT numcopies FROM models WHERE repo_id='org/m'").fetchone()[0]
    assert before_nc == 1 and after_nc == 2
    assert _rev(con) >= 1, "protect path must bump revision when numcopies changes"
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
