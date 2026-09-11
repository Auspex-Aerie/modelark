# Stage E — public Slice qualification

Base: PR #79 merged 2026-09-11 as `3f83e5aa00a2ebd2cf620a6ea20a49091e356b8c`.
Branch: `codex/codec-public-cli-qualification` in the persistent codec worktree.
Stage D is complete; DEC-147's retained zstd classification limitation is unchanged.

## Scope and sequence

1. Bridge existing CLI-dispatch and real-delivery tests: public `cli.main(argv)`
   through actual catalog, sources, native decoding, destination and receipt.
2. Verify original paths, byte lengths and hashes for raw, whole ZipNN,
   StreamZNN and optional qualified zstd; compressed fixtures plus raw sidecars.
3. Check public failure results and absence of false complete receipts; combine
   these with the existing lower-layer interruption, attachment and FAT matrix.
4. Run targeted regressions and optional-dependency qualification; document
   exact synthetic/physical and source/installed-launcher evidence boundaries.
5. Local Grok CLI review up to three rounds for Stage E, then a new PR and
   current-head Codex review up to three rounds. No Greptile. User merges.

Tests are sequential because shared synthetic device fence identities can
conflict across simultaneous suites. No live service, archive, catalog, USB,
mount, migration, deployment or policy change is authorized by this stage.

## Evidence boundaries

The new tests invoke the public Python CLI entry point, with real parsing and
JSON/exit handling, not a separate installed console-script process. Hardware
inventory/providers, admission samples and a private launch socket address are
synthetic; SQLite, source reads, guarded ZipNN children and filesystem writes
are real and disposable. Native-folder tests do not establish physical backing
eligibility or kernel-vfat behavior. Earlier large-frame and vfat evidence must
be identified separately, not relabeled as fresh Stage E physical acceptance.

Future stored-byte exports/P2P remain deferred; this delivery is original bytes.
Physical compressed-source USB qualification remains separately scoped.

## Current state

Initial dev qualification: 14 passed, 1 optional-zstd skip, 2 warnings in167.89s.
Isolated installed D2 wheel (modelark tree byte-for-byte matched merged source,
package origin asserted): 15 passed, 2 warnings in180.52s, including zstd0.25.0.
These tests exercise the installed Python entry point, not the generated launcher.
Nine mapped regression suites: 278 passed, 11 optional-zstd skips in237.10s.
Ruff and diff checks passed.

Local Grok round1 ACCEPT, no blockers. Session01a08feb-1952-7321-a84a-ab4efc4d8f71:
the first88KB prompt was offloaded/truncated without a verdict, then the SAME
round/session received a compact complete snapshot of the three new files and
unchanged fixture context. No code iteration or review-counter reset occurred.
Reviewer ran no tests. Its optional suggestions led to a disposable missing path
for repeated Start, an explicit check of the wrongly requested path, and this
updated evidence log. Main-agent checks also now require each injected refusal's
reason and proof that the killed-child case actually launched its child.
Refined installed-package qualification: 15 passed, 2 warnings in182.11s.
Optional-zstd guarded Slice regressions in the same isolated package: 12 passed,
37 deselected in0.31s. These supplement the dev environment's optional skips.
No production file changed; the installed package source still matches this tree.

Local Grok round2 ACCEPT with no actionable findings, session
01a08ff2-4ee1-7d90-b1ca-c1b8af2431b9. Snapshot-only; reviewer ran no tests. It
confirmed the test refinements and requested this final actual-results update.
No common production architecture defect emerged from Stage E; changes stayed
within test isolation, assertion strength and truthful evidence reporting.

Stage E local2/3 used, remote0/3 before publication. New PR/Codex review is next;
operator merges after exact-head acceptance and CI. No Greptile or live changes.
This checkpoint is not remote approval, deployment or physical USB acceptance.

## PR #80 remote round1

Codex found one P2: the killed-worker test's `SOURCE_DECODE_` prefix could accept
an incorrect invalid/resource classification. Require the exact
`SOURCE_DECODE_WORKER_FAILED` code instead. All candidate failure assertions now
parse the structured reason and compare the complete code, not a substring.
This strengthens test assertions;
production supervisor behavior is unchanged. Validate before pushing and request
Codex round2 on the new head. This is assertion precision, not a newly discovered
production architecture defect or a reason to reopen DEC-147.

Exact-code installed-package failure matrix: 7 passed, 8 deselected, 2 warnings
in31.27s. Ruff and diff checks passed. Local review remains2/3; the remote
test-only correction is submitted to Codex round2/3 on its new pushed head.
