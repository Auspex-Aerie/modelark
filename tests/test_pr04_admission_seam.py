"""PR-04 / #35-C admission-authority seam (tests-first, RFC-002 / DEC-049).

Gate 1 pins the SHARED admission-evidence seam BEFORE production. The pure precedence rule
(``capacity_evidence.derive``) already exists and is authoritative; this slice adds the small NEUTRAL
observation shell (``modelark.admission``) that feeds it, and cuts every admission consumer onto its
typed ``Evidence`` output. It encodes the operator's Gate-1 rulings:

  1. Preview and execution are DISTINCT observation paths that share the derivation rules, not one
     volatile snapshot. The preview/API/CLI path captures facts, tries the identity-derived drive fence
     NON-BLOCKING, revalidates the captured epoch/fingerprint after acquisition, observes, derives, then
     RELEASES (a snapshot, not a reservation). The per-file EXECUTION path consumes a fresh observation
     taken while ``drive_mutation`` ALREADY holds the fence — it never reacquires the fence and never
     falls back to an unfenced/legacy read. The shell takes an INJECTED observation callback and never
     imports the protected transport's private functions.
  2. One capacity representation: ``CapacityDrive`` carries raw ``observed_free`` AND final
     ``admissible_free``; ``usable_now`` is the latter DIRECTLY with no second safety-floor subtraction;
     the floor basis is the current-epoch ``filesystem_capacity_bytes`` (never nominal ``capacity_bytes``);
     evidence kind/code and ``observed_at``/``identity_epoch`` provenance travel with the drive.
  4. Unknown evidence contributes zero executable capacity and its typed code survives into the ledger,
     diagnostics, and operator actions (recommend mount/reconcile — never misrepresented as observed
     free-space exhaustion). This PR does NOT import #38's mixed-fleet graded-feasibility ladder.

RED until ``modelark.admission`` and the evidence-fed ``capacity`` representation exist; the lazy import
+ ``_require_admission()`` guard makes each test fail for the reviewed missing behavior, not a fixture
cascade. GREEN characterization tests freeze the pure ``derive`` contract this slice builds on.
"""
from __future__ import annotations

import sqlite3
from unittest import mock

from modelark.core import db

try:
    import modelark.admission as admission
    _HAS_ADMISSION = True
except ModuleNotFoundError as exc:               # ONLY the exact absent submodule — a real defect surfaces
    if exc.name != "modelark.admission":
        raise
    admission = None
    _HAS_ADMISSION = False

from modelark import capacity, capacity_evidence, drive_fence, drive_mutation, reconcile

_FP = "a" * 64
_FP_OTHER = "b" * 64


def _require_admission():
    if not _HAS_ADMISSION:
        raise AssertionError(
            "PR-04 must add the neutral shell modelark/admission.py exposing execution_evidence("
            "con, label, observation, *, now) and preview_by_drive(con, labels, *, observe, now, "
            "fence=drive_fence.hold_drives_sorted) — both funnelling through capacity_evidence.derive")


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    return con


def _proven(con, label="drive-00", *, role="primary", raid=0, epoch=1, generation=0,
            fscap=1000, free=900, nominal=None, fp=_FP, authority="dedicated_local"):
    """A reconciled drive: proven identity + epoch filesystem capacity + dedicated_local authority."""
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes,identity_epoch,"
        "write_generation,filesystem_capacity_bytes,identity_fingerprint,write_authority) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        [label, role, raid, nominal if nominal is not None else fscap, free,
         epoch, generation, fscap, fp, authority])
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _migrated(con, label="drive-09", *, role="primary", raid=0, capacity_bytes=1000, free=500):
    """A migrated/registered drive: nominal scalars only, no proven identity/authority -> the valid
    ``unknown`` state until ``drive reconcile`` bootstraps it (fail-closed baseline)."""
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes) VALUES(?,?,?,?,?)",
        [label, role, raid, capacity_bytes, free])
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _dirty(con, label, *, epoch=1, gen=1, op="x"):
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code)"
                " VALUES(?,?,?,?)", [label, epoch, gen, op])


def _anchor(con, label, *, epoch=1, gen=1, free=900, fscap=1000, fp=_FP, now="2026-01-01 00:00:00"):
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
        "observed_at) VALUES(?,?,?,?,?,?, 'dedicated_local','p','p', ?)",
        [label, epoch, gen, free, fscap, fp, now])


