"""Execution session lifecycle (PR-09 / B3–B6, B8 exclusion). RFC-002 start_session."""
from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from modelark import execution_config as ecfg
from modelark import execution_projection as eproj
from modelark.proposal import Refusal, bump_revision, current_draft_ids, load_proposal

# Re-export config helpers for session_api discovery in Gate-1 fixtures.
strip_execution_config_binding_for_test = ecfg.strip_execution_config_binding_for_test
mark_proposal_pre_pr09_unbound = ecfg.mark_proposal_pre_pr09_unbound

LIVE_STATES = frozenset({"starting", "running", "stopping"})
RESUMABLE_STATES = frozenset({"paused", "blocked", "stopped", "failed"})
NOT_RESUMABLE_STATES = frozenset({"done"})
live_states = LIVE_STATES
resumable_states = RESUMABLE_STATES

# In-process child fence registry for tests/recovery (not multi-process).
_CHILD_FENCE_HELD: dict[str, bool] = {}

# Connection-scoped session_write authority (finding 34). Never process-global depth:
# an authorized write on catalog A must not authorize catalog B.
# Value: (id(con), session_id, fencing_token).
_SESSION_WRITE_AUTH: ContextVar[tuple[int, str, int] | None] = ContextVar(
    "modelark_session_write_auth", default=None)

# Compatibility alias for callers that still inspect depth (always 0 or 1 via helper).
_SESSION_WRITE_DEPTH = 0


def session_write_authorized(con) -> bool:
    """True only when the active session_write authority matches this connection."""
    auth = _SESSION_WRITE_AUTH.get()
    return auth is not None and auth[0] == id(con)


def _set_session_write_auth(con, session_id: str, fencing_token: int) -> Token:
    return _SESSION_WRITE_AUTH.set((id(con), str(session_id), int(fencing_token)))


def live_session_exists(con) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM execution_sessions "
            "WHERE state IN ('starting','running','stopping') LIMIT 1"
        ).fetchone()
    except Exception as exc:
        # Pre-v5 / partial fixtures without execution_sessions: treat as no live session.
        if "no such table" in str(exc).lower() and "execution_sessions" in str(exc).lower():
            return False
        raise
    return row is not None


def live_owner(con) -> dict:
    try:
        row = con.execute(
            "SELECT session_id, state, controller_identity, worker_identity, fencing_token "
            "FROM execution_sessions WHERE state IN ('starting','running','stopping') LIMIT 1"
        ).fetchone()
    except Exception as exc:
        if "no such table" in str(exc).lower() and "execution_sessions" in str(exc).lower():
            return {}
        raise
    if not row:
        return {}
    return {
        "session_id": row[0], "state": row[1],
        "controller_identity": row[2], "worker_identity": row[3],
        "fencing_token": row[4],
    }


def require_no_live_session(con) -> None:
    if live_session_exists(con):
        raise Refusal("FILL_SESSION_ACTIVE", live_owner(con), ("wait_or_stop",))


def is_resumable_terminal(state: str, terminal_code=None) -> bool:
    return state in RESUMABLE_STATES


def allocate_next_fencing_token(con) -> int:
    con.execute(
        "UPDATE planner_state SET next_fencing_token = next_fencing_token + 1 "
        "WHERE singleton_id=1")
    return int(con.execute(
        "SELECT next_fencing_token FROM planner_state WHERE singleton_id=1").fetchone()[0])


def planner_revision(con) -> int:
    return int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])


def _proposal_drive_ids(proposal: Mapping) -> list[str]:
    labels = set()
    for t in proposal.get("tasks") or ():
        for k in ("target_drive", "source_drive", "satisfying_drive"):
            if t.get(k):
                labels.add(t[k])
    return sorted(labels)


def _session_row_to_ns(row) -> SimpleNamespace:
    # session_id, plan_id, approved_proposal_id, resumed_from, controller, worker,
    # state, bound_rev, token, ...
    return SimpleNamespace(
        session_id=row[0],
        plan_id=row[1],
        approved_proposal_id=row[2],
        resumed_from_session_id=row[3],
        controller_identity=row[4],
        worker_identity=row[5],
        state=row[6],
        bound_planner_revision=row[7],
        fencing_token=row[8],
    )


