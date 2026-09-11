# Stage D — production codec adoption

Started 2026-09-11 after reboot, based on merged PR77 `83438eb`.
Operator authorized continuing the approved DEC-138/139 arc.

## Delivery increments

1. D1: guarded production writer startup and shared-decoder canary inside that
   same child, with explicit safe raw fallback on resource/format refusal.
   Preserve codec selection, disk bounds, original retention, write fences,
   monitored cancellation and result-file transport. No nested canary worker.
2. D2: external canary/deep verification and restore adoption. Inventory unknown
   original sizes, legacy formats/modes, zstd concatenation/window support and
   return/error conventions before choosing a compatible caller adapter. Keep
   restore retrieval/publication and verification evidence outside the decoder.
3. Qualification: same-profile real writer/reader parity, installed runtime and
   lifecycle/failure matrix. E public CLI and physical USB remain separate.

D1 alone is NOT complete production parity or authority to deploy.

## Caller inventory at the merged base

| Caller | Existing contract | D1 disposition |
|---|---|---|
| fetch._compress_isolated | child result file; write-fence inheritance; crash/stall/output-cap raw fallback | add common admission and isolated guarded startup |
| compress_worker.run | compress then canary; keep raw until success; no nested child | shared original decoder, exact original size/hash |
| compress.canary_ok | called both in worker AND directly by verifier; no expected length parameter | leave public legacy path unchanged in D1 |
| verifier deep checks | exceptions become failed evidence, not a resource-wait state | compatibility/evidence review in D2 |
| compress.decompress_file | magic dispatch; native whole/standalone StreamZNN/zstd acceptance | preserve legacy behavior until D2 |
| restore._materialize | own temporary, original hash, then atomic publication; retrieval separate | preserve in D1 |
| standalone StreamZNN | MIT; broad native-mode compatibility; path wrappers share loop | no changes in D1 |
| Slice original_stream | exact sealed size/policy, strict qualified formats, guarded child | keep default execution and seals unchanged |

The 4x wishlist gate selects a codec; it is not an enforced process-memory bound.
D1 retains that preference and adds the shared qualified envelope to actual
execution. A refused compression keeps the verified downloaded original.

## Review budget

Stage D local Grok CLI: 2/3 used, round2 ACCEPT. Remote Codex: 0/3. Counts carry across D increments
and pushes unless operator explicitly grants a separate cycle.
No Greptile under the current operator exception; no ledger entry for that
exception. Every new pushed PR head needs Codex review. Operator merges.
After the cap: stop, summarize findings and shared architectural cause.

## Restart

Persistent worktree:
`/home/phaze/PycharmProjects/modelark/claudedocs/operator-scratch/worktrees/codec-stage-d`
Branch: `codex/codec-production-adoption`.
Primary checkout is older and has unrelated files: preserve it.
No live service/catalog/archive/USB operations. PR77 watcher stays paused.

## Validation

Initial existing codec tests: 48 passed, 2 optional-zstd skips.
New production/startup/isolation tests: 38 passed.
Broad codec/archive-boundary suite: 242 passed, 2 optional skips, one subprocess
test double lacked poll(); updated the double, then all 48 affected tests passed.
Both new ingestion resource/decode-refusal raw-publication tests passed.
Installed-wheel tests: 81 passed with optional zstd0.25.0 and package origin
pinned to /tmp/modelark-d1-wheel.m9eM9y/venv/lib/python3.10/site-packages.
Full CI-equivalent suite: 3545 passed,19 skipped in766.52s.
Grok round1 session `01a08ee8-ea80-74d2-9762-d05f5b42f7e0` was interrupted:
its review checkpoint fabricated src/modelark/hf_fetch.py, IsolatedCodecError,
environment flags and different limits. Exact checkout checks disprove those
claims. No actual-code finding or acceptance is inferred from that invalid pass.
Round2 is a fresh bounded snapshot-only CLI review, excluding contaminated
memory and requiring exact quoted source evidence. Round1 still counts; no reset.
Round2 session `01a08ef9-7dfa-73c1-84d1-ce2ddf5e52bc` ACCEPTED the supplied
implementation and tests, with no blocking findings. Optional uncertainties were
checked against the complete code: all compressor branches return dst and clean
temporaries on BaseException, hash mismatch intentionally stays a failed canary,
and bootstrap imports before initialize_worker are non-native. Guard validation
failure remains fail-closed, not permission to publish or drop the original.
Commit/PR publication follows local acceptance; remote review still pending.
Old qualification files in /tmp were lost on reboot.

Fresh D1 installed-dependency qualification:
/tmp/modelark-codec-qualification-6tj5zb1z/result.json — 25 checks passed;
all14 fingerprints match. Six actual production writer/in-child-canary to Slice
round trips at64/100/410MB, whole/StreamZNN, exact original hashes and bounded
Slice output under the same8GiB AS plus2GiB headroom. Legacy standalone
compression/canary/restore and deliberate allocation refusal also passed.
This is not D2 adoption or physical acceptance.
Persistent copy of the exact report (cmp verified):
`claudedocs/codec-stage-d-evidence/pre-review-result.json` (Git-ignored).