def _obs(*, proven=True, free=900, fscap=1000, fp=_FP):
    """A held-fence Observation as the transport produces it (duck-typed by the neutral shell)."""
    return drive_mutation.Observation(
        identity_proven=proven, free_bytes=free, filesystem_capacity=fscap,
        fingerprint=fp, identity_proof="p", fence_proof="p")


class _FakeFence:
    """A non-blocking drive-fence stand-in recording acquire/release + the observed call order."""

    def __init__(self, *, contended=False):
        self.contended = contended
        self.entered = self.exited = 0
        self.blocking = "unset"
        self.keyed = None

    def __call__(self, keyed, *, blocking=True):
        self.keyed, self.blocking = keyed, blocking
        return self

    def __enter__(self):
        if self.contended:
            raise drive_fence.FenceUnavailable("k", {"path": "k"})
        self.entered += 1
        return []

    def __exit__(self, *exc):
        self.exited += 1
        return False


# --------------------------------------------------------------------------- pure base (GREEN)

def test_pure_derive_precedence_is_the_shared_rule():
    """Characterization: both new paths MUST derive through this exact pure rule (floor once)."""
    live = capacity_evidence.derive(
        mounted=True, identity_proven=True, fence_held=True, write_authority="dedicated_local",
        current_epoch=1, filesystem_capacity_bytes=1000, current_fingerprint=_FP,
        live_free_bytes=900, dirty=True, anchor_free_bytes=None, anchor_epoch=None,
        anchor_fingerprint=_FP, anchor_filesystem_capacity=1000, safety_floor_bytes=50)
    assert live.kind == "live" and live.executable and live.observed_free == 900
    assert live.admissible_free == 850                              # 900 - 50, exactly once, while dirty
    unfenced = replace_kwargs(fence_held=False)
    assert unfenced.kind == "unknown" and unfenced.code == "DRIVE_FENCE_UNAVAILABLE"
    assert unfenced.admissible_free == 0


def replace_kwargs(**over):
    base = dict(
        mounted=True, identity_proven=True, fence_held=True, write_authority="dedicated_local",
        current_epoch=1, filesystem_capacity_bytes=1000, current_fingerprint=_FP,
        live_free_bytes=900, dirty=False, anchor_free_bytes=None, anchor_epoch=None,
        anchor_fingerprint=_FP, anchor_filesystem_capacity=1000, safety_floor_bytes=50)
    base.update(over)
    return capacity_evidence.derive(**base)


# --------------------------------------------------------------------------- ruling 1: two paths

def test_execution_path_consumes_held_observation_and_never_fences():
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=0, nominal=999_999, generation=1)  # dirty, huge nominal
    _dirty(con, "drive-00", gen=1)                                                # generation 1, no anchor
    # execution takes a FRESH held-fence observation; it must never acquire a fence itself
    with mock.patch.object(drive_fence, "hold_drives_sorted",
                           side_effect=AssertionError("execution must not acquire a fence")), \
         mock.patch.object(drive_fence, "hold_controller",
                           side_effect=AssertionError("execution must not acquire the controller")):
        ev = admission.execution_evidence(con, "drive-00", _obs(free=900, fscap=1000), now="2026-07-23")
    assert ev.kind == "live" and ev.executable, ev
    assert ev.observed_free == 900 and ev.admissible_free == 850                  # floor from epoch fscap=1000
    assert ev.observed_at == "2026-07-23" and ev.identity_epoch == 1
    con.close()


def test_execution_path_unproven_observation_is_unknown_not_legacy_fallback():
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=777)                                # legacy free must NOT leak
    ev = admission.execution_evidence(con, "drive-00", _obs(proven=False, free=None), now="t")
    assert ev.kind == "unknown" and not ev.executable and ev.admissible_free == 0
    assert ev.code == "DRIVE_IDENTITY_UNPROVEN"
    assert ev.observed_free is None                                              # no unfenced/legacy fallback
    con.close()


def test_execution_path_revalidates_observation_identity_against_persisted():
    """A swapped volume: the observation proves ITS OWN identity, but it disagrees with the captured
    fingerprint -> unknown, never admitted on a different volume's free space."""
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, fp=_FP)
    ev = admission.execution_evidence(con, "drive-00", _obs(proven=True, free=900, fp=_FP_OTHER), now="t")
    assert ev.kind == "unknown" and ev.code == "DRIVE_IDENTITY_UNPROVEN" and ev.admissible_free == 0
    con.close()


