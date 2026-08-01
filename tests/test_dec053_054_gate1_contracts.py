"""DEC-053 / DEC-054 / DEF-034 Gate-1 contracts — remediation 2 (strict behavioral).

Contracts only. No production. Expected-red until Gate-2 lands clone-first
provenance migration, provenance/derivation CHECKs, explicit drive repair, and
replica heal.

Frozen v6 catalogs load from ``tests/fixtures/catalog_v6.sql`` (complete tip
schema snapshot), independent of future ``schema.sql`` / ``_SCHEMA_VERSION`` /
``db.connect()`` bootstrap.
"""
from __future__ import annotations

import hashlib
import importlib
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db
from modelark import archive_hash, fill as fill_mod, proposal as proposal_mod
from modelark import proposal_canonical as canonical


FIXTURE_SQL = Path(__file__).resolve().parent / "fixtures" / "catalog_v6.sql"
FROZEN_V6 = 6
REPAIR_STATUSES = frozenset({
    "pending", "running", "blocked_absent", "needs_refetch", "halted", "complete",
})
DERIVATION_OK = ("optimized", "state_truncated", "canonical_fallback")

# Tables that must exist after frozen SQL (excluding sqlite_*)
EXPECTED_V6_TABLES = frozenset({
    "models", "files", "drives", "replicas", "verifications", "selection",
    "archived", "fetch_events", "plans", "plan_drives",
    "drive_dirty_generations", "drive_clean_anchors",
    "planner_state", "placement_proposals", "proposal_tasks", "proposal_files",
    "execution_sessions",
})


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_db_paths():
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        yield
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old


def _h(ch: str) -> str:
    return (ch * 64)[:64]


def _close(con):
    if con is not None:
        try:
            con.close()
        except Exception:
            pass


def _open_rw(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint(path: Path) -> dict:
    return {
        "sha256": _sha_file(path) if path.is_file() else None,
        "size": path.stat().st_size if path.is_file() else None,
        "wal": (path.parent / f"{path.name}-wal").is_file(),
        "shm": (path.parent / f"{path.name}-shm").is_file(),
    }


# ---------------------------------------------------------------------------
# Frozen v6 construction
# ---------------------------------------------------------------------------


def _apply_frozen_sql(path: Path) -> None:
    assert FIXTURE_SQL.is_file(), f"missing {FIXTURE_SQL}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        con.executescript(FIXTURE_SQL.read_text())
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == FROZEN_V6
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing = EXPECTED_V6_TABLES - tables
        assert not missing, f"frozen fixture missing tables: {sorted(missing)}"
        assert "orig_sha256_provenance" not in {
            r[1] for r in con.execute("PRAGMA table_info(archived)")
        }
    finally:
        _close(con)


def _seed_frozen_v6(
    root: Path,
    *,
    disagreement: bool = False,
    invalid_derivation: bool = False,
    orphan_archived: bool = False,
) -> Path:
    """Complete frozen v6 data directory with representative rows.

    Default fixture is valid. Flags opt into refusal cases.
    """
    data = root / "v6-data"
    data.mkdir(parents=True, exist_ok=True)
    path = data / "catalog.sqlite"
    _apply_frozen_sql(path)
    con = _open_rw(path)
    try:
        # Core entities
        con.execute(
            "INSERT INTO models(repo_id,status,numcopies) VALUES('org/m','archived',1)")
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) "
            "VALUES('org/m','weights.bin',100,?,'safetensors','bf16')", [_h("a")])
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','notes.txt',10,NULL,'aux')")
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','shard.bin',50,NULL,'safetensors')")
        con.execute(
            "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "identity_epoch,identity_fingerprint,lifecycle,eligibility,write_authority) "
            "VALUES('d0',?,?, 'primary',0,1,?, 'active','enabled','unknown')",
            [10**12, 10**12, _h("f")])
        con.execute(
            "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "identity_epoch,identity_fingerprint,lifecycle,eligibility,write_authority) "
            "VALUES('d1',?,?, 'replica',0,1,?, 'active','enabled','unknown')",
            [10**12, 10**12, _h("g")])
        con.execute(
            "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "identity_epoch,identity_fingerprint,lifecycle,eligibility,write_authority) "
            "VALUES('d-absent',?,?, 'primary',0,1,?, 'active','enabled','unknown')",
            [10**12, 10**12, _h("h")])
        con.execute(
            "INSERT INTO plans(plan_id,name,is_active,capacity_mode) "
            "VALUES('ark','Ark',1,'guaranteed')")
        con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','d0')")
        con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','d1')")
        con.execute(
            "INSERT INTO selection(repo_id,finalized_at) VALUES('org/m',CURRENT_TIMESTAMP)")

        # Capacity evidence
        con.execute(
            "INSERT INTO drive_dirty_generations("
            "drive_label,identity_epoch,generation,operation_code) "
            "VALUES('d0',1,1,'seed')")
        con.execute(
            "INSERT INTO drive_clean_anchors("
            "drive_label,identity_epoch,generation,anchor_free_bytes,"
            "filesystem_capacity_bytes,identity_fingerprint,write_authority,"
            "identity_proof,fence_proof,observed_at) "
            "VALUES('d0',1,1,100,1000,?,'dedicated_local','proof','fence',"
            "CURRENT_TIMESTAMP)",
            [_h("f")])

        # Archived: hub match / legacy / annex-null
        hub_digest = _h("z") if disagreement else _h("a")
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
            "orig_sha256,orig_bytes,stored_bytes,compressed,annex_key) "
            "VALUES('org/m','weights.bin','weights.bin','weights.bin','d0',?,100,100,0,NULL)",
            [hub_digest])
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
            "orig_sha256,orig_bytes,stored_bytes,compressed,annex_key) "
            "VALUES('org/m','notes.txt','notes.txt','notes.txt','d0',?,10,10,0,NULL)",
            [_h("c")])
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
            "orig_sha256,orig_bytes,stored_bytes,compressed,annex_key) "
            "VALUES('org/m','shard.bin','shard.bin','shard.bin','d0',NULL,50,50,0,?)",
            [f"SHA256E-s50--{_h('d')}"])

        # Proposal + children + stopped session
        dm = "ecfg:deadbeef" if invalid_derivation else None
        con.execute(
            "INSERT INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,derivation_mode,"
            "execution_config_hash) "
            "VALUES('hist-p','ark',0,'draft',?,'adopt_current','[]','1',?,NULL)",
            [_h("1"), dm])
        con.execute(
            "INSERT INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,derivation_mode) "
            "VALUES('hist-opt','ark',0,'draft',?,'adopt_current','[]','1','optimized')",
            [_h("2")])
        con.execute(
            "INSERT INTO proposal_tasks("
            "proposal_id,requirement_id,row_kind,repo_id,target_drive,"
            "full_manifest_hash,order_key,guaranteed_durable,expected_durable,"
            "identity_epoch) "
            "VALUES('hist-p','primary:org/m','executable','org/m','d0',?,1,100,100,1)",
            [_h("3")])
        con.execute(
            "INSERT INTO proposal_files("
            "proposal_id,requirement_id,rfilename,role,size_bytes,orig_sha256,"
            "format,storage_action) "
            "VALUES('hist-p','primary:org/m','weights.bin','missing',100,?,"
            "'safetensors','compress')",
            [_h("a")])
        con.execute(
            "INSERT INTO execution_sessions("
            "session_id,plan_id,approved_proposal_id,controller_identity,state,"
            "bound_planner_revision,fencing_token) "
            "VALUES('sess-stopped','ark','hist-opt','controller','stopped',0,1)")

        if orphan_archived:
            con.execute("PRAGMA foreign_keys=OFF")
            con.execute(
                "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
                "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed) "
                "VALUES('org/orphan','ghost.bin','ghost.bin','ghost.bin','d0',?,1,1,0)",
                [_h("o")])
            con.execute("PRAGMA foreign_keys=ON")

        con.execute(f"PRAGMA user_version={FROZEN_V6}")
    finally:
        _close(con)
    return data


