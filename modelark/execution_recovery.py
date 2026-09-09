"""Session recovery, dirty-owner pairing, OS-visible child fence holds (PR-09 / B9–B10)."""
from __future__ import annotations

import fcntl
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from modelark import execution_authority as authority
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


def inherit_drive_fence_fds(
    *, session_id=None, drive_labels=None, catalog_path=None, con=None,
    marker_only=False, **_k
):
    """Acquire OS-visible flock FDs for the session (child inherits pass_fds).

    Every compatible physical key plus the stable session marker is retained.
    Nonempty drive selections require proven catalog facts, never bare labels.
    Catalog-free marker fixtures must explicitly opt into ``marker_only=True``;
    this seam cannot be used for a nonempty drive selection.
    """
    from modelark import drive_fence
    from modelark.drive_identity import compatible_keys
    from modelark.execution_service import _capture_fence_identities

    sid = session_id or ""
    labels = list(drive_labels or ())
    if (not labels and con is None and marker_only is not True) or (marker_only and labels):
        raise Refusal("DRIVE_IDENTITY_UNPROVEN", {
            "session_id": sid, "reason": "explicit_empty_marker_authority_required"}, ())
    captured = _capture_fence_identities(con, labels)
    keys = compatible_keys(identity for _, identity in captured)
    paths = []
    handles = []
    marker = _lock_dir() / f"session-child-{sid}.lock"
    marker.parent.mkdir(parents=True, exist_ok=True)
    meta_path = _lock_dir() / f"session-child-{sid}.json"
    # Never release an existing hold to reacquire: children may share its open
    # file descriptions. Also never unlink the marker and split its namespace.
    if sid in _CHILD_FENCE_HANDLES:
        raise Refusal("CHILD_FENCE_HELD", {"session_id": sid}, ())
    marker_held = False
    try:
        for path in [marker, *(drive_fence.drive_lock_path(*key) for key in keys)]:
            path.parent.mkdir(parents=True, exist_ok=True)
            h = open(path, "w")  # noqa: SIM115
            # Register before flock so the just-opened descriptor is closed on failure.
            handles.append(h)
            try:
                fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Refusal("CHILD_FENCE_HELD", {
                    "session_id": sid, "path": str(path)}, ()) from exc
            if path == marker:
                marker_held = True
            paths.append(str(path))
        if _capture_fence_identities(con, labels) != captured:
            raise Refusal("DRIVE_IDENTITY_CHANGED", {"drives": labels}, ())
        meta_path.write_text(json.dumps({
            "session_id": sid, "paths": paths, "labels": labels,
        }))
    except BaseException:
        # Clean our metadata only while owning the marker. A failed marker
        # acquisition must not erase another process's diagnostic metadata.
        if marker_held:
            try:
                meta_path.unlink(missing_ok=True)
            except OSError:
                pass
        for handle in reversed(handles):
            try:
                handle.close()
            except OSError:
                pass
        raise
    _CHILD_FENCE_HANDLES[sid] = handles
    _CHILD_FENCE_META[sid] = paths
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
    # Close, do not LOCK_UN: inherited descriptors share the flock description,
    # and a still-running child must retain exclusion after this parent releases.
    handles = _CHILD_FENCE_HANDLES.pop(session_id, [])
    _CHILD_FENCE_META.pop(session_id, None)
    if handles:
        meta = _lock_dir() / f"session-child-{session_id}.json"
        try:
            meta.unlink(missing_ok=True)
        except OSError:
            pass
    for h in handles:
        try:
            h.close()
        except OSError:
            pass


fence_fds_held = child_fence_still_held


def can_recover(*_a, **_k):
    return not child_fence_still_held()


