# ModelArk — Fill pipeline & failsafes

How the guided fill (`fill.execute`) runs tier by tier, and where every gate, write-probe, and
failsafe plugs in. Companion to `modelark/fill.py` + `fetch.py`. Failsafe ledger: DEC-019
(the three gates), DEC-026 (re-plan + probes), DEC-030 (the first-class Plan), DEC-031 (the per-model
capacity failsafe + provisioning-aware fill + DEF-022 fail-soft replica).

**Now Plan-scoped (DEC-030/031).** The fill resolves the active **Plan** and runs against its FIXED
drive set (`plan_drives`) in the plan's PROVISIONING currency: `uncompressed` (default) packs bulk +
must-have copy#1 against RAW sizes (over-provision — never runs out); `compressed` bets on the ZipNN
estimate. A per-model `fits` check breaks a batch before an ENOSPC so the re-plan re-homes the overflow
or stops cleanly as **`plan-capacity-stop`** ("add a drive to the plan, re-run"). Copy#2 is always
sized COMPRESSED (it's a copy of a compressed blob). DEF-022: `run_replica` write-probes source+targets
and defers offline ones; GATE-C PAUSES (not errors) when copy#1 is safe and only copy#2 is deferred.

## Control flow

```
  modelark fill ──► fill.execute(guided)     [re-plans each pass, so it self-corrects as reality shifts]
  │
  ├─ GATE-A (CLI only) ─ every target mounted up front? ──no──► BLOCKED (refuse; no bytes fetched)
  │        │ yes
  ▼        ▼
┌─ PRIMARY TIER · copy#1 (bulk + must-have copy#1) · RE-PLAN LOOP ──────────────────────┐
│                                                                                        │
│   ┌─►(1) RE-PLAN from LIVE reality   (plan_placements · live disk free · DEC-025)      │
│   │        │                                                                           │
│   │   (2) GATE-B  does the plan fit the fleet? ──no──► BLOCKED  "add a drive, re-run"  │
│   │        │ yes                                                                       │
│   │   (3) work = repos still needing copy#1, grouped by drive (RAID → largest → …)     │
│   │        │                                                                           │
│   │        ├── none left? ──yes───────────────────────────────► REPLICA TIER          │
│   │        │ no                                                                        │
│   │   (4) no progress since last pass? ──yes──► 24h cap → PAUSED · else block bad repo │
│   │        │ no                                                                        │
│   │   (5) drive = highest-priority drive that has copy#1 work                          │
│   │        │                                                                           │
│   │   (6) _await_drive:  mounted AND write-probe OK? ──no──► await + "re-seat" prompt  │ ◄ boundary probe
│   │        │ yes                                            (loops until writable)     │
│   │   (7) fetch.run(drive, repos) — per shard:                                        │
│   │        │    download → sha256 vs HF → ZipNN + round-trip canary → store → record  │
│   │        │    repo error AND drive now unwritable? ──yes──► BAIL → await drive       │ ◄ #5 mid-batch probe
│   │        ▼                                                                           │
│   └────────( loop — reclaims a drive's freed slack, advances to the next drive )       │
└────────────────────────────────────────────────────────────────────────────────────────┘
   │  (all copy#1 down)
   ▼
┌─ REPLICA TIER · copy#2 of must-haves (a LOCAL copy, no HF re-download) ────────────────┐
│   await replica drive(s)                                                               │
│   run_replica:   git annex copy   FROM drive-00 (NAS)   TO   drive-04                  │
│                  └─ source + target write-probed; offline copies defer the tier softly │ ◄ DEC-031
└────────────────────────────────────────────────────────────────────────────────────────┘
   │
   ▼
  GATE-C  does every must-have hold its N copies? ──no──► copy#1 safe? PAUSE : ERROR
          └─ yes ─► DONE
```

## Drive topology & the copy#2 single point of failure

```
      copy#1 (bulk + must-have #1)                       copy#2 (must-have #2)
      lands on the primaries                             a LOCAL annex copy
          │                                                     ▲
          ▼                                                     │  git annex copy --to
    ┌──────────┐   ┌──────────┐  ┌──────────┐            ┌──────┴─────┐
    │ drive-00 │   │ drive-01 │  │ drive-02 │  · · ·      │  drive-04  │
    │ NAS·RAID │   │   USB    │  │   USB    │            │  REPLICA   │
    │ (iSCSI)  │   └──────────┘  └──────────┘            └────────────┘
    └────┬─────┘                                               ▲
         │                                                     │
         └──────────── SOURCE of EVERY copy#2 ─────────────────┘

  Copy#2 currently reads from the designated source. If it is offline, DEC-031 records a deferred
  target and pauses cleanly; it does not churn or misreport a completed replica set.
```

## Failsafes — what stops the fill, and how it recovers

| State | Trigger | Recovers by |
|-------|---------|-------------|
| **BLOCKED** (Gate-A/B) | targets unmounted up front / the selection exceeds the plan's fleet up front | mount / add a drive to the plan → re-plan |
| **plan-capacity-stop** (#37) | a drive filled MID-fill — the next model no longer fits any plan drive's live free (actual > estimate) | add a drive to the plan → re-run (resumable, nothing lost) |
| **PAUSED** | 24h download cap (DEC-027) **OR** copy#1 safe + copy#2 deferred by an offline replica source/target (DEF-022) | window frees / raise `download.max_24h_gb`; or re-seat the replica drive → re-run |
| **await drive** | a drive is unmounted OR mounted-but-unwritable — caught at the boundary (`_await_drive`) or mid-batch (`_dest_writable`) | re-seat the drive → auto-continues |
| **ERROR** (Gate-C) | a must-have is genuinely below its copy count (copy#1 itself missing) | fix the underlying failure → re-run |

Every non-DONE terminal is persisted (`catalog/last_fill.json`) and surfaced as a loud modal on portal
open until acknowledged (DEF-023 / DEC-032).

## Resolved gaps (were open last session)
- **DEF-022** ✅ (DEC-031) — `run_replica` now write-probes source + targets and DEFERS offline ones
  (no churn); GATE-C PAUSES on a copy#1-safe / copy#2-deferred run instead of red-erroring (INC-009).
- **DEF-021** ✅ (DEC-033) — the Verifier surfaces disruption "suspects" (raw-fallback / partial copy /
  disruption-window) + re-verifies archived copies on demand (record consistency + mounted-drive canary).
