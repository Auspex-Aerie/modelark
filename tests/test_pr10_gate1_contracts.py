"""PR-10 Gate-1 contracts — DEC-055, DEC-052, v6 probe hygiene (expected red until Gate 2).

No production code in Gate 1. Each pin must fail if the rule is absent or reverted.
Call-shape for DEC-055: resolve stored digest via archive_hash.expected_sha256 with
catalog_sha=None (archive-row fields only; proposal_files remain file-list authority).
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _pr09_gate1_fixtures as f
from modelark.core import db


def _annex_key(digest: str, size: int = 10) -> str:
    return f"SHA256E-s{size}--{digest}"


def _proj_primary(repo="org/a", target="d0", rid="primary:org/a"):
    return SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id=repo, target_drive=target,
        source_drive=None, requirement_id=rid,
        schedule_state="ready", order_key=1,
        guaranteed_durable=10, expected_durable=10,
    ),))


def _proj_replica(repo="org/a", target="d1", source="d0", rid="replica:org/a"):
    return SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id=repo, target_drive=target,
        source_drive=source, requirement_id=rid,
        schedule_state="waiting_dependency", order_key=1,
        guaranteed_durable=10, expected_durable=10,
    ),))


# ---------------------------------------------------------------------------
# DEC-055 — shared restore-evidence resolution for Fill satisfaction
# ---------------------------------------------------------------------------


def test_dec055_routes_through_archive_hash_expected_sha256():
    """Fill satisfaction must call the shared accessor (not column compare alone)."""
    from modelark import archive_hash, fill as fill_mod

    digest = "a" * 64
    key = _annex_key(digest)
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','tiny.bin','d0',0,10,10,NULL,?)",
        [key])
    pfiles = [{
        "requirement_id": "primary:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    with mock.patch.object(
            archive_hash, "expected_sha256", wraps=archive_hash.expected_sha256
    ) as spy:
        fill_mod._projection_work_units(
            con, _proj_primary(), proposal_files=pfiles, require_proposal_files=True)
    assert spy.called, (
        "DEC-055: fill must route through archive_hash.expected_sha256 "
        "(Gate-1 red until Gate 2 wires the call)")
    # catalog_sha must not reopen files authority
    for call in spy.call_args_list:
        kwargs = call.kwargs if call.kwargs else {}
        if "catalog_sha" in kwargs:
            assert kwargs["catalog_sha"] is None, (
                "inventory call-shape: catalog_sha=None on fill satisfaction path")


def test_dec055_target_annex_key_null_orig_shrinks_out():
    """Approved null + raw SHA256E annex key → satisfied on target (DEC-055)."""
    from modelark import fill as fill_mod

    digest = "b" * 64
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','tiny.bin','d0',0,10,10,NULL,?)",
        [_annex_key(digest)])
    pfiles = [{
        "requirement_id": "primary:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    units = fill_mod._projection_work_units(
        con, _proj_primary(), proposal_files=pfiles, require_proposal_files=True)
    assert units == [], (
        "DEC-055: raw annex key must satisfy null-approved target "
        f"(got units={units!r})")


def test_dec055_source_annex_key_null_orig_ready():
    """Approved null + raw SHA256E on source → replica ready (DEC-055)."""
    from modelark import fill as fill_mod

    digest = "c" * 64
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','tiny.bin','d0',0,10,10,NULL,?)",
        [_annex_key(digest)])
    pfiles = [{
        "requirement_id": "replica:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    units = fill_mod._projection_work_units(
        con, _proj_replica(), proposal_files=pfiles, require_proposal_files=True)
    assert units and units[0].schedule_state == "ready", units
    assert units[0].kind is not None


def test_dec055_compressed_annex_key_without_orig_fails_closed_target():
    """Compressed annex key names compressed bytes — not original-byte satisfaction."""
    from modelark import fill as fill_mod

    digest = "d" * 64
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','tiny.bin','d0',1,10,10,NULL,?)",
        [_annex_key(digest)])
    pfiles = [{
        "requirement_id": "primary:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    units = fill_mod._projection_work_units(
        con, _proj_primary(), proposal_files=pfiles, require_proposal_files=True)
    assert units and "tiny.bin" in units[0].missing_files


def test_dec055_compressed_annex_key_without_orig_fails_closed_source():
    from modelark import fill as fill_mod

    digest = "e" * 64
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','tiny.bin','d0',1,10,10,NULL,?)",
        [_annex_key(digest)])
    pfiles = [{
        "requirement_id": "replica:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    units = fill_mod._projection_work_units(
        con, _proj_replica(), proposal_files=pfiles, require_proposal_files=True)
    assert units and units[0].schedule_state == "waiting_dependency"
    assert units[0].kind is None


def test_dec055_approved_present_requires_resolved_match_not_annex_alone():
    """When approved hash is set, resolved digest must equal it (annex of wrong digest fails)."""
    from modelark import fill as fill_mod

    want = "1" * 64
    other = "9" * 64
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','model.bin','d0',0,10,10,NULL,?)",
        [_annex_key(other)])
    pfiles = [{
        "requirement_id": "primary:org/a", "rfilename": "model.bin",
        "size_bytes": 10, "orig_sha256": want, "format": None, "quant": None,
    }]
    units = fill_mod._projection_work_units(
        con, _proj_primary(), proposal_files=pfiles, require_proposal_files=True)
    assert units and "model.bin" in units[0].missing_files


def test_dec055_approved_present_matches_annex_resolved_digest():
    """Approved hash equals annex-derived digest, orig_sha256 null → satisfied."""
    from modelark import fill as fill_mod

    want = "f" * 64
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','model.bin','d0',0,10,10,NULL,?)",
        [_annex_key(want)])
    pfiles = [{
        "requirement_id": "primary:org/a", "rfilename": "model.bin",
        "size_bytes": 10, "orig_sha256": want, "format": None, "quant": None,
    }]
    units = fill_mod._projection_work_units(
        con, _proj_primary(), proposal_files=pfiles, require_proposal_files=True)
    assert units == [], "approved hash matching annex-resolved digest must shrink out"


def test_dec055_both_null_no_annex_still_fails_closed_both_sides():
    """DEC-051 fail-closed retained when nothing is resolvable."""
    from modelark import fill as fill_mod

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','tiny.bin','d0',0,10,10,NULL,NULL)")
    pfiles = [{
        "requirement_id": "primary:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    units_t = fill_mod._projection_work_units(
        con, _proj_primary(), proposal_files=pfiles, require_proposal_files=True)
    assert units_t and "tiny.bin" in units_t[0].missing_files

    pfiles_r = [{
        "requirement_id": "replica:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    units_s = fill_mod._projection_work_units(
        con, _proj_replica(), proposal_files=pfiles_r, require_proposal_files=True)
    assert units_s and units_s[0].schedule_state == "waiting_dependency"


def test_dec055_reverts_if_column_only_presence_returns():
    """If implementation falls back to presence-only on null/null+annex, pin fails."""
    from modelark import archive_hash, fill as fill_mod

    digest = "aa" * 32
    # Shared accessor says resolvable
    assert archive_hash.expected_sha256(
        catalog_sha=None, orig_sha256=None, compressed=False,
        annex_key=_annex_key(digest),
    ) == digest
    # Fill must agree with that resolution on the target path
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,annex_key) "
        "VALUES('org/a','tiny.bin','d0',0,10,10,NULL,?)",
        [_annex_key(digest)])
    pfiles = [{
        "requirement_id": "primary:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None,
    }]
    units = fill_mod._projection_work_units(
        con, _proj_primary(), proposal_files=pfiles, require_proposal_files=True)
    # Revert to column-only DEC-051 leaves this missing; DEC-055 shrinks out.
    assert units == [], "pin: annex resolution must not be dropped by column-only path"


# ---------------------------------------------------------------------------
# DEC-052 — acceptance-evidence identity and RO/copy-first access
# ---------------------------------------------------------------------------


def test_dec052_validate_does_not_gate_on_source_sqlite_sha256_mismatch():
    """Container byte hash is provenance only — content hashes bind identity."""
    from modelark import execution_benchmark as bench
    from modelark.proposal import Refusal

    # Minimal descriptor path: content hashes match, container hash deliberately wrong.
    desc = {
        "harness_generator_version": "gate1-dec052",
        "selected_repository_count": 2,
        "model_count": 2,
        "file_count": 2,
        "source_sqlite_sha256": "0" * 64,  # wrong container hash
        "prepared_canonical_input_hash": "1" * 64,
        "prepared_projection_hash": "2" * 64,
        "sqlite_path": None,
    }
    op = {
        "source_sqlite_sha256": "0" * 64,
        "selected_repository_count": 2,
        "model_count": 2,
    }

    def fake_recompute(_path):
        return {
            "source_sqlite_sha256": "f" * 64,  # different container
            "prepared_canonical_input_hash": "1" * 64,
            "prepared_projection_hash": "2" * 64,
            "selected_repository_count": 2,
            "model_count": 2,
            "file_count": 2,
            "requirement_count": 2,
            "task_count": 2,
            "sqlite_path": "x",
            "proposal_task_rows": [],
        }

    # With a path, current code refuses on source_sha — DEC-052 must not.
    desc["sqlite_path"] = "/nonexistent-but-recompute-mocked"
    try:
        bench.validate_acceptance_fixture_descriptor(
            desc, operator_approved_identity=op, recompute=fake_recompute)
        ok = True
        err = None
    except Refusal as exc:
        ok = False
        err = exc
    assert ok, (
        f"DEC-052: source_sqlite_sha256 mismatch must not gate when content hashes "
        f"agree (got {err!r})")


def test_dec052_recompute_opens_sqlite_read_only(tmp_path):
    """Evidence catalogs must open read-only (or copy-first for write paths)."""
    from modelark import execution_benchmark as bench

    # Tiny valid sqlite for recompute
    path = tmp_path / "ev.sqlite"
    con = sqlite3.connect(str(path))
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/a',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/a','m.bin',1,'safetensors','bf16',?)", ["a" * 64])
    con.execute(
        "INSERT INTO selection(repo_id,finalized_at) VALUES('org/a','2026-01-01')")
    con.commit()
    con.close()

    modes = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        modes.append({"args": args, "kwargs": dict(kwargs)})
        return real_connect(*args, **kwargs)

    with mock.patch("modelark.execution_benchmark.sqlite3.connect", side_effect=tracking_connect):
        bench.recompute_fixture_identity(path)

    assert modes, "expected at least one connect"
    # DEC-052: must use URI mode=ro or equivalent read-only flag
    def is_readonly(entry):
        args, kwargs = entry["args"], entry["kwargs"]
        uri = str(args[0]) if args else ""
        if "mode=ro" in uri or kwargs.get("uri") is True and "mode=ro" in uri:
            return True
        # sqlite3.connect(..., uri=True) with file:...?mode=ro
        if kwargs.get("uri") and "mode=ro" in uri:
            return True
        return False

    assert all(is_readonly(m) for m in modes), (
        f"DEC-052: recompute_fixture_identity must open RO; got {modes!r}")


def test_dec052_measure_refresh_does_not_open_evidence_readwrite(tmp_path):
    """measure_executor_refresh_boundaries must not hold RW handle on evidence."""
    from modelark import execution_benchmark as bench

    # Contract pin via source inspection + connect tracking when measure is invocable.
    src = inspect.getsource(bench.measure_executor_refresh_boundaries)
    assert "mode=ro" in src or "copy" in src.lower() or "tempfile" in src.lower() or \
        "NamedTemporaryFile" in src or "backup" in src.lower(), (
        "DEC-052: measure_executor_refresh_boundaries must document RO or copy-first "
        "access to the evidence artifact (Gate-1 red until Gate 2)")


def test_dec052_fixture_path_from_config_not_env_and_skips_when_absent():
    """Fixture location from config (not env); absent → typed skip + skipped_measurement."""
    from modelark import execution_benchmark as bench

    # Prefer an explicit resolver if present after Gate 2
    resolver = getattr(bench, "resolve_acceptance_fixture_path", None)
    assert resolver is not None, (
        "DEC-052: need resolve_acceptance_fixture_path (or equivalent) reading "
        "config — not an environment variable")
    # When config omits path, must not raise blindly — typed skip contract
    path, reason = resolver(config={})
    assert path is None
    assert reason, "typed skip reason required when fixture path absent"


# ---------------------------------------------------------------------------
# Item 4 hygiene — v6 short-hash probe must assert CHECK-specific failure
# ---------------------------------------------------------------------------


def test_gate1_v6_probe_requires_check_specific_integrity_error():
    """Probe must not treat an unrelated IntegrityError as proof the CHECK works."""
    src = inspect.getsource(db._migrate_execution_config_hash_v6)
    # Contract: after catching IntegrityError on short-hash insert, must verify
    # the error is the CHECK (execution_config_hash / check), not bare `pass`.
    # Accept either explicit message assertion or a dedicated helper call.
    has_specific = (
        ("execution_config_hash" in src and "check" in src.lower()
         and "IntegrityError" in src
         and "raise" in src)
        and (
            "if \"execution_config_hash\"" in src
            or "if 'execution_config_hash'" in src
            or "_assert" in src
            or "not in str" in src
        )
    )
    # Current probe1: `except IntegrityError: pass` with no message check — red.
    # Probe2 has a partial check; require probe1 (first short insert) also specific.
    first_probe = src.split("Positive control")[0] if "Positive control" in src else src
    probe1_specific = (
        "execution_config_hash" in first_probe
        and (
            "not in str" in first_probe
            or "execution_config_hash" in first_probe.split("IntegrityError")[-1][:400]
            and "if" in first_probe.split("IntegrityError")[-1][:200]
        )
        and "pass  # expected" not in first_probe
        and "pass  # expected — CHECK (or PK/FK)" not in first_probe
    )
    # Stronger behavioral pin: helper that classifies IntegrityError
    helper = getattr(db, "_is_execution_config_hash_check_error", None)
    if helper is not None:
        assert helper(Exception("CHECK constraint failed: execution_config_hash"))
        assert not helper(Exception("UNIQUE constraint failed: placement_proposals.proposal_id"))
        return
    assert probe1_specific or has_specific and "pass  # expected — CHECK (or PK/FK)" not in first_probe, (
        "v6 short-hash probe must assert CHECK-specific IntegrityError, "
        "not accept any IntegrityError (PK/FK collision would false-pass)")
