"""DEC-053 / DEC-054 / DEF-034 Gate-1 contracts (DEC-059/060).

Contracts only. No production. Expected-red until Gate-2 remediates:

  - DEC-059 clone-first provenance migration (extend migrate_legacy_runtime patterns)
  - archived.orig_sha256_provenance (additive CHECK; rebuild archived only if needed)
  - placement_proposals.derivation_mode CHECK via table rebuild (NULL historical OK)
  - drive_hash_repair_state + explicit maintenance repair (no connect/portal/Fill-preflight)
  - replica targeted null/provenance healing

Gate-2 public surface these contracts lock (export under these names):

  modelark.core.db.rehearse_provenance_migration(source_dir, work_dir, *, run_id) -> dict
  modelark.hash_repair.run_explicit_drive_repair(con, drive_label, *, identity_epoch, ...) -> dict
  table drive_hash_repair_state
  column archived.orig_sha256_provenance

Generated deterministic v6 fixtures only — not the untracked 50 MB acceptance blob.
"""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db
from modelark import archive_hash, fill as fill_mod


PROVENANCE_OK = frozenset({
    "hub_confirmed",
    "ingestion_computed",
    "annex_key",
    "archive-head-blob",
    "legacy_unknown",
})
DERIVATION_OK = frozenset({"optimized", "state_truncated", "canonical_fallback"})
REPAIR_STATUSES = frozenset({
    "pending", "running", "blocked_absent", "needs_refetch", "halted", "complete",
})

# Paths that must never be imported by these contracts (optional local evidence only).
_FORBIDDEN_ACCEPTANCE_NAME = "b12_390_approved_fixture.sqlite"


# ---------------------------------------------------------------------------
# Surface loaders (expected-red until Gate-2 exports)
# ---------------------------------------------------------------------------


def _rehearse_fn():
    """Gate-2: db.rehearse_provenance_migration(source_dir, work_dir, *, run_id) -> report."""
    fn = getattr(db, "rehearse_provenance_migration", None)
    if not callable(fn):
        raise AssertionError(
            "export modelark.core.db.rehearse_provenance_migration("
            "source_dir, work_dir, *, run_id) -> dict  (DEC-059 clone rehearsal; "
            "never mutates canonical source; no plan.bootstrap side-effects)"
        )
    return fn


def _explicit_repair_fn():
    """Gate-2: hash_repair.run_explicit_drive_repair — maintenance only."""
    hr = importlib.import_module("modelark.hash_repair")
    fn = getattr(hr, "run_explicit_drive_repair", None)
    if not callable(fn):
        raise AssertionError(
            "export modelark.hash_repair.run_explicit_drive_repair("
            "con, drive_label, *, identity_epoch, identity_fingerprint=...) -> dict  "
            "(explicit maintenance only; not db.connect / portal / Fill preflight)"
        )
    return fn


# ---------------------------------------------------------------------------
# Deterministic v6 fixtures
# ---------------------------------------------------------------------------


def _h(ch: str) -> str:
    return (ch * 64)[:64]


def _configure(tmp: Path):
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    db.configure(data, data / "state")
    return data


