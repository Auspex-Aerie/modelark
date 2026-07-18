# ModelArk fill pipeline and safety boundaries

The CLI and portal both run the same reconciled executor in `modelark/fill.py`. Durable catalog facts
are the only completion truth: ModelArk never persists a work queue or infers success from an
`.incomplete` file. At every drive-batch boundary it derives missing work again from canonical
manifests, exact per-drive `archived` rows, plan policy, and drive facts (DEC-045).

The active Plan owns a fixed drive set and a capacity mode. `guaranteed` uses raw-bounded admission;
`compression_aware` admits against expected stored bytes plus a safety margin. The API and CLI retain
the old `provisioning` / `uncompressed` / `compressed` spellings as deprecated one-release aliases,
but the schema and operator surfaces are canonical.

## Control flow

```text
CLI / portal
    |
    v
resolve active Plan + fixed plan_drives
    |
    v
validate configured Hub credential
    | HTTP 401
    +------------------------------> Gate A typed auth terminal
    |
    v
reconcile durable facts ──> exact requirements ──> unassigned intents
    |                                               |
    |                                               v
    +──────────────────────────────────────> tiered_v1 placement
                                                    |
                                                    v
                                             capacity ledger
                                                    |
                      +-----------------------------+-----------------------------+
                      | feasible                                                  | not feasible
                      v                                                           v
         ready tasks (dependency-safe)                              Gate B typed terminal
                      |                                             BLOCKED before writes, or
                      v                                             plan-capacity-stop after progress
         choose highest-priority drive
         and pin it until its ready batch drains
                      |
          +-----------+-----------+
          |                       |
          v                       v
  FETCH exact missing files   REPLICATE exact annex keys
  - stale-row check/file      - stale-row check/key
  - live per-file preflight   - copy from chosen safe source
  - same-filesystem stage     - whereis target UUID proof
  - download + SHA check      - only then record target row
  - bounded compression
  - atomic publish + annex
  - archived row
          |                       |
          +-----------+-----------+
                      |
                      v
            discard ephemeral tasks and reconcile again
```

Satisfied copies reserve zero bytes. A partial copy reserves only its missing files. Candidate-specific
budgets are calculated before deterministic placement, so a partial target and a fresh target may
have different costs without changing the definition of completion.

## Scheduling and restart behavior

- Fetch work runs before replica work globally. Within fetch work, partial resumes and giant models
  retain the DEC-034 priority. The scheduler pins the selected drive until its ready work is exhausted,
  avoiding USB hot-swap thrash.
- Each fetch receives an explicit task manifest. The fetch layer cannot broaden it to every file in a
  repository. Before every file, it rechecks the target row and current usable capacity.
- Download retries write beneath a deterministic private staging directory on the target filesystem,
  never through a final worktree path. Original-byte verification and any compression canary finish
  before atomic publication. Staging is ephemeral and never counts as task completion.
- A dangling worktree link may be replaced only when `git annex lookupkey` proves it is an annex
  placeholder in that archive. Identical verified bytes are reused. Arbitrary links, mismatched files,
  directories, cross-filesystem staging, and local I/O errors fail closed without network cooldowns.
- A process crash loses only in-memory placement. On restart, already-recorded files disappear from
  the next derived task and only the missing suffix remains.
- Replica work is per requirement and source. A successful `git annex copy` is insufficient evidence:
  `git annex whereis --key` must show the registered target UUID before the catalog records that file.
- Reconciliation uses one bulk manifest query and, in installed CLI/portal runs, a dedicated read-only
  SQLite connection. File writes retain the existing short shared-connection lock.
- A repository-specific gated response is not a network retry. The first response raises a retained
  notice while other tasks continue. The second raises a five-minute retry/skip prompt with the fixed
  Hugging Face repository link. Retry reconciles immediately; skip or timeout parks that repository
  for this session and records a typed Verify follow-up without changing selection.

## Capacity and transient workspace

The drive ledger charges durable missing bytes once plus the maximum transient workspace of the
single-threaded tasks assigned to that drive. It does not sum workspace across files or replica tasks.
Admission uses live free-space evidence for mounted drives and the last catalog snapshot for offline
drives; execution always performs a fresh per-file live check on the drive being written.

StreamZNN, whole-ZipNN, and zstd enforce their declared output caps before the write that would cross
the bound. Poorly compressing data falls back to inert raw storage. The measured BF16 and copied-catalog
entry gates are recorded in `docs/capacity-evidence.md`.

## Terminal states

| State | Meaning | Recovery |
|---|---|---|
| `blocked` | Gate A/B refused the run before writes: invalid configured credential, unavailable CLI drive, policy blocker, invariant failure, or infeasible committed capacity | Follow the typed actions, then start again |
| `plan-capacity-stop` | Live capacity changed after useful progress and the remaining graph is no longer feasible | Add eligible capacity or trim/re-plan; completed rows remain safe |
| `paused` | Useful work is durable but a typed acquisition conflict, download window, or offline source/target prevents continued copy #2 work (DEF-022) | Follow typed recovery actions, then explicitly resume |
| `error` | A bounded transient fetch retry, annex-key proof, or graph invariant failed | Inspect the typed evidence and logs; correct the fault before retrying |
| `stopped` | Operator requested Stop | Start again; reconciliation resumes missing files |
| `done` | Every committed requirement is satisfied, or all remaining tasks are explicitly parked gated-access follow-ups (`PLAN_COMPLETE_WITH_FOLLOWUPS`) | Resolve any access follow-ups and Start again |

Gate B remains whole-plan for this release: a structurally undersized replica tier blocks otherwise
feasible primary work before the run begins. That conservative admission choice is distinct from an
offline drive or mid-run capacity change, both of which are resumable. A partial-continuation mode is
deferred to a separately reviewed operator contract.

Every non-clean terminal is atomically persisted as a versioned, typed `last_fill.json` payload with
its code, gate, evidence, actions, and bounded affected-item list. The Fill page and page-load path use
the same terminal classifier and show the prominent modal immediately; acknowledgement clears it.

## Safety decisions

- DEC-019: three fill gates and refusal before unsafe work.
- DEC-022: bounded compression and raw fallback.
- DEC-026/034: write probes, re-plan boundaries, and drive-affine priority.
- DEC-031 / DEF-022: safe copy #1 plus offline copy #2 is a resumable pause, not missing-data error.
- DEC-032 / DEF-023: durable, prominent operator terminal reporting.
- DEC-040 / DEF-011: pickle-only is refused by default; an explicit private policy may archive it as
  inert raw bytes, but ModelArk never loads it and deeper scanning remains deferred.
- DEC-045 / INC-014: derive exact missing work from durable facts and admit it through one ledger.
- DEC-046 / INC-018 / INC-019: validate configured credentials before work; stage and verify on the
  target filesystem before proof-driven atomic publication; never retry local namespace/I/O failures
  as transient network stalls.
- DEC-047 / INC-020 / DEF-010: gated repository access is a bounded operator decision and typed
  follow-up, not a generic fetch failure or an archive-integrity claim.
