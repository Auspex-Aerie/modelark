# Annex payload migration plan — Grok CLI review 2

Date: 2026-09-13 America/New_York (completed 2026-09-14 UTC)
Planning pass: 2 of 3; pass 1 was Codex CLI.
Reviewer: installed Grok CLI 1.0.30 (04b7ffed98c6), default configured model (not independently reported).
Session: 01a09d4c-38e6-7b21-8e9c-13402d26f282
Exit: 0.
Scope: read-only local plan/source review; no web, subagents, tests or live archive/catalog changes.
Reviewed plan SHA-256: de463429e92435e80d21bf81ce0d81a89307f39eb596b03ab9975f4de619983e
Plan: [annex-payload-migration.md](../plans/annex-payload-migration.md).
Source checkout HEAD: 4c0c67a. Review line references refer to the reviewed input, before the amendments below.

## Disposition

ACCEPT for beginning Stage 0 disposable qualification, not implementation or live migration.
All three pass-1 P1 findings closed at design level. One P2 wording clarification requested.

After this review, the plan explicitly requires a catalog user_version greater than 8, migration from
both v7 and v8, and old-reader/writer refusal tests; it prohibits reusing SERIAL_REPAIR_CATALOG_VERSION.
The non-blocking ensure_library name correction was also applied. These direct requested clarifications
and review-status updates were not sent for another pass. The review below is preserved verbatim;
do not represent the post-review text as having received a third review.

## Invocation

```text
grok --cwd /home/phaze/PycharmProjects/modelark/claudedocs/operator-scratch/worktrees/codec-stage-d --permission-mode plan --no-subagents --disable-web-search --prompt-file /tmp/modelark-annex-plan-grok-review-2.txt --output-format plain
```

## Reviewer response

**ACCEPT** for beginning Stage 0 qualification only. Not an authorization to implement conversion, bump a live catalog, resume Fill, or treat untested Git/annex behavior as already proven.

This was one static plan/source pass of `docs/plans/annex-payload-migration.md` against `docs/reviews/annex-payload-plan-codex-1.md` and local code. No files were changed.

## Prior findings

**1. Maintenance obligation vs ordinary recovery — CLOSED at design level.**  
Sections D (157–189) and G (329–330) now require a catalog-visible obligation, created before mutation, bound to the captured owner/generation (including Drive 01’s existing dirty tuple and sessionless owners). Ordinary reconcile, including `_recover_owned_generation` and the sessionless branch, must return `MAINTENANCE_REQUIRED` and must not clear the obligation because inventory passed. Only the explicit closure transaction may satisfy the shared guard, which also covers admission, approval, sync/checkout, and `_publish_anchor_locked`. That is the missing invariant, not deferred adapter work.

Current code still has the hole the first review described; the plan no longer claims otherwise. `_recover_owned_generation` inventories then publishes a clean anchor (`drive_bootstrap.py` 746–765). Sessionless recovery does the same (939–959). `_require_complete_inventory` refuses missing claims and returns extras (288–298, 272–281). `_publish_anchor_locked` has no maintenance check (189–203). Those are implementation targets, not remaining design gaps.

**2. Map/ref protocol and returning-clone interception — CLOSED at design level.**  
Sections F (247–290), D (167–172), and G (333–334) now specify the new map lock and lock order, captured map/index/ref OIDs, the exact allowed tree delta, an annex-metadata allowlist, conflict policy, a durable receipt, interception **before** any ModelArk tree-changing sync, unclaimed Git copies without invented `archived`/`replicas` rows, and retry rules (expected-old retry, intended-new ack, any other ref/index advance → new reviewed plan). If interception of ModelArk paths cannot be shown, map retirement is disabled at the qualification gate.

Current propagation remains unsuitable for reuse, and the plan now says so: `fetch.run()` treats failed `annex sync` as a warning (1351–1360); `run_replica_tasks()` ignores the map sync return code (1710–1717); `register` also `annex sync`s on registration (569). `_reconcile_touched` documents that `git annex copy --to` does not create a worktree path (774–781). The map lock is explicitly new work.

