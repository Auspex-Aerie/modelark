"""First-class Plan entity (#33, DEF-016) — a fill campaign's identity + its capacity model.

A Plan = {identity, the global catalog selection it fills, a FIXED set of registered drives
(`plan_drives`), its annex, a capacity mode}. It fuels the level-1 capacity failsafe by exposing
three numbers computed LIVE against the catalog every call (never stored snapshots):

  • uncompressed = Σ raw (full-precision) footprint of the finalized selection, COPY-AWARE (× numcopies).
                   The BOUNDARY CURRENCY (DEF-016): what decides "are we out". Never a bet.
  • compressed   = what's actually archived so far + an observed-ratio estimate for the copies still to
                   write, copy-aware. uncompressed − compressed = the compression dividend, in drive terms.
  • capacity     = Σ (drive capacity − headroom) over `plan_drives`. The only capacity that exists.

Capacity mode (DEC-045): 'guaranteed' (default) reserves against the raw-bounded footprint;
'compression_aware' uses observed compression plus margin and relies on the live per-file guard.
BOTH forecasts are always reported (the two bars, #36) so
an unexpected inflation (orphans / a bug making actual > expected) is visible in either mode.

`selection` + `archived` stay GLOBAL for the single plan `ark`; a future plan_id column on them is the
multi-plan future (a DEF). Exactly one plan is is_active (the backend/portal's current context); the
#35 UI gate additionally forces an explicit operator pick per session.

Layering: this imports librarian (headroom / observed-ratio / placed-copies — one-directional, librarian
never imports plan) and register (annex root). registration's reverse call into add_drive (#34) is a
LAZY import inside register_drive, so there is no module-level cycle.
"""
from __future__ import annotations

import warnings

from modelark.core import db
from modelark import archive_manifest, capacity as capacity_model, librarian, register, wishlist

DEFAULT_PLAN = "ark"
CAPACITY_MODES = ("guaranteed", "compression_aware")
_LEGACY_TO_CANONICAL = {
    "uncompressed": "guaranteed",
    "compressed": "compression_aware",
}
_CANONICAL_TO_LEGACY = {value: key for key, value in _LEGACY_TO_CANONICAL.items()}
_FIELDS = ["plan_id", "name", "annex_root", "capacity_mode", "status", "is_active", "created_at", "notes"]


def normalize_capacity_mode(value: str, *, warn_legacy: bool = False) -> str:
    if value in CAPACITY_MODES:
        return value
    if value in _LEGACY_TO_CANONICAL:
        if warn_legacy:
            warnings.warn(
                f"capacity mode {value!r} is deprecated; use {_LEGACY_TO_CANONICAL[value]!r}",
                DeprecationWarning,
                stacklevel=2,
            )
        return _LEGACY_TO_CANONICAL[value]
    raise ValueError(
        f"capacity_mode must be 'guaranteed' or 'compression_aware', got {value!r}"
    )


def legacy_capacity_mode(value: str) -> str:
    return _CANONICAL_TO_LEGACY[normalize_capacity_mode(value)]

# ---- CRUD -------------------------------------------------------------------

def create(
    con,
    plan_id,
    name=None,
    annex_root=None,
    capacity_mode=None,
    notes=None,
    *,
    provisioning=None,
) -> dict:
    if provisioning is not None:
        legacy_mode = normalize_capacity_mode(provisioning, warn_legacy=True)
        if capacity_mode is not None and normalize_capacity_mode(capacity_mode) != legacy_mode:
            raise ValueError("capacity_mode and deprecated provisioning disagree")
        capacity_mode = legacy_mode
    else:
        capacity_mode = normalize_capacity_mode(capacity_mode or "guaranteed", warn_legacy=True)
    db.upsert(con, "plans", {
        "plan_id": plan_id, "name": name or plan_id,
        "annex_root": annex_root or str(register.library_root()),
        "capacity_mode": capacity_mode, "status": "active", "notes": notes,
    }, pk=["plan_id"])
    return get(con, plan_id)


def get(con, plan_id) -> dict | None:
    row = con.execute(
        f"SELECT {', '.join(_FIELDS)} FROM plans WHERE plan_id=?", [plan_id]).fetchone()
    if not row:
        return None
    d = dict(zip(_FIELDS, row))
    d["is_active"] = bool(d["is_active"])
    # One-release Python/API compatibility alias. It is not persisted.
    d["provisioning"] = legacy_capacity_mode(d["capacity_mode"])
    d["drives"] = plan_drive_labels(con, plan_id)
    return d