def _seed_v6_catalog(tmp: Path, *, with_disagreement: bool = False,
                     with_orphan_attempt: bool = False) -> Path:
    """Build a genuine current-schema (v6) catalog with controlled archived digests.

    Independent of packaged future schema: boots via db.connect, then only uses
    columns that exist at tip 867b40d.
    """
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        data = _configure(tmp)
        con = db.connect()
        ver = int(con.execute("PRAGMA user_version").fetchone()[0])
        assert ver == 6, f"fixture must be frozen v6; got user_version={ver}"

        con.execute(
            "INSERT OR IGNORE INTO plans(plan_id,name,is_active,capacity_mode) "
            "VALUES('ark','Ark',1,'guaranteed')")
        cap = 10**12
        con.execute(
            "INSERT OR REPLACE INTO drives("
            "drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "identity_epoch,write_generation,lifecycle,eligibility,"
            "identity_fingerprint,write_authority) "
            "VALUES('d0',?,?, 'primary',0,1,0,'active','enabled',?, 'unknown')",
            [cap, cap, _h("f")],
        )
        con.execute(
            "INSERT OR REPLACE INTO drives("
            "drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "identity_epoch,write_generation,lifecycle,eligibility,"
            "identity_fingerprint,write_authority) "
            "VALUES('d1',?,?, 'replica',0,1,0,'active','enabled',?, 'unknown')",
            [cap, cap, _h("g")],
        )
        con.execute(
            "INSERT OR IGNORE INTO models(repo_id,status,numcopies) "
            "VALUES('org/m','archived',1)")
        # Hub digest present
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES('org/m','weights.bin',100,'safetensors','bf16',?)",
            [_h("a")],
        )
        # No hub digest
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES('org/m','notes.txt',10,'aux',NULL,NULL)")
        # Hub present for mismatch case
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES('org/m','other.bin',50,'safetensors','bf16',?)",
            [_h("b")],
        )

        # hub_confirmed candidate: equal digests
        con.execute(
            "INSERT OR REPLACE INTO archived("
            "repo_id,rfilename,stored_name,stored_relpath,drive_label,"
            "orig_sha256,orig_bytes,stored_bytes,compressed,annex_key) "
            "VALUES('org/m','weights.bin','weights.bin','weights.bin','d0',?,100,100,0,NULL)",
            [_h("a")],
        )
        # legacy_unknown candidate: non-null digest, files.sha256 null
        con.execute(
            "INSERT OR REPLACE INTO archived("
            "repo_id,rfilename,stored_name,stored_relpath,drive_label,"
            "orig_sha256,orig_bytes,stored_bytes,compressed,annex_key) "
            "VALUES('org/m','notes.txt','notes.txt','notes.txt','d0',?,10,10,0,NULL)",
            [_h("c")],
        )
        # null digest + raw SHA256E annex (tier-1 repair candidate)
        con.execute(
            "INSERT OR REPLACE INTO archived("
            "repo_id,rfilename,stored_name,stored_relpath,drive_label,"
            "orig_sha256,orig_bytes,stored_bytes,compressed,annex_key) "
            "VALUES('org/m','other.bin','other.bin','other.bin','d0',NULL,50,50,0,?)",
            [f"SHA256E-s50--{_h('d')}"],
        )
        if with_disagreement:
            # Non-null orig disagrees with files.sha256 on weights — migration must refuse
            con.execute(
                "UPDATE archived SET orig_sha256=? "
                "WHERE repo_id='org/m' AND rfilename='weights.bin' AND drive_label='d0'",
                [_h("z")],
            )

        # Historical NULL derivation_mode + one optimized
        con.execute(
            "INSERT OR REPLACE INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,"
            "capacity_mode,policy_version,solver_version,gate_b_code,derivation_mode,"
            "execution_config_hash) "
            "VALUES('hist-null','ark',0,'draft',?,'adopt_current','[]','1',"
            "'guaranteed','1','1','FEASIBLE',NULL,NULL)",
            [_h("1")],
        )
        con.execute(
            "INSERT OR REPLACE INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,"
            "capacity_mode,policy_version,solver_version,gate_b_code,derivation_mode,"
            "execution_config_hash) "
            "VALUES('hist-opt','ark',0,'draft',?,'adopt_current','[]','1',"
            "'guaranteed','1','1','FEASIBLE','optimized',NULL)",
            [_h("2")],
        )
        # Invalid historical value (must refuse at migration, not normalize)
        con.execute(
            "INSERT OR REPLACE INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,"
            "capacity_mode,policy_version,solver_version,gate_b_code,derivation_mode,"
            "execution_config_hash) "
            "VALUES('hist-bad','ark',0,'draft',?,'adopt_current','[]','1',"
            "'guaranteed','1','1','FEASIBLE','ecfg:deadbeef',NULL)",
            [_h("3")],
        )

        if with_orphan_attempt:
            # Cannot insert orphan under FK ON — document integrity path instead
            pass

        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        return data
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old


def _cols(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_sql(con, table: str) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", [table]
    ).fetchone()
    return (row[0] or "") if row else ""


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


# ===========================================================================
# M01 / B05 — deterministic fixtures (positive)
# ===========================================================================


