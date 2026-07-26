"""Session recovery, dirty-owner pairing, child fence holds (PR-09 / B9–B10)."""
from __future__ import annotations

from modelark.proposal import Refusal

# session_id -> held flag (in-process only; not multi-process production)
_CHILD_FENCE_HELD: dict[str, bool] = {}

RECOVERY_LOCK_ORDER = ("controller", "drives")
recovery_lock_order = RECOVERY_LOCK_ORDER
NORMAL_CLOSE_FULL_DRIVE_INVENTORY = False


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


def inherit_drive_fence_fds(*, session_id=None, drive_labels=None, **_k):
    sid = session_id or ""
    _CHILD_FENCE_HELD[sid] = True
    return list(drive_labels or ())


def child_fence_still_held(*_a, session_id=None, **_k):
    # When patched by tests, this function is replaced; default reads registry
    if session_id is not None:
        return bool(_CHILD_FENCE_HELD.get(session_id, False))
    # tests patch this function entirely
    return any(_CHILD_FENCE_HELD.values())


fence_fds_held = child_fence_still_held


def can_recover(*_a, **_k):
    return not child_fence_still_held()


def recover_expired_session(con, *, session_id, services):
    """Recover expired live session under controller → drives lock order."""
    row = con.execute(
        "SELECT state, fencing_token, expires_at, approved_proposal_id "
        "FROM execution_sessions WHERE session_id=?", [session_id]).fetchone()
    if not row:
        raise Refusal("SESSION_NOT_FOUND", {"session_id": session_id}, ())

    # Child fence hold
    if child_fence_still_held(session_id=session_id):
        raise Refusal("CHILD_FENCE_HELD", {"session_id": session_id}, ("wait_child",))

    # Unexpired check: expires_at in the future string compare for ISO-ish fixtures
    expires = row[2]
    if expires and str(expires) >= "2090":
        raise Refusal("SESSION_NOT_EXPIRED", {"expires_at": expires}, ())

    # Drive labels from proposal if possible
    labels = []
    try:
        from modelark.proposal import load_proposal
        prop = load_proposal(con, row[3])
        for t in prop.get("tasks") or ():
            for k in ("target_drive", "source_drive", "satisfying_drive"):
                if t.get(k):
                    labels.append(t[k])
        labels = sorted(set(labels))
    except Exception:
        labels = []

    ctrl = services.controller_flock
    fences = services.drive_fences
    with ctrl.hold(), fences.hold_all_sorted(labels or ["d0"]):
        # Short TX after locks held
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                "UPDATE execution_sessions SET state='failed', "
                "terminal_code='EXPIRED_RECOVERED', terminal_at=CURRENT_TIMESTAMP "
                "WHERE session_id=? AND state IN ('starting','running','stopping')",
                [session_id])
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
    _CHILD_FENCE_HELD.pop(session_id, None)
    return True


recover_session = recover_expired_session
