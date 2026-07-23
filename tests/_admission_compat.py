"""Test compatibility shim for the #35-C admission cutover.

The placement, reconciled-executor, and library-projection unit tests supply drives via ``free_bytes``
and exercise ``tiered_v1`` placement / the reconciled executor / the projection adapters — NOT the
capacity-evidence seam, which the PR-04 admission suites cover directly against real fenced/anchor
evidence.

After #35-C those callers derive admission free from the evidence seam, so a synthetic ``free_bytes``
drive with no proven identity/anchor is ``unknown`` (zero executable). This shim patches the seam (and
the ``inspect_drives`` fallback used by tests that call ``plan_capacity`` directly) to synthesize LIVE
evidence from each drive's ``free_bytes`` using the pre-cutover snapshot semantics — ``free − Σ
stored_bytes − safety_floor``, recomputed on every call so capacity still shrinks as the executor
archives — keeping those tests exercising their real subject unchanged.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest import mock

from modelark import admission, capacity, capacity_evidence

_NOW = "2026-01-01 00:00:00"


def _synth_one(con, label: str, now: str) -> capacity_evidence.Evidence:
    row = con.execute(
        "SELECT coalesce(capacity_bytes,0), coalesce(free_bytes,0), coalesce(raid_backed,0) "
        "FROM drives WHERE drive_label=?", [label]).fetchone()
    if row is None:
        return capacity_evidence.Evidence(
            kind="unknown", executable=False, admissible_free=0, code="CAPACITY_EVIDENCE_UNKNOWN",
            observed_at=now, identity_epoch=1)
    cap, free, raid = int(row[0]), int(row[1]), bool(row[2])
    archived = con.execute(
        "SELECT coalesce(sum(stored_bytes),0) FROM archived WHERE drive_label=?", [label]).fetchone()[0]
    net = max(0, free - int(archived or 0))
    floor = capacity.safety_floor(cap, raid)
    return capacity_evidence.Evidence(
        kind="live", executable=True, admissible_free=max(0, net - floor), observed_free=net,
        observed_at=now, identity_epoch=1)


def evidence_for_plan(con, plan_id: str = "ark") -> dict:
    labels = [row[0] for row in con.execute(
        "SELECT drive_label FROM plan_drives WHERE plan_id=? ORDER BY drive_label", [plan_id]).fetchall()]
    return {label: _synth_one(con, label, _NOW) for label in labels}


def _preview(con, labels, *, observe=None, now=_NOW, fence=None):
    return {label: _synth_one(con, label, now) for label in labels}


def _execution(con, label, observation, *, now=_NOW):
    return _synth_one(con, label, now)


@contextmanager
def seam_patch():
    """Patch both admission entry points, and make ``inspect_drives`` synthesize when a caller passes no
    evidence (direct ``plan_capacity`` tests), to the pre-cutover snapshot semantics."""
    real_inspect = capacity.inspect_drives

    def inspect(con, plan_id, *, evidence_by_drive=None):
        if not evidence_by_drive:
            evidence_by_drive = evidence_for_plan(con, plan_id)
        return real_inspect(con, plan_id, evidence_by_drive=evidence_by_drive)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(admission, "preview_by_drive", _preview))
        stack.enter_context(mock.patch.object(admission, "execution_evidence", _execution))
        stack.enter_context(mock.patch.object(capacity, "inspect_drives", inspect))
        yield
