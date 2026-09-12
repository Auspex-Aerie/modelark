# Evidence-bound USB-bridge serial repair

Status: bounded correction implemented and accepted in the one authorized extra local Grok pass. Cloud review and physical qualification remain required.

## Goal and authority

Repair the known Drive 01 mismatch (`VR1KV4LK` registered versus
`5652314B56344C4B` historically observed) without changing its registered serial,
old evidence or archive files. The operator approved the repair, consistent
consumers and useful refusal messages. A generic serial-alias registry, new
hardware support or weakened identity checks are not authorized.

Local Grok review is capped at three rounds including design; cloud Codex at
three rounds after the PR. No Greptile. The operator merges. Live repair follows
reviewed merge, fresh backup and Drive 01 attachment; no automatic approval or
Fill start.
After the three-round stop below, the operator explicitly authorized the bounded
DEC-154 correction and exactly one additional local review. This does not reset
the default budget or authorize a fifth local pass.

## Why a fingerprint-only repair is insufficient

The USB bridge can continue reporting the encoded string on every connection.
Changing only the catalog fingerprint would make the next read reject it again.
Conversely, accepting the hex encoding of every registered serial would give
unrelated drives a second accepted serial without drive-specific evidence.

Canonical identity and raw observation must remain distinct. One shared matcher
must require explicit, drive-bound repair evidence before accepting an alternate
raw observation. Raw observations must never be rewritten in fence evidence.

## Bounded implementation contract

Use the existing append-only repair transition as the proof, not arbitrary old
anchors and not a new mutable alias list:

1. Before repair, require the exact current clean anchor, both nonempty UUIDs,
   registered bounded printable ASCII serial S, and identical strict v1 identity
   and fence proofs carrying E. E must be exactly the ASCII hex encoding of S
   (hex-digit case is irrelevant only to recognizing the encoding relationship).
   The old fingerprint must reproduce exactly with the recorded E and capacity.
   Dirty recovery is unsupported for this third repair case.
2. Under the existing controller and compatible physical locks, require a fresh
   attachment with both UUIDs and capacity unchanged, and raw serial exactly E.
   Keep full inventory, backup/rehearsal, repeated observation and atomic commit.
3. Append generation g+1 with operation `serial_identity_repair` and a new clean
   anchor: canonical identity proof serial S, **raw fence proof serial E**,
   canonical fingerprint computed with S. Preserve generation g and its anchor.
   Supersede affected approvals as the existing workflow does.
4. Subsequent matching may accept only the literal registered S or that exact E.
   E is authorized only by the validated adjacent transition above, for the same
   drive label, epoch, both UUIDs, capacity, canonical fingerprint and serial.
   No case folding of fresh observations, recursive decoding, alternate spelling,
   unrelated old serials, NULL-serial repairs or hash-only repair authority.
5. There must be exactly one `serial_identity_repair` record per drive epoch;
   a previous repair prevents this bridge repair, and ambiguous history cannot
   grant bridge matching. Resolve the transition through one catalog adapter and validate it with one
   pure proof checker. Bootstrap/reconciliation and Fill use the same matcher.
   Reconciliation and later writes retain raw E in fence evidence; later clean
   generations can refer back to that one explicit transition in the same epoch.
   This is not a scan for arbitrary old serials. If fresh hardware reports literal
   S instead, the new raw fence proof honestly records S, not a fabricated E.
6. Slice already seals source epoch, generation, canonical fingerprint and clean
   anchor ID. Its source gate resolves authorization only from that unique transition
   no newer than that sealed source generation, after verifying the exact sealed
   anchor at exactly the sealed generation against the fresh catalog under the
   physical fences. To grant encoded-serial matching, current source generation
   must still equal the sealed one. This is not a new prerequisite for ordinary
   literal/serial-less reads: absent, stale, malformed or ambiguous bridge proof
   returns no binding. The reader then follows its original strict rules, while
   the transaction's existing stale-plan checks remain unchanged.
   Do not consult a
   latest alias list or allow evidence added after the sealed generation. Pass
   the ephemeral validated matching context to the local reader explicitly through
   an internal trusted adapter seam, never CLI input or serialized alias data. Standalone
   readers without that context remain literal-only. Raw attachment rechecks
   still require the unchanged actual device/mount/serial tuple on every boundary.

No new Slice evidence fields or unsealed runtime exception are permitted in
this repair. The existing immutable anchor reference and exact sealed generation
are the whole source-side boundary. If review or testing invalidates that proof,
stop for scope confirmation instead of adding fields or bypasses. Missing or
changed anchors, newer transitions, or duplicate repair transitions grant no
bridge permission. Encoded reads without that permission still refuse.