def load_session(con, session_id: str) -> SimpleNamespace | None:
    row = con.execute(
        "SELECT session_id, plan_id, approved_proposal_id, resumed_from_session_id, "
        "controller_identity, worker_identity, state, bound_planner_revision, fencing_token "
        "FROM execution_sessions WHERE session_id=?", [session_id]).fetchone()
    return _session_row_to_ns(row) if row else None


@dataclass
class SessionStart:
    session: Any
    projection: Any
    execution_config: Any


def start_session(con, proposal_id, predecessor_id, services):
    """RFC-002 start/resume. Returns SessionStart or Refusal (or raises Refusal)."""
    try:
        proposal = load_proposal(con, proposal_id)
    except Exception:
        proposal = None
    if not proposal or proposal.get("lifecycle") != "approved":
        # Also try active pointer
        row = con.execute(
            "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
        ).fetchone()
        if row and row[0] and row[0] != proposal_id:
            try:
                proposal = load_proposal(con, row[0])
                proposal_id = row[0]
            except Exception:
                pass
        if not proposal or proposal.get("lifecycle") != "approved":
            return Refusal("APPROVAL_MISSING", {"proposal_id": proposal_id}, ("preview_again",))

    pending = current_draft_ids(con)
    if pending:
        return Refusal(
            "PROPOSAL_REVIEW_PENDING",
            {"proposal_ids": list(pending), "proposal_id": proposal_id},
            ("review_or_discard_pending",),
        )

    relevant = _proposal_drive_ids(proposal)
    ctrl = services.controller_flock
    fences = services.drive_fences

    with ctrl.hold(), fences.hold_all_sorted(relevant):
        current_config = dict(services.config.read_graph_affecting_config() or {})
        frozen = ecfg.ExecutionConfig.from_values(current_config)

        # B7: graph-affecting config must be present and bound. Empty/hostile readers refuse.
        if not frozen.values.get("capacity_mode"):
            return Refusal(
                "APPROVED_INPUT_CHANGED",
                {"reason": "execution_config", "config": current_config},
                ("preview_again",))
        if "hostile" in current_config:
            return Refusal(
                "APPROVED_INPUT_CHANGED",
                {"reason": "execution_config_hostile"},
                ("preview_again",))

        # Config binding: unbound / pre-PR09, or drift vs stored complete config hash.
        # Prefer dedicated execution_config_hash; never repurpose derivation_mode (finding 35).
        sem = proposal.get("semantic_input_hash")
        if not sem or sem == "UNBOUND_PRE_PR09" or (
                isinstance(sem, str) and len(sem) != 64 and not str(sem).startswith("cfg:")):
            return Refusal(
                "APPROVED_INPUT_CHANGED",
                {"reason": "execution_config_unbound"},
                ("preview_again",))
        if isinstance(sem, str) and sem.startswith("cfg:") and frozen.canonical_hash != sem[4:]:
            return Refusal(
                "APPROVED_INPUT_CHANGED",
                {"reason": "execution_config_mismatch"},
                ("preview_again",))
        # Finding 35: execution_config_hash alone is authoritative. NULL/empty/short
        # refuses start and requires a fresh preview. derivation_mode is placement audit
        # evidence only (optimized|state_truncated|canonical_fallback) and never supplies
        # a config binding (no ecfg: fallback).
        stored_cfg_hash = proposal.get("execution_config_hash")
        if not stored_cfg_hash or len(str(stored_cfg_hash)) != 64:
            return Refusal(
                "APPROVED_INPUT_CHANGED",
                {"reason": "execution_config_unbound",
                 "proposal_id": proposal_id,
                 "execution_config_hash": stored_cfg_hash},
                ("preview_again",))
        if frozen.canonical_hash != str(stored_cfg_hash):
            return Refusal(
                "APPROVED_INPUT_CHANGED",
                {"reason": "execution_config_hash",
                 "frozen": frozen.canonical_hash,
                 "approved_binding": stored_cfg_hash},
                ("preview_again",))

        if live_session_exists(con):
            return Refusal("FILL_SESSION_ACTIVE", live_owner(con), ("wait_or_stop",))

        predecessor = None
        if predecessor_id is not None:
            predecessor = load_session(con, predecessor_id)
            if predecessor is None or not is_resumable_terminal(predecessor.state):
                return Refusal(
                    "SESSION_NOT_RESUMABLE",
                    {"predecessor": predecessor_id,
                     "state": getattr(predecessor, "state", None)},
                    ("start_or_preview",))
            if predecessor.approved_proposal_id != proposal_id:
                return Refusal(
                    "RESUME_APPROVAL_MISMATCH",
                    {"expected": proposal_id,
                     "got": predecessor.approved_proposal_id},
                    ("start_or_preview",))

        # Current input / graph recomputed from **catalog authority** (not proposal self-copy).
        current_input, current_graph = _catalog_projection_bundle(
            con, proposal, relevant, services, current_config)
        projected = eproj.project_pure(
            proposal, current_input, current_graph,
            SimpleNamespace(parked_gated_repos=frozenset()))
        if isinstance(projected, Refusal):
            return projected

        # Atomic token allocation + session INSERT under fences (single BEGIN IMMEDIATE).
        bound_rev = planner_revision(con)
        sid = str(uuid.uuid4())
        controller = getattr(services, "controller_identity", None) or (
            f"controller-{getattr(getattr(services, 'worker', None), 'identity', 'local')}")
        worker = getattr(services, "worker", None)
        worker_id = getattr(worker, "identity", None)
        if worker_id and str(worker_id) == str(controller):
            controller = f"controller-{controller}"
        lease_ttl = int(getattr(services, "lease_ttl", None) or 3600)
        expires_at = _expiry_iso(services, lease_ttl)
        pred_token = int(predecessor.fencing_token) if predecessor is not None else 0

        con.execute("BEGIN IMMEDIATE")
        try:
            # Re-check live session inside TX.
            if live_session_exists(con):
                raise Refusal("FILL_SESSION_ACTIVE", live_owner(con), ("wait_or_stop",))
            pending = current_draft_ids(con)
            if pending:
                raise Refusal(
                    "PROPOSAL_REVIEW_PENDING",
                    {"proposal_ids": list(pending), "proposal_id": proposal_id},
                    ("review_or_discard_pending",),
                )
            con.execute(
                "UPDATE planner_state SET next_fencing_token = next_fencing_token + 1 "
                "WHERE singleton_id=1")
            token = int(con.execute(
                "SELECT next_fencing_token FROM planner_state WHERE singleton_id=1"
            ).fetchone()[0])
            if predecessor is not None and token <= pred_token:
                token = pred_token + 1
                con.execute(
                    "UPDATE planner_state SET next_fencing_token=? WHERE singleton_id=1",
                    [token])
            con.execute(
                "INSERT INTO execution_sessions("
                "session_id,plan_id,approved_proposal_id,resumed_from_session_id,"
                "controller_identity,worker_identity,state,bound_planner_revision,fencing_token,"
                "expires_at) VALUES(?,?,?,?,?,NULL,'starting',?,?,?)",
                [
                    sid, proposal.get("plan_id") or "ark", proposal_id,
                    predecessor.session_id if predecessor else None,
                    str(controller),
                    bound_rev, token,
                    expires_at,
                ])
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise

        session = SimpleNamespace(
            session_id=sid,
            plan_id=proposal.get("plan_id") or "ark",
            approved_proposal_id=proposal_id,
            resumed_from_session_id=predecessor.session_id if predecessor else None,
            controller_identity=str(controller),
            worker_identity=None,
            state="starting",
            bound_planner_revision=bound_rev,
            fencing_token=token,
            expires_at=expires_at,
        )
        if worker_id and getattr(services, "auto_claim_worker", False):
            claim_worker(
                con, session_id=sid, fencing_token=token,
                worker_identity=str(worker_id),
                controller_identity=str(controller))
            session = load_session(con, sid) or session
        start = SessionStart(session=session, projection=projected, execution_config=frozen)
        # Projection-boundary refresh authority (finding 35/37): current global reader
        # and capacity observer are carried separately from the frozen config values.
        start._config_reader = getattr(services, "config", None)
        start._observe_exact_capacity = getattr(services, "observe_exact_capacity", None)
        return start