def test_preview_path_tryholds_nonblocking_observes_under_fence_then_releases():
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=900, fp=_FP)
    fence = _FakeFence()
    order = {}

    def observe(label):
        order["observe_after_enter"] = fence.entered > 0                          # observed UNDER the fence
        return _obs(free=900, fscap=1000, fp=_FP)

    got = admission.preview_by_drive(con, ["drive-00"], observe=observe, now="2026-07-23", fence=fence)
    ev = got["drive-00"]
    assert ev.kind == "live" and ev.admissible_free == 850
    assert fence.blocking is False                                               # NON-blocking try-hold
    assert order["observe_after_enter"] is True                                  # observe after acquire
    assert fence.entered == 1 and fence.exited == 1                              # released — a snapshot
    assert ev.observed_at == "2026-07-23" and ev.identity_epoch == 1
    con.close()


def test_preview_path_contended_fence_is_unknown_fail_closed():
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=900)
    got = admission.preview_by_drive(
        con, ["drive-00"], observe=lambda label: _obs(free=900), now="t",
        fence=_FakeFence(contended=True))
    ev = got["drive-00"]
    assert ev.kind == "unknown" and ev.code == "DRIVE_FENCE_UNAVAILABLE" and ev.admissible_free == 0
    con.close()


def test_preview_path_offline_uses_anchor_or_typed_unknown():
    _require_admission()
    con = _mem()
    _proven(con, "clean", fscap=1000, free=900, generation=1)
    _dirty(con, "clean", gen=1)
    _anchor(con, "clean", gen=1, free=800, fscap=1000, fp=_FP)                   # matching clean anchor
    _proven(con, "dirtyd", fscap=1000, free=900, generation=2)
    _dirty(con, "dirtyd", gen=2)                                                 # dirty, no anchor
    _proven(con, "bare", fscap=1000, free=900, generation=0)                     # never written, no anchor

    got = admission.preview_by_drive(
        con, ["clean", "dirtyd", "bare"], observe=lambda label: None, now="t")   # all offline
    assert got["clean"].kind == "anchor" and got["clean"].observed_free == 800
    assert got["clean"].admissible_free == 750
    assert got["dirtyd"].kind == "unknown" and got["dirtyd"].code == "DRIVE_RECONCILIATION_REQUIRED"
    assert got["bare"].kind == "unknown" and got["bare"].code == "CAPACITY_EVIDENCE_UNKNOWN"
    con.close()


def test_preview_path_revalidates_facts_changed_between_capture_and_fence():
    """The race the try-hold guards: the drive's identity/epoch changes AFTER facts are captured but
    while the fence is being acquired. The seam must RE-READ facts under the held fence and fail closed —
    never admit on the pre-lock snapshot. Modeled by mutating the row at fence acquisition."""
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=900, fp=_FP)          # captured identity: epoch 1 / _FP

    class _MutatingFence:
        """Acquisition commits a lifecycle change (epoch bump) between capture and the held fence."""

        def __init__(self):
            self.blocking = "unset"

        def __call__(self, keyed, *, blocking=True):
            self.blocking = blocking
            return self

        def __enter__(self):
            con.execute("UPDATE drives SET identity_epoch=2, identity_fingerprint=? "
                        "WHERE drive_label='drive-00'", [_FP_OTHER])
            return []

        def __exit__(self, *exc):
            return False

    got = admission.preview_by_drive(
        con, ["drive-00"],
        observe=lambda label: _obs(free=900, fscap=1000, fp=_FP),   # attests the pre-lock identity
        now="t", fence=_MutatingFence())
    ev = got["drive-00"]
    assert ev.kind == "unknown" and ev.admissible_free == 0, ev     # re-read under the fence -> fail closed
    assert ev.code == "DRIVE_IDENTITY_UNPROVEN", ev
    con.close()


# --------------------------------------------------------------------------- ruling 2: representation

