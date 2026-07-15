"""The librarian (DEC-014 → DEC-017) — plan where each model's bytes live.

Tiers, split by importance (operator-set via `protect` + drive `role`/`raid_backed`):
  • RAID tier (raid_backed): the redundant safe home — must-have COPY #1 lands here, pulled OUT
    of the size sort (an 8 TB local can exceed a 5 TB RAID). Absent a RAID (public fleets),
    copy #1 falls back to the largest primary disk.
  • PRIMARY / bulk (role=primary, non-RAID): the re-fetchable working set (numcopies=1),
    consolidated onto the FEWEST drives (variable-sized bin packing, largest-first), rest freed.
  • REPLICA tier (role=replica): must-have COPY #2+ — the smallest-sufficient INDEPENDENT
    drive (whole set on one, else span; each model kept whole).

The plan emits coverage advisories. Planning + fetch open the catalog, so run with the portal
stopped.
"""
from __future__ import annotations

import shutil

from modelark import capacity as capacity_model
from modelark import fetch, reconcile, register  # noqa: F401

_ZIPNN_FLOAT_RATIO = capacity_model.DEFAULT_FLOAT_RATIO
_SIZE_MARGIN = capacity_model.EXPECTED_MARGIN
_RATIO_MIN_SAMPLE = capacity_model.RATIO_MIN_SAMPLE


def headroom_bytes(capacity: int) -> int:
    return capacity_model.headroom_bytes(capacity)


def observed_float_ratio(con) -> float | None:
    """stored/orig ACTUALLY achieved on float (compressible) weights so far — blends real ZipNN
    compression AND the raw-fallbacks (INC-005 crash/hang shards stored uncompressed). None until
    _RATIO_MIN_SAMPLE bytes of evidence. This is the fix for the plan creeping onto more drives each
    restart: the estimate must track reality (raw-fallbacks push actual > a fixed 0.67), not guess."""
    return capacity_model.observed_float_ratio(con)


def plan_float_ratio(con) -> float:
    """The float ratio est_stored_bytes should use: our observed average once there's enough evidence,
    but NEVER more optimistic than the _ZIPNN_FLOAT_RATIO baseline (a conservative floor)."""
    return capacity_model.plan_float_ratio(con)


def est_stored_bytes(con, repo_id: str, float_ratio: float | None = None,
                     capacity_mode: str = "guaranteed") -> int:
    """Estimated footprint of one copy. Capacity-mode aware (DEC-045):
      • 'guaranteed' (legacy: 'uncompressed') → the exact raw-bounded footprint — the
        over-provision basis; the fill reserves full space so it can never run out, and compression
        just leaves the reservation with room to spare.
      • 'compression_aware' (legacy: 'compressed') → the ZipNN estimate plus margin; packs
        each drive to its predicted TRUE capacity, with the per-model failsafe carrying the risk.
    `float_ratio` (from plan_float_ratio, computed ONCE per plan) tracks the fill's real average so
    drives don't over-pack; omitted → computed here (a query per call, fine for one-offs)."""
    files = fetch.plan(con, repo_id)
    comp = sum(f["size"] or 0 for f in files if f["mode"] == "compress")
    raw = sum(f["size"] or 0 for f in files if f["mode"] == "raw")
    mode = capacity_model.mode_from_value(capacity_mode)
    if mode == capacity_model.CapacityMode.GUARANTEED:
        return comp + raw
    ratio = float_ratio if float_ratio is not None else plan_float_ratio(con)
    return int((comp * ratio + raw) * _SIZE_MARGIN)


