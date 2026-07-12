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

from modelark import fetch, register  # noqa: F401

_ZIPNN_FLOAT_RATIO = 0.67          # bf16 ZipNN on-disk estimate; the CONSERVATIVE FLOOR + fresh-catalog default
_SIZE_MARGIN = 1.08                # +8% headroom so a mislabeled fp8 (HYP-001) can't under-provision
_RATIO_MIN_SAMPLE = 50_000_000_000 # need ≥50 GB of float weights archived before trusting the observed ratio

# Reserved headroom per drive — marginal tranches, so the effective % shrinks as a drive grows.
_HEADROOM_TRANCHES = [
    (1e12,         0.05),    # first 1 TB     -> 5%
    (4e12,         0.02),    # 1–4 TB         -> 2%
    (16e12,        0.0125),  # 4–16 TB        -> 1.25%
    (float("inf"), 0.009),   # above 16 TB    -> 0.9%
]


def headroom_bytes(capacity: int) -> int:
    reserved, lo = 0.0, 0.0
    for hi, rate in _HEADROOM_TRANCHES:
        band = min(capacity, hi) - lo
        if band <= 0:
            break
        reserved += band * rate
        lo = hi
    return int(reserved)


def observed_float_ratio(con) -> float | None:
    """stored/orig ACTUALLY achieved on float (compressible) weights so far — blends real ZipNN
    compression AND the raw-fallbacks (INC-005 crash/hang shards stored uncompressed). None until
    _RATIO_MIN_SAMPLE bytes of evidence. This is the fix for the plan creeping onto more drives each
    restart: the estimate must track reality (raw-fallbacks push actual > a fixed 0.67), not guess."""
    stored, orig = con.execute(
        "SELECT coalesce(sum(a.stored_bytes), 0), coalesce(sum(a.orig_bytes), 0) "
        "FROM archived a JOIN files f ON a.repo_id = f.repo_id AND a.rfilename = f.rfilename "
        "WHERE f.format = 'safetensors' AND a.orig_bytes > 0 AND "
        "(f.quant IS NULL OR lower(f.quant) IN "
        "('bf16','bfloat16','fp16','f16','float16','fp32','f32','float32'))").fetchone()
    return stored / orig if orig >= _RATIO_MIN_SAMPLE else None


def plan_float_ratio(con) -> float:
    """The float ratio est_stored_bytes should use: our observed average once there's enough evidence,
    but NEVER more optimistic than the _ZIPNN_FLOAT_RATIO baseline (a conservative floor)."""
    return max(observed_float_ratio(con) or _ZIPNN_FLOAT_RATIO, _ZIPNN_FLOAT_RATIO)


def est_stored_bytes(con, repo_id: str, float_ratio: float | None = None,
                     provisioning: str = "uncompressed") -> int:
    """Estimated footprint of one copy. Provisioning-aware (DEF-016):
      • 'uncompressed' (default) → the EXACT raw full-precision footprint (comp + raw, no ratio) — the
        over-provision basis; the fill reserves full space so it can never run out, and compression
        just leaves the reservation with room to spare.
      • 'compressed'   → the ZipNN estimate (comp × observed-ratio + raw) × margin — the BET; packs
        each drive to its predicted TRUE capacity, with the per-model failsafe carrying the risk.
    `float_ratio` (from plan_float_ratio, computed ONCE per plan) tracks the fill's real average so
    drives don't over-pack; omitted → computed here (a query per call, fine for one-offs)."""
    files = fetch.plan(con, repo_id)
    comp = sum(f["size"] or 0 for f in files if f["mode"] == "compress")
    raw = sum(f["size"] or 0 for f in files if f["mode"] == "raw")
    if provisioning == "uncompressed":
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
        mb = sum(i["size"] for i in must_items)              # copy#1 footprint (provisioning currency)
        mb_repl = sum(i["size"] for i in must_repl)          # copy#2 footprint (compressed — the real copy size)
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
                    provisioning: str = "uncompressed") -> dict:
    """DEC-017 tiered layout, scoped to a Plan (#33). RAID is pulled OUT of the size sort: must-have
    COPY #1 → the RAID (else the largest primary if no RAID); BULK (numcopies=1) → the non-RAID PRIMARY
    drives (consolidated); must-have COPY #2+ → the smallest-sufficient INDEPENDENT REPLICA drive(s)
    (whole set on one, else span). `plan_id` restricts the fleet to `plan_drives`; `provisioning`
    picks the packing currency (raw vs compressed est, DEF-016). Resumable per DEC-019; emits advisories."""
    # DEC-019: a repo leaves the pool only when FULLY placed — its COMPLETE-copy count meets its
    # numcopies. (models.status flips to 'archived' after the FIRST copy, so it is NOT a safe
    # done-signal for a must-have; an interrupted 2nd copy must stay schedulable.)
    placed = placed_copies(con)
    want = dict(con.execute("SELECT repo_id, coalesce(numcopies,1) FROM models").fetchall())
    cands = repos or fetch.finalized(con)
    pool = [r for r in cands if placed.get(r, 0) < want.get(r, 1)]
    n_done = len(cands) - len(pool)
    float_ratio = plan_float_ratio(con)                  # observed avg (raw-fallbacks included), computed once
    size = {r: est_stored_bytes(con, r, float_ratio, provisioning) for r in pool}
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
    # its size is known — never raw-over-provision it. Under 'uncompressed' provisioning, sizing copy#2
    # at raw would falsely short the small replica tier and GATE-B a fill that actually fits (the copies
    # are compressed on both drives). So the replica tier is ALWAYS sized against the compressed estimate.
    size_repl = ({r: est_stored_bytes(con, r, float_ratio, "compressed") for r in pool}
                 if provisioning == "uncompressed" else size)
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


