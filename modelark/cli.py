"""modelark command-line interface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from modelark.core import db
from modelark import discover, verify, wishlist

EXPORT_TABLES = ["models", "files", "verifications", "drives", "replicas"]


def _humanize(n: int | None) -> str:
    if not n:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f}{u}"
        f /= 1024
    return f"{f:.1f}PB"


def cmd_discover(args):
    if args.walk or args.org:
        wl = wishlist.load()
        orgs = args.org or wl["always_include"]["orgs"]
        cats = wl["scope"]["include_categories"]
        print(f"Walking {len(orgs)} org(s); in-scope categories: {', '.join(cats)}")
        stats, path, excluded = discover.discover_orgs(
            orgs, cats, limit_per_org=args.limit_per_org, exclusions_path=args.exclusions)
        cataloged = sum(s["cataloged"] for s in stats.values())
        reasons: dict[str, int] = {}
        for r in excluded:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        print(f"\nCataloged {cataloged} in-scope models across {len(orgs)} org(s).")
        print(f"Excluded {len(excluded)} repos -> {path}")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"   {n:>5}  {reason}")
    if args.top:
        discover.discover_top(args.top, task=args.task)
    if args.repo:
        discover.discover_repos(args.repo)
    if not (args.walk or args.org or args.top or args.repo):
        raise SystemExit("nothing to discover: pass --walk, --org, --repo, and/or --top")


def cmd_verify(args):
    con = db.connect()
    try:
        if args.repo:
            ids = args.repo
        elif args.all:
            ids = [r[0] for r in con.execute(
                "SELECT repo_id FROM models WHERE status != 'skip' ORDER BY params_b").fetchall()]
        else:
            raise SystemExit("pass --repo or --all")
        verify.verify_many(ids, con=con)
    finally:
        con.close()


def cmd_ls(args):
    con = db.connect(read_only=True)
    try:
        rows = con.execute("""
            SELECT repo_id, params_b, category, variant, total_size_bytes, license
            FROM models
            WHERE status != 'skip'
            ORDER BY params_b DESC NULLS LAST
        """).fetchall()
        by_cat = con.execute("""
            SELECT coalesce(category,'(none)') AS category, count(*) n
            FROM models WHERE status != 'skip' GROUP BY 1 ORDER BY n DESC
        """).fetchall()
    finally:
        con.close()
    print(f"{'repo_id':52} {'params':>8} {'category':>15} {'variant':>9} {'size':>9}")
    print("-" * 100)
    for repo, params, category, variant, size, lic in rows:
        pstr = f"{params:.1f}B" if params else "-"
        print(f"{repo[:52]:52} {pstr:>8} {str(category or '-'):>15} {str(variant or '-'):>9} {_humanize(size):>9}")
    print(f"\n{len(rows)} models. By category: " +
          ", ".join(f"{c}={n}" for c, n in by_cat))


def cmd_query(args):
    con = db.connect(read_only=True)
    try:
        cur = con.execute(args.sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        if cols:
            print(" | ".join(cols))
            print("-" * min(100, sum(len(c) + 3 for c in cols)))
        for r in rows:
            print(" | ".join("" if v is None else str(v) for v in r))
        print(f"\n({len(rows)} row{'' if len(rows) == 1 else 's'})")
    finally:
        con.close()


def cmd_export(args):
    """Dump the catalog to JSONL — the diffable, git-committed source of truth. SQLite has no
    DuckDB `COPY … (FORMAT json)`, so we stream rows and json-encode each (one object per line)."""
    export_dir = db.CATALOG_DIR / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    con = db.connect(read_only=True)
    try:
        for t in EXPORT_TABLES:
            out = export_dir / f"{t}.jsonl"
            cur = con.execute(f"SELECT * FROM {t} ORDER BY 1")
            cols = [d[0] for d in cur.description]
            n = 0
            with open(out, "w") as fh:
                for row in cur:
                    fh.write(json.dumps(dict(zip(cols, row)), default=str) + "\n")
                    n += 1
            print(f"  exported {n:>6} rows -> {out}")
    finally:
        con.close()


def cmd_recompute(args):
    """Re-derive variant + fix no-weight categories from stored metadata (no re-walk)."""
    from modelark import formats
    con = db.connect()
    try:
        rows = con.execute("SELECT repo_id, tags, category, status FROM models").fetchall()
        wc = dict(con.execute(
            "SELECT repo_id, count(*) FILTER (WHERE format IN "
            "('safetensors','gguf','pytorch','onnx','mlx')) FROM files GROUP BY repo_id").fetchall())
        nvar = ncat = nskip = 0
        con.execute("BEGIN")
        for repo_id, tags, category, status in rows:
            variant = formats.parse_variant(repo_id, json.loads(tags) if tags else [])
            new_cat, new_status = category, status
            if status != "skip" and wc.get(repo_id, 0) == 0:
                if "tokenizer" in repo_id.lower():
                    new_cat = "accessory"
                    ncat += 1
                else:
                    new_status = "skip"   # API-only / code-only / framework port
                    nskip += 1
            con.execute("UPDATE models SET variant=?, category=?, status=? WHERE repo_id=?",
                        [variant, new_cat, new_status, repo_id])
            nvar += 1
        con.execute("COMMIT")
        print(f"recomputed variant: {nvar} models | {ncat} -> accessory | {nskip} -> skip (no weights)")
    finally:
        con.close()


def cmd_serve(args):
    from modelark.web import server
    server.serve(port=args.port, open_browser=not args.no_open, resume=args.resume)


def cmd_fetch(args):
    from modelark import fetch
    fetch.run(dest=args.dest, drive_label=args.drive, limit=args.limit,
              repos=args.repo, dry_run=args.dry_run, max_24h_gb=args.max_24h_gb)


def cmd_restore(args):
    from modelark import restore
    con = db.connect(read_only=True)
    try:
        for repo_id in args.repo:
            try:
                result = restore.restore_repo(con, repo_id, args.dest)
            except restore.RestoreError as exc:
                raise SystemExit(f"restore failed: {exc}") from exc
            retrieved = result["annex_retrievals"]
            suffix = f"; {retrieved} retrieved through git-annex" if retrieved else ""
            print(f"restored {repo_id}: {result['n_files']} verified file(s) -> {result['path']}{suffix}")
            for warning in result["warnings"]:
                print(f"  warning: {warning['file']}: {warning['detail']}")
    finally:
        con.close()


def cmd_repair_hashes(args):
    from modelark import hash_repair
    con = db.connect(read_only=not args.apply)
    try:
        try:
            result = hash_repair.repair_hashes(con, args.repo, apply=args.apply)
        except hash_repair.HashRepairError as exc:
            raise SystemExit(f"hash repair failed: {exc}") from exc
    finally:
        con.close()

    action = "applied" if args.apply else "would repair"
    for item in result["repairs"]:
        print(
            f"  {action}: {item['repo_id']}/{item['rfilename']} on {item['drive_label']} "
            f"sha256={item['sha256']} ({item['bytes']} bytes; {item['evidence']})"
        )
    for item in result["errors"]:
        subject = item["repo_id"]
        if item["rfilename"]:
            subject += "/" + item["rfilename"]
        if item["drive_label"]:
            subject += " on " + item["drive_label"]
        print(f"  blocked: {subject}: {item['code']}: {item['detail']}", file=sys.stderr)
    print(
        f"legacy hash evidence: {result['already_verifiable']} already verifiable, "
        f"{result['missing_evidence']} missing, {len(result['repairs'])} provable, "
        f"{len(result['errors'])} blocked"
    )
    if result["backup"]:
        print(f"catalog backup: {result['backup']}")
    if result["errors"]:
        raise SystemExit(1)


def cmd_library_init(args):
    from modelark import register
    path = register.ensure_library(Path(args.path).expanduser() if args.path else None)
    print(f"library map ready at {path}")


def cmd_library_nas_add(args):
    from modelark import register
    r = register.register_nas(remote=args.remote, label=args.label, role=args.role)
    print(f"recorded {r['label']} (role={r['role']}) — special remote '{r['remote']}', uuid {r['uuid']}")
    print(f"  {r['directory']}  ·  free {r['free']/1e12:.2f} TB / {r['total']/1e12:.2f} TB")
    print(f"  plan: {r['plan']}  (added to the active plan's set — #34)")


def cmd_library_plan(args):
    from modelark import librarian, fetch, fill, plan, reconcile
    if getattr(args, "explain", False):
        if args.apply:
            raise SystemExit("--explain is read-only and cannot be combined with --apply")
        con = db.connect(read_only=True)
        try:
            prow = plan.active(con)
            if prow is None:
                raise SystemExit("no active plan; select a plan before requesting a shadow explanation")
            print(json.dumps(reconcile.shadow_report(
                con, prow["plan_id"], repo_ids=args.repo,
                capacity_mode=prow["capacity_mode"],
            ), default=str, sort_keys=True))
        finally:
            con.close()
        return
    # Planning is a diagnostic until --apply is explicit. In particular, do not let an apparently
    # read-only JSON projection replay/bootstrap the catalog while an operator is qualifying a
    # migrated runtime (RFC-001). Execution retains one RW connection for plan → apply → check.
    read_only = not args.apply
    con = db.connect(read_only=read_only)
    try:
        prow = plan.active(con)
        if prow is None:
            if read_only:
                raise SystemExit("no active plan; create/select a plan before requesting placement")
            prow = plan.bootstrap(con)
        pid, capacity_mode = prow["plan_id"], prow["capacity_mode"]
        if getattr(args, "json", False):
            print(json.dumps(librarian.plan_view(
                con, repos=args.repo, plan_id=pid, capacity_mode=capacity_mode
            ), default=str))
            return
        p = librarian.plan_placements(
            con, repos=args.repo, plan_id=pid, capacity_mode=capacity_mode
        )
        dinfo = {d["label"]: d for d in librarian.drives(con, pid)}

        hdr = (
            f"Placement plan — plan '{pid}' (capacity mode={capacity_mode}) — "
            f"{p['n_planned']} model(s)"
        )
        if p["n_done"]:
            hdr += f" ({p['n_done']} already archived)"
        if p["n_must"]:
            hdr += f", {p['n_must']} must-have(s)"
        print(hdr + ":\n")

        def _tier(name, tier):
            if not tier["drives"]:
                print(f"  {name}: (no drives with this role)")
                return
            print(f"  {name}:")
            for label in sorted(tier["assign"]):
                items = tier["assign"][label]
                if not items:
                    continue
                d = dinfo.get(label, {})
                print(f"    {label:12} {len(items):>4} models · {sum(i['size'] for i in items)/1e12:6.2f} TB · "
                      f"{tier['rem'][label]/1e12:5.2f} TB free after  (free {d.get('free',0)/1e12:.2f} TB − "
                      f"{d.get('headroom',0)/1e9:.0f} GB headroom)")

        _tier("PRIMARY", p["primary"])
        if p["freed"]:
            print(f"    free/unused primary drives: {', '.join(p['freed'])}")
        _tier("REPLICA", p["replica"])

        print("\n  Advisories:")
        icon = {"error": "🔴", "warn": "🟡", "ok": "✅", "info": "·"}
        for a in p["advisories"] or [{"level": "info", "msg": "(none)"}]:
            print(f"    {icon.get(a['level'], '·')} {a['msg']}")

        if args.apply:
            # Same backend as the portal's Fill worker (fill.execute): GATE-B → GATE-A → PRIMARY →
            # REPLICA → GATE-C. The CLI ctx prints each narration line, never locks (single writer),
            # and never self-cancels. fill.execute returns a result rather than raising, so the exit
            # code is decided here.
            print()
            ctx = fetch.RunCtx(
                con=con,
                on_progress=lambda ev: print(ev["say"]) if "say" in ev else None,
                read_connection_factory=lambda: db.connect(read_only=True),
            )
            res = fill.execute(ctx, plan_id=pid, max_24h_gb=args.max_24h_gb, repo_scope=args.repo, guided=False)
            if not res["ok"]:
                raise SystemExit(1)
    finally:
        con.close()


def _tb(n):
    return f"{(n or 0) / 1e12:.2f} TB"


def cmd_plan(args):
    """The first-class Plan (#33): identity + fixed drive set + the three live capacity numbers."""
    from modelark import plan
    sub = args.plan_cmd
    read_only = sub in ("list", "show")
    con = db.connect(read_only=read_only)
    try:
        # Mutations retain the historical bootstrap convenience. List/show must reflect durable
        # facts without upserting plan membership merely because an operator inspected it.
        if not read_only:
            plan.bootstrap(con)
        if sub == "list":
            for p in plan.list_plans(con):
                t = plan.totals(con, p["plan_id"])
                mark = "*" if p["is_active"] else " "
                print(f"{mark} {p['plan_id']:12} {(p['name'] or ''):16} mode={p['capacity_mode']:18} "
                      f"drives={len(p['drives']):>2}  cap={_tb(t['capacity'])}  "
                      f"raw={_tb(t['uncompressed'])}  expected={_tb(t['compressed'])}")
        elif sub == "show":
            pid = args.plan or (plan.active(con) or {}).get("plan_id")
            if not pid:
                raise SystemExit(
                    "no active plan; create/select a plan before running show"
                )
            if plan.get(con, pid) is None:
                raise SystemExit(f"no such plan: {pid}")
            p, t = plan.get(con, pid), plan.totals(con, pid)
            print(f"Plan {p['plan_id']} ({p['name']}) — {'ACTIVE' if p['is_active'] else 'inactive'}, "
                  f"capacity mode={p['capacity_mode']}")
            print(f"  annex:  {p['annex_root']}")
            print(f"  drives ({len(p['drives'])}): {', '.join(p['drives']) or '(none)'}")
            print(f"  raw forecast           : {_tb(t['uncompressed'])}   "
                  f"({t['n_selection']} models, {t['n_must']} must-have · copy-aware)")
            print(f"  expected stored forecast: {_tb(t['compressed'])}   "
                  "(archived-so-far + estimate for the rest)")
            print(f"  fleet capacity         : {_tb(t['capacity'])}   "
                  f"({t['uncompressed_pct']*100:.0f}% raw / "
                  f"{t['compressed_pct']*100:.0f}% expected)")
            if t["over_uncompressed"]:
                print("  🔴 Raw forecast exceeds capacity — add a drive or trim the set.")
            elif t["over_compressed"]:
                print("  🟡 Expected stored forecast exceeds capacity — compression-aware admission fails.")
        elif sub == "create":
            raw_mode = args.capacity_mode or args.provisioning or "guaranteed"
            if args.provisioning is not None or raw_mode in ("uncompressed", "compressed"):
                print(
                    "warning: provisioning/uncompressed/compressed is deprecated; use "
                    "--capacity-mode guaranteed|compression_aware",
                    file=sys.stderr,
                )
            p = plan.create(con, args.id, name=args.name, capacity_mode=raw_mode)
            print(f"created plan {p['plan_id']} (capacity mode={p['capacity_mode']})")
        elif sub == "select":
            p = plan.set_active(con, args.id)
            print(f"active plan → {p['plan_id']}")
        elif sub in ("capacity-mode", "provisioning"):
            pid = args.plan or (plan.active(con) or {}).get("plan_id")
            if sub == "provisioning" or args.mode in ("uncompressed", "compressed"):
                print(
                    "warning: plan provisioning and its legacy values are deprecated; use "
                    "plan capacity-mode guaranteed|compression_aware",
                    file=sys.stderr,
                )
            p = plan.set_capacity_mode(con, pid, args.mode)
            print(f"plan {p['plan_id']} capacity mode → {p['capacity_mode']}")
    finally:
        con.close()


def cmd_protect(args):
    con = db.connect()
    try:
        for rid in args.repo:
            con.execute("UPDATE models SET numcopies=? WHERE repo_id=?", [args.numcopies, rid])
        total = con.execute("SELECT count(*) FROM models WHERE coalesce(numcopies,1) >= 2").fetchone()[0]
    finally:
        con.close()
    print(f"set numcopies={args.numcopies} on {len(args.repo)} repo(s); "
          f"{total} model(s) now must-have (numcopies>=2 → a replica-tier 2nd copy).")


def cmd_drive_register(args):
    from modelark import register
    r = register.register_drive(dev=args.dev, label=args.label, mount=args.mount,
                                format_fs=args.format_fs, location=args.location,
                                library=args.library, dry_run=args.dry_run, role=args.role,
                                skip_smart=args.skip_smart,
                                confirm_format=args.confirm_format)
    if args.dry_run:
        b = r["smart"]
        print(f"DRY RUN — {args.dev}: {b['model']} {b['serial']}")
        print(f"  SMART verdict={b['verdict']}  realloc={b['reallocated']} "
              f"pending={b['pending']} offline_unc={b['offline_uncorrectable']} "
              f"poh={b['power_on_hours']}h passed={b['smart_passed']}")
        print(f"  would register as '{args.label}' (format={args.format_fs}) and clone the map onto it.")
        if b["verdict"] == "reject":
            print("  ⚠️  verdict=reject — real registration would refuse this drive.")
        return
    print(f"registered {r['label']}: {r['model']} {r['serial']}  health={r['health']}")
    print(f"  archive:    {r['archive']}")
    print(f"  annex uuid: {r['annex_uuid']}")
    print(f"  map:        {r['library']}")
    print(f"  plan:       {r['plan']}  (drive added to the active plan's set — #34)")
    if r["health"] == "unchecked":
        print("  ⚠ health UNCHECKED — SMART was not read (USB bridge / --skip-smart, INC-002). "
              "Verify this drive's health externally before trusting it with irreplaceable copies.")


def cmd_drive_list(args):
    from modelark import register
    con = db.connect(read_only=True)
    try:
        drives = register.list_drives(con)
    finally:
        con.close()
    if not drives:
        print("no drives registered — run: modelark drive register --dev /dev/sdX --label lib-01")
        return
    for d in drives:
        cap, free = _humanize(d["capacity_bytes"]), _humanize(d["free_bytes"])
        print(f"{d['drive_label']:12} {str(d['hw_model'] or '-'):26} {str(d['serial'] or '-'):16} "
              f"{str(d['health'] or '-'):8} {free}/{cap} free  {d['physical_location'] or ''}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="modelark", description="Catalog & verify open model weights.")
    p.add_argument("--data-dir", type=Path,
                   help="writable catalog/runtime-data directory (default: platform user-data dir)")
    p.add_argument("--state-dir", type=Path,
                   help="writable logs/state directory (default: platform user-state dir)")
    p.add_argument("--config", type=Path,
                   help="wishlist/config YAML (default: user config, source checkout, packaged default)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="record HF model metadata in the catalog")
    d.add_argument("--repo", action="append", help="explicit repo id (repeatable)")
    d.add_argument("--top", type=int, help="discover top-N by downloads")
    d.add_argument("--task", default="text-generation", help="pipeline tag for --top")
    d.add_argument("--walk", action="store_true",
                   help="walk every configured org, filtered by architecture-derived categories")
    d.add_argument("--org", action="append",
                   help="walk specific org(s), scope-filtered (repeatable)")
    d.add_argument("--limit-per-org", type=int, default=40,
                   help="max repos per org, by downloads (default 40)")
    d.add_argument("--exclusions", help="path for skipped-repos JSONL (default catalog/exclusions/)")
    d.set_defaults(func=cmd_discover)

    v = sub.add_parser("verify", help="Tier A structural verification (no download)")
    v.add_argument("--repo", action="append", help="explicit repo id (repeatable)")
    v.add_argument("--all", action="store_true", help="verify every catalogued model")
    v.set_defaults(func=cmd_verify)

    ls = sub.add_parser("ls", help="list catalogued models")
    ls.set_defaults(func=cmd_ls)

    q = sub.add_parser("query", help="run raw SQL against the catalog")
    q.add_argument("sql")
    q.set_defaults(func=cmd_query)

    e = sub.add_parser("export", help="export catalog to JSONL for git")
    e.set_defaults(func=cmd_export)

    rc = sub.add_parser("recompute", help="re-derive variant/category from stored metadata (no re-walk)")
    rc.set_defaults(func=cmd_recompute)

    pr = sub.add_parser("protect", help="mark model(s) must-have (numcopies=2) → a replica-tier 2nd copy")
    pr.add_argument("--repo", action="append", required=True, help="repo id (repeatable)")
    pr.add_argument("--numcopies", type=int, choices=[1, 2], default=2,
                    help="copies to require (default 2; 1 = unprotect)")
    pr.set_defaults(func=cmd_protect)

    pl = sub.add_parser("plan", help="the first-class Plan: drive set + live capacity evidence")
    plsub = pl.add_subparsers(dest="plan_cmd", required=True)
    pls = plsub.add_parser("list", help="list plans (active marked *) with their three live numbers")
    pls.set_defaults(func=cmd_plan)
    plsh = plsub.add_parser("show", help="show a plan's drives + raw/expected forecasts/capacity")
    plsh.add_argument("--plan", help="plan id (default: the active plan)")
    plsh.set_defaults(func=cmd_plan)
    plc = plsub.add_parser("create", help="create a new plan")
    plc.add_argument("--id", required=True, help="plan slug, e.g. ark")
    plc.add_argument("--name", help="display name")
    mode_choices = ["guaranteed", "compression_aware", "uncompressed", "compressed"]
    create_modes = plc.add_mutually_exclusive_group()
    create_modes.add_argument(
        "--capacity-mode", choices=mode_choices,
        help="guaranteed (default) | compression_aware; legacy values warn",
    )
    create_modes.add_argument(
        "--provisioning", choices=mode_choices,
        help="deprecated alias for --capacity-mode",
    )
    plc.set_defaults(func=cmd_plan)
    plse = plsub.add_parser("select", help="set the active plan")
    plse.add_argument("--id", required=True, help="plan slug to activate")
    plse.set_defaults(func=cmd_plan)
    plm = plsub.add_parser("capacity-mode", help="set guaranteed or compression-aware admission")
    plm.add_argument("mode", choices=mode_choices)
    plm.add_argument("--plan", help="plan id (default: the active plan)")
    plm.set_defaults(func=cmd_plan)
    plp = plsub.add_parser("provisioning", help="deprecated alias for capacity-mode")
    plp.add_argument("mode", choices=mode_choices)
    plp.add_argument("--plan", help="plan id (default: the active plan)")
    plp.set_defaults(func=cmd_plan)

    sv = sub.add_parser("serve", help="run the local selection web app (reads/writes the catalog)")
    sv.add_argument("--port", type=int, default=8077)
    sv.add_argument("--no-open", action="store_true", help="don't auto-open a browser")
    sv.add_argument("--resume", action="store_true",
                    help="auto-resume the fill on boot if work remains (for the supervised systemd service, DEC-023)")
    sv.set_defaults(func=cmd_serve)

    ft = sub.add_parser("fetch", help="download the finalized wishlist onto a drive")
    ft.add_argument("--dest", help="explicit archive dir override (default: resolved from --drive)")
    ft.add_argument("--drive", help="registered drive label (e.g. drive-01) → its on-drive archive dir")
    ft.add_argument("--limit", type=int, help="only fetch the first N models")
    ft.add_argument("--repo", action="append", help="explicit repo(s) instead of the finalized set")
    ft.add_argument("--dry-run", action="store_true", help="show the plan without downloading")
    ft.add_argument("--max-24h", dest="max_24h_gb", type=float, default=1000,
                    help="stop at the next repo boundary if >N GB were downloaded in the last 24h "
                         "(default 1000 = 1 TB; 0 disables)")
    ft.set_defaults(func=cmd_fetch)

    rs = sub.add_parser("restore", help="restore an archived model to its original Hugging Face layout")
    rs.add_argument("--repo", action="append", required=True, help="archived repo id (repeatable)")
    rs.add_argument("--dest", type=Path, required=True,
                    help="output root; each model is restored below <dest>/<org>/<model>")
    rs.set_defaults(func=cmd_restore)

    rh = sub.add_parser(
        "repair-hashes",
        help="audit legacy restore hashes; validate Git-tracked bytes before optional backfill",
    )
    rh.add_argument("--repo", action="append", help="limit to an archived repo (repeatable)")
    rh.add_argument(
        "--apply", action="store_true",
        help="back up the catalog and apply every provable repair (default is read-only dry-run)",
    )
    rh.set_defaults(func=cmd_repair_hashes)

    lib = sub.add_parser("library", help="the central git-annex map repo")
    libsub = lib.add_subparsers(dest="library_cmd", required=True)
    li = libsub.add_parser("init", help="create the map repo if absent")
    li.add_argument("--path", help="map repo path (default ~/modelark-library)")
    li.set_defaults(func=cmd_library_init)
    na = libsub.add_parser("nas-add", help="record a git-annex directory special remote (NAS) as a target")
    na.add_argument("--remote", default="nas", help="special-remote name (default: nas)")
    na.add_argument("--label", default="drive-99", help="drive label (default: drive-99)")
    na.add_argument("--role", choices=["primary", "replica"], default="replica")
    na.set_defaults(func=cmd_library_nas_add)
    lp = libsub.add_parser("plan", help="plan model→drive placement across the fleet (consolidate + replica tier)")
    lp.add_argument("--repo", action="append", help="plan specific repo(s) instead of the finalized set")
    lp.add_argument("--apply", action="store_true", help="execute the plan (fetch per drive)")
    lp.add_argument("--json", action="store_true", help="emit the plan as JSON (portal Fill tab / scripting)")
    lp.add_argument("--explain", action="store_true",
                    help="read-only DEC-045 graph/ledger + legacy comparison JSON")
    lp.add_argument("--max-24h", dest="max_24h_gb", type=float, default=1000,
                    help="24h download cap in GB across the fleet (default 1000 = 1 TB; 0 disables)")
    lp.set_defaults(func=cmd_library_plan)

    dr = sub.add_parser("drive", help="register & list archive drives")
    drsub = dr.add_subparsers(dest="drive_cmd", required=True)
    reg = drsub.add_parser("register", help="qualify (SMART) + git-annex init + record a drive")
    reg.add_argument("--dev", required=True, help="block device, e.g. /dev/sdb")
    reg.add_argument("--label", required=True, help="drive label, e.g. lib-01")
    reg.add_argument("--mount", help="existing mountpoint (else auto-mount to /mnt/<label>)")
    reg.add_argument("--format", dest="format_fs", choices=["ext4", "xfs"],
                     help="reformat the device first (DESTRUCTIVE)")
    reg.add_argument("--confirm-format", metavar="DEVICE",
                     help="required with --format: repeat the exact --dev value after reviewing --dry-run")
    reg.add_argument("--location", help="physical location note, e.g. 'shelf box A slot 3'")
    reg.add_argument("--library", help="map repo path (default ~/modelark-library)")
    reg.add_argument("--dry-run", action="store_true", help="qualify + show the plan, change nothing")
    reg.add_argument("--role", choices=["primary", "replica"], default="primary",
                     help="primary (bin-packed working set) | replica (holds must-have 2nd copies) — DEC-014")
    reg.add_argument("--skip-smart", action="store_true",
                     help="skip SMART qualification (USB bridge won't pass SMART / INC-002) — registers "
                          "with health='unchecked'; verify the drive's health externally")
    reg.set_defaults(func=cmd_drive_register)
    dl = drsub.add_parser("list", help="list registered drives")
    dl.set_defaults(func=cmd_drive_list)

    args = p.parse_args(argv)
    if args.data_dir is not None or args.state_dir is not None:
        db.configure(args.data_dir, args.state_dir)
    if args.config is not None:
        wishlist.configure(args.config)
    args.func(args)


if __name__ == "__main__":
    main()