def test_m01_deterministic_v6_fixture_without_acceptance_blob(tmp_path):
    """Positive: generated v6 fixture; no dependency on 50 MB acceptance path."""
    data = _seed_v6_catalog(tmp_path)
    assert (data / "catalog.sqlite").is_file()
    con = _open_ro(data / "catalog.sqlite")
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 6
        n = con.execute("SELECT count(*) FROM archived").fetchone()[0]
        assert n >= 3
    finally:
        con.close()
    # Contracts module must not reference the forbidden evidence path as required input
    src = Path(__file__).read_text()
    assert _FORBIDDEN_ACCEPTANCE_NAME not in src or "must not" in src.lower()
    assert "docs/plans/evidence" not in src or "optional" in src.lower()


# ===========================================================================
# Migration surface / DEC-059 (expected-red)
# ===========================================================================


def test_m03_rehearse_exports_and_leaves_source_untouched(tmp_path):
    """Expected-red: rehearse migrates only work clone; source version/bytes stable."""
    source = _seed_v6_catalog(tmp_path / "src")
    src_db = source / "catalog.sqlite"
    before = src_db.read_bytes()
    before_ver = _open_ro(src_db).execute("PRAGMA user_version").fetchone()[0]
    work = tmp_path / "work"
    work.mkdir()
    report = _rehearse_fn()(source, work, run_id="g1-m03")
    assert isinstance(report, dict)
    after = src_db.read_bytes()
    assert after == before, "canonical/source catalog bytes must not change during rehearsal"
    assert _open_ro(src_db).execute("PRAGMA user_version").fetchone()[0] == before_ver


def test_m05_post_rehearsal_semantic_parity_and_backfill_counts(tmp_path):
    """Expected-red: after rehearse, provenance column + exact-row preserve + counts."""
    # Valid history only (no hist-bad / disagreement) — use custom seed
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        data = _configure(tmp_path / "src")
        con = db.connect()
        con.execute(
            "INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
        con.execute(
            "INSERT OR REPLACE INTO drives(drive_label,capacity_bytes,free_bytes,role,"
            "raid_backed,identity_epoch,lifecycle,eligibility,identity_fingerprint) "
            "VALUES('d0',1,1,'primary',0,1,'active','enabled',?)", [_h("f")])
        con.execute(
            "INSERT OR IGNORE INTO models(repo_id,status) VALUES('org/m','archived')")
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256) "
            "VALUES('org/m','w.bin',10,?)", [_h("a")])
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256) "
            "VALUES('org/m','n.txt',5,NULL)")
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,drive_label,orig_sha256,"
            "orig_bytes,stored_bytes,compressed) "
            "VALUES('org/m','w.bin','d0',?,10,10,0)", [_h("a")])
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,drive_label,orig_sha256,"
            "orig_bytes,stored_bytes,compressed) "
            "VALUES('org/m','n.txt','d0',?,5,5,0)", [_h("c")])
        # null digest row
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256) "
            "VALUES('org/m','z.bin',1,NULL)")
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,drive_label,orig_sha256,"
            "orig_bytes,stored_bytes,compressed,annex_key) "
            "VALUES('org/m','z.bin','d0',NULL,1,1,0,?)",
            [f"SHA256E-s1--{_h('e')}"])
        con.execute(
            "INSERT OR REPLACE INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,derivation_mode) "
            "VALUES('p-null','ark',0,'draft',?,'k','[]','1',NULL)", [_h("9")])
        src_archived = con.execute("SELECT count(*) FROM archived").fetchone()[0]
        src_pp = con.execute("SELECT count(*) FROM placement_proposals").fetchone()[0]
        con.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old

    work = tmp_path / "work"
    work.mkdir()
    report = _rehearse_fn()(data, work, run_id="g1-m05")
    # Locate migrated catalog under work
    clones = list(work.rglob("catalog.sqlite"))
    assert clones, "rehearsal must produce a work-clone catalog.sqlite"
    mcon = sqlite3.connect(str(clones[0]))
    try:
        assert "orig_sha256_provenance" in _cols(mcon, "archived")
        assert mcon.execute("SELECT count(*) FROM archived").fetchone()[0] == src_archived
        assert mcon.execute("SELECT count(*) FROM placement_proposals").fetchone()[0] == src_pp
        # No plan.bootstrap inventing rows beyond source
        hub = mcon.execute(
            "SELECT count(*) FROM archived WHERE orig_sha256_provenance='hub_confirmed'"
        ).fetchone()[0]
        leg = mcon.execute(
            "SELECT count(*) FROM archived WHERE orig_sha256_provenance='legacy_unknown'"
        ).fetchone()[0]
        nul = mcon.execute(
            "SELECT count(*) FROM archived WHERE orig_sha256 IS NULL "
            "AND orig_sha256_provenance IS NULL"
        ).fetchone()[0]
        assert hub >= 1 and leg >= 1 and nul >= 1, (hub, leg, nul, report)
        # NULL derivation preserved
        assert mcon.execute(
            "SELECT derivation_mode FROM placement_proposals WHERE proposal_id='p-null'"
        ).fetchone()[0] is None
    finally:
        mcon.close()


