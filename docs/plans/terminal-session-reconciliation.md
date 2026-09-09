# Terminal-session drive reconciliation — accepted scope

## Goal and observed gap

Enable the existing launch-guarded `modelark drive reconcile LABEL --dedicated` command to
reconcile a dirty generation whose recorded Fill owner has ended. The live acceptance blocker
is drive-07 epoch 1/generation 2, owned by a paused session with terminal code
DRIVE_RECONCILIATION_REQUIRED. Ordinary reconciliation currently rejects any recorded owner;
expired-session recovery only handles expired live sessions and never publishes an anchor.

This PR is code, tests and documentation only. Do not recover the live catalog, stop/restart the
portal, remount/write USB, alter archive payloads, deploy or merge as part of implementation.

## Proposed contract

- Reuse `drive_bootstrap.reconcile_drive` and its CLI, full report-only inventory, fresh final
  physical evidence, controller then sorted drive fences, and short atomic publication.
  No new schema, separate repair tool, automatic Fill resume or new destructive flag.
- A dirty session-owned generation is eligible only with a complete owner-session/token pair,
  an existing matching session token, and a recognized ended state (paused, blocked, stopped,
  failed or done). No globally live Fill session may exist. Missing/unknown/mismatched owner
  evidence refuses; never erase ownership to make this look sessionless.
- Probe only the exact owner session's existing child marker on the owned-dirty path, before
  waiting on controller locks when possible, and again while fenced. Missing or present-unlocked
  marker means not held: pause normally removes the marker. Do not create a missing marker.
  A held marker or probe error (including EACCES) refuses. Marker absence is not proof that a
  writer exited: transport children inherit physical drive-fence FDs, not session-marker FDs.
  Retain the
  existing controller/physical-drive fences through inventory and commit; a stale timestamp or
  terminal state alone is not evidence that a surviving writer has exited.
- Validate owner evidence before every path that might publish or advance this owned dirty
  generation. In particular, do not let the existing capacity-epoch transition branch bypass
  owner validation. Keep capacity/identity transition of an owned dirty generation out of this
  small recovery path: refuse rather than invent a new generation or discard its association.
- Reuse full inventory's existing semantics: every catalogued claim must be proven; debris and
  extras are reported, never deleted/adopted. This does not add full hash verification or claim
  that an inventory is a physical byte-integrity scan. Retain actual free-space observation.
- Before publishing, re-read under BEGIN IMMEDIATE: absence of a live session, exact captured
  owner pair/session token/state, and unchanged drive epoch/generation/identity/capacity/authority.
  Use the shared execution-authority helper for owner/token/state checks where appropriate.
  Publish the existing generation's clean anchor and planner revision atomically. On a mismatch
  or any inventory/final-evidence/publication failure, publish nothing.
  Capture after both fences and before inventory; at commit normalize schema INTEGER tokens
  and allow only the captured exact state. Do not use today's `publish_clean_anchor` unchanged
  (its live-session check is outside BEGIN), `session_write`, or `recover_expired_session`.
- Preserve session state, terminal reason, fencing token, dirty-generation owner pair, approval
  and task history. Do not infer that the prior Fill completed; recovery only restores drive
  admission evidence. Subsequent Fill/Slice operations revalidate through their normal paths.
- Preserve existing sessionless recovery, clean refresh/drift, bootstrap and legacy refusal
  behavior except for precise new owner/child-evidence refusals. Repeated ordinary reconciliation
  after a successful recovery follows the existing clean-refresh contract.

## Expected implementation surface

`modelark/drive_bootstrap.py`: small private captured-owner validation and atomic recovered-anchor
publication helpers integrated into existing reconciliation. Reuse, do not rewrite,
`execution_authority`/`execution_recovery` child-fence and `drive_mutation` primitives.
CLI help/operator docs explain ended-owner support and the distinction from expired-session
recovery. Focused tests in drive-bootstrap/recovery test modules and public CLI coverage.
`cmd_drive_reconcile` must also translate `proposal.Refusal`, including FILL_SESSION_ACTIVE,
to its existing clean SystemExit format; no traceback for expected session/fence refusals.
Use explicit drive recovery refusal codes for missing/mismatched owner evidence, child exclusion,
and owned-dirty identity/capacity transitions. Preserve the legacy missing-session test as a
missing-owner-evidence case, not as purported coverage of an actual live session row.

## Regression matrix

- Success for each recognized ended state; current real-style paused/no-expiry case.
- Preserve session/task/dirty-owner records; only expected clean anchor/revision changes.
- Refuse live owner or any other live session, missing session, unknown state, missing/half owner
  pair, token mismatch, held/unprovable child fence; refuse before inventory where possible.
- Owner/token/state/dirty pair/global-live/drive generation or identity changes during inventory
  or at the commit boundary publish no anchor. Exercise real SQLite rollback and held fences.
- Incomplete inventory, disappearing/replaced physical identity and failed publication leave dirty.
- Capacity transition cannot bypass owned-dirty recovery guards.
- Existing sessionless recovery/refresh/CLI progress and full relevant legacy suites remain green.
- Installed-package tests and public invocation; no live catalog/device fixtures.

## Local scope review

Grok CLI session `01a08749-385a-72a2-a39d-cec7861a859a`, pass 1 REQUEST_CHANGES.
The clarified contract above incorporates all four requested items: normal absent-marker semantics,
guard-before-epoch-transition, captured fenced owner/CAS inside BEGIN, and public CLI Refusal handling.
Add explicit real-style paused/no-expiry, real held-child flock, unchanged history, no publication
on commit race, and ordinary second clean-refresh tests. No live recovery was attempted.

Pass 2 in the same local Grok CLI session: ACCEPT. All four scope corrections and the
regression matrix were accepted before implementation.

Pass 3 reviewed the implementation and new regressions: ACCEPT, with no remaining correctness
defect in the accepted scope. Focused legacy/new suites passed 177 tests; an isolated installed
wheel passed 83 recovery/bootstrap/authority tests. All-source Ruff passed.

## Review sequence

Local Grok CLI checks this scope before implementation. Then implementation, regression tests,
new PR and canonical Greptile/Codex requests. At most three newly requested web rounds; fix
confirmed findings between rounds 1/2, stop on acceptance with green CI or after completed round 3.
Summarize residual findings and common architectural assumptions at the cap. Never auto-merge.