def list_plans(con) -> list[dict]:
    return [get(con, r[0]) for r in
            con.execute("SELECT plan_id FROM plans ORDER BY created_at, plan_id").fetchall()]


def active(con) -> dict | None:
    row = con.execute("SELECT plan_id FROM plans WHERE is_active LIMIT 1").fetchone()
    return get(con, row[0]) if row else None


def set_active(con, plan_id) -> dict:
    if get(con, plan_id) is None:
        raise ValueError(f"no such plan: {plan_id}")
    con.execute("BEGIN")                                     # atomic flip — a crash between the two UPDATEs
    try:                                                     # must never leave EVERY plan is_active=false
        con.execute("UPDATE plans SET is_active=false")
        con.execute("UPDATE plans SET is_active=true WHERE plan_id=?", [plan_id])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get(con, plan_id)


def set_capacity_mode(con, plan_id, mode) -> dict:
    """Set canonical admission accounting without changing how archive bytes are stored."""
    mode = normalize_capacity_mode(mode, warn_legacy=True)
    if get(con, plan_id) is None:
        raise ValueError(f"no such plan: {plan_id}")
    con.execute("UPDATE plans SET capacity_mode=? WHERE plan_id=?", [mode, plan_id])
    return get(con, plan_id)


def set_provisioning(con, plan_id, mode) -> dict:
    """Deprecated one-release alias for :func:`set_capacity_mode`."""
    warnings.warn(
        "set_provisioning() is deprecated; use set_capacity_mode()",
        DeprecationWarning,
        stacklevel=2,
    )
    return set_capacity_mode(con, plan_id, mode)


def add_drive(con, plan_id, drive_label) -> None:
    """Idempotent — a re-registered drive (stable drive-NN key, DEC-018) re-adds to the same row."""
    db.upsert(con, "plan_drives", {"plan_id": plan_id, "drive_label": drive_label},
              pk=["plan_id", "drive_label"])


def remove_drive(con, plan_id, drive_label) -> None:
    con.execute("DELETE FROM plan_drives WHERE plan_id=? AND drive_label=?", [plan_id, drive_label])


def plan_drive_labels(con, plan_id) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT drive_label FROM plan_drives WHERE plan_id=? ORDER BY drive_label", [plan_id]).fetchall()]


def bootstrap(con, plan_id=DEFAULT_PLAN) -> dict:
    """Idempotent: ensure plan `ark` exists, owns every currently-registered drive, and is active if no
    plan is. Safe to call on every startup — creates nothing that exists, removes nothing. Called from
    the portal serve() startup, the CLI `plan`/`library plan` commands, and registration (#34)."""
    if get(con, plan_id) is None:
        create(con, plan_id, name="Ark")
    for row in con.execute("SELECT drive_label FROM drives").fetchall():
        add_drive(con, plan_id, row[0])
    if active(con) is None:
        set_active(con, plan_id)
    return get(con, plan_id)


# ---- capacity + the three live numbers --------------------------------------

def _headroom(cap: int, raid_backed: bool) -> int:
    """Reserved headroom for a drive of `cap` bytes — the librarian's size-tranched reserve, with a
    DEC-029 conservative floor for a RAID-backed LUN so a big LUN still keeps real breathing room."""
    return capacity_model.safety_floor(cap, raid_backed)


def capacity(con, plan_id) -> int:
    """Σ (capacity − headroom) over the plan's drives — the nominal fleet size the plan can fill. Uses
    the registration capacity snapshot (stable), NOT live free (that is the per-model failsafe's job)."""
    labels = plan_drive_labels(con, plan_id)
    if not labels:
        return 0
    ph = ",".join(["?"] * len(labels))
    total = 0
    for _, cap, raid in con.execute(
            f"SELECT drive_label, coalesce(capacity_bytes, free_bytes, 0), coalesce(raid_backed, false) "
            f"FROM drives WHERE drive_label IN ({ph})", labels).fetchall():
        total += max(0, cap - _headroom(cap, bool(raid)))
    return total


def _footprint_by_repo(con, repo_ids: list[str]) -> dict[str, tuple[int, int, int]]:
    """Per repo: (raw, compressible, noncompressible) from the canonical bulk manifest.

    Ineligible repositories remain absent, preserving the catalog-gate behavior while the
    shadow reconciler reports their typed manifest-policy diagnostics.
    """
    policy = archive_manifest.ArchivePolicy(allow_pickle=not wishlist.exclude_pickle_only())
    batch = archive_manifest.inspect_manifests_for_repos(con, repo_ids, policy)
    out = {}
    for repo_id, manifest in batch.manifests.items():
        compressible = sum(item.size_bytes for item in manifest if item.storage_action == "compress")
        noncompressible = sum(item.size_bytes for item in manifest if item.storage_action == "raw")
        out[repo_id] = (compressible + noncompressible, compressible, noncompressible)
    return out