def drives(con, plan_id: str | None = None) -> list[dict]:
    """Registered drives with `remaining` usable space. For a MOUNTED drive we read the LIVE disk free
    (`shutil.disk_usage`) — reality that already reflects archived bytes, real compression, AND any
    cruft (orphan partials, raw working files) — minus headroom. This is the "account for it as we run"
    fix (#31): the recorded `free_bytes` is a stale registration snapshot, so a drift there quietly
    over/under-provisions. An UNMOUNTED drive falls back to `free_bytes − archived − headroom`.
    `plan_id` restricts to that plan's FIXED drive set (`plan_drives`) — the only capacity the fill
    (#33) is allowed to use; None = every registered drive (legacy / whole-fleet views)."""
    used = dict(con.execute(
        "SELECT drive_label, coalesce(sum(stored_bytes), 0) FROM archived GROUP BY 1").fetchall())
    where, params = "", []
    if plan_id is not None:
        where = "WHERE drive_label IN (SELECT drive_label FROM plan_drives WHERE plan_id=?)"
        params = [plan_id]
    out = []
    for label, role, raid_backed, cap, free in con.execute(
            "SELECT drive_label, coalesce(role,'primary'), coalesce(raid_backed, false), "
            "coalesce(capacity_bytes, free_bytes, 0), coalesce(free_bytes, 0) FROM drives "
            f"{where} ORDER BY coalesce(capacity_bytes, free_bytes, 0) DESC, drive_label", params).fetchall():
        head = headroom_bytes(cap)
        arch = used.get(label, 0)
        mount = register.archive_path(con, label)          # live mount path, or None if not attached
        live_free = None
        if mount is not None:
            try:
                live_free = shutil.disk_usage(str(mount)).free
            except OSError:
                live_free = None
        if live_free is not None:                          # MOUNTED: trust the disk (already nets out archived + cruft)
            free_now, remaining = live_free, max(0, live_free - head)
        else:                                              # UNMOUNTED: best-effort from the (empty-ish) snapshot
            free_now, remaining = free, max(0, free - arch - head)
        out.append({"label": label, "role": role, "raid_backed": bool(raid_backed), "capacity": cap,
                    "free": free_now, "headroom": head, "archived": arch, "remaining": remaining})
    return out


def _consolidate(items: list[tuple], targets: list[dict]) -> tuple[dict, dict, list]:
    """Variable-sized bin packing, consolidate: largest items into the largest drive that still
    fits (drives tried biggest-first), opening a new drive only when forced — fewest drives used,
    smallest left free. Keep-model-whole. Returns (assign, remaining, unplaceable)."""
    order = sorted(targets, key=lambda d: (0 if d.get("raid_backed") else 1, -d["capacity"]))  # RAID first, then largest
    rem = {d["label"]: d["remaining"] for d in targets}
    assign: dict[str, list[dict]] = {d["label"]: [] for d in targets}
    unplaceable: list[dict] = []
    for repo, sz in sorted(items, key=lambda rs: rs[1], reverse=True):
        for d in order:
            if rem[d["label"]] >= sz:
                assign[d["label"]].append({"repo": repo, "size": sz})
                rem[d["label"]] -= sz
                break
        else:
            unplaceable.append({"repo": repo, "size": sz})
    return assign, rem, unplaceable


def _group(items: list[dict], targets: list[dict]) -> tuple[dict, list]:
    """Place a whole GROUP (the must-have set as a unit) on the SMALLEST single target that fits
    it; else FFD-span it (largest model first) across targets smallest-first, keeping each model
    whole (DEC-017: '2nd copy kept whole; take a larger drive or span 2+ if needed'). Returns
    (assign, unplaceable)."""
    asc = sorted(targets, key=lambda d: d["remaining"])
    total = sum(i["size"] for i in items)
    for d in asc:                                       # smallest single drive that fits the whole set
        if d["remaining"] >= total:
            return {d["label"]: list(items)}, []
    assign: dict[str, list[dict]] = {d["label"]: [] for d in targets}    # else span, models kept whole
    rem = {d["label"]: d["remaining"] for d in targets}
    un: list[dict] = []
    for it in sorted(items, key=lambda i: i["size"], reverse=True):
        for d in asc:
            if rem[d["label"]] >= it["size"]:
                assign[d["label"]].append(it); rem[d["label"]] -= it["size"]; break
        else:
            un.append(it)
    return assign, un