def plan_view(con, repos: list[str] | None = None, plan_id: str | None = None,
              provisioning: str = "uncompressed") -> dict:
    """The placement plan as UI-ready JSON — the librarian's results 'published' for the portal
    Fill tab (and `library plan --json`). Per drive: tier/role/raid, capacity + headroom, planned
    models {repo, size, category, copy}, and fill %. Plus copy#1→copy#2 links (the dotted lines),
    advisories, and totals. Scoped to a Plan's drive set + provisioning currency (#33). The SAME
    backend the CLI and the UI both call."""
    p = plan_placements(con, repos, plan_id, provisioning)
    dinfo = {d["label"]: d for d in drives(con, plan_id)}
    cats = dict(con.execute("SELECT repo_id, coalesce(category, '?') FROM models").fetchall())
    # A must-have's copy #1 stays in the plan's primary assign even after it's archived (it's still in
    # the pool for copy #2), so a drive's raw assign double-counts bytes already on it. Track what's
    # actually on each drive so `planned` means NEW work (the archived bytes are the card's grey base).
    arch_pairs = set(con.execute("SELECT DISTINCT repo_id, drive_label FROM archived").fetchall())
    src = p["replica"]["source"]

    def models_of(assign, copy):
        return [{"repo": i["repo"], "size": i["size"], "category": cats.get(i["repo"], "?"), "copy": copy}
                for i in assign]

    out = []
    for label in sorted(dinfo):
        d = dinfo[label]
        raid, role = d["raid_backed"], d["role"]
        tier = "raid" if raid else ("replica" if role == "replica" else "primary")
        models = models_of(p["primary"]["assign"].get(label, []), "1" if raid else "bulk") \
            + models_of(p["replica"]["assign"].get(label, []), "2")
        models = [m for m in models if (m["repo"], label) not in arch_pairs]   # NEW work only; already-archived = grey base
        planned = sum(m["size"] for m in models)
        usable = max(0, d["capacity"] - d["headroom"])
        out.append({
            "label": label, "tier": tier, "role": role, "raid_backed": raid,
            "capacity": d["capacity"], "headroom": d["headroom"], "free": d["free"], "usable": usable,
            "planned_bytes": planned, "archived_bytes": d["archived"], "n_models": len(models),
            "fill_pct": round(planned / usable, 4) if usable else 0.0, "models": models,
        })
    links = [{"from": src, "to": label} for label, items in p["replica"]["assign"].items() if items and src]
    return {"drives": out, "links": links, "source": src, "freed": p["freed"],
            "advisories": p["advisories"],
            "totals": {"n_planned": p["n_planned"], "n_done": p["n_done"],
                       "n_must": p["n_must"], "n_bulk": p["n_bulk"]}}


def queue_view(con, plan_id: str | None = None, provisioning: str = "uncompressed") -> dict:
    """The Fill 'queue' as ONE row per finalized model (not per copy). Returns the WHOLE finalized
    selection — DONE models included — so nothing vanishes when a model finishes. Each row carries
    size (est on-disk), category, numcopies, and the drive its copy#1 and copy#2 are planned for; the
    frontend derives per-model state (upcoming | partial | done) from live placed-copy counts
    (queue_state) and positions each row under the drive of its NEXT unfinished copy — done rows
    settle under their copy#1 home (done-placement 'b'). Heavy (~one plan pass): the Fill tab fetches
    it once on open; the cheap queue_state refreshes state at model boundaries without re-planning."""
    p = plan_placements(con, None, plan_id, provisioning)
    copy1, copy2 = {}, {}                                       # repo -> planned copy#1 / copy#2 drive
    for label, items in p["primary"]["assign"].items():
        for it in items:
            copy1.setdefault(it["repo"], label)
    for label, items in p["replica"]["assign"].items():
        for it in items:
            copy2.setdefault(it["repo"], label)
    ds = drives(con, plan_id)
    replica_labels = [d["label"] for d in ds if d["role"] == "replica"]
    is_replica = {d["label"]: (d["role"] == "replica") for d in ds}
    # DONE models have left the plan pool → copy#1 home = a NON-replica drive they're archived on.
    arch_home = {}
    for repo, label in con.execute("SELECT DISTINCT repo_id, drive_label FROM archived").fetchall():
        if not is_replica.get(label, False):
            arch_home.setdefault(repo, label)

    numcopies = dict(con.execute("SELECT repo_id, coalesce(numcopies,1) FROM models").fetchall())
    cats = dict(con.execute("SELECT repo_id, coalesce(category,'?') FROM models").fetchall())
    ratio = plan_float_ratio(con)
    models = []
    for repo in fetch.finalized(con):
        n = numcopies.get(repo, 1)
        models.append({
            "repo": repo,
            "size": est_stored_bytes(con, repo, ratio, "compressed"),   # realistic on-disk footprint for display
            "category": cats.get(repo, "?"),
            "numcopies": n,
            "copy1": copy1.get(repo) or arch_home.get(repo),
            "copy2": copy2.get(repo) or (replica_labels[0] if (n >= 2 and replica_labels) else None),
        })
    drive_info = [{"label": d["label"],
                   "tier": "raid" if d["raid_backed"] else ("replica" if d["role"] == "replica" else "primary"),
                   "capacity": d["capacity"]} for d in ds]
    return {"models": models, "drives": drive_info}


def queue_state(con) -> dict:
    """Live per-model completion for the Fill queue — {repo: copies_placed}. Cheap (one placed_copies
    pass, no plan): polled at model boundaries so rows flip upcoming -> partial -> done without a
    re-plan. done = placed >= numcopies; partial = 0 < placed < numcopies."""
    return placed_copies(con)
