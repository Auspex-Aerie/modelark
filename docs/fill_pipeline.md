# ModelArk fill pipeline and safety boundaries

The CLI and portal both run the same reconciled executor in `modelark/fill.py`. Durable catalog facts
are the only completion truth: ModelArk never persists a work queue or infers success from an
`.incomplete` file. At every drive-batch boundary it derives missing work again from canonical
manifests, exact per-drive `archived` rows, plan policy, and drive facts (DEC-045).

The active Plan owns a fixed drive set. Its legacy `provisioning` value is translated internally to a
capacity mode: `uncompressed` means guaranteed/raw-bounded admission and `compressed` means
compression-aware admission. The schema and operator-facing terminology are renamed separately in
Phase 4 so that migration risk cannot withhold the INC-014 correctness fix.

## Control flow

```text
CLI / portal
    |
    v
resolve active Plan + fixed plan_drives
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
  - download + SHA check      - whereis target UUID proof
  - bounded compression       - only then record target row
  - store + archived row
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
- A process crash loses only in-memory placement. On restart, already-recorded files disappear from
  the next derived task and only the missing suffix remains.
- Replica work is per requirement and source. A successful `git annex copy` is insufficient evidence:
  `git annex whereis --key` must show the registered target UUID before the catalog records that file.
- Reconciliation uses one bulk manifest query and, in installed CLI/portal runs, a dedicated read-only
  SQLite connection. File writes retain the existing short shared-connection lock.

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
| `blocked` | Gate A/B refused the run before writes: unavailable CLI drive, policy blocker, invariant failure, or infeasible committed capacity | Follow the typed actions, then start again |
| `plan-capacity-stop` | Live capacity changed after useful progress and the remaining graph is no longer feasible | Add eligible capacity or trim/re-plan; completed rows remain safe |
| `paused` | Download window reached, or copy #1 is safe while an offline source/target defers copy #2 (DEF-022) | Wait or re-seat the named drive, then resume |
| `error` | A bounded fetch retry, annex-key proof, or graph invariant failed | Inspect the typed evidence and logs; correct the fault before retrying |
| `stopped` | Operator requested Stop | Start again; reconciliation resumes missing files |
| `done` | Every committed requirement is satisfied by complete copies | None |

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