def _advise(primary_bulk, raid, replica, p_un, must_items, must_repl, c1_un, c2_un, freed) -> list[dict]:
    adv = []
    if p_un:
        adv.append({"level": "error", "msg":
            f"NOT ENOUGH PRIMARY — {len(p_un)} bulk model(s) / {sum(i['size'] for i in p_un)/1e12:.2f} TB "
            f"don't fit the non-RAID primary drives. Register more primary capacity."})
    if freed:
        adv.append({"level": "info", "msg": f"Bulk consolidated — free/unused primary drives: {', '.join(freed)}."})
    if must_items:
        mb = sum(i["size"] for i in must_items)              # copy#1 capacity-mode footprint
        mb_repl = sum(i["size"] for i in must_repl)          # copy#2 expected stored footprint
        if not raid:
            adv.append({"level": "warn", "msg":
                "NO RAID — must-have copy #1 falls on a single primary disk (no redundant tier). "
                "Register the NAS/RAID `raid_backed`, or accept a single-disk copy #1."})
        if c1_un:
            adv.append({"level": "error", "msg":
                f"COPY #1 SHORT — {len(c1_un)} must-have(s) / {sum(i['size'] for i in c1_un)/1e12:.2f} TB "
                f"don't fit the RAID/primary home ({mb/1e12:.2f} TB needed). Enlarge it or trim the set."})
        if not replica:
            adv.append({"level": "warn", "msg":
                f"NO REPLICA DRIVE — {len(must_repl)} must-have(s) can't get a 2nd copy. "
                f"Mark a small drive `--role replica`."})
        elif c2_un:
            adv.append({"level": "error", "msg":
                f"REPLICA SHORT — {len(c2_un)} must-have(s) can't get their 2nd copy ({mb_repl/1e12:.2f} TB "
                f"compressed needed, replica tier {sum(d['remaining'] for d in replica)/1e12:.2f} TB free). "
                f"Add/enlarge a replica drive."})
        elif not c1_un:
            adv.append({"level": "ok", "msg":
                f"All {len(must_items)} must-have(s) placed — copy #1 ({mb/1e12:.2f} TB) on the RAID, "
                f"copy #2 ({mb_repl/1e12:.2f} TB compressed) on the replica tier."})
    return adv


def placed_copies(con) -> dict[str, int]:
    """Per repo: how many drives hold a COMPLETE copy — ALL of the repo's planned files
    (safetensors + aux + gguf-when-no-safetensors, mirroring fetch.plan). Counting DISTINCT
    drives is too coarse (DEC-019): a drive with a half-fetched repo must NOT count toward
    numcopies, or an interrupted copy would look 'done'."""
    rows = con.execute(
        "WITH hasst AS (SELECT repo_id, max(CASE WHEN format='safetensors' THEN 1 ELSE 0 END) s "
        "               FROM files GROUP BY repo_id), "
        "planned AS (SELECT f.repo_id, count(*) n FROM files f JOIN hasst h USING(repo_id) "
        "            WHERE f.format IN ('safetensors','aux') OR (f.format='gguf' AND h.s=0) "
        "            GROUP BY f.repo_id), "
        "perdrive AS (SELECT repo_id, drive_label, count(*) n FROM archived GROUP BY repo_id, drive_label) "
        "SELECT pd.repo_id, count(*) FROM perdrive pd JOIN planned pl "
        "  ON pd.repo_id = pl.repo_id AND pd.n >= pl.n GROUP BY pd.repo_id").fetchall()
    return dict(rows)


