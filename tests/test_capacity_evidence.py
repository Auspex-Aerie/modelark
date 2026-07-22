"""PR-02 / #35-A pure capacity-evidence primitives (tests-first, RFC-002 / DEC-049).

Gate 1: pins the pure, I/O-free evidence contract BEFORE production. No mounts, df, fencing, or writes
— every observation is supplied synthetically. RED until `modelark.capacity_evidence` exists; the lazy
import + `_require()` guard makes each test fail cleanly for missing v3 behavior, not a fixture cascade.

Fail-closed rules (contract): unknown -> zero executable capacity; legacy free_bytes is diagnostic
only; the safety floor is subtracted exactly once; an out-of-range/mismatched anchor is typed unknown.
"""
from __future__ import annotations

from modelark.core import db

try:
    from modelark import capacity_evidence as ce
    _HAS = True
except Exception:                                # noqa: BLE001 — module is the thing under construction
    ce = None
    _HAS = False


def _require():
    if not _HAS:
        raise AssertionError("modelark.capacity_evidence not implemented yet (expected Gate-1 red)")


def _derive(**over):
    """Derive evidence from a synthetic per-drive observation; override only the relevant fields.

    `current_*` describe the drive's current identity/capacity; `anchor_*` describe what a latest
    clean anchor recorded. An anchor is authoritative only when its epoch, fingerprint, and
    filesystem capacity all equal the current drive's."""
    base = dict(
        mounted=False, identity_proven=False, fence_held=False, write_authority="unknown",
        current_epoch=1, filesystem_capacity_bytes=1000, current_fingerprint="fp",
        live_free_bytes=None, dirty=False,
        anchor_free_bytes=None, anchor_epoch=None, anchor_fingerprint="fp",
        anchor_filesystem_capacity=1000, safety_floor_bytes=100,
    )
    base.update(over)
    return ce.derive(**base)


def test_identity_fingerprint_v1_golden_vector():
    _require()
    assert ce.identity_fingerprint_v1(
        fs_uuid="filesystem-uuid", annex_uuid="annex-uuid", serial="device-serial",
        filesystem_capacity_bytes=1999844147200,
    ) == "0eabbd90a47a23c1f02866f29dd0d0a55c853fb0fad61de32f56d54df07f2651"
    # explicit nulls are part of the canonical form (fs_uuid/serial absent, annex proven)
    assert ce.identity_fingerprint_v1(
        fs_uuid=None, annex_uuid="annex-uuid", serial=None,
        filesystem_capacity_bytes=1999844147200,
    ) == "adeac55523d3bb3648ffd47053f4324742847ba80f4267d66868c52be677bc50"
    # at least one of fs_uuid / annex_uuid must be proven before a fingerprint can be computed
    try:
        ce.identity_fingerprint_v1(fs_uuid=None, annex_uuid=None, serial="s",
                                   filesystem_capacity_bytes=1)
        raise AssertionError("must refuse a fingerprint with neither fs_uuid nor annex_uuid")
    except ValueError:
        pass


def test_live_evidence_is_authoritative_even_while_dirty():
    _require()
    ev = _derive(mounted=True, identity_proven=True, fence_held=True,
                 write_authority="dedicated_local", live_free_bytes=1000, dirty=True)
    assert ev.kind == "live" and ev.executable
    # observed_free is the raw observation; admissible subtracts the floor exactly once
    assert ev.observed_free == 1000 and ev.admissible_free == 900 and ev.code is None


def test_offline_clean_anchor_is_authoritative_for_the_matching_epoch():
    _require()
    ev = _derive(write_authority="dedicated_local", anchor_free_bytes=800, anchor_epoch=1,
                 current_epoch=1, filesystem_capacity_bytes=1000)
    assert ev.kind == "anchor" and ev.executable
    assert ev.observed_free == 800 and ev.admissible_free == 700


def test_unknown_tiers_preserve_binding_distinctions():
    _require()
    # Each case is single-fault so its typed code is unambiguous; the binding distinctions are
    # preserved rather than collapsed to a generic unknown.
    cases = {
        "identity_unproven": (dict(mounted=True, identity_proven=False, fence_held=True,
                                   write_authority="dedicated_local", live_free_bytes=1000),
                              "DRIVE_IDENTITY_UNPROVEN"),
        "fence_unavailable": (dict(mounted=True, identity_proven=True, fence_held=False,
                                   write_authority="dedicated_local", live_free_bytes=1000),
                              "DRIVE_FENCE_UNAVAILABLE"),
        "shared_writer": (dict(mounted=True, identity_proven=True, fence_held=True,
                               write_authority="unknown", live_free_bytes=1000),
                          "UNSUPPORTED_SHARED_WRITER"),
        "offline_dirty": (dict(write_authority="dedicated_local", anchor_free_bytes=800,
                               anchor_epoch=1, dirty=True), "DRIVE_RECONCILIATION_REQUIRED"),
        "anchor_epoch_mismatch": (dict(write_authority="dedicated_local", anchor_free_bytes=800,
                                       anchor_epoch=2, current_epoch=1),
                                  "DRIVE_RECONCILIATION_REQUIRED"),
        "anchor_fingerprint_mismatch": (dict(write_authority="dedicated_local", anchor_free_bytes=800,
                                             anchor_epoch=1, anchor_fingerprint="stale",
                                             current_fingerprint="fp"),
                                        "DRIVE_RECONCILIATION_REQUIRED"),
        "anchor_capacity_mismatch": (dict(write_authority="dedicated_local", anchor_free_bytes=800,
                                          anchor_epoch=1, anchor_filesystem_capacity=999,
                                          filesystem_capacity_bytes=1000),
                                     "DRIVE_RECONCILIATION_REQUIRED"),
        "offline_anchorless": (dict(write_authority="dedicated_local"), "CAPACITY_EVIDENCE_UNKNOWN"),
    }
    for name, (over, code) in cases.items():
        ev = _derive(**over)
        assert ev.kind == "unknown" and not ev.executable and ev.admissible_free == 0, name
        assert ev.observed_free is None, name       # no admissible observation for unknown evidence
        assert ev.code == code, (name, ev.code)