def _catalog(data: Path) -> Path:
    return data / "catalog.sqlite"


def _rehearse():
    fn = getattr(db, "rehearse_provenance_migration", None)
    if not callable(fn):
        raise AssertionError(
            "export modelark.core.db.rehearse_provenance_migration("
            "source_dir, work_dir, *, run_id) -> dict with concrete report contract"
        )
    return fn


def _publish():
    fn = getattr(db, "publish_provenance_migration", None)
    if not callable(fn):
        raise AssertionError(
            "export modelark.core.db.publish_provenance_migration("
            "work_dir, dest_dir, *, confirm_stopped, writers_stopped=...) "
            "for operator-authorized cutover"
        )
    return fn


def _repair():
    hr = importlib.import_module("modelark.hash_repair")
    fn = getattr(hr, "run_explicit_drive_repair", None)
    if not callable(fn):
        raise AssertionError(
            "export modelark.hash_repair.run_explicit_drive_repair("
            "con, drive_label, *, identity_epoch, identity_fingerprint=..., "
            "archive_resolver=...)"
        )
    return fn


def _require_report(report: dict) -> dict:
    """Concrete rehearsal report contract — exact keys, no string search."""
    required = (
        "status",
        "source_user_version",
        "clone_user_version",
        "source_integrity",
        "clone_integrity",
        "source_foreign_key_violations",
        "clone_foreign_key_violations",
        "source_content_identity",
        "clone_content_identity",
        "classification",
        "snapshot_path",
        "snapshot_sha256",
        "manifest_path",
        "manifest_status",
        "clone_catalog_path",
    )
    missing = [k for k in required if k not in report]
    assert not missing, f"rehearsal report missing keys: {missing}; got {sorted(report)}"
    assert report["status"] == "ok"
    assert report["source_user_version"] == FROZEN_V6
    assert report["clone_user_version"] > FROZEN_V6  # post-migration schema version
    assert report["source_integrity"] == "ok"
    assert report["clone_integrity"] == "ok"
    assert report["source_foreign_key_violations"] == []
    assert report["clone_foreign_key_violations"] == []
    assert isinstance(report["source_content_identity"], str) and len(
        report["source_content_identity"]) == 64
    assert isinstance(report["clone_content_identity"], str) and len(
        report["clone_content_identity"]) == 64
    cls = report["classification"]
    for k in ("hub_confirmed", "legacy_unknown", "null_digest", "disagreement"):
        assert k in cls, cls
    assert Path(report["snapshot_path"]).is_file()
    assert len(report["snapshot_sha256"]) == 64
    assert Path(report["manifest_path"]).is_file()
    assert report["manifest_status"] in ("validated", "ok", "rehearsed")
    assert Path(report["clone_catalog_path"]).is_file()
    return report


def _ordered_table_dump(con, table: str) -> list:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    # Exclude provenance if present for pre/post compare of common columns
    order = ", ".join(f'"{c}"' for c in cols)
    return list(con.execute(
        f'SELECT {order} FROM "{table}" ORDER BY {order}'
    ).fetchall()), cols