def raw_sizes(con, repos: list[str]) -> dict[str, int]:
    """Raw (full-precision DOWNLOAD) footprint per repo — mirrors fetch.plan's file selection
    (safetensors + aux + gguf-when-no-safetensors), one pass. Used for the giants-first fetch priority
    (a 'giant' is judged by download size, independent of the provisioning-currency estimate)."""
    if not repos:
        return {}
    ph = ",".join(["?"] * len(repos))
    rows = con.execute(
        "WITH hasst AS (SELECT repo_id, max(CASE WHEN format='safetensors' THEN 1 ELSE 0 END) s "
        "               FROM files GROUP BY repo_id) "
        "SELECT f.repo_id, coalesce(sum(f.size_bytes), 0) FROM files f JOIN hasst h USING(repo_id) "
        f"WHERE (f.format IN ('safetensors','aux') OR (f.format='gguf' AND h.s=0)) AND f.repo_id IN ({ph}) "
        "GROUP BY f.repo_id", repos).fetchall()
    return dict(rows)


def plan_placements(con, repos: list[str] | None = None, plan_id: str | None = None,
                    capacity_mode: str = "guaranteed") -> dict:
    """DEC-017 tiered layout, scoped to a Plan (#33). RAID is pulled OUT of the size sort: must-have
    COPY #1 → the RAID (else the largest primary if no RAID); BULK (numcopies=1) → the non-RAID PRIMARY
    drives (consolidated); must-have COPY #2+ → the smallest-sufficient INDEPENDENT REPLICA drive(s)
    (whole set on one, else span). `plan_id` restricts the fleet to `plan_drives`; `capacity_mode`
    chooses guaranteed or compression-aware accounting. Resumable per DEC-019; emits advisories."""
    # DEC-019: a repo leaves the pool only when FULLY placed — its COMPLETE-copy count meets its
    # numcopies. (models.status flips to 'archived' after the FIRST copy, so it is NOT a safe
    # done-signal for a must-have; an interrupted 2nd copy must stay schedulable.)
    placed = placed_copies(con)
    want = dict(con.execute("SELECT repo_id, coalesce(numcopies,1) FROM models").fetchall())
    cands = repos or fetch.finalized(con)
    pool = [r for r in cands if placed.get(r, 0) < want.get(r, 1)]
    n_done = len(cands) - len(pool)
    float_ratio = plan_float_ratio(con)                  # observed avg (raw-fallbacks included), computed once
    size = {r: est_stored_bytes(con, r, float_ratio, capacity_mode) for r in pool}
    ncopies = {}
    if pool:
        ncopies = dict(con.execute(
            "SELECT repo_id, coalesce(numcopies,1) FROM models WHERE repo_id IN "
            f"({','.join(['?']*len(pool))})", pool).fetchall())

    ds = drives(con, plan_id)
    raid = [d for d in ds if d["raid_backed"]]
    nonraid = [d for d in ds if d["role"] == "primary" and not d["raid_backed"]]
    replica = [d for d in ds if d["role"] == "replica"]

    bulk = [(r, size[r]) for r in pool if ncopies.get(r, 1) < 2]
    must_items = [{"repo": r, "size": size[r]} for r in pool if ncopies.get(r, 1) >= 2]
    # A must-have COPY #2 is a `git annex copy` of the already-COMPRESSED copy#1 blob (not a fetch), so
    # its size is known — never raw-over-provision it. Under guaranteed capacity, sizing copy#2
    # at raw would falsely short the small replica tier and GATE-B a fill that actually fits (the copies
    # are compressed on both drives). So the replica tier is ALWAYS sized against the compressed estimate.
    size_repl = ({r: est_stored_bytes(con, r, float_ratio, "compression_aware") for r in pool}
                 if capacity_model.mode_from_value(capacity_mode)
                 == capacity_model.CapacityMode.GUARANTEED else size)
    must_repl = [{"repo": r, "size": size_repl[r]} for r in pool if ncopies.get(r, 1) >= 2]

    # 1. must-have COPY #1 → the RAID (reserve its space FIRST), else the largest primary (no-RAID fleets)
    homes = raid or nonraid
    c1_home = max(homes, key=lambda d: d["remaining"])["label"] if (must_items and homes) else None
    c1_assign, c1_un = {}, (list(must_items) if (must_items and not homes) else [])
    reserved = 0
    if c1_home:
        c1_assign, c1_un = _group(must_items, [next(d for d in ds if d["label"] == c1_home)])
        reserved = sum(i["size"] for i in c1_assign.get(c1_home, []))

    # 2. BULK → ALL primaries, RAID FIRST (DEC-020): fill the safe, abundant NAS space before cramming
    #    the externals — the RAID is the best primary, not just the must-have vault. Its copy#1
    #    reservation is subtracted from its bulk room; must-haves always keep first claim.
    bulk_targets = [dict(d, remaining=(d["remaining"] - reserved if d["label"] == c1_home else d["remaining"]))
                    for d in (raid + nonraid)]
    p_assign, _pr, p_un = _consolidate(bulk, bulk_targets)
    if c1_home:                                     # the RAID holds copy #1 + whatever bulk landed on it
        p_assign[c1_home] = c1_assign.get(c1_home, []) + p_assign.get(c1_home, [])

    # 3. must-have COPY #2+ → the smallest-sufficient INDEPENDENT replica drive(s), sized COMPRESSED
    if must_repl and replica:
        r_assign, c2_un = _group(must_repl, replica)
    else:
        r_assign, c2_un = {d["label"]: [] for d in replica}, (list(must_repl) if must_repl else [])

    primary_drives = raid + nonraid
    freed = [d["label"] for d in sorted(primary_drives, key=lambda d: d["capacity"]) if not p_assign.get(d["label"])]
    p_rem = {d["label"]: d["remaining"] - sum(i["size"] for i in p_assign.get(d["label"], [])) for d in primary_drives}
    r_rem = {d["label"]: d["remaining"] - sum(i["size"] for i in r_assign.get(d["label"], [])) for d in replica}
    advisories = _advise(nonraid, raid, replica, p_un, must_items, must_repl, c1_un, c2_un, freed)
    if c1_home and len(p_assign.get(c1_home, [])) > len(c1_assign.get(c1_home, [])):
        advisories.insert(0, {"level": "info", "msg":
            "Bulk is filling the RAID's free space (re-fetchable data on the redundant tier) — the higher "
            "tier fills before the externals; a future must-have preempts it on the next plan."})
    return {"primary": {"assign": p_assign, "rem": p_rem, "unplaceable": p_un + c1_un, "drives": primary_drives},
            "replica": {"assign": r_assign, "rem": r_rem, "unplaceable": c2_un, "drives": replica, "source": c1_home},
            "freed": freed, "advisories": advisories,
            "n_planned": len(pool), "n_done": n_done, "n_must": len(must_items), "n_bulk": len(bulk)}