`FenceIdentity` stays strict. The repair locks both old encoded and new canonical
fingerprint sets; their NULL-serial exclusion key overlaps. Lock aliases are
exclusion only, never evidence admitting a source or repair.

## Refusals and operator flow

Before repair, approval identifies the affected drive and directs the operator
to inspect the guarded serial repair, rather than looping through reconciliation.
Only the exact supported proof gets this diagnostic; unknown inconsistency stays
unproven. UI rendering uses textContent and includes drive and bounded reason.

After successful repair the old proposal is stale: preview again and let the
operator approve the resulting exact proposal. Do not auto-start any transfer.

## Verification

- Positive exact historical pair and same-bridge subsequent read/reconcile/Fill.
- NULL/empty/malformed/duplicate-key proofs, either UUID/capacity/epoch mismatch,
  dirty generation, missing or unrelated transitions and malformed serials refuse.
- Never-repaired drives with an encoded-looking observed serial still refuse.
- A Slice sealed before repair cannot acquire the new observation authority;
  missing/changed sealed anchor and later-generation transition refuse.
- Full raw observations and old rows stay intact; both lock sets contend;
  stale binding, ownership changes and final-observation changes refuse.
- Backup/rehearsal is retained, failures roll back atomically, archive bytes stay
  unchanged and affected approvals become superseded, not silently rebound.
- Existing literal-serial and serial-less tests remain unchanged in meaning,
  including their low-level read gate after a later clean/dirty generation and
  separate transaction-level stale-source refusal. Raw diagnostic fingerprints
  survive an unsuccessful optional bridge lookup.

Baseline: 163 existing pure serial/fence tests passed before implementation.

## Operational limits

This is not generic USB enclosure support. It supports exactly the proven raw
spelling, not arbitrary hex casing on later connections. Identity epoch or
capacity changes invalidate the bridge relationship. A different bridge that
reports the literal registered serial retains the ordinary strict path; a new
encoded observation or resize on an encoded-only connection requires separate
explicit investigation, not automatic extension of this repair.

The local 0.4.0 deployment and draft remain unchanged. Physical Drive 01
qualification happens only after reviewed merge and reconnect. The synthetic
public Slice test uses real disposable archive/output bytes with synthetic host
inventory; it is not a physical USB or live archive qualification.

## Review stop — 2026-09-12

Grok rounds 1/2 requested the bounded design closures above; final code round 3
returned NOT ACCEPT. Its high/medium findings share one cause: optional bridge
compatibility was inserted as a mandatory verifier for all LocalArchiveReader
sources. `load_bridge_binding` checks sealed generation/anchor before determining
whether a bridge transition exists. An ordinary literal-serial source read gate
therefore refuses after a later clean generation even though its physical
identity is unchanged. A disposable reproduction passed on the released base
and failed on this branch. This verifies the source-gate regression, not a claim
that full execution should ignore its existing stale-plan checks.

Separately, three existing archive-observation tests fail: unsupported serials
still refuse but `_live_evidence` discards their raw diagnostic fingerprint.
Those tests pass on the released base. Other targeted batches passed (510-test
serial family,147-test source/API batch,1 JavaScript test); do not describe the
whole validation as green. Two CLI parser failures caused by the running live
singleton passed with test-only socket isolation; the service stayed running.

Recommended correction, pending operator direction after the cap: make bridge
authority a genuinely optional, fail-closed matching result. Missing/stale/
ambiguous bridge proof yields no bridge permission, while literal/serial-less
matching retains its existing behavior and raw diagnostic observation. Enforce
the exact sealed anchor/generation only before granting encoded-serial matching.
Do not weaken normal admission, stale-plan checks, locks or proof preservation.
No new Slice fields or broad architecture rewrite is indicated by these findings.

At that stop, no further implementation fix, fourth local review, PR, push,
deployment or live repair was performed. The operator subsequently approved the
bounded correction and one extra review (DEC-154). Review/evidence files are retained under
`/tmp/modelark-bridge-review.5wGm7t`; cloud review rounds remain zero.

## Authorized correction qualification — 2026-09-12

DEC-154 separates optional bridge permission from ordinary matching. Unsupported
bridge evidence now returns no binding, and known-serial mismatches retain the
observed diagnostic fingerprint while remaining unproven. The source gate still
requires exact sealed evidence before granting encoded matching; all existing
transaction-level stale-source checks remain in place.

The expanded serial/bridge/bootstrap/mutation/observation/approval/source suite
passed 638 tests, including the regressions found at the stop. Two pre-existing
Torch deprecation warnings remain. CLI tests used isolated test-only launch
sockets without stopping the live portal. Repository-wide ruff and diff checks
passed. These are disposable/synthetic qualifications, not physical-drive proof.
Exactly one additional local Grok pass is authorized; review output and later
cloud status belong in the review artifacts/PR, not a silent budget reset.