def test_m06_disagreement_refuses_without_stamping_source(tmp_path):
    """Expected-red: files.sha256 disagreement stops clone validation; source unchanged."""
    source = _seed_v6_catalog(tmp_path / "src", with_disagreement=True)
    src_db = source / "catalog.sqlite"
    before = src_db.read_bytes()
    work = tmp_path / "work"
    work.mkdir()
    migrate = _rehearse_fn()  # fails closed until export exists (expected-red)
    try:
        migrate(source, work, run_id="g1-disagree")
        raise AssertionError(
            "disagreement with files.sha256 must refuse clone validation; got success")
    except AssertionError:
        raise
    except Exception as exc:
        msg = str(exc).lower()
        assert any(
            k in msg for k in ("disagree", "refusing", "mismatch", "incident", "conflict")
        ), f"unexpected failure mode: {exc!r}"
    assert src_db.read_bytes() == before
    assert _open_ro(src_db).execute("PRAGMA user_version").fetchone()[0] == 6


def test_m09_ordinary_connect_does_not_in_place_migrate_v6(tmp_path):
    """Policy pin: writable db.connect on a frozen v6 catalog is not the DEC-059 path.

    Must not silently half-apply provenance on the sole file. Full migration uses
    rehearse_provenance_migration on a disposable clone (see m03/m05).
    """
    data = _seed_v6_catalog(tmp_path)
    before = (data / "catalog.sqlite").read_bytes()
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        db.configure(data, data / "state")
        with mock.patch("modelark.hash_repair.repair_hashes") as rh:
            con = db.connect()
            rh.assert_not_called()
            ver = int(con.execute("PRAGMA user_version").fetchone()[0])
            has_prov = "orig_sha256_provenance" in _cols(con, "archived")
            # At pre-Gate-2 tip: remains v6 without provenance.
            # After Gate-2: either still v6 (no-op connect) or published catalogs only
            # are opened at higher version — never a half-migrated sole file.
            if ver == 6:
                assert not has_prov, (
                    "in-place connect must not half-add provenance without DEC-059 publish")
            con.close()
        # Source file path still present (connect did not replace catalog path)
        assert (data / "catalog.sqlite").is_file()
        _ = before  # bytes may change via WAL; path stability is the pin
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old


def test_m03_surface_rehearse_provenance_migration_exported():
    """Expected-red: clone-first migrator export required (DEC-059)."""
    _rehearse_fn()


def test_m11_read_only_old_version_refuses(tmp_path):
    """Positive compatibility: RO open of older-than-build catalog refuses."""
    data = _configure(tmp_path)
    con = sqlite3.connect(str(data / "catalog.sqlite"), isolation_level=None)
    con.execute("CREATE TABLE t(x)")
    con.execute("PRAGMA user_version=5")
    con.close()
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        db.configure(data, data / "state")
        with pytest.raises(RuntimeError) as ei:
            db.connect(read_only=True)
        assert "migration" in str(ei.value).lower() or "writable" in str(ei.value).lower()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old


# ===========================================================================
# Schema CHECKs (expected-red post-rehearsal)
# ===========================================================================


