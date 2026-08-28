"""First-class planning authority shared by every read-only planning surface (DEC-067).

Planning modes are policy values, not alternate planner implementations.  This module owns the
single impure snapshot boundary: it resolves the policy, captures identity-bound admission evidence,
reconciles requirements/candidates, and performs capacity placement while one catalog read
transaction is open.  CLI/Library, portal proposal preview, and future planning adapters consume the
result; none of them may independently choose targets or reinterpret ``drives.free_bytes``.

Approval is intentionally not implemented here.  It revalidates an already-reviewed exact assignment
under controller/drive fences with fresh evidence, without invoking the optimizer again.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from modelark import admission, archive_manifest, capacity, capacity_evidence, reconcile


@dataclass(frozen=True)
class PlanningPolicy:
    """Central policy values for one planning run; modes do not select alternate code paths."""

    capacity_mode: capacity.CapacityMode


@dataclass(frozen=True)
class DriveState:
    drive_label: str
    lifecycle: str
    eligibility: str


@dataclass(frozen=True)
class PlanningResult:
    """One immutable planning result consumed by all presentation/persistence adapters."""

    plan_id: str
    planner_revision: int
    policy: PlanningPolicy
    graph: reconcile.ReconcileResult
    capacity: capacity.CapacityPlan
    evidence_by_drive: tuple[tuple[str, capacity_evidence.Evidence], ...]
    drive_states: tuple[DriveState, ...]

    @property
    def feasible(self) -> bool:
        return self.capacity.feasible

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        graph_codes = {
            item.code
            for item in self.graph.diagnostics
            if item.severity in {
                reconcile.DiagnosticSeverity.BLOCKING,
                reconcile.DiagnosticSeverity.ERROR,
            }
        }
        capacity_codes = {item.code.value for item in self.capacity.failures}
        if self.capacity.gate_b_code != "FEASIBLE":
            capacity_codes.add(self.capacity.gate_b_code)
        return tuple(sorted(graph_codes | capacity_codes))

    @property
    def root_code(self) -> str:
        """Typed root cause, with structural/capacity admission taking precedence."""
        # Acquisition policy is selection-scoped and must remain the root refusal even when the
        # remaining placeable subset also lacks drive evidence.  Otherwise a blocked repository is
        # hidden behind an unrelated fleet diagnostic.
        if "MANIFEST_POLICY" in self.blocking_codes:
            return "MANIFEST_POLICY"
        if self.capacity.gate_b_code != "FEASIBLE":
            return self.capacity.gate_b_code
        return self.blocking_codes[0] if self.blocking_codes else "FEASIBLE"

    @property
    def proposal_gate_code(self) -> str:
        """Stable proposal wire code; detailed policy cause remains in ``gate_b_refusal``."""
        return "INFEASIBLE" if self.root_code == "MANIFEST_POLICY" else self.root_code


@contextmanager
def _consistent_read(con):
    """Capture policy, graph, evidence, lifecycle, and placement from one catalog snapshot."""
    if con.in_transaction:
        yield
        return
    con.execute("BEGIN")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def resolve_policy(
    con,
    plan_id: str,
    *,
    capacity_mode: str | capacity.CapacityMode | None = None,
) -> PlanningPolicy:
    """Resolve centrally managed mode values; every mode uses the same planner implementation."""
    value = capacity_mode
    if value is None:
        row = con.execute(
            "SELECT capacity_mode FROM plans WHERE plan_id=?", [plan_id]
        ).fetchone()
        value = row[0] if row and row[0] else capacity.CapacityMode.GUARANTEED
    return PlanningPolicy(capacity_mode=capacity.mode_from_value(value))


def preview(
    con,
    plan_id: str,
    repo_ids: Sequence[str] | None = None,
    *,
    capacity_mode: str | capacity.CapacityMode | None = None,
    archive_policy: archive_manifest.ArchivePolicy | None = None,
    compression_cfg: Mapping[str, object] | None = None,
    observe: Callable[[str], object | None] | None = None,
    now: str | None = None,
) -> PlanningResult:
    """Build the canonical read-only plan from one consistent catalog/evidence snapshot."""
    if observe is None:
        # Lazy import keeps the evidence seam neutral and makes observation injectable in tests.
        from modelark import fetch

        observe = lambda label: fetch.observe_for_admission(con, label)
    observed_at = now or datetime.now(timezone.utc).isoformat(sep=" ")

    with _consistent_read(con):
        revision_row = con.execute(
            "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("planner_state singleton missing")
        planner_revision = int(revision_row[0])
        policy = resolve_policy(con, plan_id, capacity_mode=capacity_mode)
        rows = con.execute(
            "SELECT d.drive_label,d.lifecycle,d.eligibility "
            "FROM plan_drives pd JOIN drives d USING(drive_label) "
            "WHERE pd.plan_id=? ORDER BY d.drive_label",
            [plan_id],
        ).fetchall()
        states = tuple(DriveState(str(row[0]), str(row[1]), str(row[2])) for row in rows)
        labels = tuple(item.drive_label for item in states)
        evidence = admission.preview_by_drive(
            con, labels, observe=observe, now=observed_at
        )
        graph = reconcile.reconcile_plan(
            con,
            plan_id,
            repo_ids,
            archive_policy,
            compression_cfg=compression_cfg,
        )
        ledger = capacity.plan_capacity(
            con,
            graph,
            capacity_mode=policy.capacity_mode,
            evidence_by_drive=evidence,
        )

    return PlanningResult(
        plan_id=plan_id,
        planner_revision=planner_revision,
        policy=policy,
        graph=graph,
        capacity=ledger,
        evidence_by_drive=tuple(sorted(evidence.items())),
        drive_states=states,
    )