def recover_expired_session(con, *, session_id, services):
    """Recover expired live session under controller → drives lock order with token CAS.

    Expiry is re-validated **inside** the fenced IMMEDIATE transaction so a concurrent
    lease renewal (new expires_at with same token) cannot be terminalized.
    """
    row = con.execute(
        "SELECT state, fencing_token, expires_at, approved_proposal_id "
        "FROM execution_sessions WHERE session_id=?", [session_id]).fetchone()
    if not row:
        raise Refusal("SESSION_NOT_FOUND", {"session_id": session_id}, ())

    state, token, expires_at, proposal_id = row[0], int(row[1]), row[2], row[3]

    if child_fence_still_held(session_id=session_id):
        raise Refusal("CHILD_FENCE_HELD", {"session_id": session_id}, ("wait_child",))

    # Pre-check expiry before waiting on locks (fail fast); re-check under locks.
    def _expired(exp_raw, now) -> bool:
        exp = _parse_iso(exp_raw)
        if exp is not None:
            return exp <= now
        if exp_raw and str(exp_raw) >= "2090":
            return False
        if state in ("starting", "running", "stopping") and not exp_raw:
            return False
        # Past ISO strings without TZ parse as naive — treat non-future strings as expired
        return bool(exp_raw and str(exp_raw) < "2090")

    now = _now(services)
    if not _expired(expires_at, now):
        raise Refusal("SESSION_NOT_EXPIRED", {"expires_at": expires_at, "now": now.isoformat()}, ())

    def _recovery_labels():
        from modelark.proposal import load_proposal

        # A missing/corrupt proposal is an authority failure, not permission to
        # substitute a synthetic drive. Valid empty proposals remain empty.
        try:
            prop = load_proposal(con, proposal_id)
        except (KeyError, ValueError, TypeError, sqlite3.DatabaseError) as exc:
            raise Refusal("SESSION_AUTHORITY_UNPROVEN", {
                "session_id": session_id, "proposal_id": proposal_id}, ()) from exc
        labels = set()
        for t in prop.get("tasks") or ():
            for k in ("target_drive", "source_drive", "satisfying_drive"):
                if t.get(k):
                    labels.add(t[k])
        owned = owned_dirty_generations(
            con, session_id=session_id, fencing_token=token)
        labels.update(row[0] for row in owned)
        return sorted(labels), sorted(owned)

    ctrl = services.controller_flock
    fences = services.drive_fences

    def _recover_fenced(labels, owned_before, fence_binding):
        if child_fence_still_held(session_id=session_id):
            raise Refusal("CHILD_FENCE_HELD", {"session_id": session_id}, ("wait_child",))
        con.execute("BEGIN IMMEDIATE")
        try:
            validate_fences = getattr(fence_binding, "validate_current", None)
            if callable(validate_fences):
                validate_fences(con)
            if _recovery_labels() != (labels, owned_before):
                raise Refusal("SESSION_AUTHORITY_CHANGED", {"session_id": session_id}, ())
            # Re-read under locks; CAS on token + live state + still-expired lease.
            row2 = con.execute(
                "SELECT state, fencing_token, expires_at, approved_proposal_id "
                "FROM execution_sessions "
                "WHERE session_id=?", [session_id]).fetchone()
            if not row2:
                raise Refusal("SESSION_NOT_FOUND", {"session_id": session_id}, ())
            if row2[3] != proposal_id:
                raise Refusal("SESSION_AUTHORITY_CHANGED", {"session_id": session_id}, ())
            expected = authority.Attempt(session_id, token)
            actual = authority.Attempt(session_id, int(row2[1]))
            try:
                # Preserve refusal precedence: changed identity precedes expiry, and
                # the workflow-state check follows expiry revalidation below.
                authority.require_current(expected, actual, row2[0], (row2[0],))
            except authority.AuthorityLost as exc:
                raise Refusal(
                    "SESSION_TOKEN_MISMATCH",
                    {"session_id": session_id, "token": token}, ()) from exc
            now2 = _now(services)
            if not _expired(row2[2], now2):
                raise Refusal(
                    "SESSION_NOT_EXPIRED",
                    {"expires_at": row2[2], "now": now2.isoformat()}, ())
            try:
                authority.require_current(expected, actual, row2[0],
                                          ("starting", "running", "stopping"))
            except authority.AuthorityLost as exc:
                raise Refusal(
                    "SESSION_STATE_INVALID", {"state": row2[0]}, ()) from exc
            # The clock is an injected callback inside this transaction. Keep
            # the physical-fact CAS adjacent to the write after every callback.
            if callable(validate_fences):
                validate_fences(con)
            # Expiry re-validated above; CAS on token + live state + still-expired expires_at.
            exp_bound = row2[2]
            if exp_bound is None:
                cur = con.execute(
                    "UPDATE execution_sessions SET state='failed', "
                    "terminal_code='EXPIRED_RECOVERED', terminal_at=CURRENT_TIMESTAMP "
                    "WHERE session_id=? AND fencing_token=? "
                    "AND state IN ('starting','running','stopping') AND expires_at IS NULL",
                    [session_id, token])
            else:
                cur = con.execute(
                    "UPDATE execution_sessions SET state='failed', "
                    "terminal_code='EXPIRED_RECOVERED', terminal_at=CURRENT_TIMESTAMP "
                    "WHERE session_id=? AND fencing_token=? "
                    "AND state IN ('starting','running','stopping') AND expires_at=?",
                    [session_id, token, exp_bound])
            if getattr(cur, "rowcount", 1) == 0:
                raise Refusal(
                    "SESSION_TOKEN_MISMATCH",
                    {"session_id": session_id, "token": token, "reason": "cas_miss"}, ())
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        release_child_fences(session_id)
        return True

    with ctrl.hold():
        labels, owned_before = _recovery_labels()
        with fences.hold_all_sorted(labels) as fence_binding:
            return _recover_fenced(labels, owned_before, fence_binding)


recover_session = recover_expired_session