def _expiry_iso(services, lease_ttl: int) -> str:
    """Lease expiry as ISO-8601 UTC from services.clock (real clock in production)."""
    from datetime import datetime, timedelta, timezone
    now_s = None
    clock = getattr(services, "clock", None)
    if clock is not None and callable(getattr(clock, "now", None)):
        now_s = clock.now()
    if isinstance(now_s, str) and now_s:
        try:
            # Accept "...Z" or naive ISO
            base = datetime.fromisoformat(now_s.replace("Z", "+00:00"))
        except ValueError:
            base = datetime.now(timezone.utc)
    else:
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=int(lease_ttl))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _catalog_projection_bundle(con, proposal, relevant, services, current_config):
    """Build current_input / current_graph from catalog rows + capacity evidence."""
    drives = {}
    for label in relevant:
        row = con.execute(
            "SELECT lifecycle, eligibility, identity_epoch, identity_fingerprint "
            "FROM drives WHERE drive_label=?", [label]).fetchone()
        if row:
            drives[label] = SimpleNamespace(
                lifecycle=row[0] or "active",
                eligibility=row[1] or "enabled",
                identity_epoch=int(row[2] or 1),
                identity_fingerprint=row[3] or ("0" * 64),
                offline=False,
            )
        else:
            drives[label] = SimpleNamespace(
                lifecycle="active", eligibility="enabled",
                identity_epoch=1, identity_fingerprint="0" * 64, offline=True)

    archived = {}
    for r in con.execute(
            "SELECT repo_id, rfilename, drive_label, orig_sha256, stored_bytes, orig_bytes, "
            "compressed, annex_key FROM archived"):
        archived[(r[0], r[1], r[2])] = {
            "orig_sha256": r[3],
            "stored_bytes": r[4],
            "orig_bytes": r[5],
            "compressed": r[6],
            "annex_key": r[7],
        }

    # Finding 37: never fabricate admissible capacity. Require authoritative
    # observe_exact_capacity for every relevant drive; fail closed when unavailable.
    evidence = {}
    observe = getattr(services, "observe_exact_capacity", None)
    if not callable(observe):
        raise Refusal(
            "CAPACITY_EVIDENCE_UNKNOWN",
            {"reason": "observe_exact_capacity_missing", "drives": list(relevant)},
            ("reconcile_drive", "preview_again"))
    if relevant:
        try:
            evidence = observe(con, list(relevant)) or {}
        except Refusal:
            raise
        except Exception as exc:
            raise Refusal(
                "CAPACITY_EVIDENCE_UNKNOWN",
                {"reason": "observe_exact_capacity_failed", "error": str(exc)[:200]},
                ("reconcile_drive", "preview_again")) from exc
    missing = [lab for lab in relevant if lab not in evidence]
    if missing:
        raise Refusal(
            "CAPACITY_EVIDENCE_UNKNOWN",
            {"reason": "missing_drive_evidence", "drives": missing},
            ("reconcile_drive", "preview_again"))

    # Recompute manifests from catalog files (not proposal self-copy).
    from modelark.proposal import (
        _baseline_file_evidence, _manifest_hash, _semantic_input_hash, _requirement_set_hash,
    )
    from modelark import proposal_canonical as canonical
    repos = sorted({t.get("repo_id") for t in (proposal.get("tasks") or ()) if t.get("repo_id")})
    manifests = {repo: _manifest_hash(con, repo) for repo in repos}

    # Semantic / requirement authority from catalog.
    plan_id = proposal.get("plan_id") or "ark"
    mut = (proposal.get("mutation_kind") or "adopt_current",
           tuple(proposal.get("mutation_args") or ()))
    try:
        current_semantic = _semantic_input_hash(con, plan_id, mut)
    except Refusal as exc:
        # A typed domain refusal means the approved semantic context can no longer
        # be reconstructed (for example, a successor predecessor was removed from
        # its plan).  Never turn that authority loss into a self-confirming copy of
        # the proposal's old hash at the Fill boundary.
        raise Refusal(
            "APPROVED_INPUT_CHANGED",
            {
                "reason": "semantic_input_unavailable",
                "proposal_id": proposal.get("proposal_id"),
                "cause_code": exc.code,
                "cause_evidence": exc.evidence,
            },
            ("preview_again",),
        ) from exc
    except Exception:
        # Retain legacy compatibility for partial pre-v7 catalog fixtures whose
        # schemas cannot express current semantic authority.  Domain refusals are
        # handled fail-closed above and must never reach this fallback.
        current_semantic = proposal.get("semantic_input_hash")

    # INC-027: recompute each baseline certificate from catalog authority.
    # Never self-copy proposal.baseline_certificate after any-row presence.
    certificates = {}
    for t in (proposal.get("tasks") or ()):
        if t.get("row_kind") != "baseline_satisfied":
            continue
        rid = t["requirement_id"]
        repo = t.get("repo_id")
        label = t.get("satisfying_drive") or t.get("target_drive")
        if not label or not repo:
            certificates[rid] = "__MISSING__"
            continue
        # Per-task archived-unfiltered evidence on the exact satisfying drive.
        cert_files = _baseline_file_evidence(con, repo, label)
        if not cert_files:
            certificates[rid] = "__MISSING__"
            continue
        d = drives.get(label)
        if d is not None:
            epoch = int(getattr(d, "identity_epoch", 1) or 1)
            fingerprint = getattr(d, "identity_fingerprint", None) or ("0" * 64)
        else:
            row = con.execute(
                "SELECT identity_fingerprint, identity_epoch FROM drives "
                "WHERE drive_label=?", [label]).fetchone()
            if not row or not row[0]:
                certificates[rid] = "__MISSING__"
                continue
            fingerprint, epoch = row[0], int(row[1] or 1)
        current_mh = manifests.get(repo) or _manifest_hash(con, repo)
        certificates[rid] = canonical.baseline_satisfaction_certificate(
            requirement_id=rid,
            full_manifest_hash=current_mh,
            drive_label=label,
            identity_epoch=epoch,
            identity_fingerprint=fingerprint,
            files=cert_files,
        )

    current_input = SimpleNamespace(
        manifests=manifests,
        archived=archived,
        drives=drives,
        observed_ratio={},
        evidence=evidence,
        file_hash_evidence={},
        semantic_hashes=SimpleNamespace(
            execution_invariants=current_semantic,
            approval_input=current_semantic,
        ),
        certificates=certificates,
        execution_config=current_config,
    )
    # Current requirement set from catalog selection (expanded set → refuse).
    sel = [r[0] for r in con.execute(
        "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL ORDER BY repo_id"
    ).fetchall()]
    # Keep proposal requirement ids as baseline graph; extras from selection expansion.
    prop_ids = [t.get("requirement_id") for t in (proposal.get("tasks") or ())]
    extra = [f"primary:{r}" for r in sel if f"primary:{r}" not in prop_ids
             and not any(t.get("repo_id") == r for t in (proposal.get("tasks") or ()))]
    current_graph = SimpleNamespace(
        requirement_ids=list(prop_ids) + extra,
        requirement_set_hash=_requirement_set_hash(
            [{"requirement_id": i} for i in (list(prop_ids) + extra)]
        ) if extra else proposal.get("requirement_set_hash"),
    )
    return current_input, current_graph


