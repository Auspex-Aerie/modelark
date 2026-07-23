"""PR-04 / #35-C reporting boundary (tests-first, RFC-002 / DEC-049 — Gate-1 ruling 3).

Every free/usable value an operator sees must come from the shared evidence seam, carry its evidence
kind/provenance, and show ``unknown`` rather than pretending a zero is "fully used". Legacy
``free_bytes`` may appear ONLY under an explicitly diagnostic/legacy field. The nominal footprint/cart
gate in ``plan.py`` is theoretical fleet-size screening owned by #38 (not current free-space admission);
its algorithm is characterized as UNCHANGED here.

Offline drives make the seam deterministic without mounts: an offline migrated drive is ``unknown``; an
offline ``dedicated_local`` drive with a matching clean anchor is ``anchor``.

RED until the reporting surfaces are cut over; the ``plan.py`` gate characterization is GREEN.
"""
from __future__ import annotations

import inspect
import sqlite3
from unittest import mock

from modelark.core import db
from modelark import capacity, librarian, plan
from modelark.web import library_api

try:
    from modelark import admission  # noqa: F401 — presence gate for the cutover
    _HAS_ADMISSION = True
except ImportError as exc:                       # ONLY the missing shell — a real import/init defect surfaces
    if "admission" not in f"{getattr(exc, 'name', '') or ''} {exc}":
        raise
    _HAS_ADMISSION = False

_FP = "a" * 64


def _require_admission():
    if not _HAS_ADMISSION:
        raise AssertionError("PR-04 must add modelark/admission.py (see test_pr04_admission_seam)")


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    return con


def _migrated(con, label="drive-09", *, capacity_bytes=1000, free=500):
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes) VALUES(?,?,?)",
                [label, capacity_bytes, free])
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _clean_offline(con, label="drive-00", *, fscap=1000, anchor_free=800):
    """A reconciled drive that is currently offline but has a matching clean anchor -> anchor evidence."""
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,identity_epoch,write_generation,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority) "
        "VALUES(?,?,?,1,1,?,?, 'dedicated_local')", [label, fscap, anchor_free, fscap, _FP])
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code)"
                " VALUES(?,1,1,'reconcile')", [label])
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
        "observed_at) VALUES(?,1,1,?,?,?, 'dedicated_local','p','p','2026-01-01 00:00:00')",
        [label, anchor_free, fscap, _FP])


# --------------------------------------------------------------------------- librarian.drives()

def test_librarian_drives_free_from_evidence_not_reconstruction():
    """An offline migrated drive is unknown — never `free − archived − headroom` under a `free` field.
    Legacy scalar survives only as a diagnostic."""
    _require_admission()
    con = _mem()
    _migrated(con, "drive-09", capacity_bytes=1000, free=500)
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,stored_bytes,orig_bytes,compressed) "
                "VALUES('org/m','f.bin','drive-09',200,200,0)")
    row = next(item for item in librarian.drives(con, "ark") if item["label"] == "drive-09")
    assert row["evidence_kind"] == "unknown"
    assert row.get("remaining") in (0, None)                     # NOT 500 - 200 - headroom
    assert row["legacy_free_bytes"] == 500                       # diagnostic only
    con.close()


def test_librarian_drives_offline_clean_anchor_reports_anchor_evidence():
    _require_admission()
    con = _mem()
    _clean_offline(con, "drive-00", fscap=1000, anchor_free=800)
    row = next(item for item in librarian.drives(con, "ark") if item["label"] == "drive-00")
    assert row["evidence_kind"] == "anchor"
    assert row["remaining"] == 750                               # 800 anchor − safety_floor(1000)=50
    con.close()


# --------------------------------------------------------------------------- plan_view

def test_plan_view_rows_carry_evidence_kind_and_provenance():
    _require_admission()
    con = _mem()
    _migrated(con, "drive-09", capacity_bytes=1000, free=999)    # tempting legacy scalar
    view = librarian.plan_view(con, plan_id="ark")
    row = next(item for item in view["drives"] if item["label"] == "drive-09")
    assert row["evidence_kind"] == "unknown"
    assert row["usable"] == 0                                    # unknown != "fully used"
    assert "observed_at" in row and "identity_epoch" in row      # real provenance, not only a label
    assert row.get("legacy_free_bytes") == 999                   # legacy under a diagnostic field only
    con.close()


# --------------------------------------------------------------------------- library_api fleet/total

def test_library_api_fleet_and_total_free_from_evidence_legacy_diagnostic():
    _require_admission()
    con = _mem()
    _migrated(con, "drive-09", capacity_bytes=1000, free=500)

    def _q(sql, params=()):
        return con.execute(sql, params).fetchall()

    with mock.patch.object(library_api.data, "q", _q), \
         mock.patch.object(library_api.data, "conn", lambda: con):
        out = library_api.library()
    drive = next(item for item in out["fleet"] if item["label"] == "drive-09")
    assert drive["evidence_kind"] == "unknown"
    assert drive["free"] is None                                 # no evidence -> no admissible free
    assert drive["legacy_free"] == 500                           # raw scalar only under a legacy field
    # unknown fleet -> no admissible fleet free; `is None` (not 0) so a legacy SUM(free_bytes) can't pass
    assert out["totals"]["free"] is None
    con.close()


# --------------------------------------------------------------------------- library.js minimal render

def test_library_js_renders_evidence_kind_not_bare_free():
    """Minimal front-end rendering: show `unknown` instead of `0 / cap` (which reads as fully used)."""
    from modelark.web import server
    js = (server.STATIC / "library.js").read_text()
    assert "evidence_kind" in js, "library.js must render the drive's evidence kind (unknown vs live/anchor)"
    # and the old bare arithmetic must be gone: a zero/unknown free must not silently read as "fully used"
    assert "t.capacity - t.free" not in js, \
        "library.js must not render bare `capacity − free` (unknown/None free reads as fully used)"


# --------------------------------------------------------------------------- no legacy authority

def test_inspect_drives_reads_no_legacy_free_authority():
    """The #35 contract: remove every admission read of `drives.free_bytes` and every
    `capacity − SUM(stored_bytes)` reconstruction. Assert both legacy authorities are absent from the
    admission fact loader (matched on the durable column names, so a rename/alias cannot slip past);
    nominal `capacity_bytes` may remain for display/structural sizing."""
    src = inspect.getsource(capacity.inspect_drives).lower()
    assert "free_bytes" not in src, \
        "inspect_drives must not read drives.free_bytes as admission authority (evidence is authority)"
    assert "stored_bytes" not in src, \
        "inspect_drives must not reconstruct free as capacity − Σ stored_bytes (evidence is authority)"


# --------------------------------------------------------------------------- plan.py gate stays (#38)

def test_plan_gate_footprint_algorithm_is_unchanged():
    """Characterization (GREEN): plan.py's nominal fleet-size/footprint gate is theoretical screening
    owned by #38 — NOT current free-space admission. #35-C does not alter its algorithm."""
    con = _mem()
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,raid_backed) "
                "VALUES('d0',1000000000000,500000000000,0)")
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','d0')")
    assert plan.capacity(con, "ark") == 950_000_000_000          # Σ (capacity − headroom); nominal, not free
    assert plan.gate_tier(600_000_000_000, 950_000_000_000) == "ok"
    assert plan.gate_tier(950_000_000_000, 950_000_000_000) == "prevent"
    con.close()


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