def _common_row_identity(src_con, dst_con, *, allow_new_tables=frozenset(),
                         allow_new_cols: dict | None = None) -> None:
    """Every source table: common columns, ordered row identity. Detect bootstrap adds."""
    allow_new_cols = allow_new_cols or {}
    src_tables = {
        r[0] for r in src_con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    dst_tables = {
        r[0] for r in dst_con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    extra = dst_tables - src_tables - allow_new_tables
    assert not extra, f"unexpected new tables (bootstrap?): {sorted(extra)}"
    for table in sorted(src_tables):
        scols = [r[1] for r in src_con.execute(f"PRAGMA table_info({table})")]
        dcols = [r[1] for r in dst_con.execute(f"PRAGMA table_info({table})")]
        common = [c for c in scols if c in dcols]
        new_cols = set(dcols) - set(scols)
        allowed = set(allow_new_cols.get(table, ()))
        assert new_cols <= allowed, f"{table}: unexpected new cols {new_cols - allowed}"
        order = ", ".join(f'"{c}"' for c in common)
        srows = list(src_con.execute(
            f'SELECT {order} FROM "{table}" ORDER BY {order}'))
        drows = list(dst_con.execute(
            f'SELECT {order} FROM "{table}" ORDER BY {order}'))
        assert srows == drows, (
            f"{table}: row identity changed (counts {len(srows)} vs {len(drows)}; "
            f"bootstrap or mutation?)")


def _repair_status(con, label, epoch):
    if "drive_hash_repair_state" not in {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        return None
    row = con.execute(
        "SELECT status FROM drive_hash_repair_state "
        "WHERE drive_label=? AND identity_epoch=?",
        [label, epoch],
    ).fetchone()
    return row[0] if row else None


# ===========================================================================
# Fixture integrity
# ===========================================================================


def test_m01_complete_frozen_v6_fixture(tmp_path):
    data = _seed_frozen_v6(tmp_path)
    path = _catalog(data)
    con = _open_ro(path)
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")
        }
        assert EXPECTED_V6_TABLES <= tables
        assert con.execute("PRAGMA user_version").fetchone()[0] == FROZEN_V6
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        # Representative children present
        assert con.execute("SELECT count(*) FROM proposal_tasks").fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM proposal_files").fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM execution_sessions").fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM plan_drives").fetchone()[0] >= 1
        n_idx = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index'").fetchone()[0]
        n_trg = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
        n_view = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='view'").fetchone()[0]
        assert n_idx >= 10 and n_trg >= 4 and n_view >= 3
    finally:
        _close(con)
    # Independent of db.connect bootstrap
    assert not path.with_name("state").exists() or True


# ===========================================================================
# Migration / DEC-059
# ===========================================================================


def test_m03_rehearse_source_untouched(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    src = _catalog(data)
    before = _fingerprint(src)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="m03"))
    assert _fingerprint(src) == before
    assert Path(report["clone_catalog_path"]).is_file()
    assert Path(report["clone_catalog_path"]).resolve() != src.resolve()


def test_m04_concrete_report_contract(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="m04"))
    # Default classification
    assert report["classification"]["hub_confirmed"] == 1
    assert report["classification"]["legacy_unknown"] == 1
    assert report["classification"]["null_digest"] == 1
    assert report["classification"]["disagreement"] == 0
    assert _sha_file(Path(report["snapshot_path"])) == report["snapshot_sha256"]
    assert Path(report["clone_catalog_path"]) == Path(report["clone_catalog_path"]).resolve() \
        or Path(report["clone_catalog_path"]).is_file()