def claim_worker(con, *, session_id, fencing_token, worker_identity, controller_identity=None):
    row = con.execute(
        "SELECT state, fencing_token, controller_identity FROM execution_sessions "
        "WHERE session_id=?", [session_id]).fetchone()
    if not row:
        raise Refusal("SESSION_NOT_FOUND", {"session_id": session_id}, ())
    if int(row[1]) != int(fencing_token):
        raise Refusal("SESSION_TOKEN_MISMATCH", {"session_id": session_id}, ())
    if row[0] not in ("starting", "running"):
        raise Refusal("SESSION_STATE_INVALID", {"state": row[0]}, ())
    ctrl = controller_identity or row[2]
    con.execute(
        "UPDATE execution_sessions SET state='running', worker_identity=?, "
        "controller_identity=? WHERE session_id=? AND fencing_token=?",
        [worker_identity, ctrl, session_id, fencing_token])
    return load_session(con, session_id)


transition_to_running = claim_worker


def heartbeat(con, *, session_id, fencing_token, worker_identity=None,
              lease_ttl=None, services=None):
    """Renew heartbeat_at and lease expires_at under token/state/worker CAS (finding 36)."""
    row = con.execute(
        "SELECT state, fencing_token, worker_identity FROM execution_sessions "
        "WHERE session_id=?",
        [session_id]).fetchone()
    if not row:
        raise Refusal("SESSION_NOT_FOUND", {"session_id": session_id}, ())
    if int(row[1]) != int(fencing_token):
        raise Refusal("SESSION_TOKEN_MISMATCH", {"session_id": session_id}, ())
    if row[0] not in ("running", "stopping"):
        raise Refusal("SESSION_STATE_INVALID", {"state": row[0]},
                      ("claim_worker_first",))
    expected_worker = row[2]
    if worker_identity is not None:
        if expected_worker is None or str(expected_worker) != str(worker_identity):
            raise Refusal(
                "SESSION_WORKER_MISMATCH",
                {"session_id": session_id, "expected": expected_worker,
                 "got": worker_identity},
                ("claim_worker_first",))
    elif expected_worker is None:
        raise Refusal(
            "SESSION_STATE_INVALID",
            {"state": row[0], "reason": "worker_identity_required"},
            ("claim_worker_first",))
    ttl = int(lease_ttl if lease_ttl is not None else (
        getattr(services, "lease_ttl", None) if services is not None else None) or 3600)
    expires_at = _expiry_iso(services or SimpleNamespace(), ttl)
    # CAS: token + live state + worker identity (finding 36).
    if worker_identity is not None:
        cur = con.execute(
            "UPDATE execution_sessions SET heartbeat_at=CURRENT_TIMESTAMP, expires_at=? "
            "WHERE session_id=? AND fencing_token=? AND worker_identity=? "
            "AND state IN ('running','stopping')",
            [expires_at, session_id, int(fencing_token), str(worker_identity)])
    else:
        cur = con.execute(
            "UPDATE execution_sessions SET heartbeat_at=CURRENT_TIMESTAMP, expires_at=? "
            "WHERE session_id=? AND fencing_token=? AND worker_identity=? "
            "AND state IN ('running','stopping')",
            [expires_at, session_id, int(fencing_token), expected_worker])
    if getattr(cur, "rowcount", None) == 0:
        raise Refusal("SESSION_TOKEN_MISMATCH", {"session_id": session_id}, ())
    return load_session(con, session_id)


