"""DEC-053 / DEC-054 / DEF-034 Gate-1 contracts — remediation 4 (contracts only).

Contracts only. No production. Expected-red until Gate-2 lands clone-first
provenance migration, provenance/derivation CHECKs, explicit drive repair, and
replica heal.

Frozen v6 catalogs load from ``tests/fixtures/catalog_v6.sql``.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db
from modelark import archive_hash, fill as fill_mod, proposal as proposal_mod
from modelark import proposal_canonical as canonical
from modelark.web import data as web_data


FIXTURE_SQL = Path(__file__).resolve().parent / "fixtures" / "catalog_v6.sql"
FROZEN_V6 = 6
PROVENANCE_VALUES = frozenset({
    "hub_confirmed", "ingestion_computed", "annex_key",
    "archive-head-blob", "legacy_unknown",
})
DERIVATION_VALUES = frozenset({"optimized", "state_truncated", "canonical_fallback"})
REPAIR_STATUSES = frozenset({
    "pending", "running", "blocked_absent", "needs_refetch", "halted", "complete",
})
EXPECTED_V6_TABLES = frozenset({
    "models", "files", "drives", "replicas", "verifications", "selection",
    "archived", "fetch_events", "plans", "plan_drives",
    "drive_dirty_generations", "drive_clean_anchors",
    "planner_state", "placement_proposals", "proposal_tasks", "proposal_files",
    "execution_sessions",
})
ALLOW_NEW_TABLES = frozenset({"drive_hash_repair_state"})
ALLOW_NEW_COLS = {"archived": frozenset({"orig_sha256_provenance"})}


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_db_paths():
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    # Reset portal singleton between tests
    web_data._con = None  # type: ignore[attr-defined]
    try:
        yield
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old
        web_data._con = None  # type: ignore[attr-defined]


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
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint(path: Path) -> dict:
    return {
        "sha256": _sha_file(path) if path.is_file() else None,
        "size": path.stat().st_size if path.is_file() else None,
        "wal_present": (path.parent / f"{path.name}-wal").is_file(),
        "shm_present": (path.parent / f"{path.name}-shm").is_file(),
        "wal_size": (
            (path.parent / f"{path.name}-wal").stat().st_size
            if (path.parent / f"{path.name}-wal").is_file() else None),
        "shm_size": (
            (path.parent / f"{path.name}-shm").stat().st_size
            if (path.parent / f"{path.name}-shm").is_file() else None),
    }


def _logical_identity(con: sqlite3.Connection) -> str:
    """Canonical logical identity over every user table (ordered rows/cols)."""
    digest = hashlib.sha256()
    tables = sorted(
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )
    for table in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        # Stable: column-name order as declared
        order = ", ".join(f'"{c}"' for c in cols)
        digest.update(table.encode())
        digest.update(b"|")
        digest.update(",".join(cols).encode())
        digest.update(b"\n")
        for row in con.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}'):
            digest.update(repr(tuple(row)).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _semantic_shape(con: sqlite3.Connection, table: str) -> dict:
    """Full semantic column/FK/index shape (type, nullability, default, PK order,
    uniqueness/origin/partial, indexed columns). Not raw CREATE SQL identity."""
    cols = list(con.execute(f'PRAGMA table_info("{table}")'))
    # cid, name, type, notnull, dflt_value, pk
    col_shape = [
        {
            "name": r[1], "type": (r[2] or "").upper(),
            "notnull": int(r[3]), "dflt": r[4], "pk": int(r[5]),
        }
        for r in cols
    ]
    pk_order = [c["name"] for c in sorted(
        [c for c in col_shape if c["pk"] > 0], key=lambda c: c["pk"])]
    fks = [
        {
            "id": r[0], "seq": r[1], "table": r[2], "from": r[3], "to": r[4],
            "on_update": r[5], "on_delete": r[6],
        }
        for r in con.execute(f'PRAGMA foreign_key_list("{table}")')
    ]
    # PRAGMA index_list: seq, name, unique, origin, partial
    indexes = []
    for seq, name, unique, origin, partial in con.execute(
            f'PRAGMA index_list("{table}")'):
        if not name or str(name).startswith("sqlite_"):
            continue
        indexed_cols = [
            r[2] for r in con.execute(f'PRAGMA index_info("{name}")')
            if r[2] is not None
        ]
        indexes.append({
            "name": name,
            "unique": int(unique),
            "origin": origin,
            "partial": int(partial),
            "columns": indexed_cols,
        })
    indexes.sort(key=lambda x: x["name"] or "")
    return {
        "columns": col_shape,
        "pk_order": pk_order,
        "fks": sorted(fks, key=lambda x: (x["id"], x["seq"])),
        "indexes": indexes,
    }


def _assert_check_probes(con: sqlite3.Connection) -> None:
    """Provenance + derivation CHECK probes on a catalog that has probe rows."""
    for bad in ("mirrored", "foo", ""):
        _check_rejects(
            con, "archived", "orig_sha256_provenance", bad,
            "rfilename=?", ["weights.bin"])
    for good in PROVENANCE_VALUES:
        con.execute(
            "UPDATE archived SET orig_sha256_provenance=? "
            "WHERE rfilename='weights.bin'", [good])
    con.execute(
        "UPDATE archived SET orig_sha256_provenance=NULL "
        "WHERE rfilename='weights.bin'")
    for bad in ("ecfg:x", "arbitrary", ""):
        _check_rejects(
            con, "placement_proposals", "derivation_mode", bad,
            "proposal_id=?", ["hist-opt"])
    for good in DERIVATION_VALUES:
        con.execute(
            "UPDATE placement_proposals SET derivation_mode=? "
            "WHERE proposal_id='hist-opt'", [good])
    # historical NULL remains legal
    con.execute(
        "UPDATE placement_proposals SET derivation_mode=NULL "
        "WHERE proposal_id='hist-p'")


def _seed_fresh_check_probe_rows(con: sqlite3.Connection) -> None:
    """Minimal legal rows so CHECK UPDATE probes can run on a fresh catalog."""
    con.execute(
        "INSERT OR IGNORE INTO models(repo_id,status,numcopies) "
        "VALUES('org/m','archived',1)")
    con.execute(
        "INSERT OR IGNORE INTO files(repo_id,rfilename,size_bytes,sha256,format) "
        "VALUES('org/m','weights.bin',100,?,'safetensors')", [_h("a")])
    con.execute(
        "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,"
        "raid_backed,identity_epoch,identity_fingerprint,lifecycle,eligibility,"
        "write_authority) "
        "VALUES('d0',?,?, 'primary',0,1,?, 'active','enabled','unknown')",
        [10**12, 10**12, _h("f")])
    con.execute(
        "INSERT OR IGNORE INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
        "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,"
        "orig_sha256_provenance) "
        "VALUES('org/m','weights.bin','weights.bin','weights.bin','d0',?,100,100,0,"
        "'hub_confirmed')", [_h("a")])
    con.execute(
        "INSERT OR IGNORE INTO plans(plan_id,name,is_active,capacity_mode) "
        "VALUES('ark','Ark',1,'guaranteed')")
    con.execute(
        "INSERT OR IGNORE INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,mutation_args_json,serializer_version,derivation_mode) "
        "VALUES('hist-opt','ark',0,'draft',?,'adopt_current','[]','1','optimized')",
        [_h("2")])
    con.execute(
        "INSERT OR IGNORE INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,mutation_args_json,serializer_version,derivation_mode) "
        "VALUES('hist-p','ark',0,'draft',?,'adopt_current','[]','1',NULL)",
        [_h("1")])


def _check_rejects(con, table: str, col: str, bad_value, where_sql: str, where_params):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            f'UPDATE "{table}" SET "{col}"=? WHERE {where_sql}',
            [bad_value, *where_params],
        )


# ---------------------------------------------------------------------------
# Surface loaders
# ---------------------------------------------------------------------------


def _rehearse():
    fn = getattr(db, "rehearse_provenance_migration", None)
    if not callable(fn):
        raise AssertionError(
            "export modelark.core.db.rehearse_provenance_migration("
            "source_dir, work_dir, *, run_id) -> dict"
        )
    return fn


def _publish():
    fn = getattr(db, "publish_provenance_migration", None)
    if not callable(fn):
        raise AssertionError(
            "export modelark.core.db.publish_provenance_migration("
            "work_dir, dest_dir, *, confirm_stopped, writers_stopped)"
        )
    return fn


def _repair():
    hr = importlib.import_module("modelark.hash_repair")
    fn = getattr(hr, "run_explicit_drive_repair", None)
    if not callable(fn):
        raise AssertionError(
            "export modelark.hash_repair.run_explicit_drive_repair"
        )
    return fn


def _require_report(report: dict, *, source_identity: str) -> dict:
    """Concrete successful rehearsal report — no string-search acceptance."""
    required = (
        "status", "source_user_version", "clone_user_version",
        "source_integrity", "clone_integrity",
        "source_foreign_key_violations", "clone_foreign_key_violations",
        "source_content_identity", "clone_content_identity",
        "classification", "snapshot_path", "snapshot_sha256",
        "manifest_path", "manifest_status", "clone_catalog_path", "manifest",
    )
    missing = [k for k in required if k not in report]
    assert not missing, f"report missing keys {missing}; got {sorted(report)}"
    assert report["status"] == "ok"
    assert report["source_user_version"] == FROZEN_V6
    assert int(report["clone_user_version"]) > FROZEN_V6
    assert report["source_integrity"] == "ok"
    assert report["clone_integrity"] == "ok"
    assert report["source_foreign_key_violations"] == []
    assert report["clone_foreign_key_violations"] == []
    assert report["source_content_identity"] == source_identity, (
        "reported source_content_identity must equal independently computed "
        f"logical identity\n reported={report['source_content_identity']!r}\n"
        f" computed={source_identity!r}"
    )
    assert isinstance(report["clone_content_identity"], str)
    assert len(report["clone_content_identity"]) == 64
    cls = report["classification"]
    for k in ("hub_confirmed", "legacy_unknown", "null_digest", "disagreement"):
        assert k in cls and isinstance(cls[k], int), cls
    assert report["manifest_status"] == "validated"
    snap = Path(report["snapshot_path"])
    assert snap.is_file()
    assert _sha_file(snap) == report["snapshot_sha256"]
    assert len(report["snapshot_sha256"]) == 64
    man_path = Path(report["manifest_path"])
    assert man_path.is_file()
    clone = Path(report["clone_catalog_path"])
    assert clone.is_file()
    # Manifest structure
    man = report["manifest"]
    if isinstance(man, str):
        man = json.loads(Path(man).read_text() if Path(man).is_file() else man)
    assert isinstance(man, dict)
    for key in ("source_db", "source_wal", "source_shm"):
        assert key in man, f"manifest missing {key}"
        entry = man[key]
        assert set(entry) >= {"path", "size", "sha256", "present"}
        if entry["present"]:
            p = Path(entry["path"])
            assert p.is_file(), entry
            assert p.stat().st_size == entry["size"]
            assert _sha_file(p) == entry["sha256"]
        else:
            assert entry["size"] is None or entry["size"] == 0
            assert entry["sha256"] is None or entry["sha256"] == ""
    return report


# ---------------------------------------------------------------------------
# Frozen v6 construction
# ---------------------------------------------------------------------------


def _apply_frozen_sql(path: Path) -> None:
    assert FIXTURE_SQL.is_file(), f"missing {FIXTURE_SQL}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    for side in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if side.exists():
            side.unlink()
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        con.executescript(FIXTURE_SQL.read_text())
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == FROZEN_V6
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")
        }
        assert not (EXPECTED_V6_TABLES - tables)
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
    enable_wal: bool = False,
) -> Path:
    data = root / "v6-data"
    data.mkdir(parents=True, exist_ok=True)
    path = data / "catalog.sqlite"
    _apply_frozen_sql(path)
    con = _open_rw(path)
    try:
        if enable_wal:
            con.execute("PRAGMA journal_mode=WAL")
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
        for lab, fp, role in (
            # Fingerprints must be 64-hex (DEC-053/054 repair identity).
            ("d0", _h("f"), "primary"),
            ("d1", _h("e"), "replica"),
            ("d-absent", _h("b"), "primary"),
            ("d-lost-empty", _h("c"), "primary"),
        ):
            con.execute(
                "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
                "identity_epoch,identity_fingerprint,lifecycle,eligibility,write_authority) "
                "VALUES(?,?,?, ?,0,1,?, 'active','enabled','unknown')",
                [lab, 10**12, 10**12, role, fp])
        con.execute(
            "UPDATE drives SET lifecycle='lost' WHERE drive_label='d-lost-empty'")
        con.execute(
            "INSERT INTO plans(plan_id,name,is_active,capacity_mode) "
            "VALUES('ark','Ark',1,'guaranteed')")
        con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','d0')")
        con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','d1')")
        con.execute(
            "INSERT INTO selection(repo_id,finalized_at) VALUES('org/m',CURRENT_TIMESTAMP)")
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


def _repair_status(con, label, epoch):
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "drive_hash_repair_state" not in tables:
        return None
    row = con.execute(
        "SELECT status FROM drive_hash_repair_state "
        "WHERE drive_label=? AND identity_epoch=?",
        [label, epoch],
    ).fetchone()
    return row[0] if row else None


def _common_row_identity(src_con, dst_con) -> None:
    src_tables = {
        r[0] for r in src_con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")
    }
    dst_tables = {
        r[0] for r in dst_con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")
    }
    extra = dst_tables - src_tables - ALLOW_NEW_TABLES
    assert not extra, f"unexpected new tables: {sorted(extra)}"
    for table in sorted(src_tables):
        scols = [r[1] for r in src_con.execute(f'PRAGMA table_info("{table}")')]
        dcols = [r[1] for r in dst_con.execute(f'PRAGMA table_info("{table}")')]
        common = [c for c in scols if c in dcols]
        new_cols = set(dcols) - set(scols)
        assert new_cols <= ALLOW_NEW_COLS.get(table, frozenset()), (
            f"{table}: unexpected cols {new_cols}")
        order = ", ".join(f'"{c}"' for c in common)
        srows = list(src_con.execute(
            f'SELECT {order} FROM "{table}" ORDER BY {order}'))
        drows = list(dst_con.execute(
            f'SELECT {order} FROM "{table}" ORDER BY {order}'))
        assert srows == drows, f"{table}: row identity drift ({len(srows)} vs {len(drows)})"


# ===========================================================================
# M01 fixture
# ===========================================================================


def test_m01_complete_frozen_v6_fixture(tmp_path):
    data = _seed_frozen_v6(tmp_path)
    con = _open_ro(_catalog(data))
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
        assert con.execute("SELECT count(*) FROM proposal_tasks").fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM execution_sessions").fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] >= 1
    finally:
        _close(con)


# ===========================================================================
# Snapshot / report evidence
# ===========================================================================


def test_m04_wal_snapshot_identity_and_manifest(tmp_path):
    """WAL-resident committed content, logical identity match, exact manifest."""
    data = _seed_frozen_v6(tmp_path / "src", enable_wal=True)
    path = _catalog(data)
    # Keeper connection: commit marker, leave open so bytes may live in WAL
    keeper = sqlite3.connect(str(path), isolation_level=None)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute(
        "INSERT INTO models(repo_id,status,numcopies) "
        "VALUES('org/wal-marker','discovered',1)")
    # Autocommit: committed; do not checkpoint; keep open
    assert (path.parent / f"{path.name}-wal").is_file()
    # Identity including marker (visible to any connection via WAL)
    id_con = _open_ro(path)
    try:
        source_identity = _logical_identity(id_con)
        assert id_con.execute(
            "SELECT 1 FROM models WHERE repo_id='org/wal-marker'"
        ).fetchone()
    finally:
        _close(id_con)

    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="m04-wal"),
        source_identity=source_identity,
    )
    # Snapshot must contain WAL marker
    snap_con = _open_ro(Path(report["snapshot_path"]))
    try:
        assert snap_con.execute(
            "SELECT 1 FROM models WHERE repo_id='org/wal-marker'"
        ).fetchone(), "snapshot missing WAL-committed content"
        assert _logical_identity(snap_con) == source_identity
    finally:
        _close(snap_con)
    # Independent clone_content_identity vs logical identity of reported clone
    clone_con = _open_ro(Path(report["clone_catalog_path"]))
    try:
        assert report["clone_content_identity"] == _logical_identity(clone_con)
    finally:
        _close(clone_con)
    # Manifest path/size/hash validated in _require_report; pin presence fields
    man = report["manifest"]
    if not isinstance(man, dict):
        man = json.loads(Path(report["manifest_path"]).read_text())
    assert man["source_db"]["present"] is True
    assert man["source_db"]["size"] == Path(man["source_db"]["path"]).stat().st_size
    assert man["source_db"]["sha256"] == _sha_file(Path(man["source_db"]["path"]))
    # WAL artifact: present while keeper open
    assert man["source_wal"]["present"] is True
    assert man["source_wal"]["size"] > 0
    assert man["source_wal"]["sha256"] == _sha_file(Path(man["source_wal"]["path"]))
    _close(keeper)


def test_m03_source_bytes_untouched(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    path = _catalog(data)
    before = _fingerprint(path)
    id_con = _open_ro(path)
    try:
        ident = _logical_identity(id_con)
    finally:
        _close(id_con)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="m03"), source_identity=ident)
    # Entire source fingerprint (main + sidecars presence/size) unchanged
    assert _fingerprint(path) == before
    # Independent clone_content_identity vs logical identity of reported clone
    clone_con = _open_ro(Path(report["clone_catalog_path"]))
    try:
        assert report["clone_content_identity"] == _logical_identity(clone_con)
    finally:
        _close(clone_con)


def test_m05_full_preservation_empty_repair_state_and_semantic_parity(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    id_con = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(id_con)
        src_plan_drives = id_con.execute(
            "SELECT count(*) FROM plan_drives").fetchone()[0]
    finally:
        _close(id_con)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="m05"), source_identity=ident)
    src = _open_ro(_catalog(data))
    dst = _open_ro(Path(report["clone_catalog_path"]))
    try:
        _common_row_identity(src, dst)
        assert dst.execute(
            "SELECT count(*) FROM plan_drives").fetchone()[0] == src_plan_drives
        # New repair-state table initially empty
        assert "drive_hash_repair_state" in {
            r[0] for r in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert dst.execute(
            "SELECT count(*) FROM drive_hash_repair_state").fetchone()[0] == 0
        assert report["classification"] == {
            "hub_confirmed": 1, "legacy_unknown": 1,
            "null_digest": 1, "disagreement": 0,
        }
        # Fresh comparison catalog: empty dir + ordinary packaged db.connect()
        # (target schema) — not a second v6 rehearsal.
        fresh_dir = tmp_path / "fresh-target"
        fresh_dir.mkdir()
        db.configure(fresh_dir, fresh_dir / "state")
        fresh = db.connect()
        try:
            fresh_ver = int(fresh.execute("PRAGMA user_version").fetchone()[0])
            mig_ver = int(dst.execute("PRAGMA user_version").fetchone()[0])
            assert fresh_ver == mig_ver, (
                f"fresh and migrated versions must match: {fresh_ver} vs {mig_ver}")
            assert fresh_ver > FROZEN_V6 and mig_ver > FROZEN_V6, (
                f"both must exceed frozen v6; fresh={fresh_ver} migrated={mig_ver}")
            for table in ("archived", "placement_proposals", "drive_hash_repair_state"):
                a = _semantic_shape(dst, table)
                b = _semantic_shape(fresh, table)
                assert a["columns"] == b["columns"], table
                assert a["pk_order"] == b["pk_order"], table
                assert a["fks"] == b["fks"], table
                assert a["indexes"] == b["indexes"], table
        finally:
            _close(fresh)
    finally:
        _close(src)
        _close(dst)

    # CHECK probes against both migrated clone and fresh target-schema catalog
    wdst = _open_rw(Path(report["clone_catalog_path"]))
    try:
        _assert_check_probes(wdst)
    finally:
        _close(wdst)
    wfresh = _open_rw(fresh_dir / "catalog.sqlite")
    try:
        _seed_fresh_check_probe_rows(wfresh)
        _assert_check_probes(wfresh)
    finally:
        _close(wfresh)


def test_m05_app_open_plan_projection_without_bootstrap(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
        before_plans = idc.execute("SELECT count(*) FROM plans").fetchone()[0]
        before_pd = idc.execute("SELECT count(*) FROM plan_drives").fetchone()[0]
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="m05-app"), source_identity=ident)
    clone = Path(report["clone_catalog_path"])
    # Application open on clone via configure + connect (clone is designated disposable)
    clone_dir = clone.parent
    db.configure(clone_dir, clone_dir / "state")
    con = db.connect()
    try:
        assert con.execute("SELECT count(*) FROM plans").fetchone()[0] == before_plans
        assert con.execute("SELECT count(*) FROM plan_drives").fetchone()[0] == before_pd
        # plan entrypoint
        from modelark import plan as plan_mod
        p = plan_mod.get(con, "ark")
        assert p is not None and p["plan_id"] == "ark"
        # projection entrypoint
        from modelark.execution_projection import project_pure
        from types import SimpleNamespace
        proposal = {
            "lifecycle": "approved",
            "proposal_id": "hist-p",
            "plan_id": "ark",
            "tasks": [{
                "requirement_id": "primary:org/m",
                "row_kind": "executable",
                "repo_id": "org/m",
                "target_drive": "d0",
                "source_drive": None,
                "full_manifest_hash": _h("3"),
                "order_key": 1,
                "guaranteed_durable": 100,
                "expected_durable": 100,
                "identity_epoch": 1,
                "baseline_certificate": None,
            }],
            "files": [{
                "requirement_id": "primary:org/m",
                "rfilename": "weights.bin",
                "role": "missing",
                "size_bytes": 100,
                "orig_sha256": _h("a"),
                "format": "safetensors",
                "quant": "bf16",
                "storage_action": "compress",
            }],
            "requirement_set_hash": _h("r"),
            "semantic_input_hash": _h("s"),
            "capacity_mode": "guaranteed",
            "policy_version": "1",
            "solver_version": "1",
        }
        # Use session bundle if available
        from modelark.execution_session import _catalog_projection_bundle
        services = SimpleNamespace(
            observe_exact_capacity=lambda *a, **k: {
                "d0": SimpleNamespace(
                    kind="offline", executable=True, admissible_free=10**12),
                "d1": SimpleNamespace(
                    kind="offline", executable=True, admissible_free=10**12),
            })
        inp, graph = _catalog_projection_bundle(
            con, proposal, ["d0", "d1"], services, {"capacity_mode": "guaranteed"})
        out = project_pure(
            proposal, inp, graph, SimpleNamespace(parked_gated_repos=frozenset()))
        assert out is not None
        # No bootstrap row growth
        assert con.execute("SELECT count(*) FROM plans").fetchone()[0] == before_plans
        assert con.execute("SELECT count(*) FROM plan_drives").fetchone()[0] == before_pd
    finally:
        _close(con)


def test_m05b_additive_alter_trace(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    statements: list[str] = []
    real_connect = sqlite3.connect

    def tracing_connect(*a, **k):
        con = real_connect(*a, **k)
        con.set_trace_callback(lambda s: statements.append(s))
        return con

    with mock.patch("sqlite3.connect", side_effect=tracing_connect):
        _require_report(
            _rehearse()(data, work, run_id="alter"), source_identity=ident)
    assert any(
        "ALTER TABLE" in s.upper() and "ARCHIVED" in s.upper() and "ADD" in s.upper()
        for s in statements
    ), f"expected ALTER TABLE archived ADD; saw {len(statements)} statements"
    assert not any(
        ("DROP TABLE" in s.upper() and "ARCHIVED" in s.upper())
        or ("RENAME" in s.upper() and "ARCHIVED" in s.upper())
        for s in statements
    )


def test_m07_repeatability_full_identity_and_schema(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    w1, w2 = tmp_path / "w1", tmp_path / "w2"
    w1.mkdir()
    w2.mkdir()
    r1 = _require_report(_rehearse()(data, w1, run_id="r1"), source_identity=ident)
    r2 = _require_report(_rehearse()(data, w2, run_id="r2"), source_identity=ident)
    c1 = _open_ro(Path(r1["clone_catalog_path"]))
    c2 = _open_ro(Path(r2["clone_catalog_path"]))
    try:
        assert _logical_identity(c1) == _logical_identity(c2)
        for table in ("archived", "placement_proposals", "drive_hash_repair_state"):
            assert _semantic_shape(c1, table)["pk_order"] == \
                _semantic_shape(c2, table)["pk_order"]
            assert _semantic_shape(c1, table)["columns"] == \
                _semantic_shape(c2, table)["columns"]
        assert r1["classification"] == r2["classification"]
    finally:
        _close(c1)
        _close(c2)


def test_m06_disagreement_refuses(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src", disagreement=True)
    path = _catalog(data)
    before = _fingerprint(path)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Exception) as ei:
        _rehearse()(data, work, run_id="dis")
    if "export modelark.core.db.rehearse" in str(ei.value):
        raise AssertionError(str(ei.value)) from ei.value
    assert any(k in str(ei.value).lower() for k in (
        "disagree", "mismatch", "conflict", "incident"))
    assert _fingerprint(path) == before


def test_m06b_invalid_derivation_refuses(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src", invalid_derivation=True)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Exception) as ei:
        _rehearse()(data, work, run_id="baddm")
    if "export modelark.core.db.rehearse" in str(ei.value):
        raise AssertionError(str(ei.value)) from ei.value
    assert "derivation" in str(ei.value).lower() or "check" in str(ei.value).lower()


def test_m06c_orphan_refuses(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src", orphan_archived=True)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(Exception) as ei:
        _rehearse()(data, work, run_id="orphan")
    if "export modelark.core.db.rehearse" in str(ei.value):
        raise AssertionError(str(ei.value)) from ei.value
    assert any(k in str(ei.value).lower() for k in (
        "foreign", "orphan", "integrity", "fk"))


def test_m06d_internal_inject_rolls_back_clone(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    path = _catalog(data)
    before = _fingerprint(path)
    work = tmp_path / "work"
    work.mkdir()
    migrate = _rehearse()
    target = None
    for mod_name, attr in (
        ("modelark.core.db", "_apply_provenance_backfill"),
        ("modelark.core.db", "_validate_migrated_clone"),
        ("modelark.catalog_migration", "_apply_provenance_backfill"),
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
        if hasattr(mod, attr):
            target = (mod, attr)
            break
    if target is None:
        raise AssertionError(
            "export patchable internal used mid-rehearsal "
            "(db._apply_provenance_backfill or db._validate_migrated_clone)"
        )
    mod, attr = target
    with mock.patch.object(mod, attr, side_effect=RuntimeError("injected")):
        with pytest.raises(Exception):
            migrate(data, work, run_id="inject")
    assert _fingerprint(path) == before
    for c in work.rglob("catalog.sqlite"):
        if any(x in str(c).lower() for x in ("quarantine", "failed", "reject")):
            continue
        con = _open_ro(c)
        try:
            assert con.execute("PRAGMA user_version").fetchone()[0] == FROZEN_V6
            assert "orig_sha256_provenance" not in {
                r[1] for r in con.execute("PRAGMA table_info(archived)")
            }
        finally:
            _close(con)


# ===========================================================================
# Publication
# ===========================================================================


def test_m08_missing_authorization_raises(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    _require_report(_rehearse()(data, work, run_id="p0"), source_identity=ident)
    with pytest.raises(Exception) as ei:
        _publish()(work, tmp_path / "dest", confirm_stopped="", writers_stopped=True)
    assert ei.type is not AssertionError or "export" in str(ei.value)
    assert any(k in str(ei.value).lower() for k in (
        "confirm", "stop", "author", "refus", "required"))


def test_m08b_active_writer_refuses_even_if_flag_true(tmp_path):
    """Hold a real write transaction; publication must re-prove quiescence and refuse."""
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="p1"), source_identity=ident)
    # Busy writer on source catalog
    busy = sqlite3.connect(str(_catalog(data)), isolation_level=None)
    busy.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Exception) as ei:
            _publish()(
                work, tmp_path / "dest",
                confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
        assert ei.type is not AssertionError or "export" in str(ei.value)
        assert any(k in str(ei.value).lower() for k in (
            "writer", "busy", "stop", "lock", "quiesc"))
    finally:
        busy.execute("ROLLBACK")
        _close(busy)
    _ = report


def test_m08c_existing_destination_refuses(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "catalog.sqlite").write_bytes(b"occupied")
    _require_report(_rehearse()(data, work, run_id="p2"), source_identity=ident)
    with pytest.raises(Exception) as ei:
        _publish()(
            work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert ei.type is not AssertionError or "export" in str(ei.value)
    assert "exist" in str(ei.value).lower() or "overwrite" in str(ei.value).lower()


def test_m08d_success_rollback_artifact_same_fs_atomic(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    src = _catalog(data)
    idc = _open_ro(src)
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest-data"
    publication_target = dest / "catalog.sqlite"
    assert not publication_target.exists()
    src_fp = _fingerprint(src)
    report = _require_report(
        _rehearse()(data, work, run_id="p3"), source_identity=ident)
    real_replace = os.replace
    real_link = os.link
    replace_calls: list[tuple[Path, Path]] = []
    publish_dsts: list[Path] = []

    def spy_replace(src_p, dst_p):
        replace_calls.append((Path(src_p), Path(dst_p)))
        publish_dsts.append(Path(dst_p))
        return real_replace(src_p, dst_p)

    def spy_link(src_p, dst_p):
        publish_dsts.append(Path(dst_p))
        return real_link(src_p, dst_p)

    real_fd = db._link_fd_no_clobber

    def spy_fd(fd, dest_cat):
        publish_dsts.append(Path(dest_cat))
        return real_fd(fd, dest_cat)

    with mock.patch.object(db.os, "replace", side_effect=spy_replace), \
            mock.patch.object(db.os, "link", side_effect=spy_link), \
            mock.patch.object(db, "_link_fd_no_clobber", side_effect=spy_fd):
        pub = _publish()(
            work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert _fingerprint(src) == src_fp
    # Exact final publication onto the (previously absent) publication target
    final_dsts = [dst for _s, dst in replace_calls] + publish_dsts
    assert publication_target in final_dsts or dest in final_dsts, (
        f"publish primitive must land on absent publication target; calls={final_dsts}"
    )
    assert "rollback_artifact" in pub
    rb = Path(pub["rollback_artifact"])
    assert rb.is_file()
    # Rollback artifact hash equals rehearsal snapshot hash exactly
    assert _sha_file(rb) == report["snapshot_sha256"]
    dest_cat = publication_target if publication_target.is_file() else None
    if dest_cat is None:
        found = list(dest.rglob("catalog.sqlite"))
        assert found, "publication target missing catalog"
        dest_cat = found[0]
    # Source, rollback artifact, and published target share st_dev
    assert os.stat(src).st_dev == os.stat(rb).st_dev == os.stat(dest_cat).st_dev
    dcon = _open_ro(dest_cat)
    try:
        assert int(dcon.execute("PRAGMA user_version").fetchone()[0]) > FROZEN_V6
        assert "orig_sha256_provenance" in {
            r[1] for r in dcon.execute("PRAGMA table_info(archived)")
        }
    finally:
        _close(dcon)
    assert pub["manifest_status"] == "validated"


def test_m08e_atomic_replace_failure_no_partial_dest(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    src = _catalog(data)
    idc = _open_ro(src)
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "dest-fail"
    publication_target = dest / "catalog.sqlite"
    src_fp = _fingerprint(src)
    report = _require_report(
        _rehearse()(data, work, run_id="p4"), source_identity=ident)
    real_replace = os.replace
    real_link = os.link
    real_fd = db._link_fd_no_clobber
    final_publish_fired = {"yes": False}

    def boom(src_p, dst_p, real):
        dst = Path(dst_p)
        # Final publication primitive onto the publication destination
        if dst == dest or dst == publication_target or (
                dest in dst.parents and dst.name == "catalog.sqlite"):
            final_publish_fired["yes"] = True
            raise OSError("injected atomic publish failure")
        return real(src_p, dst_p)

    def boom_fd(fd, dest_cat):
        dst = Path(dest_cat)
        if dst == dest or dst == publication_target or (
                dest in dst.parents and dst.name == "catalog.sqlite"):
            final_publish_fired["yes"] = True
            raise OSError("injected atomic publish failure")
        return real_fd(fd, dest_cat)

    with mock.patch.object(
        db.os, "replace",
        side_effect=lambda src, dst: boom(src, dst, real_replace),
    ), mock.patch.object(
        db.os, "link",
        side_effect=lambda src, dst: boom(src, dst, real_link),
    ), mock.patch.object(
        db, "_link_fd_no_clobber", side_effect=boom_fd,
    ):
        with pytest.raises(Exception) as ei:
            _publish()(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
        assert ei.type is not AssertionError or "export" in str(ei.value)
    assert final_publish_fired["yes"] is True, (
        "final publication primitive hook must fire before failure"
    )
    assert _fingerprint(src) == src_fp
    # Publication destination must not exist after failed replace
    assert not publication_target.exists()
    assert not dest.exists() or not any(dest.rglob("catalog.sqlite"))
    # Snapshot still intact
    assert Path(report["snapshot_path"]).is_file()
    assert _sha_file(Path(report["snapshot_path"])) == report["snapshot_sha256"]


def test_m09_connect_refuses_without_mutation(tmp_path):
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
# Proposal writers — real preview_pure → publish_draft
# ===========================================================================


def test_s08_preview_publish_three_modes_and_null_refused(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="s08"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        # Capture the real assignment function BEFORE any patch — never call a
        # mocked function from its own side effect.
        real_build_assignment = proposal_mod._build_assignment

        for mode in ("optimized", "canonical_fallback", "state_truncated"):
            def _ba(con_, plan_id, mutation, _mode=mode, _real=real_build_assignment):
                out = _real(con_, plan_id, mutation)
                if isinstance(out, dict):
                    tasks = out["tasks"]
                    files = out["files"]
                    gate = out.get("gate_b_code") or out.get("gate") or "FEASIBLE"
                    pol = out.get("policy_gate")
                else:
                    tasks, files = out[0], out[1]
                    gate = out[2] if len(out) > 2 else "FEASIBLE"
                    pol = out[3] if len(out) > 3 else None
                if _mode == "canonical_fallback":
                    gate = "INFEASIBLE"
                elif _mode in ("optimized", "state_truncated"):
                    gate = "FEASIBLE"
                # Locked assignment-result shape: fifth value carries derivation_mode
                # from placement into preview_pure (structured result also acceptable
                # in production; contracts pin the fifth-value form).
                return tasks, files, gate, pol, _mode

            with mock.patch.object(
                    proposal_mod, "_build_assignment", side_effect=_ba):
                payload = proposal_mod.preview_pure(
                    con, "ark", ("adopt_current", ()))
            assert payload["header"]["derivation_mode"] == mode, (
                f"preview_pure must surface assignment derivation_mode={mode!r}; "
                f"got {payload['header'].get('derivation_mode')!r}"
            )
            published = proposal_mod.publish_draft(con, payload)
            ppid = published.get("proposal_id")
            assert ppid
            stored = con.execute(
                "SELECT derivation_mode FROM placement_proposals WHERE proposal_id=?",
                [ppid],
            ).fetchone()[0]
            assert stored == mode

        # NULL publication must raise identifying invalid/missing derivation mode
        payload_null = proposal_mod.preview_pure(con, "ark", ("adopt_current", ()))
        payload_null["header"]["derivation_mode"] = None
        payload_null["canonical_hash"] = canonical.proposal_hash(
            payload_null["header"], payload_null["tasks"], payload_null["files"])
        with pytest.raises(Exception) as ei:
            proposal_mod.publish_draft(con, payload_null)
        msg = str(ei.value).lower()
        assert "derivation" in msg, (
            "NULL derivation_mode publish must identify invalid/missing "
            f"derivation mode; got {ei.value!r}"
        )
        assert any(k in msg for k in ("invalid", "missing", "null", "required", "mode"))
    finally:
        _close(con)


# ===========================================================================
# Fetch ingest + replica heal
# ===========================================================================


def test_w01_w02_fetch_model_sets_provenance(tmp_path):
    """Hit the archived upsert seam inside fetch_model (files row first)."""
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="ing"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        fetch = importlib.import_module("modelark.fetch")
        # New file with Hub digest
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) "
            "VALUES('org/m','hub.bin',8,?,'safetensors','bf16')", [_h("n")])
        # New file without Hub digest
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','nohub.bin',8,NULL,'aux')")

        dest = tmp_path / "drive-root"
        dest.mkdir()
        captured: list[dict] = []
        real_upsert = db.upsert

        def spy_upsert(c, table, row, pk=None, touch=None):
            if table == "archived":
                captured.append(dict(row))
            return real_upsert(c, table, row, pk=pk or [], touch=touch)

        def fake_download(ctx, repo_id, rfilename, download_dir, base, **k):
            out = Path(download_dir) / Path(rfilename).name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"12345678")
            return out

        from modelark import archive_manifest
        ctx = fetch.RunCtx(con=con)

        def run_one(rfilename, sha):
            digest = sha or _h("m")
            mf = (archive_manifest.ManifestFile(
                rfilename=rfilename, size_bytes=8, sha256=sha,
                format="safetensors" if sha else "aux", quant="bf16" if sha else None,
                storage_action="raw",
            ),)

            def fake_publish(dest_p, staged, target, digest_p, rfn, annex, **kw):
                target = Path(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                if Path(staged).resolve() != target.resolve():
                    os.replace(str(staged), str(target))
                return target

            with mock.patch.object(db, "upsert", side_effect=spy_upsert), \
                    mock.patch.object(
                        fetch, "_download_shard", side_effect=fake_download), \
                    mock.patch.object(
                        fetch, "_publish_staged", side_effect=fake_publish), \
                    mock.patch.object(fetch, "_annex_add", return_value=None), \
                    mock.patch.object(fetch, "_annex_metadata", return_value=None), \
                    mock.patch.object(fetch, "_sweep_incomplete", return_value=0), \
                    mock.patch.object(
                        fetch.compress, "sha256_file", return_value=digest), \
                    mock.patch.object(
                        fetch.compress, "should_compress", return_value=False):
                # Must complete normally — no broad except pass
                result = fetch.fetch_model(
                    ctx, "org/m", dest, "d0", False,
                    {"max_compress_ram_gb": 4, "threads": 1},
                    manifest=mf,
                )
                assert result["files"] == 1

        run_one("hub.bin", _h("n"))
        run_one("nohub.bin", None)

        by_name = {r.get("rfilename"): r for r in captured}
        assert "hub.bin" in by_name and "nohub.bin" in by_name, (
            "fetch_model must reach db.upsert for both archived ingest rows "
            f"(captured {list(by_name)})"
        )
        assert by_name["hub.bin"].get("orig_sha256_provenance") == "hub_confirmed"
        assert by_name["nohub.bin"].get("orig_sha256_provenance") == "ingestion_computed"
        # Persisted provenance
        assert con.execute(
            "SELECT orig_sha256_provenance FROM archived WHERE rfilename='hub.bin'"
        ).fetchone()[0] == "hub_confirmed"
        assert con.execute(
            "SELECT orig_sha256_provenance FROM archived WHERE rfilename='nohub.bin'"
        ).fetchone()[0] == "ingestion_computed"
    finally:
        _close(con)


def test_w05_replica_heal_matrix(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="heal"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        fetch = importlib.import_module("modelark.fetch")
        heal = getattr(fetch, "heal_replica_archived_from_source", None)
        if not callable(heal):
            raise AssertionError(
                "export modelark.fetch.heal_replica_archived_from_source"
            )
        # 1) null target copies digest+provenance
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,annex_key,"
            "orig_sha256_provenance) "
            "VALUES('org/m','weights.bin','weights.bin','weights.bin','d1',"
            "NULL,100,100,0,NULL,NULL)")
        src_row = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE drive_label='d0' AND rfilename='weights.bin'"
        ).fetchone()
        heal(con, source_drive="d0", target_drive="d1",
             repo_id="org/m", rfilename="weights.bin")
        tgt = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE drive_label='d1' AND rfilename='weights.bin'"
        ).fetchone()
        assert tgt[0] == src_row[0] == _h("a")
        assert tgt[1] == src_row[1] == "hub_confirmed"
        # 2) matching digest, NULL provenance → fill provenance only
        con.execute(
            "UPDATE archived SET orig_sha256_provenance=NULL "
            "WHERE drive_label='d1' AND rfilename='weights.bin'")
        heal(con, source_drive="d0", target_drive="d1",
             repo_id="org/m", rfilename="weights.bin")
        tgt2 = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE drive_label='d1' AND rfilename='weights.bin'"
        ).fetchone()
        assert tgt2[0] == _h("a")
        assert tgt2[1] == "hub_confirmed"
        # 3) mismatch raises; complete target row unchanged
        con.execute(
            "UPDATE archived SET orig_sha256=?, orig_sha256_provenance='ingestion_computed' "
            "WHERE drive_label='d1' AND rfilename='weights.bin'",
            [_h("x")])
        before = con.execute(
            "SELECT repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
            "compressed,annex_key,orig_sha256_provenance FROM archived "
            "WHERE drive_label='d1' AND rfilename='weights.bin'"
        ).fetchone()
        with pytest.raises(Exception) as ei:
            heal(con, source_drive="d0", target_drive="d1",
                 repo_id="org/m", rfilename="weights.bin")
        assert ei.type is not AssertionError
        after = con.execute(
            "SELECT repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
            "compressed,annex_key,orig_sha256_provenance FROM archived "
            "WHERE drive_label='d1' AND rfilename='weights.bin'"
        ).fetchone()
        assert after == before
    finally:
        _close(con)


# ===========================================================================
# Repair
# ===========================================================================


def test_w03_tier1_complete(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="t1"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        st = rep.get("status") or _repair_status(con, "d0", 1)
        row = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE rfilename='shard.bin'"
        ).fetchone()
        assert row[0] == _h("d")
        assert row[1] == "annex_key"
        assert con.execute(
            "SELECT count(*) FROM archived WHERE drive_label='d0' AND orig_sha256 IS NULL"
        ).fetchone()[0] == 0
        assert st == "complete"
    finally:
        _close(con)


def test_w04_archive_head_git_fixture(tmp_path):
    hr = importlib.import_module("modelark.hash_repair")
    archive = tmp_path / "archive"
    archive.mkdir()
    subprocess.run(["git", "init"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=archive, check=True, capture_output=True)
    repo_dir = archive / "org" / "m"
    repo_dir.mkdir(parents=True)
    content = b"archive-head-bytes-xyz"
    (repo_dir / "headfile.bin").write_bytes(content)
    subprocess.run(["git", "add", "-A"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "s"], cwd=archive, check=True, capture_output=True)

    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="ah"), source_identity=ident)
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
            "NULL,?,?,0,NULL,NULL)", [len(content), len(content)])
        row = {
            "repo_id": "org/m", "rfilename": "headfile.bin",
            "stored_name": "headfile.bin", "stored_relpath": "headfile.bin",
            "drive_label": "d0", "orig_sha256": None,
            "orig_bytes": len(content), "stored_bytes": len(content),
            "compressed": 0, "annex_key": None,
            "catalog_sha": None, "catalog_bytes": len(content),
        }
        with pytest.raises(hr.HashRepairError):
            hr._validate_candidate(dict(row, compressed=1), archive)
        got = hr._validate_candidate(row, archive)
        assert got["evidence"] == "archive-head-blob"
        assert got["sha256"] == hashlib.sha256(content).hexdigest()
        _repair()(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"),
                  archive_resolver=lambda *a, **k: archive)
        persisted = con.execute(
            "SELECT orig_sha256, orig_sha256_provenance FROM archived "
            "WHERE rfilename='headfile.bin'"
        ).fetchone()
        assert persisted[0] == hashlib.sha256(content).hexdigest()
        assert persisted[1] == "archive-head-blob"
    finally:
        _close(con)


def test_w09_pk_and_vocabulary(tmp_path):
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w09"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        info = list(con.execute("PRAGMA table_info(drive_hash_repair_state)"))
        pk = [r[1] for r in sorted(
            [r for r in info if r[5] > 0], key=lambda r: r[5])]
        assert pk == ["drive_label", "identity_epoch"], pk
        for bad in ("done", "ok", "failed", ""):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO drive_hash_repair_state("
                    "drive_label,identity_epoch,identity_fingerprint,status) "
                    "VALUES('d0',99,?,?)", [_h("f"), bad])
        for ok in REPAIR_STATUSES:
            con.execute("DELETE FROM drive_hash_repair_state WHERE identity_epoch=99")
            con.execute(
                "INSERT INTO drive_hash_repair_state("
                "drive_label,identity_epoch,identity_fingerprint,status) "
                "VALUES('d0',99,?,?)", [_h("f"), ok])
    finally:
        _close(con)


def test_w15_blocked_absent(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w15"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        rep = repair(
            con, "d-absent", identity_epoch=1, identity_fingerprint=_h("b"),
            archive_resolver=lambda *a, **k: None)
        assert (rep.get("status") or _repair_status(con, "d-absent", 1)) == "blocked_absent"
    finally:
        _close(con)


def test_w17_fingerprint_halt_no_archive_mutation(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w17"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        before = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("Z"))
        assert (rep.get("status") or _repair_status(con, "d0", 1)) == "halted"
        after = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        assert after == before
    finally:
        _close(con)


def test_w16_complete_zero_unresolved(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w16"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert (rep.get("status") or _repair_status(con, "d0", 1)) == "complete"
        assert con.execute(
            "SELECT count(*) FROM archived WHERE drive_label='d0' AND orig_sha256 IS NULL"
        ).fetchone()[0] == 0
    finally:
        _close(con)


def test_w11_epoch2_needs_refetch_epoch1_unchanged(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w11"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert _repair_status(con, "d0", 1) == "complete"
        e1_state = list(con.execute(
            "SELECT * FROM drive_hash_repair_state WHERE drive_label='d0' "
            "AND identity_epoch=1"))
        # Advance identity; create genuine tier-3 unresolved work
        con.execute(
            "UPDATE drives SET identity_epoch=2, identity_fingerprint=? "
            "WHERE drive_label='d0'", [_h("F")])
        con.execute(
            "UPDATE archived SET orig_sha256=NULL, annex_key=NULL, compressed=1, "
            "orig_sha256_provenance=NULL WHERE rfilename='shard.bin'")
        rep2 = repair(con, "d0", identity_epoch=2, identity_fingerprint=_h("F"))
        assert (rep2.get("status") or _repair_status(con, "d0", 2)) == "needs_refetch"
        assert _repair_status(con, "d0", 1) == "complete"
        # Exact epoch-1 state row unchanged after epoch-2 needs_refetch work
        assert list(con.execute(
            "SELECT * FROM drive_hash_repair_state WHERE drive_label='d0' "
            "AND identity_epoch=1")) == e1_state
    finally:
        _close(con)


def test_w12_lost_unresolved_not_complete(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w12"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        con.execute(
            "UPDATE archived SET orig_sha256=NULL, annex_key=NULL, "
            "orig_sha256_provenance=NULL WHERE rfilename='shard.bin'")
        con.execute("UPDATE drives SET lifecycle='lost' WHERE drive_label='d0'")
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert con.execute(
            "SELECT count(*) FROM archived WHERE drive_label='d0' AND orig_sha256 IS NULL"
        ).fetchone()[0] > 0
        assert (rep.get("status") or _repair_status(con, "d0", 1)) != "complete"
    finally:
        _close(con)


def test_w13_needs_refetch_exact(tmp_path):
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w13"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        con.execute(
            "UPDATE archived SET orig_sha256=NULL, annex_key=NULL, compressed=1, "
            "orig_sha256_provenance=NULL WHERE rfilename='shard.bin'")
        rep = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert (rep.get("status") or _repair_status(con, "d0", 1)) == "needs_refetch"
    finally:
        _close(con)


def test_w14_lost_drive_zero_archives_no_repair_obligation(tmp_path):
    """Drive-02 equivalent: lost + zero archived facts → no repair-state, no I/O."""
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w14"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        calls = []

        def resolver(*a, **k):
            calls.append((a, k))
            return Path("/no/such/archive")

        rep = repair(
            con, "d-lost-empty", identity_epoch=1, identity_fingerprint=_h("c"),
            archive_resolver=resolver)
        # No obligation: no state row required, or explicit no-op status without I/O
        st = rep.get("status") or _repair_status(con, "d-lost-empty", 1)
        assert st in (None, "pending") or rep.get("obligation") is False
        assert calls == [], f"must not resolve archive/file I/O; calls={calls}"
        assert con.execute(
            "SELECT count(*) FROM archived WHERE drive_label='d-lost-empty'"
        ).fetchone()[0] == 0
        # Prefer no repair-state row
        n = con.execute(
            "SELECT count(*) FROM drive_hash_repair_state "
            "WHERE drive_label='d-lost-empty'"
        ).fetchone()[0]
        assert n == 0
    finally:
        _close(con)


def test_w10_trigger_abort_after_row_before_terminal_state(tmp_path):
    """Inject failure after archive writes, before terminal state publication."""
    repair = _repair()
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        _rehearse()(data, work, run_id="w10"), source_identity=ident)
    con = _open_rw(Path(report["clone_catalog_path"]))
    try:
        before_arch = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        before_st = list(con.execute(
            "SELECT drive_label,identity_epoch,status FROM drive_hash_repair_state "
            "ORDER BY 1,2"))
        # Abort when terminal complete is published (INSERT or UPDATE)
        con.execute("""
            CREATE TRIGGER IF NOT EXISTS g1_abort_complete_ins
            BEFORE INSERT ON drive_hash_repair_state
            WHEN NEW.status = 'complete'
            BEGIN
              SELECT RAISE(ABORT, 'injected terminal state abort');
            END;
        """)
        con.execute("""
            CREATE TRIGGER IF NOT EXISTS g1_abort_complete_upd
            BEFORE UPDATE ON drive_hash_repair_state
            WHEN NEW.status = 'complete'
            BEGIN
              SELECT RAISE(ABORT, 'injected terminal state abort');
            END;
        """)
        with pytest.raises(Exception) as ei:
            repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert ei.type is not AssertionError or "export" in str(ei.value)
        after_arch = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        after_st = list(con.execute(
            "SELECT drive_label,identity_epoch,status FROM drive_hash_repair_state "
            "ORDER BY 1,2"))
        assert after_arch == before_arch
        assert after_st == before_st
    finally:
        _close(con)


def test_w07_connect_and_portal_conn_do_not_call_engine(tmp_path):
    hr = importlib.import_module("modelark.hash_repair")
    if not hasattr(hr, "run_explicit_drive_repair"):
        raise AssertionError("export run_explicit_drive_repair")
    data = tmp_path / "fresh"
    data.mkdir()
    db.configure(data, data / "state")
    with mock.patch.object(hr, "run_explicit_drive_repair") as eng:
        con = db.connect()
        _close(con)
        eng.assert_not_called()
    # Portal path
    data2 = tmp_path / "portal"
    data2.mkdir()
    db.configure(data2, data2 / "state")
    web_data._con = None
    with mock.patch.object(hr, "run_explicit_drive_repair") as eng2:
        c = web_data.conn()
        eng2.assert_not_called()
        _close(c)
        web_data._con = None


def test_w08_cli_exact_engine_args(tmp_path):
    from modelark import cli
    hr = importlib.import_module("modelark.hash_repair")
    if not hasattr(hr, "run_explicit_drive_repair"):
        raise AssertionError("export run_explicit_drive_repair for CLI")
    eng = mock.Mock(return_value={"status": "complete"})
    with mock.patch.object(hr, "run_explicit_drive_repair", eng):
        with mock.patch.object(db, "connect") as conn:
            mock_con = mock.Mock()
            conn.return_value = mock_con
            try:
                cli.main([
                    "repair-drive",
                    "--drive", "d0",
                    "--identity-epoch", "1",
                    "--identity-fingerprint", _h("f"),
                ])
            except SystemExit:
                pass
            if eng.call_count == 0:
                raise AssertionError(
                    "CLI repair-drive must call run_explicit_drive_repair"
                )
            # Exact drive/epoch/fingerprint
            args, kwargs = eng.call_args
            # Accept (con, 'd0', identity_epoch=1, identity_fingerprint=...)
            label = args[1] if len(args) > 1 else kwargs.get("drive_label")
            epoch = kwargs.get("identity_epoch")
            if epoch is None and len(args) > 2:
                epoch = args[2]
            fp = kwargs.get("identity_fingerprint")
            assert label == "d0"
            assert int(epoch) == 1
            assert fp == _h("f")


# ===========================================================================
# Boundaries
# ===========================================================================


def test_b01_dec055_neutral():
    d = _h("d")
    assert fill_mod._archive_content_satisfies(
        d, orig_sha256=d, compressed=False, annex_key=None)
    import inspect
    assert "provenance" not in inspect.signature(
        archive_hash.expected_sha256).parameters


def test_b04_inc023_present():
    assert (Path(__file__).parent / "test_inc023_gate1_contracts.py").is_file()
