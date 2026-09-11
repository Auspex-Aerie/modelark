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

## D2 continuation — 2026-09-11

PR78 merged as `9e68ba8793b13bf9e183b8193b6fd9b2b6a953d3`.
D1 local Grok accepted round2; Codex accepted remote round1 and all CI passed.
Branch `codex/codec-reader-adoption` starts from that merge in the same persistent
worktree. Review counters remain cumulative: local2/3, remote1/3 before D2 review.
No Greptile, automatic merge, live service/catalog/archive/USB work, or old watcher.

### Compatibility inventory and implementation

- Public `canary_ok` has no original-size argument. False for an absent expected
  hash; StreamZNN malformed-hash ValueError retained; decoded mismatch is False.
- Whole/legacy ZipNN dispatch remains the native self-describing byte decoder,
  not Slice's restricted header/mode parser. Native work and whole buffers now
  reside in the guarded child. Parent output pieces never exceed64KiB.
- StreamZNN uses the standalone shared `iter_decompress` framing with its legacy
  native decoder, including an empty container. No new Slice64MiB frame ceiling.
- Legacy zstd keeps streaming native acceptance, unknown content size and
  concatenated/skippable frames after the initial ordinary magic. It does not
  inherit Slice's single-frame/64MiB-window/0.25.0-only contract.
- A v3-legacy request uses shared admission, startup, pipes, diagnostics draining
  and kill/reap ownership. Metadata, bounded data packets and counted final EOF
  support streaming without a sealed output size. Native output never uses stdout.
- Public decompression writes a parent-owned same-directory temporary for all
  formats, including zstd, and replaces only after complete successful execution.
  Restore keeps its additional original hash verification and atomic model/file
  publication. Decoder children receive no archive/destination paths or handles.
- Typed unavailable/resource execution maps to UNKNOWN in deep verification;
  missing files, recorded-hash conflict, malformed content and digest mismatch
  still fail. Known independent failures retain precedence over unknown checks.
  Restore reports unavailable work without claiming the source is corrupt.
- No new schema or migration. Original size still comes from existing contracts;
  an unknown legacy size is not fabricated from catalog guesses.

This delivers shared RAM/lifecycle protection, not one identical format allowlist
or proof that every historic archive is accepted by Slice. DEC-146 records why.
End-to-end E and physical compressed-source USB acceptance remain separate gates.

### D2 validation in progress

Focused existing/new readers, transport, startup, restore and verifier suite:
122 passed,4 optional-zstd skips. Follow-up production/evidence suite:
66 passed,7 optional-zstd skips. Full suite, installed-wheel optional-zstd coverage,
large actual writer-to-public-reader/Slice qualification and final local review
are pending; do not treat this checkpoint as review acceptance.

### D2 qualification and unresolved review gate

- Full CI-equivalent core:3585 passed,26 skipped,5 warnings in865.48s.
- Installed D2 wheel:174 passed with optional zstd0.25.0, plus2 additional
  real native-streaming/large-zstd-window compatibility checks. Updated an old
  tiny-guard test expectation to accept explicit unavailable-dependency refusal;
  the implementation was unchanged. Extra ZipNN fixture uses a private mutable
  input copy because the native compressor mutates its input buffer.
- Large installed-dependency qualification:31 passed at64/100/410MB across
  whole/StreamZNN production writer, public canary/restore and Slice. All17 source
  fingerprints still match. Report:claudedocs/codec-stage-d-evidence/d2-pre-review-result.json.
- Independent blocker reproduced through the actual installed D2 public canary:
  a valid zstd frame with a2GiB declared window is refused by the native decoder
  as "Frame requires too much memory for decoding". The worker's generic catch
  sends I; v3 maps that to CODEC_INVALID/StreamZnnError, so verifier calls it a
  failed/corrupt check instead of unavailable. Repro is retained under the ignored
  evidence directory as reproduce-d2-window-refusal.py. This is NOT a passing
  qualification case and is not cleared by the full regression result.
- Architectural correction candidate: preserve resource, unsupported/unavailable,
  proven-invalid and unknown internal outcomes at the native codec boundary,
  then carry those types through worker/parent adapters. Do not add independent
  caller guesses or widen a memory limit to make this valid refusal disappear.
- Final local round3/3 is being concluded in the same Grok session after its
  internal tool-turn cap; no code fix, fourth review, commit, PR or push has been
  made. Stop for operator direction with this confirmed blocker even if Grok
  finds no additional issue. Remote budget remains1/3 used (D1 only).

Final local verdict: **NOT ACCEPT**, confirmed the independently reproduced
zstd classification blocker; no second distinct actionable defect. Same session
01a08faa-2496-7cb1-8062-0be8dcf5a650 completed its verdict-only continuation.
Local3/3 is exhausted. All tests/qualification/review processes have finished.
No commit, PR, push, watcher or live change. Await operator direction for the
bounded shared-outcome correction and additional local review allowance.

### Operator disposition — DEC-147

After clarifying that this was an artificial2GiB zstd window refusal, not an
observed archive exhausting RAM, the operator said "Leave it then". Leave the
implementation unchanged; do not pursue the proposed classifier correction or
an extra local review cycle. Retain the actual local verdict (NOT ACCEPT, this
single finding) and disclose the limitation in any D2 PR. Do not silently label
the review clean, reset counters, or treat this disposition as a waiver of other
findings. Remaining Codex budget and operator merge authority are unchanged.

Operator subsequently authorized continuing to PR/Codex review with DEC-147
disclosed. No additional local review or implementation fix was made. All17
qualification fingerprints rechecked unchanged; Ruff and diff checks passed.
The first D2 PR review request will be cumulative Codex round2/3. The actual
local NOT ACCEPT verdict and narrow operator disposition remain visible.