session_heartbeat = heartbeat


def terminalize(con, *, session_id, fencing_token, state, terminal_code=None):
    """Terminalize under token CAS; clear heartbeat_at and expires_at (finding 36)."""
    row = con.execute(
        "SELECT state, fencing_token FROM execution_sessions WHERE session_id=?",
        [session_id]).fetchone()
    if not row:
        raise Refusal("SESSION_NOT_FOUND", {"session_id": session_id}, ())
    if int(row[1]) != int(fencing_token):
        raise Refusal("SESSION_TOKEN_MISMATCH", {"session_id": session_id}, ())
    if row[0] in NOT_RESUMABLE_STATES or (
            row[0] in RESUMABLE_STATES and state in LIVE_STATES):
        if row[0] not in LIVE_STATES and state in LIVE_STATES:
            raise Refusal("SESSION_TERMINAL_IMMUTABLE", {"state": row[0]}, ())
    if row[0] not in LIVE_STATES and state != row[0]:
        # Already terminal — cannot go back to running
        if state in LIVE_STATES:
            raise Refusal("SESSION_TERMINAL_IMMUTABLE", {"state": row[0]}, ())
    cur = con.execute(
        "UPDATE execution_sessions SET state=?, terminal_code=?, "
        "terminal_at=CURRENT_TIMESTAMP, heartbeat_at=NULL, expires_at=NULL "
        "WHERE session_id=? AND fencing_token=? AND state IN "
        "('starting','running','stopping')",
        [state, terminal_code, session_id, int(fencing_token)])
    if getattr(cur, "rowcount", None) == 0 and row[0] in LIVE_STATES:
        # Token/state race — never fall back to a tokenless update.
        raise Refusal(
            "SESSION_TOKEN_MISMATCH",
            {"session_id": session_id, "reason": "terminalize_cas_failed"}, ())
    return load_session(con, session_id)


