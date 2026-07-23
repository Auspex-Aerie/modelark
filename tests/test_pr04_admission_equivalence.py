"""PR-04 / #35-C admission equivalence (tests-first, RFC-002 / DEC-049 — Gate-1 ruling 5).

Two groups, kept deliberately distinct:

  Group 1 (same record -> same math): given ONE injected ``Evidence`` per drive, the planner ledger,
  its serialization, and per-file preflight all compute the SAME admissible bytes. They share the
  derivation RULES and a single injected record.

  Group 2 (execution is fresher, may be more conservative): at runtime the per-file check takes a NEW
  held-fence observation. It may admit LESS than an earlier preview; it must never reuse the stale
  preview number. This test proves the execution path is not a replay of the preview snapshot.

RED until ``capacity.plan_capacity``/``inspect_drives`` accept ``evidence_by_drive`` and the neutral
``modelark.admission`` execution path exists.
"""
from __future__ import annotations

import sqlite3

from modelark.core import db

try:
    from modelark import admission
    _HAS_ADMISSION = True
except Exception:                                # noqa: BLE001
    admission = None
    _HAS_ADMISSION = False

from modelark import capacity, capacity_evidence, drive_mutation, reconcile

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


def _proven(con, label, *, role="primary", raid=0, fscap=1000, free=900, fp=_FP):
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes,identity_epoch,"
        "write_generation,filesystem_capacity_bytes,identity_fingerprint,write_authority) "
        "VALUES(?,?,?,?,?,1,0,?,?, 'dedicated_local')",
        [label, role, raid, fscap, free, fscap, fp])
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _repo(con, repo, *, size=200):
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01')", [repo])
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
                "VALUES(?,?,?, 'gguf', NULL)", [repo, "model.gguf", size])


def _evidence(*, admissible, observed, kind="live", epoch=1):
    """One injected admission-evidence record shared by all consumers (group 1)."""
    return capacity_evidence.Evidence(
        kind=kind, executable=(kind != "unknown"), admissible_free=admissible, code=None,
        observed_free=observed, optimistic_usable_max=None, legacy_free_bytes=None,
        observed_at="2026-07-23", identity_epoch=epoch)


def test_same_injected_evidence_yields_equal_admissible_across_consumers():
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=900)
    _repo(con, "org/model", size=100)
    graph = reconcile.reconcile_plan(con, "ark")

    injected = {"drive-00": _evidence(admissible=850, observed=900)}

    # planner ledger
    result = capacity.plan_capacity(con, graph, evidence_by_drive=injected)
    ledger = next(item for item in result.ledgers if item.drive_label == "drive-00")
    # its serialization
    serialized = result.to_dict()["ledgers"][0]
    # per-file preflight over the same injected record
    drive = next(item for item in capacity.inspect_drives(con, "ark", evidence_by_drive=injected)
                 if item.drive_label == "drive-00")

    assert ledger.usable_now == 850
    assert serialized["usable_now"] == 850
    assert drive.usable_now == 850
    assert ledger.usable_now == serialized["usable_now"] == drive.usable_now == 850
    con.close()


def test_runtime_per_file_takes_fresh_observation_and_may_be_more_conservative():
    """The drive filled between preview and execution. Per-file admission uses the fresh held-fence
    observation and refuses; it does NOT reuse the earlier optimistic preview number."""
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=900)

    # earlier PREVIEW said 850 admissible (feasible for a 500-byte file)
    preview = admission.preview_by_drive(
        con, ["drive-00"],
        observe=lambda label: drive_mutation.Observation(True, 900, 1000, _FP, "p", "p"),
        now="preview", fence=_PassFence())
    assert preview["drive-00"].admissible_free == 850

    budget = capacity.FileBudget(rfilename="model.gguf", guaranteed_durable=500, expected_durable=500,
                                 workspace_peak_guaranteed=0, workspace_peak_expected=0, evidence="estimate")
    # RUNTIME: a fresh held-fence observation shows only 100 bytes free -> admissible 50 -> refuse
    fresh = admission.execution_evidence(
        con, "drive-00", drive_mutation.Observation(True, 100, 1000, _FP, "p", "p"), now="run")
    assert fresh.admissible_free == 50                                          # fresh, not the 850 preview
    drive = next(item for item in capacity.inspect_drives(con, "ark", evidence_by_drive={"drive-00": fresh})
                 if item.drive_label == "drive-00")
    failure = capacity.preflight_file(drive, budget, capacity.CapacityMode.GUARANTEED)
    assert failure is not None and failure.available_bytes == 50                # conservative, not reused 850
    con.close()


class _PassFence:
    def __call__(self, keyed, *, blocking=True):
        return self

    def __enter__(self):
        return []

    def __exit__(self, *exc):
        return False


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
