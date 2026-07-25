"""PR-08 / #39-A complete graph-writer revision closure (tests-first, A3).

Public supported entry points must bump planner_revision. Inventory matches Gate-0 accepted
writers; lifecycle ops only when already exposed — do not invent new axis APIs.
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
    try:
        from modelark.web import data
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
        "write_authority,filesystem_capacity_bytes,fs_uuid,annex_uuid,serial) "
        "VALUES('d0',1000,900,'primary',0,'active','enabled',1,0,?,'dedicated_local',1000,"
        "'fs-d0','anx-d0','ser-d0')",
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


def test_inventory_lists_accepted_supported_writers():
    """A3 inventory must name real supported entrypoints; each must share TX with revision bump."""
    names = _inv_names()
    required = [
        "selection_api.finalize", "selection_api.clear", "selection_api.toggle",
        "selection_api.bulk",
        "discover.discover_one", "discover.discover_repos", "db.replace_files",
        "cli.cmd_protect",
        "plan.create", "plan.add_drive", "plan.remove_drive", "plan.set_active",
        "plan.bootstrap", "plan.set_capacity_mode",
        "drive_mutation.begin_generation", "drive_mutation.publish_clean_anchor",
        "drive_bootstrap.reconcile_drive", "register.register_drive",
        "hash_repair.repair_hashes",
        "proposal.approve",
    ]
    missing = [s for s in required if not any(s in n for n in names)]
    assert not missing, f"inventory incomplete; missing {missing}; have={sorted(names)}"
    # Inventory alone is not enough — graph_write must document same-TX bump.
    prop = importlib.import_module("modelark.proposal")
    gw = getattr(prop, "graph_write", None)
    assert gw is not None
    assert "revision" in (gw.__doc__ or "").lower() or hasattr(prop, "bump_planner_revision"), (
        "graph_write must document/perform revision bump in the same transaction as the mutation")


def test_plan_create_membership_capacity_bootstrap_active(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import plan
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "plan.create", lambda: plan.create(con, "p2", name="Two"))
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


def test_discover_one_and_replace_files_bump(tmp_path):
    """Call actual discover.discover_one with mocked HF API — not generic upsert."""
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import discover
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    # Minimal HF model info mock for discover_one(api, con, repo_id).
    info = SimpleNamespace(
        id="org/new",
        siblings=[],
        cardData={},
        downloads=0,
        likes=0,
        tags=[],
        pipeline_tag="text-generation",
        private=False,
        gated=False,
    )
    api = mock.Mock()
    api.model_info.return_value = info
    before = _rev(con)
    try:
        discover.discover_one(api, con, "org/new")
    except Exception as exc:
        # If discover_one signature differs, try discover_repos
        try:
            discover.discover_repos(["org/new"], con=con)
        except Exception as exc2:
            raise AssertionError(
                f"discover.discover_one/repos must run as supported writer: {exc!r} / {exc2!r}"
            ) from exc2
    assert _rev(con) == before + 1, "discover catalog write must bump revision"
    rows = [{"rfilename": "model.safetensors", "size_bytes": 101, "format": "safetensors",
             "quant": "bf16", "sha256": "2" * 64}]
    _require_bump(con, "db.replace_files", lambda: db.replace_files(con, "org/m", rows))
    con.close()


def test_cmd_protect_and_approval_transaction_bump(tmp_path):
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

    # Successful approval transaction bumps (once).
    prop = importlib.import_module("modelark.proposal")
    con = db.connect()
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    create = getattr(prop, "create_draft", None) or prop.preview_and_draft
    approve = getattr(prop, "approve", None) or prop.approve_proposal
    try:
        draft = create(con, plan_id="ark", mutation=("adopt_current", ()))
    except TypeError:
        draft = create(con, "ark", ("adopt_current", ()))
    pid = draft["proposal_id"] if isinstance(draft, dict) else draft
    before = _rev(con)
    try:
        approve(con, pid)
    except TypeError:
        approve(con, proposal_id=pid)
    assert _rev(con) == before + 1, "proposal.approve must bump once"
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
    data._con = None

    def rev():
        c = db.connect()
        try:
            return _rev(c)
        finally:
            c.close()

    b = rev()
    selection_api.finalize()
    assert rev() == b + 1
    b = rev()
    selection_api.toggle("org/a", True)
    assert rev() == b + 1
    b = rev()
    selection_api.toggle("org/a", False)
    assert rev() == b + 1
    b = rev()
    selection_api.bulk(["org/a", "org/b"], True)
    assert rev() == b + 1
    b = rev()
    selection_api.bulk(["org/a", "org/b"], False)
    assert rev() == b + 1
    b = rev()
    selection_api.clear()
    assert rev() == b + 1


def test_dirty_and_clean_anchor_bump(tmp_path):
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import drive_mutation as dm
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "begin_generation", lambda: dm.begin_generation(con, "d0", "test-op"))
    gen = con.execute(
        "SELECT write_generation FROM drives WHERE drive_label='d0'").fetchone()[0]
    obs = SimpleNamespace(
        free_bytes=800, filesystem_capacity=1000, fingerprint="a" * 64,
        identity_proven=True, identity_proof="p", fence_proof="p")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _require_bump(con, "publish_clean_anchor",
                  lambda: dm.publish_clean_anchor(con, "d0", 1, gen, obs, "now"))
    con.close()


def test_drive_bootstrap_reconcile_drive_with_real_live_evidence_type(tmp_path):
    """Use real _LiveEvidence / Inventory; mock only physical boundaries (finding 23)."""
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import drive_bootstrap as dbp
    from modelark.drive_bootstrap import Inventory, _LiveEvidence
    assert hasattr(dbp, "reconcile_drive")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    live = _LiveEvidence(
        path="/mnt/d0", fs_uuid="fs-d0", annex_uuid="anx-d0", serial="ser-d0",
        capacity=1000, free=900, alloc_unit=4096, fingerprint="a" * 64, proven=True)
    assert hasattr(live, "observation") and callable(live.observation)
    inv = Inventory(present=[], missing=[], debris=[], extra=[])
    with mock.patch.object(dbp, "_live_evidence", return_value=live):
        with mock.patch.object(dbp, "_inventory", return_value=inv):
            with mock.patch.object(dbp, "_annex_key_present", return_value=True):
                with mock.patch.object(dbp, "_final_observation", return_value=live):
                    before = _rev(con)
                    try:
                        dbp.reconcile_drive(con, "d0", now="2026-01-01T00:00:00", dedicated=True)
                    except TypeError:
                        dbp.reconcile_drive(
                            con, "d0", now="2026-01-01T00:00:00", dedicated=True,
                            accept_drift=False)
                    after = _rev(con)
    assert after == before + 1, (
        f"reconcile_drive must bump when it mutates; {before}→{after}")
    con.close()


def test_hash_repair_apply_with_eligible_archived_row(tmp_path):
    """Seed legacy archived without orig_sha256; mock audit/backup so apply lands a bump."""
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import hash_repair
    # Eligible legacy archived row: NULL orig_sha256; clear files.sha256 so repair has work.
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,stored_bytes,"
        "orig_sha256) VALUES('org/m','model.safetensors','d0',0,100,100,NULL)")
    con.execute("UPDATE files SET sha256=NULL WHERE repo_id='org/m'")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    repairs = [{
        "repo_id": "org/m", "rfilename": "model.safetensors",
        "drive_label": "d0", "sha256": "9" * 64,
    }]
    with mock.patch.object(hash_repair, "audit_hashes", return_value={
        "errors": [], "repairs": repairs,
    }):
        with mock.patch.object(hash_repair, "_consistent_backup", return_value=Path("/tmp/x.bak")):
            before = _rev(con)
            hash_repair.repair_hashes(con, repo_ids=["org/m"], apply=True)
            assert _rev(con) == before + 1, "repair_hashes(apply=True) with repairs must bump"
            # And the archived row should gain the hash when apply actually writes.
            row = con.execute(
                "SELECT orig_sha256 FROM archived WHERE repo_id='org/m' AND drive_label='d0'"
            ).fetchone()
            # Production may write files and/or archived; require at least one evidence update.
            file_sha = con.execute(
                "SELECT sha256 FROM files WHERE repo_id='org/m'").fetchone()[0]
            assert row[0] == "9" * 64 or file_sha == "9" * 64, (
                "apply must write the repaired hash into durable catalog evidence")
    con.close()


def test_register_drive_catalog_write_shares_revision_tx(tmp_path):
    """register.register_drive is the supported path; mock physical steps, assert catalog+revision TX."""
    names = _inv_names()
    assert any("register.register_drive" in n for n in names), names
    con = _setup_catalog(tmp_path)
    _seed(con)
    from modelark import register
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    # Mock destructive/physical boundaries; allow catalog INSERT/upsert of a new label.
    with mock.patch.object(register, "probe_fs_uuid", return_value="fs-new"):
        with mock.patch.object(register, "probe_annex_uuid", return_value="anx-new"):
            with mock.patch.object(register, "probe_serial", return_value="ser-new"):
                with mock.patch.object(register, "_git", return_value=None):
                    with mock.patch.object(register, "smart_check", return_value={
                        "verdict": "ok", "model": "m", "serial": "ser-new",
                        "reallocated": 0, "pending": 0, "offline_uncorrectable": 0,
                        "power_on_hours": 0, "smart_passed": True,
                    }):
                        before = _rev(con)
                        try:
                            register.register_drive(
                                dev="/dev/null", label="d-reg", mount=str(tmp_path / "mnt"),
                                format_fs=False, dry_run=False, skip_smart=True)
                        except Exception as exc:
                            # If full register is too physical, require graph_write-wrapped catalog
                            # completion path used by register after probes.
                            raise AssertionError(
                                f"register.register_drive must be exercisable with mocked "
                                f"physical seams and bump revision: {exc}"
                            ) from exc
                        # register opens its own connection via db.DB_PATH
                        c2 = db.connect()
                        after = _rev(c2)
                        c2.close()
                        assert after == before + 1, (
                            f"register_drive must bump revision in same TX as catalog write; "
                            f"{before}→{after}")
    con.close()


def test_archived_progress_via_fetch_or_supported_writer(tmp_path):
    """Archived progress/removal via existing supported paths — not invented proposal helpers."""
    con = _setup_catalog(tmp_path)
    _seed(con)
    prop = importlib.import_module("modelark.proposal")
    # Prefer existing fetch/archive writer if proposal does not invent record_archived.
    record = getattr(prop, "record_archived", None)
    remove = getattr(prop, "remove_archived", None)
    # Fallback: graph_write wrapping the INSERT that fetch uses is insufficient alone —
    # require a named supported entry in inventory that is actually called.
    if record is None:
        # fetch path is heavy; require production to route archived INSERT through graph_write
        # and export the entrypoint used by fetch (e.g. archive_catalog.record_copy).
        for mod_name in ("modelark.archive_catalog", "modelark.fetch", "modelark.proposal"):
            try:
                mod = importlib.import_module(mod_name)
            except ModuleNotFoundError:
                continue
            for attr in ("record_copy", "record_archived", "insert_archived", "note_archived"):
                if hasattr(mod, attr):
                    record = getattr(mod, attr)
                    break
            if record:
                break
    assert record is not None, (
        "supported archived-progress entrypoint required (fetch/archive_catalog/proposal) "
        "wired through graph_write — do not invent helpers solely for tests")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    before = _rev(con)
    try:
        record(con, repo_id="org/m", rfilename="model.safetensors", drive_label="d0",
               orig_bytes=100, stored_bytes=100, compressed=0)
    except TypeError:
        record(con, "org/m", "model.safetensors", "d0", 100, 100, 0)
    assert _rev(con) == before + 1
    # Removal: same rule
    if remove is None:
        for mod_name in ("modelark.archive_catalog", "modelark.fetch", "modelark.proposal"):
            try:
                mod = importlib.import_module(mod_name)
            except ModuleNotFoundError:
                continue
            for attr in ("remove_archived", "delete_archived", "drop_archived"):
                if hasattr(mod, attr):
                    remove = getattr(mod, attr)
                    break
            if remove:
                break
    assert remove is not None, "supported archived-removal entrypoint required"
    before = _rev(con)
    try:
        remove(con, repo_id="org/m", rfilename="model.safetensors", drive_label="d0")
    except TypeError:
        remove(con, "org/m", "model.safetensors", "d0")
    assert _rev(con) == before + 1
    con.close()


def test_lifecycle_axis_only_if_already_exposed(tmp_path):
    """Do not require new lifecycle op APIs; if exposed, they must bump."""
    prop = importlib.import_module("modelark.proposal")
    con = _setup_catalog(tmp_path)
    _seed(con)
    for name, args in (
        ("set_drive_eligibility", ("d0", "excluded")),
        ("set_drive_lifecycle", ("d0", "lost")),
        ("set_drive_role", ("d0", "replica")),
    ):
        fn = getattr(prop, name, None)
        if fn is None:
            continue  # not exposed — OK for PR-08
        con.execute(
            "UPDATE drives SET lifecycle='active', eligibility='enabled', role='primary' "
            "WHERE drive_label='d0'")
        con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
        _require_bump(con, name, lambda f=fn, a=args: f(con, *a))
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


def test_graph_write_mutation_and_bump_share_one_transaction(tmp_path):
    """A3: mutation and planner_revision update share one BEGIN…COMMIT (finding 22)."""
    con = _setup_catalog(tmp_path)
    _seed(con)
    prop = importlib.import_module("modelark.proposal")
    gw = getattr(prop, "graph_write", None)
    assert gw is not None

    class _TxOrderCon:
        def __init__(self, inner):
            self._inner = inner
            self.events: list[str] = []
            self._depth = 0

        def execute(self, sql, *a):
            text = sql if isinstance(sql, str) else str(sql)
            s = text.strip().upper()
            if s.startswith("BEGIN"):
                self._depth += 1
                self.events.append("BEGIN")
            elif s.startswith("COMMIT"):
                self.events.append("COMMIT")
                self._depth = max(0, self._depth - 1)
            elif s.startswith("ROLLBACK"):
                self.events.append("ROLLBACK")
                self._depth = max(0, self._depth - 1)
            elif "INSERT INTO MODELS" in s:
                self.events.append(f"MUTATE@{self._depth}")
            elif "PLANNER_REVISION" in s or "planner_revision" in text:
                self.events.append(f"BUMP@{self._depth}")
            return self._inner.execute(sql, *a)

        def __getattr__(self, n):
            return getattr(self._inner, n)

    spy = _TxOrderCon(con)
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")

    class Result:
        proven_noop = False
        value = None

    def op(c):
        c.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/tx',1)")
        return Result()

    gw(spy, op)
    assert any(e == "MUTATE@1" for e in spy.events), spy.events
    assert any(e == "BUMP@1" for e in spy.events), spy.events
    mut_i = next(i for i, e in enumerate(spy.events) if e.startswith("MUTATE"))
    bump_i = next(i for i, e in enumerate(spy.events) if e.startswith("BUMP"))
    between = spy.events[min(mut_i, bump_i):max(mut_i, bump_i) + 1]
    assert "COMMIT" not in between, f"mutation and bump must share one TX; events={spy.events}"
    assert _rev(con) == 1
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