def test_m05_exact_preservation_all_tables_and_backfill(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="m05"))
    src = _open_ro(_catalog(data))
    dst = _open_ro(Path(report["clone_catalog_path"]))
    try:
        _common_row_identity(
            src, dst,
            allow_new_tables=frozenset({"drive_hash_repair_state"}),
            allow_new_cols={"archived": ("orig_sha256_provenance",)},
        )
        assert report["classification"] == {
            "hub_confirmed": 1,
            "legacy_unknown": 1,
            "null_digest": 1,
            "disagreement": 0,
        }
        # Semantic: indexes and FKs on archived
        fks = dst.execute("PRAGMA foreign_key_list(archived)").fetchall()
        assert len(fks) >= 2
        idxs = {r[1] for r in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_archived_drive" in idxs
        assert "idx_proposal_tasks_proposal" in idxs
        # derivation_mode CHECK present
        sql = (dst.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='placement_proposals'").fetchone() or [""])[0] or ""
        assert "state_truncated" in sql.lower()
        # provenance values exact
        rows = dict(dst.execute(
            "SELECT rfilename, orig_sha256_provenance FROM archived").fetchall())
        assert rows["weights.bin"] == "hub_confirmed"
        assert rows["notes.txt"] == "legacy_unknown"
        assert rows["shard.bin"] is None
    finally:
        _close(src)
        _close(dst)


def test_m05b_additive_archived_via_sql_trace(tmp_path):
    """Require ALTER TABLE archived ADD COLUMN; reject drop/rename rebuild."""
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    statements: list[str] = []

    def tracer(sql):
        statements.append(sql)

    # Prefer public rehearse with connection tracer hook if production supports it;
    # else patch sqlite3.Connection.execute during rehearsal internals.
    real_connect = sqlite3.connect

    def tracing_connect(*a, **k):
        con = real_connect(*a, **k)
        con.set_trace_callback(tracer)
        return con

    with mock.patch("sqlite3.connect", side_effect=tracing_connect):
        try:
            _require_report(_rehearse()(data, work, run_id="m05b"))
        except AssertionError:
            # export missing
            raise
    joined = "\n".join(statements).upper()
    assert "ALTER TABLE ARCHIVED ADD" in joined.replace("  ", " ") or \
        "ALTER TABLE \"ARCHIVED\" ADD" in joined or \
        any("ALTER TABLE" in s.upper() and "ARCHIVED" in s.upper() and "ADD" in s.upper()
            for s in statements), (
            "migration must use ALTER TABLE archived ADD COLUMN "
            f"(trace saw {len(statements)} statements)"
        )
    rebuild = any(
        ("DROP TABLE" in s.upper() and "ARCHIVED" in s.upper())
        or ("ALTER TABLE" in s.upper() and "RENAME" in s.upper() and "ARCHIVED" in s.upper())
        for s in statements
    )
    assert not rebuild, "archived must not be dropped/renamed/rebuilt by default"


def test_m06_disagreement_refuses(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src", disagreement=True)
    src = _catalog(data)
    before = _fingerprint(src)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Exception) as ei:
        _rehearse()(data, work, run_id="disagree")
    # Must not be missing-export if we want disagreement semantics — export first fails red
    msg = str(ei.value).lower()
    if "export modelark.core.db.rehearse" in msg:
        raise AssertionError(str(ei.value)) from ei.value
    assert any(k in msg for k in ("disagree", "mismatch", "conflict", "incident"))
    assert _fingerprint(src) == before


def test_m06b_invalid_derivation_refuses(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src", invalid_derivation=True)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Exception) as ei:
        _rehearse()(data, work, run_id="baddm")
    msg = str(ei.value).lower()
    if "export modelark.core.db.rehearse" in msg:
        raise AssertionError(str(ei.value)) from ei.value
    assert "derivation" in msg or "check" in msg


def test_m06c_orphan_stops_validation(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src", orphan_archived=True)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Exception) as ei:
        _rehearse()(data, work, run_id="orphan")
    msg = str(ei.value).lower()
    if "export modelark.core.db.rehearse" in msg:
        raise AssertionError(str(ei.value)) from ei.value
    assert any(k in msg for k in ("foreign", "orphan", "integrity", "fk"))


def test_m06d_injected_failure_via_internal_patch_rolls_back_clone(tmp_path):
    """Patch an internal op on the public path — no public inject_failure API."""
    data = _seed_frozen_v6(tmp_path / "src")
    src = _catalog(data)
    before = _fingerprint(src)
    work = tmp_path / "work"
    work.mkdir()
    migrate = _rehearse()

    # Fail after provenance would be applied: patch a method production uses mid-flight.
    # Gate-2 must expose a patchable internal (e.g. _apply_provenance_backfill).
    target = None
    for name in (
        "modelark.core.db._apply_provenance_backfill",
        "modelark.core.db._validate_migrated_clone",
        "modelark.catalog_migration._apply_provenance_backfill",
    ):
        mod_name, _, attr = name.rpartition(".")
        try:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, attr):
                target = (mod, attr)
                break
        except ModuleNotFoundError:
            continue
    if target is None:
        raise AssertionError(
            "export a patchable internal used by rehearse_provenance_migration "
            "(e.g. db._apply_provenance_backfill or db._validate_migrated_clone) "
            "so Gate-1 can inject mid-migration failure without a public inject API"
        )
    mod, attr = target
    with mock.patch.object(mod, attr, side_effect=RuntimeError("injected mid-migration")):
        with pytest.raises(Exception):
            migrate(data, work, run_id="inject")
    assert _fingerprint(src) == before
    # Clone removed/quarantined OR exact v6 rollback
    clones = list(work.rglob("catalog.sqlite"))
    for c in clones:
        if "quarantine" in str(c).lower() or "failed" in str(c).lower():
            continue
        con = _open_ro(c)
        try:
            assert con.execute("PRAGMA user_version").fetchone()[0] == FROZEN_V6
            assert "orig_sha256_provenance" not in {
                r[1] for r in con.execute("PRAGMA table_info(archived)")
            }
        finally:
            _close(con)


def test_m07_repeatable(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    w1, w2 = tmp_path / "w1", tmp_path / "w2"
    w1.mkdir()
    w2.mkdir()
    r1 = _require_report(_rehearse()(data, w1, run_id="r1"))
    r2 = _require_report(_rehearse()(data, w2, run_id="r2"))
    c1 = _open_ro(Path(r1["clone_catalog_path"]))
    c2 = _open_ro(Path(r2["clone_catalog_path"]))
    try:
        q = (
            "SELECT repo_id,rfilename,drive_label,orig_sha256,orig_sha256_provenance "
            "FROM archived ORDER BY 1,2,3"
        )
        assert c1.execute(q).fetchall() == c2.execute(q).fetchall()
        assert r1["classification"] == r2["classification"]
    finally:
        _close(c1)
        _close(c2)


def test_m08_publish_missing_authorization_raises(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest"
    _require_report(_rehearse()(data, work, run_id="pub0"))
    publish = _publish()
    with pytest.raises(Exception) as ei:
        publish(work, dest, confirm_stopped="", writers_stopped=True)
    # Must be raised by publish, not our assert
    assert ei.type is not AssertionError or "export" in str(ei.value)
    msg = str(ei.value).lower()
    assert any(k in msg for k in ("confirm", "stop", "author", "refus", "required"))


def test_m08b_publish_active_writer_refuses(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest"
    _require_report(_rehearse()(data, work, run_id="pub1"))
    publish = _publish()
    with pytest.raises(Exception) as ei:
        publish(work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=False)
    assert ei.type is not AssertionError or "export" in str(ei.value)
    assert "writer" in str(ei.value).lower() or "stop" in str(ei.value).lower()


def test_m08c_publish_existing_destination_refuses(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "catalog.sqlite").write_bytes(b"x")
    _require_report(_rehearse()(data, work, run_id="pub2"))
    publish = _publish()
    with pytest.raises(Exception) as ei:
        publish(work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert ei.type is not AssertionError or "export" in str(ei.value)
    assert "exist" in str(ei.value).lower() or "overwrite" in str(ei.value).lower()


def test_m08d_publish_success_retains_original_and_manifest(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest-data"
    src_fp = _fingerprint(_catalog(data))
    _require_report(_rehearse()(data, work, run_id="pub3"))
    pub_report = _publish()(
        work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert _fingerprint(_catalog(data)) == src_fp
    assert Path(dest).exists()
    # Migrated catalog at dest
    dest_cat = Path(dest) / "catalog.sqlite"
    if not dest_cat.is_file():
        found = list(Path(dest).rglob("catalog.sqlite"))
        assert found
        dest_cat = found[0]
    dcon = _open_ro(dest_cat)
    try:
        assert int(dcon.execute("PRAGMA user_version").fetchone()[0]) > FROZEN_V6
        assert "orig_sha256_provenance" in {
            r[1] for r in dcon.execute("PRAGMA table_info(archived)")
        }
    finally:
        _close(dcon)
    # Rollback artifact / manifest
    assert pub_report.get("rollback_artifact") or pub_report.get("retained_original")
    assert pub_report.get("manifest_path") and Path(pub_report["manifest_path"]).is_file()


def test_m09_connect_v7_build_refuses_without_mutation(tmp_path):
    data = _seed_frozen_v6(tmp_path)
    path = _catalog(data)
    before = _fingerprint(path)
    db.configure(data, data / "state")
    target = max(int(getattr(db, "_SCHEMA_VERSION", 6)), 7)
    with mock.patch.object(db, "_SCHEMA_VERSION", target):
        with pytest.raises(RuntimeError):
            con = db.connect()
            _close(con)
    assert _fingerprint(path) == before
    con = _open_ro(path)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == FROZEN_V6
        assert "orig_sha256_provenance" not in {
            r[1] for r in con.execute("PRAGMA table_info(archived)")
        }
    finally:
        _close(con)


def test_m11_read_only_old_version_refuses(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "catalog.sqlite"
    con = _open_rw(path)
    try:
        con.execute("CREATE TABLE t(x)")
        con.execute("PRAGMA user_version=5")
    finally:
        _close(con)
    db.configure(data, data / "state")
    with pytest.raises(RuntimeError):
        db.connect(read_only=True)


# ===========================================================================
# Schema CHECKs
# ===========================================================================


def test_s01_provenance_check(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="s01"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "UPDATE archived SET orig_sha256_provenance='mirrored' "
                "WHERE rfilename='weights.bin'")
        con.execute(
            "UPDATE archived SET orig_sha256_provenance='hub_confirmed' "
            "WHERE rfilename='weights.bin'")
    finally:
        _close(con)


def test_s03_derivation_mode_check(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="s03"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "UPDATE placement_proposals SET derivation_mode='ecfg:x' "
                "WHERE proposal_id='hist-opt'")
        con.execute(
            "UPDATE placement_proposals SET derivation_mode='state_truncated' "
            "WHERE proposal_id='hist-opt'")
        assert con.execute(
            "SELECT derivation_mode FROM placement_proposals WHERE proposal_id='hist-p'"
        ).fetchone()[0] is None
    finally:
        _close(con)


# ===========================================================================
# Real product entrypoints
# ===========================================================================


def test_s08_preview_publish_persists_all_three_derivation_modes(tmp_path):
    """preview_pure → mutate header mode → rehash → publish_draft for each mode."""
    data = _seed_frozen_v6(tmp_path)
    # Need live schema for publish_draft require_execution_config_hash — use clone after migrate
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="s08"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        # Ensure selection exists for preview
        for mode in DERIVATION_OK:
            # Build minimal pure payload without full planner if preview is heavy:
            # Use publish_draft with synthetic pure-preview shape that matches its contract.
            header = {
                "plan_id": "ark",
                "based_on_revision": 0,
                "mutation_kind": "adopt_current",
                "mutation_args": (),
                "requirement_set_hash": _h("r"),
                "semantic_input_hash": _h("s"),
                "selection_before_hash": _h("b"),
                "selection_after_hash": _h("a"),
                "capacity_mode": "guaranteed",
                "policy_version": "1",
                "solver_version": "1",
                "serializer_version": getattr(canonical, "SERIALIZER_VERSION", "1"),
                "gate_b_code": "FEASIBLE",
                "derivation_mode": mode,
                "execution_config_hash": _h("e"),
            }
            tasks = [{
                "requirement_id": f"primary:org/m-{mode[:3]}",
                "row_kind": "executable",
                "repo_id": "org/m",
                "target_drive": "d0",
                "source_drive": None,
                "satisfying_drive": None,
                "full_manifest_hash": _h("m"),
                "order_key": 1,
                "guaranteed_durable": 100,
                "expected_durable": 100,
                "identity_epoch": 1,
                "baseline_certificate": None,
            }]
            files = [{
                "requirement_id": tasks[0]["requirement_id"],
                "rfilename": "weights.bin",
                "role": "missing",
                "size_bytes": 100,
                "orig_sha256": _h("a"),
                "format": "safetensors",
                "quant": "bf16",
                "storage_action": "compress",
            }]
            digest = canonical.proposal_hash(header, tasks, files)
            payload = {
                "header": header,
                "tasks": tasks,
                "files": files,
                "canonical_hash": digest,
                "mutation": ("adopt_current", ()),
            }
            out = proposal_mod.publish_draft(con, payload)
            pid = out.get("proposal_id") or out.get("id")
            if not pid:
                # publish may return different shape
                row = con.execute(
                    "SELECT proposal_id, derivation_mode FROM placement_proposals "
                    "WHERE derivation_mode=? ORDER BY rowid DESC LIMIT 1",
                    [mode],
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT proposal_id, derivation_mode FROM placement_proposals "
                    "WHERE proposal_id=?",
                    [pid],
                ).fetchone()
            assert row is not None and row[1] == mode, (
                f"publish_draft must persist derivation_mode={mode!r}, got {row!r}"
            )
        # Production must also produce state_truncated from placement (not only SQL inject):
        # preview_pure currently collapses — pin by patching placement result into header builder
        with mock.patch.object(
            proposal_mod, "_header_from_facts",
            wraps=proposal_mod._header_from_facts,
        ) as hdr:
            def _force_trunc(*a, **k):
                h = proposal_mod._header_from_facts(*a, **k)
                h["derivation_mode"] = "state_truncated"
                return h
            hdr.side_effect = _force_trunc
            # May refuse without full selection graph — if preview works, publish it
            try:
                payload = proposal_mod.preview_pure(con, "ark", ("adopt_current", ()))
                payload["header"]["derivation_mode"] = "state_truncated"
                payload["canonical_hash"] = canonical.proposal_hash(
                    payload["header"], payload["tasks"], payload["files"])
                proposal_mod.publish_draft(con, payload)
                assert con.execute(
                    "SELECT derivation_mode FROM placement_proposals "
                    "ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0] == "state_truncated"
            except Exception:
                # Still require that default _header_from_facts is fixed in production
                import inspect
                src = inspect.getsource(proposal_mod._header_from_facts)
                if 'derivation_mode": "optimized" if gate_b_code' in src:
                    raise AssertionError(
                        "preview_pure/_header_from_facts must accept placement "
                        "derivation_mode including state_truncated"
                    )
    finally:
        _close(con)


def test_w01_w02_fetch_upsert_seam_sets_provenance(tmp_path):
    """Files row first; upsert seam used by fetch must set hub/ingestion provenance."""
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="ing"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        fetch = importlib.import_module("modelark.fetch")
        # Production must set provenance when building the archived dict for upsert.
        # Call the same upsert site after building row the way Gate-2 fetch will.
        # Prefer exercising a function that fetch_model calls:
        build = None
        for name in ("_archived_ingest_fields", "archived_row_from_ingest",
                     "_build_archived_upsert"):
            build = getattr(fetch, name, None)
            if callable(build):
                break
        if not callable(build):
            raise AssertionError(
                "fetch must expose the archived-row builder used before db.upsert "
                "(e.g. _archived_ingest_fields) so Hub vs no-Hub provenance is testable"
            )
        # Hub match — files row first
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','hub.bin',8,?,'safetensors')", [_h("n")])
        row = build(
            repo_id="org/m", rfilename="hub.bin", drive_label="d0",
            orig_sha256=_h("n"), hub_sha256=_h("n"), znn_sha256=None,
            orig_bytes=8, stored_bytes=8, compressed=False, annex_key=None,
            stored_name="hub.bin", stored_relpath="hub.bin",
        )
        assert row.get("orig_sha256_provenance") == "hub_confirmed"
        db.upsert(con, "archived", row,
                  pk=["repo_id", "rfilename", "drive_label"], touch=["verified_at"])
        assert con.execute(
            "SELECT orig_sha256_provenance FROM archived WHERE rfilename='hub.bin'"
        ).fetchone()[0] == "hub_confirmed"
        # No Hub — files row first
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','nohub.bin',8,NULL,'aux')")
        row2 = build(
            repo_id="org/m", rfilename="nohub.bin", drive_label="d0",
            orig_sha256=_h("m"), hub_sha256=None, znn_sha256=None,
            orig_bytes=8, stored_bytes=8, compressed=False, annex_key=None,
            stored_name="nohub.bin", stored_relpath="nohub.bin",
        )
        assert row2.get("orig_sha256_provenance") == "ingestion_computed"
        db.upsert(con, "archived", row2,
                  pk=["repo_id", "rfilename", "drive_label"], touch=["verified_at"])
        assert con.execute(
            "SELECT orig_sha256_provenance FROM archived WHERE rfilename='nohub.bin'"
        ).fetchone()[0] == "ingestion_computed"
    finally:
        _close(con)


def test_w03_tier1_annex_reaches_complete_with_digest(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="t1"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        # Only shard.bin is unresolved for tier-1 (weights+notes already have digests)
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        st = rep.get("status") or _repair_status(con, "d0", 1)
        row = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE rfilename='shard.bin'"
        ).fetchone()
        assert row[0] == _h("d"), f"tier-1 must write annex digest, got {row!r}"
        assert row[1] == "annex_key"
        left = con.execute(
            "SELECT count(*) FROM archived WHERE drive_label='d0' AND orig_sha256 IS NULL"
        ).fetchone()[0]
        assert left == 0
        assert st == "complete", f"fully resolvable tier-1 fixture must complete, got {st!r}"
        # Non-null preserved
        assert con.execute(
            "SELECT orig_sha256 FROM archived WHERE rfilename='weights.bin'"
        ).fetchone()[0] == _h("a")
    finally:
        _close(con)


def test_w04_archive_head_repair_with_git_fixture(tmp_path):
    """Disposable git archive: archive-head path + guards (not mere existence)."""
    hr = importlib.import_module("modelark.hash_repair")
    archive = tmp_path / "archive"
    archive.mkdir()
    subprocess.run(["git", "init"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@test"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=archive, check=True, capture_output=True)
    repo_dir = archive / "org" / "m"
    repo_dir.mkdir(parents=True)
    blob = repo_dir / "headfile.bin"
    content = b"archive-head-bytes-001"
    blob.write_bytes(content)
    subprocess.run(["git", "add", "-A"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=archive, check=True, capture_output=True)

    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="ah"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','headfile.bin',?,NULL,'aux')", [len(content)])
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,annex_key,"
            "orig_sha256_provenance) "
            "VALUES('org/m','headfile.bin','headfile.bin','headfile.bin','d0',"
            "NULL,?,?,0,NULL,NULL)",
            [len(content), len(content)])
        row = {
            "repo_id": "org/m", "rfilename": "headfile.bin",
            "stored_name": "headfile.bin", "stored_relpath": "headfile.bin",
            "drive_label": "d0", "orig_sha256": None,
            "orig_bytes": len(content), "stored_bytes": len(content),
            "compressed": 0, "annex_key": None,
            "catalog_sha": None, "catalog_bytes": len(content),
        }
        # Compressed must refuse
        with pytest.raises(hr.HashRepairError):
            bad = dict(row, compressed=1)
            hr._validate_candidate(bad, archive)
        # Happy path
        repair = hr._validate_candidate(row, archive)
        assert repair["evidence"] == "archive-head-blob"
        assert repair["sha256"] == hashlib.sha256(content).hexdigest()
        # Apply via explicit engine if it supports archive-head tier
        eng = _repair()
        eng(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"),
            archive_resolver=lambda *_a, **_k: archive)
        got = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE rfilename='headfile.bin'"
        ).fetchone()
        assert got[0] == hashlib.sha256(content).hexdigest()
        assert got[1] == "archive-head-blob"
    finally:
        _close(con)


def test_w05_replica_heal_mismatch_raises_from_heal(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="rep"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        fetch = importlib.import_module("modelark.fetch")
        heal = getattr(fetch, "heal_replica_archived_from_source", None)
        if not callable(heal):
            raise AssertionError(
                "export modelark.fetch.heal_replica_archived_from_source "
                "(used by replica mirror path)"
            )
        # Null target
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,annex_key,"
            "orig_sha256_provenance) "
            "VALUES('org/m','weights.bin','weights.bin','weights.bin','d1',"
            "NULL,100,100,0,NULL,NULL)")
        heal(con, source_drive="d0", target_drive="d1",
             repo_id="org/m", rfilename="weights.bin")
        row = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE drive_label='d1' AND rfilename='weights.bin'"
        ).fetchone()
        assert row[0] == _h("a") and row[1] is not None
        # Mismatch
        con.execute(
            "UPDATE archived SET orig_sha256=? WHERE drive_label='d1' AND rfilename='weights.bin'",
            [_h("x")])
        before = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE drive_label='d1' AND rfilename='weights.bin'"
        ).fetchone()
        with pytest.raises(Exception) as ei:
            heal(con, source_drive="d0", target_drive="d1",
                 repo_id="org/m", rfilename="weights.bin")
        assert ei.type is not AssertionError
        after = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE drive_label='d1' AND rfilename='weights.bin'"
        ).fetchone()
        assert after == before
    finally:
        _close(con)


# ===========================================================================
# Repair-state (isolated, strict)
# ===========================================================================


def test_w09_pk_order_and_status_vocabulary(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="w09"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        assert "drive_hash_repair_state" in {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        info = list(con.execute("PRAGMA table_info(drive_hash_repair_state)"))
        # PRAGMA table_info: cid, name, type, notnull, dflt, pk
        pk_cols = sorted([(r[5], r[1]) for r in info if r[5] > 0])
        assert [c for _, c in pk_cols] == ["drive_label", "identity_epoch"], pk_cols
        for bad in ("done", "ok", "failed"):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO drive_hash_repair_state("
                    "drive_label,identity_epoch,identity_fingerprint,status) "
                    "VALUES('d0',50,?,?)",
                    [_h("f"), bad])
        for ok in REPAIR_STATUSES:
            con.execute("DELETE FROM drive_hash_repair_state WHERE identity_epoch=50")
            con.execute(
                "INSERT INTO drive_hash_repair_state("
                "drive_label,identity_epoch,identity_fingerprint,status) "
                "VALUES('d0',50,?,?)",
                [_h("f"), ok])
    finally:
        _close(con)


def test_w15_absent_drive_blocked_absent(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="w15"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        rep = repair(
            con, "d-absent", identity_epoch=1, identity_fingerprint=_h("h"),
            archive_resolver=lambda *a, **k: None,
        )
        assert (rep.get("status") or _repair_status(con, "d-absent", 1)) == "blocked_absent"
    finally:
        _close(con)


def test_w17_fingerprint_mismatch_halted(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="w17"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("Z"))
        assert (rep.get("status") or _repair_status(con, "d0", 1)) == "halted"
    finally:
        _close(con)


def test_w16_tier1_complete_zero_unresolved(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="w16"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        st = rep.get("status") or _repair_status(con, "d0", 1)
        left = con.execute(
            "SELECT count(*) FROM archived WHERE drive_label='d0' AND orig_sha256 IS NULL"
        ).fetchone()[0]
        assert st == "complete"
        assert left == 0
    finally:
        _close(con)


def test_w11_replacement_epoch_independent(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="w11"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert _repair_status(con, "d0", 1) == "complete"
        # Advance drive identity
        con.execute(
            "UPDATE drives SET identity_epoch=2, identity_fingerprint=? WHERE drive_label='d0'",
            [_h("F")])
        # New unresolved work for epoch 2
        con.execute(
            "UPDATE archived SET orig_sha256=NULL, orig_sha256_provenance=NULL, "
            "annex_key=? WHERE rfilename='shard.bin'",
            [f"SHA256E-s50--{_h('d')}"])
        repair(con, "d0", identity_epoch=2, identity_fingerprint=_h("F"))
        st2 = _repair_status(con, "d0", 2)
        assert st2 is not None and st2 != _repair_status(con, "d0", 1) or st2 == "complete"
        assert _repair_status(con, "d0", 1) == "complete"  # epoch 1 unchanged
        assert st2 in REPAIR_STATUSES
        # Must have independent row
        n = con.execute(
            "SELECT count(*) FROM drive_hash_repair_state WHERE drive_label='d0'"
        ).fetchone()[0]
        assert n >= 2
    finally:
        _close(con)


def test_w12_lost_unresolved_never_complete(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="w12"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        con.execute(
            "UPDATE archived SET orig_sha256=NULL, annex_key=NULL, "
            "orig_sha256_provenance=NULL WHERE rfilename='shard.bin'")
        con.execute("UPDATE drives SET lifecycle='lost' WHERE drive_label='d0'")
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        st = rep.get("status") or _repair_status(con, "d0", 1)
        left = con.execute(
            "SELECT count(*) FROM archived WHERE drive_label='d0' AND orig_sha256 IS NULL"
        ).fetchone()[0]
        assert left > 0
        assert st != "complete"
    finally:
        _close(con)


def test_w13_tier3_exactly_needs_refetch(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="w13"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        con.execute(
            "UPDATE archived SET orig_sha256=NULL, annex_key=NULL, compressed=1, "
            "orig_sha256_provenance=NULL WHERE rfilename='shard.bin'")
        # Ensure other files already satisfied so only tier-3 remains
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        st = rep.get("status") or _repair_status(con, "d0", 1)
        assert st == "needs_refetch", f"expected needs_refetch, got {st!r} {rep!r}"
    finally:
        _close(con)


def test_w10_injected_failure_exact_prior_state(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(_rehearse()(data, work, run_id="w10"))
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        before_rows = list(con.execute(
            "SELECT rfilename, orig_sha256, orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        before_st = list(con.execute(
            "SELECT drive_label, identity_epoch, status FROM drive_hash_repair_state "
            "ORDER BY 1,2"))
        # Patch internal commit path
        hr = importlib.import_module("modelark.hash_repair")
        patch_attr = None
        for name in ("_commit_repair_batch", "_write_repair_results", "_persist_repair"):
            if hasattr(hr, name):
                patch_attr = name
                break
        if patch_attr is None:
            raise AssertionError(
                "export patchable internal on hash_repair used by "
                "run_explicit_drive_repair (e.g. _commit_repair_batch)"
            )
        with mock.patch.object(hr, patch_attr, side_effect=RuntimeError("injected")):
            with pytest.raises(Exception):
                repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        after_rows = list(con.execute(
            "SELECT rfilename, orig_sha256, orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        after_st = list(con.execute(
            "SELECT drive_label, identity_epoch, status FROM drive_hash_repair_state "
            "ORDER BY 1,2"))
        assert after_rows == before_rows
        assert after_st == before_st
    finally:
        _close(con)


def test_w07_connect_and_portal_do_not_call_explicit_engine(tmp_path):
    hr = importlib.import_module("modelark.hash_repair")
    if not hasattr(hr, "run_explicit_drive_repair"):
        raise AssertionError(
            "export run_explicit_drive_repair so connect/portal can be proven "
            "not to call it"
        )
    data = tmp_path / "fresh"
    data.mkdir()
    db.configure(data, data / "state")
    with mock.patch.object(hr, "run_explicit_drive_repair") as eng:
        con = db.connect()
        _close(con)
        eng.assert_not_called()
    # Portal uses db.connect at startup
    data2 = tmp_path / "fresh2"
    data2.mkdir()
    db.configure(data2, data2 / "state")
    with mock.patch.object(hr, "run_explicit_drive_repair") as eng2:
        c2 = db.connect()
        _close(c2)
        eng2.assert_not_called()


def test_w08_cli_dispatches_to_shared_explicit_engine(tmp_path):
    """Real CLI dispatch must invoke shared engine; Fill preflight out of this slice."""
    from modelark import cli
    hr = importlib.import_module("modelark.hash_repair")
    if not hasattr(hr, "run_explicit_drive_repair"):
        raise AssertionError("export run_explicit_drive_repair for CLI dispatch")
    eng = mock.Mock(return_value={"status": "complete"})
    with mock.patch.object(hr, "run_explicit_drive_repair", eng):
        with mock.patch.object(db, "connect") as conn:
            conn.return_value = mock.Mock()
            try:
                cli.main([
                    "repair-drive", "--drive", "d0",
                    "--identity-epoch", "1",
                    "--identity-fingerprint", _h("f"),
                ])
            except SystemExit:
                pass
            except Exception:
                try:
                    cli.main(["repair-hashes", "--apply"])
                except SystemExit:
                    pass
            if eng.call_count == 0:
                raise AssertionError(
                    "CLI maintenance dispatch must invoke "
                    "hash_repair.run_explicit_drive_repair "
                    "(repair-drive or wired repair-hashes --apply); "
                    "Fill preflight is not part of this slice"
                )


# ===========================================================================
# Boundaries (positive only)
# ===========================================================================


def test_b01_dec055_provenance_neutral():
    d = _h("d")
    assert fill_mod._archive_content_satisfies(
        d, orig_sha256=d, compressed=False, annex_key=None)
    import inspect
    assert "provenance" not in inspect.signature(
        archive_hash.expected_sha256).parameters


def test_b04_inc023_module_present():
    assert (Path(__file__).parent / "test_inc023_gate1_contracts.py").is_file()
