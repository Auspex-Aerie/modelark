# C2 — guarded Slice decoding

Approved scope: integrate the qualified C1 worker with Slice original delivery,
version all three approval bindings, inspect attached candidates before output
mutation/FAT attempt consumption, and preserve old approvals and native resume.
No live deployment, catalog/archive changes, physical USB operations, stored
export, or production compression/canary/restore adoption (Stage D).

Review instructions for this stage: up to three local Grok CLI passes, then a
new PR with up to three Codex review/fix rounds. **No Greptile requests.** These
are stage-wide counters, not fresh allowances per push. The operator merges;
hand off readiness explicitly in bold after current-head review and CI pass.
This review exception is recorded here, not in the decision ledger, as requested.

## Local implementation checkpoint before PR review

- Branch: `codex/slice-guarded-decoding`, based on merged PR #76 (`5799635`).
- Initial implementation complete; final full regression in progress.
- Local Grok: rounds 1 and 2 accepted their inspected versions; round 2 covers
  the self-review corrections (session `2f39485c-a247-4937-9d89-dff3da0a7450`).
- Codex PR: 0/3 at this checkpoint; first request accompanies publication.
- Pending DEC-143/AGENTS quota policy from the preceding stage is preserved.

## Compatibility contract

New public previews carry `modelark.original-decode.v1`: stored and decoded
frame ceilings derived from the shared AS envelope, 1 MiB caller reads, a 64 MiB zstd window, and C1's
8 GiB virtual-address-space ceiling plus 2 GiB sampled headroom for ZipNN.
These are upper bounds, not a guarantee that every permitted frame succeeds.
There is no independent fixed Slice ZipNN frame cap: each single buffer must
fit within the same worker AS envelope; runtime mappings and concurrent buffers
can still cause a contained resource refusal for a smaller frame.
ZipNN always uses the guarded helper under this policy. zstd remains bounded
streaming in the parent; its native window setting is explicitly bytes.
That setting is qualified for zstandard 0.25.0's C extension; other versions or
backends refuse under v1 of the new policy. Existing legacy approvals retain
their previous backend behavior. No unsupported backend is silently treated as
having the same units.

Direct admission v2 hashes the complete policy into its binding. Native and
FAT32 transaction v2 include it in both their canonical plan and admission hash.
Their v1 forms preserve the exact old serialized bytes and limits; old public
readers reject v2. No live catalog or private-state schema migration is needed.
A private store containing v2 plans requires the upgraded reader, including
when checking overlapping ownership; downgrading is not supported for those
records. Existing v1 records remain readable without reinterpretation.

Before the first destination mutation, source preflight runs under destination
exclusion and per-candidate archive read fences, but before an attempt is claimed.
It inspects every attached alternative and every StreamZNN frame header using
bounded reads. One usable attached alternative is enough; offline alternatives
remain unchecked. If none is usable, absence yields a wait rather than destroying
the approved alternatives. This is not full decode/digest verification.

Native partial recovery authenticates existing output without requiring completed
files' sources online. All actual reads still recheck source evidence, headers,
resource admission, and original length/hash. During decoding, stop/destination
and source checks reach the worker supervisor; helper cleanup completes before
source descriptors and archive fences release. Worker success alone never
publishes a receipt.

## Validation / review notes

- Initial full Slice suite: 1,373 passed, 16 optional-codec skips. Current
  focused optional-zstd/codec suite: 186 passed. Disposable installed wheel:
  42 C2 tests passed. These runs predate the three corrections below; final
  regression and qualification runs are in progress.
- `/tmp/modelark-codec-qualification-pyfaco3s/result.json`: 25 checks passed,
  all 11 fingerprints matched, both formats decoded at 67,108,864 / 99,630,640 /
  409,993,344 original bytes, with at most 65,536 output bytes per piece.
  This precedes the frame-bound correction below; it is historical evidence.
- Actual installed C1 wheel readers were exercised: native/FAT v1 canonical
  bytes and direct v1 admission remain valid; all three old readers reject v2.
- Round 1 Grok accepted, without executing tests. Main-agent probes additionally
  found: a separate draft 512 MiB frame ceiling; fresh copy evidence checked only
  after inspection reads; and a stop between preflight/claim spending FAT's
  attempt without output. Corrections derive frame ceilings from the shared AS
  envelope, validate fresh candidates before reading, and acknowledge Stop before
  consuming the one-shot attempt. Regression tests accompany all three.
- No exception entry was added to the decision ledger. D/E and physical tests
  remain separate; none of these results claims production writer parity.
- Final corrected-code qualification: 25 checks passed in
  `/tmp/modelark-codec-qualification-tn1wzy18/result.json`, all fingerprints
  matched. Six whole/StreamZNN Slice round trips retain the original hashes and
  65,536-byte output ceiling under the same AS/headroom envelope.
- Final focused optional-zstd/codec tests: 193 passed. Final installed wheel:
  49 C2 tests passed from `/tmp/modelark-c2-final-wheel.ekVCUM/installed`.
  Standalone writer/canary wrapper and process-guard regressions: 46 passed.
- Four actual FencedSources + LocalArchiveReader + child-process tests prove
  reaping occurs while the real archive fence is still held, on success, Stop,
  simulated source loss and destination failure. The original sealed hash is
  checked on success; consumer exceptions retain identity.
- PR #77's first CI run exposed one regression on both Python versions: the
  public serial-repair integration test is outside the `test_slice*` selection.
  Source preflight correctly blocked the obsolete identity, but its exception
  bypassed the usual public blocked-source result. The shared delivery authority
  now returns the recorded source wait/block status before releasing exclusion;
  unrelated failures retain their exception behavior. FAT reports its attempt
  as unspent. Tests cover all three public destination paths, retry, a racing
  Stop, and unchanged old receipts/output. The full CI-equivalent suite is part
  of follow-up validation, not just the Slice filename subset.
