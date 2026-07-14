"""Library API — what's archived and where (DEF-006).

Reads the durable `archived` + `drives` records (the SQLite offline mirror, DEC-024), so it
works even when drives are unplugged — no git-annex shelling. `archived` holds one
row per (repo, file, drive), so copy counts fall out of counting distinct drives.
"""
from __future__ import annotations

from modelark.web import data

# One row per (repo, file), collapsing copies across drives. orig/stored bytes are identical across a
# file's copies, so max() picks the value exactly (SQLite has no any_value); n_copies = distinct drives.
_FILES_ONCE = """
    SELECT repo_id, rfilename,
           max(orig_bytes)   AS orig_bytes,
           max(stored_bytes) AS stored_bytes,
           max(compressed)   AS compressed,
           count(DISTINCT drive_label) AS n_copies
    FROM archived GROUP BY repo_id, rfilename
"""


def library() -> dict:
    fleet = [dict(zip(
        ["label", "model", "serial", "health", "capacity", "free",
         "location", "last_seen", "n_models", "n_files", "on_disk", "raw"], r))
        for r in data.q(
            "SELECT d.drive_label, d.hw_model, d.serial, d.health, d.capacity_bytes, "
            "d.free_bytes, d.physical_location, d.last_seen, "
            "count(DISTINCT a.repo_id), count(a.rfilename), "
            "coalesce(sum(a.stored_bytes), 0), coalesce(sum(a.orig_bytes), 0) "
            "FROM drives d LEFT JOIN archived a ON a.drive_label = d.drive_label "
            "GROUP BY d.drive_label ORDER BY d.drive_label")]

    drives_by_repo = {r[0]: (r[1].split(",") if r[1] else []) for r in data.q(
        "SELECT repo_id, group_concat(DISTINCT drive_label) FROM archived GROUP BY repo_id")}
    verified_by_repo = dict(data.q(
        "SELECT repo_id, max(verified_at) FROM archived GROUP BY repo_id"))
    models = [{
        "repo_id": r[0], "category": r[1], "params_b": r[2], "n_files": r[3],
        "n_compressed": r[4], "raw": r[5], "on_disk": r[6], "min_copies": r[7],
        "drives": drives_by_repo.get(r[0], []), "verified_at": verified_by_repo.get(r[0]),
    } for r in data.q(
        f"WITH fo AS ({_FILES_ONCE}) "
        "SELECT fo.repo_id, m.category, m.params_b, count(*), "
        "count(*) FILTER (WHERE fo.compressed), "
        "coalesce(sum(fo.orig_bytes), 0), coalesce(sum(fo.stored_bytes), 0), min(fo.n_copies) "
        "FROM fo LEFT JOIN models m ON m.repo_id = fo.repo_id "
        "GROUP BY fo.repo_id, m.category, m.params_b ORDER BY 7 DESC")]

    lg = data.q(f"WITH fo AS ({_FILES_ONCE}) "
                "SELECT count(DISTINCT repo_id), count(*), coalesce(sum(orig_bytes), 0), "
                "coalesce(sum(stored_bytes), 0) FROM fo")[0]
    phys = data.q("SELECT coalesce(sum(stored_bytes), 0) FROM archived")[0][0]
    cap = data.q("SELECT count(*), coalesce(sum(capacity_bytes), 0), "
                 "coalesce(sum(free_bytes), 0) FROM drives")[0]
    return {
        "fleet": fleet,
        "models": models,
        "totals": {"n_models": lg[0], "n_files": lg[1], "raw": lg[2], "on_disk": lg[3],
                   "physical": phys, "n_drives": cap[0], "capacity": cap[1], "free": cap[2]},
    }


def _active_plan() -> tuple[str, str]:
    """(plan_id, capacity_mode) of the active Plan — bootstraps `ark` if none yet. Callers already
    hold data._lock; conn()/plan.* don't re-acquire it, so this is safe under the outer lock."""
    from modelark import plan as _plan
    con = data.conn()
    p = _plan.active(con) or _plan.bootstrap(con)
    return p["plan_id"], p["capacity_mode"]


def plan() -> dict:
    """The librarian's placement plan as UI JSON (drive tiers, fill %, models-by-category, copy#1→#2
    links) for the Fill tab, scoped to the active Plan's drive set + capacity mode (#33).
    `plan_view` is read-only, so it runs on the portal's own connection — same backend as `library
    plan --json`."""
    from modelark import librarian
    with data._lock:
        pid, capacity_mode = _active_plan()
        return librarian.plan_view(data.conn(), plan_id=pid, capacity_mode=capacity_mode)


def queue() -> dict:
    """One-row-per-model Fill queue (whole finalized selection, done included) — read-only, portal
    connection, scoped to the active Plan (#33). Heavy (~one plan pass); the Fill tab fetches it once
    on open. See librarian.queue_view."""
    from modelark import librarian
    with data._lock:
        pid, capacity_mode = _active_plan()
        return librarian.queue_view(data.conn(), plan_id=pid, capacity_mode=capacity_mode)


def queue_state() -> dict:
    """Live {repo: copies_placed} for the Fill queue — cheap, polled at model boundaries so rows flip
    upcoming -> partial -> done without a re-plan."""
    from modelark import librarian
    with data._lock:
        return librarian.queue_state(data.conn())