def test_capacity_drive_usable_now_is_admissible_free_with_no_second_subtraction():
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=900, nominal=999_999_999)          # nominal is NOT the basis
    evidence = admission.preview_by_drive(
        con, ["drive-00"], observe=lambda label: _obs(free=900, fscap=1000), now="2026-07-23",
        fence=_FakeFence())
    drives = capacity.inspect_drives(con, "ark", evidence_by_drive=evidence)
    drive = next(item for item in drives if item.drive_label == "drive-00")
    assert drive.usable_now == 850                                               # == admissible_free, once
    assert drive.usable_now == evidence["drive-00"].admissible_free
    assert drive.observed_free == 900                                            # raw observation preserved
    assert drive.evidence_kind == "live" and drive.evidence_code is None
    assert drive.observed_at == "2026-07-23" and drive.identity_epoch == 1
    assert drive.safety_floor == 50                                             # available for reporting only
    con.close()


def test_inspect_drives_ignores_legacy_free_bytes_as_authority():
    """A migrated drive with a large legacy free_bytes but no proven evidence admits ZERO."""
    _require_admission()
    con = _mem()
    _migrated(con, "drive-09", capacity_bytes=1_000_000, free=999_999)           # tempting legacy scalar
    evidence = admission.preview_by_drive(con, ["drive-09"], observe=lambda label: None, now="t")
    drives = capacity.inspect_drives(con, "ark", evidence_by_drive=evidence)
    drive = next(item for item in drives if item.drive_label == "drive-09")
    assert drive.evidence_kind == "unknown" and drive.usable_now == 0            # free_bytes never authority
    con.close()


def test_preflight_file_uses_admissible_free_directly():
    _require_admission()
    con = _mem()
    _proven(con, "drive-00", fscap=1000, free=900)
    evidence = admission.preview_by_drive(
        con, ["drive-00"], observe=lambda label: _obs(free=900, fscap=1000), now="t",
        fence=_FakeFence())
    drive = next(item for item in capacity.inspect_drives(con, "ark", evidence_by_drive=evidence)
                 if item.drive_label == "drive-00")
    fits = capacity.FileBudget(rfilename="f", guaranteed_durable=850, expected_durable=850,
                               workspace_peak_guaranteed=0, workspace_peak_expected=0, evidence="estimate")
    over = capacity.FileBudget(rfilename="g", guaranteed_durable=851, expected_durable=851,
                               workspace_peak_guaranteed=0, workspace_peak_expected=0, evidence="estimate")
    assert capacity.preflight_file(drive, fits, capacity.CapacityMode.GUARANTEED) is None
    failure = capacity.preflight_file(drive, over, capacity.CapacityMode.GUARANTEED)
    assert failure is not None and failure.available_bytes == 850               # admissible, not re-floored
    con.close()


# --------------------------------------------------------------------------- ruling 4: unknown survives

def test_unknown_evidence_survives_into_ledger_and_recommends_reconcile():
    """A conservatively-blocked plan on an unknown fleet must EXPOSE unknown evidence and recommend
    mount/reconcile — not misrepresent it as observed free-space exhaustion. #38 owns the final ladder."""
    _require_admission()
    con = _mem()
    _migrated(con, "drive-00", capacity_bytes=1_000_000, free=999_999)
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('org/m','2026-01-01')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
                "VALUES('org/m','model.safetensors',100,'safetensors','bf16')")
    graph = reconcile.reconcile_plan(con, "ark")
    evidence = admission.preview_by_drive(con, ["drive-00"], observe=lambda label: None, now="t")
    result = capacity.plan_capacity(con, graph, evidence_by_drive=evidence)
    assert not result.feasible
    ledger = next(item for item in result.ledgers if item.drive_label == "drive-00")
    assert ledger.evidence_kind == "unknown"                                    # kind survives to the ledger
    failure = result.failures[0]
    assert failure.evidence_code == "CAPACITY_EVIDENCE_UNKNOWN"                  # typed code survives
    # the operator is told to mount/reconcile, NOT that observed free space is exhausted. The complete
    # outcome-code ladder (which top-level FailureCode applies) belongs to #38, not this PR.
    assert any("reconcile" in action or "mount" in action for action in failure.actions), failure.actions
    con.close()


def main():
    import inspect
    import shutil
    import tempfile
    from pathlib import Path
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        tmp = None
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                tmp = Path(tempfile.mkdtemp(prefix="mark-pr04-"))
                fn(tmp)
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
        finally:
            if tmp is not None:                  # the standalone runner cleans its own temp trees
                shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