**3. Atomic archived + replicas + revision cutover — CLOSED at design level.**  
Sections E (220–245), D (174–179, 191–193), F (303–308), and G (331–332) now require one `BEGIN IMMEDIATE` CAS of archived + replica-row-or-absence + source facts + operation/generation + planner revision; resume against that whole post-state; mixed/stale revision as a typed refusal; and explicit approval supersession, not a side effect of `bump_revision`. An interrupted batch keeps the central obligation and revoked approval.

That matches current readers/writers rather than inventing support. `graph_write()` commits graph mutation and `bump_revision` together (`proposal.py` 129–147); `bump_revision` itself does not supersede approvals (103–126). The existing composition pattern is serial repair: one TX with `_publish_anchor_locked`, `supersede_serial_repair_approvals`, optional `PRAGMA user_version=8`, then `bump_revision` (`drive_bootstrap.py` 543–557; `proposal.py` 1193–1214). Slice joins `archived` to `replicas.present` and treats only `present is False` as absent (`slice/catalog.py` 62–76; `slice/domain.py` 277–278). Live fetch/replica writers upsert `archived`, not `replicas`.

## Remaining P1 / P2 plan blockers

No remaining **P1**.

**P2 — schema fence must not reuse serial-repair v8.** Plan 164–165, 353, 389.  
`catalog_versions.py` 8–11: layout is 7; `{7, 8}` are supported; v8 only raises the reader floor and does not add tables. Serial repair stamps `user_version=8` in-place (`drive_bootstrap.py` 555). `db.connect()` and Slice refuse only versions **above** 8 (`core/db.py` 293–298; `slice/catalog.py` 18–37). Adding obligation tables without a new version, or calling that bump “v8”, would leave old binaries able to open the catalog and ignore the tables — the fail-open this plan is trying to close.  
**Amendment:** require a new `user_version` **greater than 8**, migrate both v7 and v8 catalogs, and treat “old binaries fail closed” as a test against current MAX=8 readers/writers (including Slice’s closed `{7,8}` map). Do not overload `SERIAL_REPAIR_CATALOG_VERSION`.

No other P1/P2 design holes. `register.init_library` (plan 50) is a name slip for `ensure_library` (`register.py` 104–118); not a blocker.

## Qualification vs unresolved flaws

These are already stop-gates for Stage 0/1, not reasons to reject the plan because the experiments have not run:

- **Duplicate-interval attributes** (102–108, 335–336). Parent `.gitattributes` still applies until retirement. A repo-local policy (not a neutral basename) must be proven on git-annex 8.20210223 and the newer supported version.
- **Portable ownership** (105–108). Specified: digest-bound manifest in a dedicated ref; filename/hash alone is not ownership; conflicting versions refuse.
- **Replica path readability** (115–120). `run_replica_tasks()` copies by key and mirrors `archived` (1646–1694) without creating the worktree path. Slice opens `{repo}/{stored_relpath}` (`slice/local_source.py` 108–132); authoritative restore refuses `git annex get` on `dedicated_local` (`restore.py` 51–60, 150–155). Stage 0/1 must prove a readable mapped path before copy evidence is published, or add guarded publication. Do not treat `replicas.present` as the live success bit: writers today do not maintain that table; preserve absence unless a real writer already does.
- **Evidence strength** (292–300). Inventory uses annex location logs / raw existence, not byte hashing (`drive_bootstrap.py` 192–270). Closure now separates converted-file hashes, unchanged presence, extras, and map/clone receipts.
- **Staging** is coherent: Stage 0 qualifies; Stage 1 fails closed before conversion is enabled; Stage 2 is apply/resume; Stage 3 is attended Drive 01. Live session IDs in the census are later-apply context, not Stage 0 work.

## Common cause and scope

The first review’s cause still holds: payload identity spans per-drive catalog rows, shared Git trees, and generation/approval authority. A private journal cannot police those planes. The amendment now states the shared contracts; remaining risk is Git/annex/schema qualification against them, not missing invariants.

Scope: Stage 0 is disposable repos plus a call-site/ref/schema audit. It does not convert Drive 01, resume Fill, rewrite history, or relax reconciliation.

## Permitted next step

Apply the one-line schema-version clarification if you want Stage 0 fixtures aimed at v9 rather than v8, then **begin Stage 0 qualification only**. Do not implement inspect/apply, enable conversion, migrate the live catalog, or treat a successful experiment as approval of live layout cutover. Keep the three-round stage budget; this pass does not reset it.