def test_out_of_range_anchor_is_typed_unknown():
    _require()
    ev = _derive(write_authority="dedicated_local", anchor_free_bytes=2000, anchor_epoch=1,
                 current_epoch=1, filesystem_capacity_bytes=1000, anchor_filesystem_capacity=1000)
    assert ev.kind == "unknown" and not ev.executable and ev.admissible_free == 0
    assert ev.code == "ANCHOR_OUT_OF_RANGE"


def test_unknown_optimistic_usable_max_is_post_floor_or_none():
    _require()
    known = _derive(write_authority="dedicated_local", filesystem_capacity_bytes=1000,
                    safety_floor_bytes=100)
    assert known.kind == "unknown" and known.optimistic_usable_max == 900   # post-floor, diagnostic only
    assert not known.executable and known.admissible_free == 0              # never authorizes bytes
    unknown_cap = _derive(write_authority="dedicated_local", filesystem_capacity_bytes=None)
    assert unknown_cap.kind == "unknown" and unknown_cap.optimistic_usable_max is None


def test_safety_floor_subtracted_once_and_clamped_at_zero():
    _require()
    exact = _derive(mounted=True, identity_proven=True, fence_held=True,
                    write_authority="dedicated_local", live_free_bytes=250, safety_floor_bytes=100)
    assert exact.observed_free == 250 and exact.admissible_free == 150   # 250 - 100, exactly once
    clamped = _derive(mounted=True, identity_proven=True, fence_held=True,
                      write_authority="dedicated_local", live_free_bytes=50, safety_floor_bytes=100)
    assert clamped.kind == "live" and clamped.observed_free == 50 and clamped.admissible_free == 0


def test_shadow_selects_current_epoch_anchor_not_a_higher_old_epoch_generation(tmp_path):
    """A stale old-epoch anchor at a higher generation must not shadow the valid current-epoch anchor.
    The reader selects the anchor matching the drive's exact current (identity_epoch, write_generation)."""
    _require()
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3, "fresh catalog must be v3 (Gate-1 red)"
    fp = "a" * 64
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,identity_epoch,write_generation,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority) "
        "VALUES('drive-00',1000,500,2,1,1000,?,'dedicated_local')", [fp])
    # current epoch 2, generation 1: the valid clean anchor
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                "VALUES('drive-00',2,1,'x')")
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
        "observed_at) VALUES('drive-00',2,1,500,1000,?,'dedicated_local','p','p','2026-01-01')", [fp])
    # stale OLD epoch 1 at a much HIGHER generation 100: must be ignored
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                "VALUES('drive-00',1,100,'x')")
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
        "observed_at) VALUES('drive-00',1,100,900,1000,?,'dedicated_local','p','p','2026-01-01')", [fp])

    shadow = ce.shadow_by_drive(con)
    ev = shadow["drive-00"]
    assert ev.kind == "anchor" and ev.executable, ev          # not DRIVE_RECONCILIATION_REQUIRED
    assert ev.observed_free == 500, ev                        # the current-epoch anchor, not the old 900
    con.close()


def test_shadow_read_is_side_effect_free_and_all_unknown(tmp_path):
    _require()
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    con = db.connect()                                   # fresh catalog is v3 directly (no migration here)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3, \
        "fresh catalog must be v3 (expected Gate-1 red)"
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes) VALUES('drive-00',1000,500)")
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes) VALUES('drive-01',1000,900)")
    before_free = con.execute("SELECT drive_label, free_bytes FROM drives ORDER BY 1").fetchall()

    shadow = ce.shadow_by_drive(con)                     # internal diagnostic accessor only
    assert set(shadow) == {"drive-00", "drive-01"}
    for e in shadow.values():
        # a migrated/unproven drive: unknown, zero executable, no admission authority
        assert e.kind == "unknown" and not e.executable and e.admissible_free == 0, shadow
        # current-epoch filesystem capacity is unknown post-migration -> optimistic max is None
        assert e.optimistic_usable_max is None
    # legacy free_bytes is exposed ONLY as a diagnostic, never as executable/admissible free
    assert shadow["drive-00"].legacy_free_bytes == 500 and shadow["drive-01"].legacy_free_bytes == 900
    # diagnostic read: no admission input mutated, no evidence fabricated
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    assert con.execute("SELECT drive_label, free_bytes FROM drives ORDER BY 1").fetchall() == before_free
    assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
    con.close()


def main():
    import inspect
    import tempfile
    from pathlib import Path
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                fn(Path(tempfile.mkdtemp(prefix="mark-ev-")))
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