# Graduated catalog gate (#38): tiers on the COMPRESSED footprint vs capacity (compressed is what
# actually lands; if even it exceeds capacity the set definitely won't fit → prevent).
_GATE_SOFT = 0.70          # below this × capacity → just show the bars
_GATE_WARN = 0.90          # [soft, warn) → soft warn; [warn, 1.0) → warn harder; ≥ 1.0 → prevent


def _footprint(con, plan_id, repo_ids) -> tuple[int, int, int]:
    """Copy-aware (uncompressed, compressed, n_must) footprint over `repo_ids`. uncompressed = Σ raw ×
    numcopies (the boundary currency); compressed = bytes actually archived so far + an observed-ratio
    estimate for the copies still to write. Over-counts one in-flight partial copy at most (transient)."""
    numcopies = dict(con.execute("SELECT repo_id, coalesce(numcopies,1) FROM models").fetchall())
    placed = librarian.placed_copies(con)
    ratio = librarian.plan_float_ratio(con)                 # observed float ratio, floored at 0.67
    margin = librarian._SIZE_MARGIN                         # single source of truth for the est margin
    fp = _footprint_by_repo(con, repo_ids)
    uncompressed = est_remaining = n_must = 0
    for repo in repo_ids:
        nc = numcopies.get(repo, 1)
        n_must += nc >= 2
        raw, comp, noncomp = fp.get(repo, (0, 0, 0))
        uncompressed += raw * nc
        est_per_copy = int((comp * ratio + noncomp) * margin)
        est_remaining += est_per_copy * max(0, nc - placed.get(repo, 0))
    # actual archived bytes MUST be scoped to `repo_ids` — an archived row for a repo no longer in the
    # selection (e.g. after re-curating the cart) would otherwise inflate the compressed footprint.
    if repo_ids:
        ph = ",".join(["?"] * len(repo_ids))
        actual_stored = con.execute(
            f"SELECT coalesce(sum(stored_bytes), 0) FROM archived WHERE repo_id IN ({ph})",
            repo_ids).fetchone()[0]
    else:
        actual_stored = 0
    return uncompressed, actual_stored + est_remaining, int(n_must)


def gate_tier(compressed: int, capacity: int) -> str:
    """#38 gate tier from the compressed footprint vs capacity: ok | soft | warn | prevent."""
    if not capacity:
        return "ok"
    if compressed >= capacity:
        return "prevent"
    r = compressed / capacity
    return "warn" if r >= _GATE_WARN else ("soft" if r >= _GATE_SOFT else "ok")


def _numbers(con, plan_id, repo_ids, count_key) -> dict:
    unc, comp, n_must = _footprint(con, plan_id, repo_ids)
    cap = capacity(con, plan_id)
    p = get(con, plan_id)
    return {
        "plan_id": plan_id,
        "capacity_mode": p["capacity_mode"] if p else "guaranteed",
        "provisioning": p["provisioning"] if p else "uncompressed",
        "uncompressed": unc, "compressed": comp, "capacity": cap,
        count_key: len(repo_ids), "n_must": n_must,
        "n_drives": len(plan_drive_labels(con, plan_id)),
        "uncompressed_pct": round(unc / cap, 4) if cap else 0.0,
        "compressed_pct": round(comp / cap, 4) if cap else 0.0,
        "over_uncompressed": bool(cap and unc > cap),
        "over_compressed": bool(cap and comp > cap),
        "tier": gate_tier(comp, cap),
    }


def totals(con, plan_id) -> dict:
    """The three LIVE numbers over the FINALIZED selection — the fill's actual footprint (#36 bars, #37
    failsafe). Copy-aware: a must-have (numcopies≥2) counts every physical copy against capacity."""
    sel = [r[0] for r in con.execute(
        "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL").fetchall()]
    return _numbers(con, plan_id, sel, "n_selection")


def cart_totals(con, plan_id) -> dict:
    """The live numbers over the WHOLE cart (every selection row, finalized or not) — powers the
    graduated catalog gate (#38) so the operator sees the footprint climb + a prevent tier while
    BUILDING the set, before Finish commits it."""
    cart = [r[0] for r in con.execute("SELECT repo_id FROM selection").fetchall()]
    return _numbers(con, plan_id, cart, "n_cart")