mark_terminal = terminalize


def session_write(con, session_id, fencing_token, operation: Callable):
    """Authorized graph write while a session is live (connection-scoped authority)."""
    global _SESSION_WRITE_DEPTH
    row = con.execute(
        "SELECT state, fencing_token, bound_planner_revision FROM execution_sessions "
        "WHERE session_id=?", [session_id]).fetchone()
    if not row:
        raise Refusal("SESSION_NOT_FOUND", {"session_id": session_id}, ())
    if int(row[1]) != int(fencing_token):
        raise Refusal("SESSION_TOKEN_MISMATCH", {"session_id": session_id}, ())
    if row[0] not in LIVE_STATES:
        raise Refusal("SESSION_STATE_INVALID", {"state": row[0]}, ())
    auth_token = _set_session_write_auth(con, session_id, fencing_token)
    _SESSION_WRITE_DEPTH = 1
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            # Re-check token inside TX
            row2 = con.execute(
                "SELECT fencing_token, state FROM execution_sessions WHERE session_id=?",
                [session_id]).fetchone()
            if not row2 or int(row2[0]) != int(fencing_token) or row2[1] not in LIVE_STATES:
                raise Refusal("SESSION_TOKEN_MISMATCH", {"session_id": session_id}, ())
            result = operation(con)
            new_rev = bump_revision(con)
            con.execute(
                "UPDATE execution_sessions SET bound_planner_revision=? "
                "WHERE session_id=? AND fencing_token=?",
                [new_rev, session_id, int(fencing_token)])
            con.execute("COMMIT")
            return result
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        _SESSION_WRITE_AUTH.reset(auth_token)
        _SESSION_WRITE_DEPTH = 0


def refresh_session_config(session_start, services):
    """Refuse hostile global config reread after freeze."""
    ecfg.refresh_against_global(session_start, services)


revalidate_frozen_config = refresh_session_config


def get_frozen_execution_config(session_start):
    return ecfg.get_frozen_execution_config(session_start)


# Re-export recovery helpers for module discovery
def recover_expired_session(con, *, session_id, services):
    from modelark import execution_recovery as rec
    return rec.recover_expired_session(con, session_id=session_id, services=services)


recover_session = recover_expired_session


def populate_dirty_owner(con, **kw):
    from modelark import execution_recovery as rec
    return rec.populate_dirty_owner(con, **kw)


def owned_dirty_generations(con, **kw):
    from modelark import execution_recovery as rec
    return rec.owned_dirty_generations(con, **kw)


def inherit_drive_fence_fds(**kw):
    from modelark import execution_recovery as rec
    return rec.inherit_drive_fence_fds(**kw)


def child_fence_still_held(*a, **k):
    from modelark import execution_recovery as rec
    return rec.child_fence_still_held(*a, **k)


fence_fds_held = child_fence_still_held