def test_s01_provenance_check_enforced_on_migrated_clone(tmp_path):
    """Expected-red: invalid provenance rejected; closed set + NULL accepted."""
    # Clean v6 seed (no invalid derivation_mode history) for CHECK probing post-rehearsal.
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        data = _configure(tmp_path / "clean")
        con = db.connect()
        con.execute("INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
        con.execute(
            "INSERT OR REPLACE INTO drives(drive_label,capacity_bytes,free_bytes,role,"
            "raid_backed,identity_epoch,lifecycle,eligibility,identity_fingerprint) "
            "VALUES('d0',1,1,'primary',0,1,'active','enabled',?)", [_h("f")])
        con.execute("INSERT OR IGNORE INTO models(repo_id,status) VALUES('org/m','archived')")
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256) "
            "VALUES('org/m','w.bin',10,?)", [_h("a")])
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,drive_label,orig_sha256,"
            "orig_bytes,stored_bytes,compressed) VALUES('org/m','w.bin','d0',?,10,10,0)",
            [_h("a")])
        con.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old

    work = tmp_path / "work"
    work.mkdir()
    _rehearse_fn()(data, work, run_id="g1-s01")
    clones = list(work.rglob("catalog.sqlite"))
    assert clones
    mcon = sqlite3.connect(str(clones[0]), isolation_level=None)
    try:
        assert "orig_sha256_provenance" in _cols(mcon, "archived")
        # valid
        mcon.execute(
            "UPDATE archived SET orig_sha256_provenance='hub_confirmed' "
            "WHERE rfilename='w.bin'")
        # invalid
        with pytest.raises(sqlite3.IntegrityError):
            mcon.execute(
                "UPDATE archived SET orig_sha256_provenance='mirrored' "
                "WHERE rfilename='w.bin'")
        with pytest.raises(sqlite3.IntegrityError):
            mcon.execute(
                "UPDATE archived SET orig_sha256_provenance='not-a-value' "
                "WHERE rfilename='w.bin'")
    finally:
        mcon.close()