def _reconciled_projection(con, repos, plan_id, capacity_mode):
    """Build one read-only graph/ledger snapshot using live free space where it is observable."""
    if plan_id is None:
        row = con.execute(
            "SELECT plan_id FROM plans WHERE is_active ORDER BY plan_id LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("no active plan; select a plan before requesting library placement")
        plan_id = row[0]
    labels = [row[0] for row in con.execute(
        "SELECT drive_label FROM plan_drives WHERE plan_id=? ORDER BY drive_label", [plan_id]
    ).fetchall()]
    live_free = {}
    for label in labels:
        path = register.archive_path(con, label)
        if path is None:
            continue
        try:
            live_free[label] = shutil.disk_usage(str(path)).free
        except OSError:
            pass
    graph = reconcile.reconcile_plan(con, plan_id, repos)
    ledger = capacity_model.plan_capacity(
        con, graph, capacity_mode=capacity_mode, live_free_by_drive=live_free,
    )
    return graph, ledger


def _diagnostic_payload(graph) -> list[dict]:
    return [{
        "code": item.code,
        "severity": item.severity.value,
        "recovery": item.recovery.value,
        "requirement_id": item.requirement_id,
        "detail": dict(item.detail),
    } for item in graph.diagnostics]


def _blocked_repos(graph, ledger) -> dict[str, list[str]]:
    """Map every selected repo with a blocking graph/capacity condition to stable typed codes."""
    requirement_repo = {
        item.requirement_id: item.repo_id for item in graph.requirements
    }
    blocked: dict[str, set[str]] = {}
    for item in graph.diagnostics:
        if item.severity not in {
            reconcile.DiagnosticSeverity.BLOCKING,
            reconcile.DiagnosticSeverity.ERROR,
        }:
            continue
        detail = dict(item.detail)
        repo_id = detail.get("repo_id") or requirement_repo.get(item.requirement_id)
        if repo_id:
            blocked.setdefault(str(repo_id), set()).add(item.code)
    for item in ledger.failures:
        repo_id = requirement_repo.get(item.requirement_id)
        if repo_id:
            blocked.setdefault(repo_id, set()).add(item.code.value)
    for item in ledger.unassigned_intents:
        blocked.setdefault(item.repo_id, set()).add("GRAPH_UNASSIGNED")
    return {repo_id: sorted(codes) for repo_id, codes in sorted(blocked.items())}


def _projection_advisories(diagnostics: list[dict], failures: list[dict]) -> list[dict]:
    """Make typed evidence prominent without rendering hundreds of near-identical banners."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for item in diagnostics:
        grouped.setdefault((item["severity"], item["code"]), []).append(item)
    advisories = []
    levels = {"blocking": "error", "error": "error", "warning": "warn", "info": "info"}
    for (severity, code), items in sorted(grouped.items()):
        repos = sorted({
            str(item["detail"].get("repo_id"))
            for item in items if item["detail"].get("repo_id")
        })
        examples = ", ".join(repos[:3])
        tail = (f" — {examples}" + (f" (+{len(repos) - 3} more)" if len(repos) > 3 else "")) \
            if examples else ""
        advisories.append({
            "level": levels.get(severity, "warn"),
            "code": code,
            "count": len(items),
            "msg": f"{code}: {len(items)} reconciled diagnostic(s){tail}",
        })
    failures_by_code: dict[str, list[dict]] = {}
    for item in failures:
        failures_by_code.setdefault(item["code"], []).append(item)
    for code, items in sorted(failures_by_code.items()):
        shortfall = sum(int(item.get("shortfall_bytes", 0) or 0) for item in items)
        advisories.append({
            "level": "error",
            "code": code,
            "count": len(items),
            "msg": (f"{code}: {len(items)} capacity requirement(s) blocked; "
                    f"combined shortfall {shortfall / 1e12:.3f} TB"),
        })
    return advisories


def plan_view(con, repos: list[str] | None = None, plan_id: str | None = None,
              capacity_mode: str = "guaranteed") -> dict:
    """Publish the canonical reconciled work graph in the existing Fill-view JSON shape.

    DEC-045 retired the legacy placement result as execution authority.  This adapter deliberately
    keeps the established drive/model/advisory fields for the CLI and browser, but derives every
    assignment and byte from ``reconcile_plan`` + ``plan_capacity``.  Policy-invalid repositories
    remain in typed diagnostics instead of raising on the first one or disappearing from the cart.
    """
    graph, ledger = _reconciled_projection(con, repos, plan_id, capacity_mode)
    diagnostics = _diagnostic_payload(graph)
    failures = ledger.to_dict()["failures"]
    cats = dict(con.execute("SELECT repo_id, coalesce(category, '?') FROM models").fetchall())
    archived = dict(con.execute(
        "SELECT drive_label,coalesce(sum(stored_bytes),0) FROM archived GROUP BY drive_label"
    ).fetchall())
    drive_rows = con.execute(
        "SELECT d.drive_label,coalesce(d.role,'primary'),coalesce(d.raid_backed,0),"
        "coalesce(d.capacity_bytes,d.free_bytes,0) "
        "FROM plan_drives pd JOIN drives d USING(drive_label) WHERE pd.plan_id=? "
        "ORDER BY d.drive_label",
        [graph.plan_id],
    ).fetchall()
    ledger_by_drive = {item.drive_label: item for item in ledger.ledgers}
    tasks_by_drive: dict[str, list] = {}
    for task in ledger.tasks:
        tasks_by_drive.setdefault(task.target_drive, []).append(task)

    out = []
    for label, role, raid, drive_capacity in drive_rows:
        raid = bool(raid)
        tier = "raid" if raid else ("replica" if role == "replica" else "primary")
        models = []
        for task in tasks_by_drive.get(label, []):
            copy = ("2" if task.kind == reconcile.TaskKind.REPLICATE else
                    ("1" if task.requirement_id.startswith("protected_home:") else "bulk"))
            models.append({
                "repo": task.repo_id,
                "size": task.budget.durable_for(ledger.mode),
                "category": cats.get(task.repo_id, "?"),
                "copy": copy,
            })
        models.sort(key=lambda item: (-item["size"], item["repo"], item["copy"]))
        drive_ledger = ledger_by_drive[label]
        safety_floor = drive_ledger.safety_floor
        usable = max(0, int(drive_capacity or 0) - safety_floor)
        planned = sum(item["size"] for item in models)
        out.append({
            "label": label, "tier": tier, "role": role, "raid_backed": raid,
            "capacity": int(drive_capacity or 0), "headroom": safety_floor,
            "free": drive_ledger.physical_free, "usable": usable,
            "planned_bytes": planned, "archived_bytes": int(archived.get(label, 0) or 0),
            "n_models": len(models),
            "fill_pct": round(planned / usable, 4) if usable else 0.0,
            "models": models,
        })

    task_target = {task.requirement_id: task.target_drive for task in ledger.tasks}
    links = []
    for task in ledger.tasks:
        if task.kind != reconcile.TaskKind.REPLICATE:
            continue
        source = task.source_drive or task_target.get(task.depends_on_requirement or "")
        if source:
            links.append({"from": source, "to": task.target_drive})
    links = [dict(pair) for pair in sorted({tuple(sorted(item.items())) for item in links})]
    sources = sorted({item["from"] for item in links})

    requirements_by_repo: dict[str, list[str]] = {}
    for requirement in graph.requirements:
        requirements_by_repo.setdefault(requirement.repo_id, []).append(requirement.requirement_id)
    valid_repos = set(graph.manifests)
    done = {
        repo_id for repo_id, requirement_ids in requirements_by_repo.items()
        if requirement_ids and all(item in graph.satisfied for item in requirement_ids)
    }
    incomplete = valid_repos - done
    copies = dict(con.execute(
        "SELECT repo_id,coalesce(numcopies,1) FROM models"
    ).fetchall())
    blocked_repos = _blocked_repos(graph, ledger)
    freed = [
        item["label"] for item in out
        if item["role"] == "primary" and not tasks_by_drive.get(item["label"])
    ]
    blocking_codes = sorted(set(ledger.blocking_diagnostics) | {
        item["code"] for item in failures
    })
    return {
        "drives": out,
        "links": links,
        "source": sources[0] if len(sources) == 1 else None,
        "freed": freed,
        "advisories": _projection_advisories(diagnostics, failures),
        "diagnostics": diagnostics,
        "capacity_failures": failures,
        "blocking_diagnostics": blocking_codes,
        "feasible": ledger.feasible,
        "placement_policy": ledger.placement_policy,
        "capacity_mode": ledger.mode.value,
        "totals": {
            "n_planned": len(incomplete),
            "n_done": len(done),
            "n_must": sum(int(copies.get(repo_id, 1) or 1) >= 2 for repo_id in incomplete),
            "n_bulk": sum(int(copies.get(repo_id, 1) or 1) < 2 for repo_id in incomplete),
            "n_blocked": len(blocked_repos),
            "n_selected": len(graph.repo_ids),
        },
    }


def queue_view(con, plan_id: str | None = None, capacity_mode: str = "guaranteed") -> dict:
    """The Fill 'queue' as ONE row per finalized model (not per copy). Returns the WHOLE finalized
    selection — DONE models included — so nothing vanishes when a model finishes. Each row carries
    size (est on-disk), category, numcopies, and the drive its copy#1 and copy#2 are planned for; the
    frontend derives per-model state (upcoming | partial | done) from live placed-copy counts
    (queue_state) and positions each row under the drive of its NEXT unfinished copy — done rows
    settle under their copy#1 home (done-placement 'b'). Heavy (~one plan pass): the Fill tab fetches
    it once on open; the cheap queue_state refreshes state at model boundaries without re-planning."""
    graph, ledger = _reconciled_projection(con, None, plan_id, capacity_mode)
    diagnostics = _diagnostic_payload(graph)
    failures = ledger.to_dict()["failures"]
    copy1, copy2 = {}, {}
    for requirement_id, label in graph.matches:
        repo = requirement_id.split(":", 1)[1]
        (copy2 if requirement_id.startswith("protected_replica:") else copy1)[repo] = label
    for task in ledger.tasks:
        target = copy2 if task.kind == reconcile.TaskKind.REPLICATE else copy1
        target.setdefault(task.repo_id, task.target_drive)
    numcopies = dict(con.execute("SELECT repo_id, coalesce(numcopies,1) FROM models").fetchall())
    cats = dict(con.execute("SELECT repo_id, coalesce(category,'?') FROM models").fetchall())
    ratio = capacity_model.plan_float_ratio(con)
    blocked = _blocked_repos(graph, ledger)
    models = []
    for repo in graph.repo_ids:
        n = numcopies.get(repo, 1)
        manifest = graph.manifests.get(repo)
        size = 0 if manifest is None else sum(
            int((item.size_bytes * ratio if item.storage_action == "compress" else item.size_bytes)
                * capacity_model.EXPECTED_MARGIN)
            for item in manifest
        )
        models.append({
            "repo": repo,
            "size": size,
            "size_known": manifest is not None,
            "category": cats.get(repo, "?"),
            "numcopies": n,
            "copy1": copy1.get(repo),
            "copy2": copy2.get(repo),
            "blocking_diagnostics": blocked.get(repo, []),
        })
    drive_info = [{
        "label": row[0],
        "tier": "raid" if row[2] else ("replica" if row[1] == "replica" else "primary"),
        "capacity": int(row[3] or 0),
    } for row in con.execute(
        "SELECT d.drive_label,coalesce(d.role,'primary'),coalesce(d.raid_backed,0),"
        "coalesce(d.capacity_bytes,d.free_bytes,0) "
        "FROM plan_drives pd JOIN drives d USING(drive_label) WHERE pd.plan_id=? "
        "ORDER BY d.drive_label",
        [graph.plan_id],
    ).fetchall()]
    blocking_codes = sorted(set(ledger.blocking_diagnostics) | {
        item["code"] for item in failures
    })
    return {
        "models": models,
        "drives": drive_info,
        "diagnostics": diagnostics,
        "capacity_failures": failures,
        "blocking_diagnostics": blocking_codes,
        "feasible": ledger.feasible,
    }


def queue_state(con) -> dict:
    """Live per-model completion for the Fill queue — {repo: copies_placed}. Cheap (one placed_copies
    pass, no plan): polled at model boundaries so rows flip upcoming -> partial -> done without a
    re-plan. done = placed >= numcopies; partial = 0 < placed < numcopies."""
    return placed_copies(con)
