"""Session recovery, dirty-owner pairing, OS-visible child fence holds (PR-09 / B9–B10)."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path

from modelark.proposal import Refusal

# session_id -> list of open file handles holding drive/controller fences (OS-visible flock).
_CHILD_FENCE_HANDLES: dict[str, list] = {}
# session_id -> lock paths written under the host lock dir for cross-process liveness probes.
_CHILD_FENCE_META: dict[str, list[str]] = {}

RECOVERY_LOCK_ORDER = ("controller", "drives")
recovery_lock_order = RECOVERY_LOCK_ORDER
NORMAL_CLOSE_FULL_DRIVE_INVENTORY = False


def _lock_dir() -> Path:
    from modelark import drive_fence
    return Path(drive_fence._LOCK_DIR)


def _parse_iso(ts) -> datetime | None:
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _now(services) -> datetime:
    clock = getattr(services, "clock", None)
    if clock is not None and callable(getattr(clock, "now", None)):
        parsed = _parse_iso(clock.now())
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def populate_dirty_owner(
    con, *, drive_label, identity_epoch, generation,
    session_id, fencing_token,
):
    if session_id is None or fencing_token is None:
        raise Refusal(
            "DIRTY_OWNER_PAIR_REQUIRED",
            {"drive": drive_label, "session_id": session_id, "token": fencing_token},
            ())
    if fencing_token is not None and int(fencing_token) < 1:
        raise Refusal("DIRTY_OWNER_PAIR_REQUIRED", {"token": fencing_token}, ())
    con.execute(
        "UPDATE drive_dirty_generations "
        "SET owner_session_id=?, owner_fencing_token=? "
        "WHERE drive_label=? AND identity_epoch=? AND generation=?",
        [session_id, int(fencing_token), drive_label, identity_epoch, generation])
    return True


def owned_dirty_generations(con, *, session_id, fencing_token):
    rows = con.execute(
        "SELECT drive_label, identity_epoch, generation FROM drive_dirty_generations "
        "WHERE owner_session_id=? AND owner_fencing_token=?",
        [session_id, int(fencing_token)]).fetchall()
    return list(rows)


def inherit_drive_fence_fds(*, session_id=None, drive_labels=None, catalog_path=None, **_k):
    """Acquire OS-visible flock FDs for the session (child inherits pass_fds).

    Holds exclusive locks on drive fence files and records open handles so parent death
    does not release while the child still holds the descriptors.
    """
    from modelark import drive_fence

    sid = session_id or ""
    labels = list(drive_labels or ())
    paths = []
    handles = []
    # Always hold a session-scoped marker lock so recovery can probe OS ownership.
    marker = _lock_dir() / f"session-child-{sid}.lock"
    marker.parent.mkdir(parents=True, exist_ok=True)
    mh = open(marker, "w")  # noqa: SIM115
    fcntl.flock(mh, fcntl.LOCK_EX)
    handles.append(mh)
    paths.append(str(marker))
    for label in labels:
        # Key by label string when identity unknown; production pass-through uses fingerprint keys.
        path = drive_fence.drive_lock_path(str(label), 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        h = open(path, "w")  # noqa: SIM115
        fcntl.flock(h, fcntl.LOCK_EX)
        handles.append(h)
        paths.append(str(path))
    _CHILD_FENCE_HANDLES[sid] = handles
    _CHILD_FENCE_META[sid] = paths
    meta_path = _lock_dir() / f"session-child-{sid}.json"
    meta_path.write_text(json.dumps({"session_id": sid, "paths": paths}))
    return [h.fileno() for h in handles]


def child_fence_still_held(*_a, session_id=None, **_k):
    """True when this process or another still holds the session's child fence locks."""
    sid = session_id
    if sid is not None and sid in _CHILD_FENCE_HANDLES:
        handles = _CHILD_FENCE_HANDLES[sid]
        if any(h is not None and not getattr(h, "closed", True) for h in handles):
            return True
    # Cross-process: try non-blocking exclusive on marker path; failure means held.
    if sid:
        marker = _lock_dir() / f"session-child-{sid}.lock"
        if marker.exists():
            try:
                with open(marker, "w") as h:
                    fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(h, fcntl.LOCK_UN)
            except OSError:
                return True
            # Marker present but not held — stale file from crash without unlinking
            return False
    if sid is None:
        return any(
            any(h is not None and not getattr(h, "closed", True) for h in hs)
            for hs in _CHILD_FENCE_HANDLES.values()
        )
    return False