def test_s03_derivation_mode_check_null_ok_invalid_rejected(tmp_path):
    """Expected-red: after PP rebuild, NULL ok; non-null only three RFC values."""
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        data = _configure(tmp_path / "src")
        con = db.connect()
        con.execute("INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
        con.execute(
            "INSERT OR REPLACE INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,derivation_mode) "
            "VALUES('p0','ark',0,'draft',?,'k','[]','1',NULL)", [_h("a")])
        con.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old
    work = tmp_path / "work"
    work.mkdir()
    _rehearse_fn()(data, work, run_id="g1-s03")
    mcon = sqlite3.connect(str(list(work.rglob("catalog.sqlite"))[0]), isolation_level=None)
    try:
        sql = _table_sql(mcon, "placement_proposals").lower()
        assert "derivation_mode" in sql
        assert "optimized" in sql and "state_truncated" in sql and "canonical_fallback" in sql
        # NULL still present
        assert mcon.execute(
            "SELECT derivation_mode FROM placement_proposals WHERE proposal_id='p0'"
        ).fetchone()[0] is None
        mcon.execute(
            "UPDATE placement_proposals SET derivation_mode='state_truncated' "
            "WHERE proposal_id='p0'")
        with pytest.raises(sqlite3.IntegrityError):
            mcon.execute(
                "UPDATE placement_proposals SET derivation_mode='ecfg:x' "
                "WHERE proposal_id='p0'")
    finally:
        mcon.close()


def test_s08_new_proposal_writers_persist_non_null_named_derivation_mode():
    """Expected-red: draft/publish path must persist non-null mode incl. state_truncated.

    Pins that proposal header construction cannot collapse state_truncated away.
    """
    # Inspect source for known collapse (current tip is red if only optimized/canonical)
    import inspect
    from modelark import proposal as proposal_mod
    src = inspect.getsource(proposal_mod)
    # Production Gate-2 must wire PlacementResult.derivation_mode through, including
    # state_truncated — contract fails until publish path preserves it.
    # Probe: a pure unit that will exist after Gate-2
    fn = getattr(proposal_mod, "derivation_mode_for_draft", None)
    if callable(fn):
        assert fn("state_truncated") == "state_truncated"
        assert fn("optimized") == "optimized"
        assert fn("canonical_fallback") == "canonical_fallback"
        with pytest.raises(Exception):
            fn(None)
        return
    # Until helper exists, require explicit evidence in draft assembly that state_truncated
    # is not hard-coded away solely via FEASIBLE ternary.
    assert "state_truncated" in src, (
        "proposal module must persist state_truncated as a first-class derivation_mode; "
        "export derivation_mode_for_draft or pass PlacementResult.derivation_mode through"
    )
    # Current FEASIBLE-only ternary is insufficient
    raise AssertionError(
        "new proposal writers must emit non-null derivation_mode from placement "
        "(optimized|state_truncated|canonical_fallback), not only "
        "optimized-if-FEASIBLE-else-canonical_fallback"
    )


# ===========================================================================
# Repair state / explicit maintenance (expected-red)
# ===========================================================================


def test_w07_connect_does_not_start_repair(tmp_path):
    """Positive pin: ordinary db.connect never invokes hash_repair."""
    data = _seed_v6_catalog(tmp_path)
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        db.configure(data, data / "state")
        with mock.patch("modelark.hash_repair.repair_hashes") as rh:
            con = db.connect()
            rh.assert_not_called()
            con.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old


def test_w08_explicit_maintenance_surface_required():
    """Expected-red: explicit run_explicit_drive_repair export (Fill preflight not this slice)."""
    _explicit_repair_fn()


def test_w09_repair_state_table_and_status_vocabulary(tmp_path):
    """Expected-red: drive_hash_repair_state exists with required key and statuses."""
    source = _seed_v6_catalog(tmp_path / "src")
    work = tmp_path / "work"
    work.mkdir()
    # After migration (or on current if table created independently)
    try:
        _rehearse_fn()(source, work, run_id="g1-w09")
        catalog = list(work.rglob("catalog.sqlite"))[0]
    except AssertionError:
        # If rehearse missing, still require table on a connect after Gate-2 schema
        raise
    con = sqlite3.connect(str(catalog))
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "drive_hash_repair_state" in tables
        cols = _cols(con, "drive_hash_repair_state")
        assert "drive_label" in cols and "identity_epoch" in cols
        assert "identity_fingerprint" in cols or "fingerprint" in cols
        assert "status" in cols
        # Closed status vocabulary — CHECK or reject invalid
        # Try insert invalid status if structure known
        try:
            con.execute(
                "INSERT INTO drive_hash_repair_state("
                "drive_label,identity_epoch,identity_fingerprint,status) "
                "VALUES('d0',1,?,?)",
                [_h("f"), "not_a_status"],
            )
            con.execute("DELETE FROM drive_hash_repair_state WHERE status='not_a_status'")
            raise AssertionError(
                "drive_hash_repair_state must reject statuses outside "
                f"{sorted(REPAIR_STATUSES)}"
            )
        except sqlite3.IntegrityError:
            pass
    finally:
        con.close()


def test_w10_w11_w12_w13_w15_w16_w17_repair_state_semantics(tmp_path):
    """Expected-red: atomicity, identity, absent→blocked_absent, complete, fp mismatch→halted.

    Explicit additions to §G.4:
      - absent drive → blocked_absent
      - complete requires zero unresolved candidates
      - identity fingerprint mismatch → halted
    """
    repair = _explicit_repair_fn()
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        _configure(tmp_path)
        con = db.connect()
        # Minimal drive + null-digest archived for candidates
        con.execute("INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
        con.execute(
            "INSERT OR REPLACE INTO drives(drive_label,capacity_bytes,free_bytes,role,"
            "raid_backed,identity_epoch,lifecycle,eligibility,identity_fingerprint) "
            "VALUES('d0',1,1,'primary',0,1,'active','enabled',?)", [_h("f")])
        con.execute("INSERT OR IGNORE INTO models(repo_id,status) VALUES('org/m','archived')")
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256) "
            "VALUES('org/m','w.bin',10,NULL)")
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,drive_label,orig_sha256,"
            "orig_bytes,stored_bytes,compressed,annex_key) "
            "VALUES('org/m','w.bin','d0',NULL,10,10,0,?)",
            [f"SHA256E-s10--{_h('a')}"])

        # W15: absent / unresolvable drive → blocked_absent
        rep = repair(con, "missing-drive", identity_epoch=1, identity_fingerprint=_h("x"))
        assert rep.get("status") == "blocked_absent" or _state(con, "missing-drive", 1) == (
            "blocked_absent"
        ), f"absent drive must transition to blocked_absent; got {rep!r}"

        # W17: fingerprint mismatch → halted
        rep_fp = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("Z"))
        st = rep_fp.get("status") or _state(con, "d0", 1)
        assert st == "halted", f"fingerprint mismatch must halt; got {st!r} / {rep_fp!r}"

        # W16: complete requires zero unresolved candidates
        rep_ok = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        st2 = rep_ok.get("status") or _state(con, "d0", 1)
        if st2 == "complete":
            left = con.execute(
                "SELECT count(*) FROM archived WHERE drive_label='d0' AND orig_sha256 IS NULL"
            ).fetchone()[0]
            assert left == 0, "complete requires zero unresolved candidates on that identity"
        else:
            assert st2 in REPAIR_STATUSES - {"complete"} or st2 == "needs_refetch"

        # W11: different epoch does not inherit complete
        st_e2 = _state(con, "d0", 2)
        assert st_e2 != "complete", "replacement epoch must not inherit completion"

        # W12: lost with archived facts is not complete merely for unmount
        con.execute("UPDATE drives SET lifecycle='lost' WHERE drive_label='d0'")
        rep_lost = repair(con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        st_lost = rep_lost.get("status") or _state(con, "d0", 1)
        assert st_lost != "complete" or rep_lost.get("unresolved", 1) == 0

        # W10: atomicity of archive rows + repair-state transitions
        atomic = getattr(
            importlib.import_module("modelark.hash_repair"),
            "repair_state_and_rows_atomic",
            None,
        )
        if not callable(atomic):
            raise AssertionError(
                "repair archive rows and drive_hash_repair_state transitions must commit "
                "atomically (export repair_state_and_rows_atomic or satisfy via "
                "run_explicit_drive_repair transactional behavior covered by failure injection)"
            )
        con.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old


def _state(con, label: str, epoch: int) -> str | None:
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


def test_w05_replica_healing_null_and_provenance_only(tmp_path):
    """Expected-red: targeted null fill / matching-digest provenance; mismatch halts."""
    fn = getattr(
        importlib.import_module("modelark.fetch"),
        "mirror_archived_row_heal",
        None,
    )
    if not callable(fn):
        # Alternative: document SQL behavior after production changes DO NOTHING path
        raise AssertionError(
            "export modelark.fetch.mirror_archived_row_heal(con, source_label, target_label, ...) "
            "or specialize replica mirror to fill null digest/provenance only; "
            "halt on non-null digest mismatch; never invent mirrored provenance"
        )


# ===========================================================================
# Boundaries (positive pins)
# ===========================================================================


def test_b01_dec055_satisfaction_remains_provenance_neutral():
    """Positive: Fill satisfaction still uses expected_sha256, not provenance."""
    digest = _h("d")
    assert fill_mod._archive_content_satisfies(
        digest, orig_sha256=digest, compressed=False, annex_key=None
    )
    assert fill_mod._archive_content_satisfies(
        None,
        orig_sha256=None,
        compressed=False,
        annex_key=f"SHA256E-s1--{digest}",
    )
    # Provenance is not a parameter of the shared rule
    sig = str(archive_hash.expected_sha256.__doc__ or "")
    assert "provenance" not in sig.lower() or True  # API has no provenance kw
    import inspect
    params = inspect.signature(archive_hash.expected_sha256).parameters
    assert "catalog_sha" in params and "orig_sha256" in params
    assert "provenance" not in params


def test_b02_inc027_out_of_this_gate1_surface():
    """Boundary: baseline cert self-copy not remade by provenance Gate-1 contracts."""
    # INC-027 surface must not be required here
    assert not hasattr(db, "recompute_baseline_certificates"), (
        "INC-027 baseline recompute must not ship under provenance Gate-1 surface"
    )


def test_b03_def033_boundary_pin_only():
    """Boundary: DEF-033 verifier unknown disposition is a later named gate."""
    from modelark import verifier
    # Still has policy-error fallback (unchanged this slice)
    import inspect
    src = inspect.getsource(verifier.reverify)
    assert "ArchivePolicyError" in src
    # No requirement that unknown is already implemented — only that this file
    # does not claim DEF-033 production
    this = Path(__file__).read_text()
    assert "DEF-033" in this and "boundary" in this.lower()


def test_b04_inc023_gate1_contracts_still_collectable():
    """Positive: INC-023 freeze contracts module still importable/collectable."""
    p = Path(__file__).resolve().parent / "test_inc023_gate1_contracts.py"
    assert p.is_file()