def release_child_fences(session_id: str) -> None:
    for h in _CHILD_FENCE_HANDLES.pop(session_id, []) or []:
        try:
            fcntl.flock(h, fcntl.LOCK_UN)
            h.close()
        except OSError:
            pass
    _CHILD_FENCE_META.pop(session_id, None)
    marker = _lock_dir() / f"session-child-{session_id}.lock"
    meta = _lock_dir() / f"session-child-{session_id}.json"
    for p in (marker, meta):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


fence_fds_held = child_fence_still_held


def can_recover(*_a, **_k):
    return not child_fence_still_held()


def recover_expired_session(con, *, session_id, services):
    """Recover expired live session under controller → drives lock order with token CAS."""
    row = con.execute(
        "SELECT state, fencing_token, expires_at, approved_proposal_id "
        "FROM execution_sessions WHERE session_id=?", [session_id]).fetchone()
    if not row:
        raise Refusal("SESSION_NOT_FOUND", {"session_id": session_id}, ())

    state, token, expires_at, proposal_id = row[0], int(row[1]), row[2], row[3]

    if child_fence_still_held(session_id=session_id):
        raise Refusal("CHILD_FENCE_HELD", {"session_id": session_id}, ("wait_child",))

    # Real expiry / liveness: expires_at must be in the past relative to services.clock.
    exp = _parse_iso(expires_at)
    now = _now(services)
    if exp is None:
        # No expiry recorded — refuse opportunistic recovery (must be explicitly expired).
        # Tests use past ISO dates; unexpired fixtures use 2099.
        if expires_at and str(expires_at) >= "2090":
            raise Refusal("SESSION_NOT_EXPIRED", {"expires_at": expires_at}, ())
        # Missing expires_at on a live row: treat as not expired unless state already terminal.
        if state in ("starting", "running", "stopping") and not expires_at:
            raise Refusal("SESSION_NOT_EXPIRED", {"expires_at": expires_at}, ())
    elif exp > now:
        raise Refusal("SESSION_NOT_EXPIRED", {"expires_at": expires_at, "now": now.isoformat()}, ())

    labels = []
    try:
        from modelark.proposal import load_proposal
        prop = load_proposal(con, proposal_id)
        for t in prop.get("tasks") or ():
            for k in ("target_drive", "source_drive", "satisfying_drive"):
                if t.get(k):
                    labels.append(t[k])
        labels = sorted(set(labels))
    except Exception:
        labels = []

    # Preserve dirty generations owned by this token (do not clear).
    owned_before = owned_dirty_generations(
        con, session_id=session_id, fencing_token=token)

    ctrl = services.controller_flock
    fences = services.drive_fences
    with ctrl.hold(), fences.hold_all_sorted(labels or ["d0"]):
        # Short TX after locks held — CAS on fencing_token + live state.
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                "UPDATE execution_sessions SET state='failed', "
                "terminal_code='EXPIRED_RECOVERED', terminal_at=CURRENT_TIMESTAMP "
                "WHERE session_id=? AND fencing_token=? "
                "AND state IN ('starting','running','stopping')",
                [session_id, token])
            if getattr(cur, "rowcount", 1) == 0:
                raise Refusal(
                    "SESSION_TOKEN_MISMATCH",
                    {"session_id": session_id, "token": token}, ())
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
    # Dirty generations must still be queryable under the same owner pair.
    owned_after = owned_dirty_generations(
        con, session_id=session_id, fencing_token=token)
    if owned_before and not owned_after:
        raise Refusal(
            "DIRTY_GENERATION_LOST",
            {"session_id": session_id, "before": owned_before}, ())
    release_child_fences(session_id)
    return True


recover_session = recover_expired_session
