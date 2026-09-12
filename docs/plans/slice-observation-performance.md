# Slice observation performance — implementation and qualification

Operator authorized profiling-informed implementation, local Grok review and a
fresh PR with cloud Codex, with performance proved before remote review. No
Greptile under the explicit current three-day exclusion. Local Grok budget three
passes total (including this design consultation); cloud Codex three rounds total.
Operator retains merge authority. No service deployment or archive data repair.

**Current acceptance (DEC-151):** operator accepted the measured 80.252-second
result and instructed us to keep this implementation. The original timing gate
below is historical and was not met; it no longer blocks this stage. No threading,
file parallelism, further performance refactor or fourth local Grok pass. Complete
ordinary tests and cloud Codex review under the existing three-round budget.

## Measured baseline and acceptance

Actual Snowflake/snowflake-arctic-embed-xs projection, NAS -> PRINTER FAT32,
12 original files / 91,306,390 bytes: ~18 minutes; all output hashes correct.
Worker CPU ~4s, parent >16min. Source-only profile:15192 attachment checks,
75982 mount parses;157/204 instrumented seconds in parse_mounts. FAT2000rechecks
perform26000mountparses;300unprofiledchecks cost5.175s.

Actual PRINTER SanDisk3.2Gen1 serial00018510111520223455, UUID20A0-2FDD,
negotiated5000Mbps. Three new-file90,272,656-byte64KiB-chunk writes from preloaded
verified originals, file+directory fsync:3.318/7.091/6.203s (27.2/12.7/14.6MB/s).
Independent read+SHA:0.757/0.748/0.781s. Payloads retained in
/media/phaze/PRINTER1/modelark-performance-baseline-2MUAJY. No existingfileschanged.

Original exploratory target: <=30s public preview/approve/start plus independent
verification; hard initial qualification ceiling <=60s each of three fresh-root
physical runs on this91MBfixture. Record actual source/cache state, runtime,
link/mount and per-phase timing; don't claim a cold-NAS/device benchmark or use
USB theoretical bandwidth as achievable throughput. This initially blocked cloud
review; DEC-151 subsequently accepts the measured 80-second result. Preserve durable result,
hashes, real guards, same worker policy, receipts and sibling isolation.

## Design candidates (Grok consultation before implementation)

Prefer the narrowest change that passes the measured gate:

1. Reuse the immutable result of parsing the EXACT freshly read mount-table text.
   Every existing caller still reads procfs on every check; changed bytes require
   full reparse and validation, read failures still fail, malformed input isn't
   cached as success. A bounded memo of pure parse results is NOT cached live
   admission, identity or capacity, and must not skip source/destination IO guards.
   Bound entries and maximum retained input size; preserve parse error semantics.
   This central seam serves BoundTree, source and native/FAT observers uniformly.
2. Re-profile. If still insufficient, distinguish codec transport cancellation/
   authority polling from source guards around actual stored-byte reads and from
   destination IO guards. Do not silently replace the existing generic callback
   with a weaker one, remove actual read checks, or change every-read semantics.
   Before broader callback changes, specify exact boundaries and test Stop,
   detached/replaced media and completion ordering. No global time-based identity
   cache, widened authority, or unqualified fast mode.

## Verification

- Tests prove repeated identical fresh reads avoid repeated parsing, changes
  (including remove/re-add and malformed records) are observed immediately,
  read errors propagate and cached immutable values can't be mutated.
- Existing attachment-loss/backing/namespace/protected-role checks, native/FAT
  publication, Stop/preclaim and codec worker lifetime tests must remain intact.
- Bound memory retention and concurrent callers; no schema/seal/policy change.
- Full suite and installed-wheel tests. Test installed code from cwd outside repo.
- Three real public CLI copies to fresh USB child folders were independently
  verified: one parser-only, one final-code uninstrumented, one final-code
  instrumented. They are not three equivalent timing samples. NAS stays read-only.
- Local Grok review of actual final patch, then fresh PR and @codex review on each
  pushed head, at most3rounds; stop/summarize if exhausted. No Greptile tags.

## Selected architecture (DEC-149, DEC-150)

Local Grok accepted both designs and the final actual code in three total passes.
Its performance refusal against the original target remains historical evidence;
DEC-151 records the operator's subsequent acceptance, not a new Grok verdict.

- Pure parser memo: two entries; exact built-in strings up to 262,144 characters;
  larger inputs use the unchanged parser. No live observation is cached.
- Explicit `FencedSources.open_polled(candidate, poll)` separates the caller's
  attempt/Stop/journal-head checks from destination storage checks. Source
  attachment checks and every codec callback remain unchanged. Existing
  `open_checked` callers keep their full callback behavior.
- `Session._poll` contains the existing authority and journal checks only.
  `_boundary` adds the unchanged destination check. Destination detachment during
  a source-only wait may be detected later, but every subsequent destination
  access still requires its full current proof. Immediate destination-unplug
  reporting during an idle worker wait is not promised; Stop/source guards stay.
- A session-owned buffer gathers short reads up to the already-requested 1 MiB
  write quantum. It polls before/after each read, including EOF, copies each
  piece, and is never reused or mutated after returning it. The previous quantum
  is released before gathering another. At most a bounded quantum plus an input
  piece and allocator overhead are retained; no whole-file buffer or changed
  codec envelope. Invalid binary reads refuse without relabeling callback errors.
- RAM-buffered bytes are not counted as disk allocation. The full destination
  boundary, parent authentication, append/fsync, ownership accounting, digest,
  fault point, final preparation and publication ordering remain intact.

## Qualification record

Parser-only candidate:

- Focused source and installed-wheel tests: 57 passed; lint passed.
- Installed Slice transaction/guard/FAT observer suite: 295 passed, 11 skipped.
- FAT 2,000 rechecks: 18.106s instrumented (was 74.239s); 300 uninstrumented:
  1.740s (was 5.175s). Source-only decoding: 49.579s instrumented (was 203.767s),
  90,272,656 original bytes and SHA verified. 15,206 source attachment callbacks
  still ran; shared parser cache recorded one miss and 105,967 hits overall.
- Physical run 1: 395.648s total = preview 0.541 + approve 0.429 + start 394.419
  + independent verification 0.259. All 12 originals and exact receipts passed;
  **performance gate failed**. A sandbox test run overlapped its initial portion
  and was stopped, so this is not a clean isolated throughput benchmark.
- The sandbox full suite was interrupted at 38 failed / 1,216 passed / 10 skipped
  after socket EPERM failures. It is not a passing suite; host-permission rerun
  is required. No code relaxation was made for the sandbox.

Second candidate: authority/destination separation and bounded output gathering.
New polling/quantum regressions: 19 passed; existing transaction/fault tests:
196 passed in the same run before correction of one new journal-test assertion.
Physical run 2 (unprofiled) passed correctness in **80.252s**: preview 0.517,
approve 0.407, start 79.069, independent verification 0.259. This is about 13x
faster than the original approximate 18-minute run, but **missed the original gate**.
DEC-151 accepts this result without further optimization. The first permitted
full suite was stopped at the operator's discussion request: 2,994 passed,
16 skipped, no failures, incomplete. The fresh permitted CI-equivalent rerun
completed: **3,722 passed, 28 skipped, 5 deprecation warnings in 673.35s**
(`pytest -q --ignore=tests/test_e2e_portal.py`). Repository-wide
`ruff check modelark scripts tests` and `git diff --check` passed.
Local Grok code review is complete.

Physical run 3 was instrumented, not an acceptance timing: 110.786s total,
all hashes/receipts correct. Full Start profile: 17,629 source callbacks / 54.138s
in attachment checks; 2,606 FAT live checks / 29.560s; 145,517 fresh procfs reads /
23.329s; 862,817 libc binding constructions / 14.299s; 244,430 SQLite executes /
7.094s; 138 fsyncs / 7.525s. Cumulative times overlap and instrumentation changes
cost: do not sum these or subtract them directly from the unprofiled result.
Profile retains the unchanged actual-source-read guards and normalized appends.
The tested polling/gathering wheel SHA-256 is
`ad24d11f7012453fd0758c7dfc4b26f955603daa85d0efd90aed1011a90c1bdf`;
the later source change to its `_poll` docstring is nonfunctional.

## Further performance ideas (not selected for this stage)

The common cause is repeating a complete observation at nested internal callback
layers, not excessive codec CPU or USB link speed. A possible next bounded stage
would give actual stored-byte reads, buffered decoder transport and destination
I/O distinct guard owners, using the existing source/representation/delivery
separation. Preserve fresh before/after proof for every actual source read and
proof before accepting decoded output; do not time-cache hardware evidence.
Any change to source-unplug responsiveness during an idle decoder wait must be
explicitly specified and tested, not silently weakened. Separately, investigate
reusing process-local libc call bindings (not syscall results) to eliminate
wrapper construction. These are proposals, not qualifications or shipped fixes.
The operator elected to keep the current result instead. File-level parallelism
was also discussed, not implemented; it would require coordinated journal,
allocation, fencing and aggregate decoder RAM ownership.

The third local Grok pass **accepted the supplied code diff/tests, not performance**.
Nonblocking notes: per-call mutable buffers rely on trusted append ports not
mutating them (both current ports use memoryview only for writing); authority
polls remain duplicated; unchecked legacy sources now receive EOF liveness
checks; buffer-discard and exact-error tests could be more precise. The changed
fault granularity is one checkpoint per gathered append, not per short read.

Do not adopt Grok's suggested larger source reads on its stated inference:
17,629 callbacks are not 17,629 actual reads. The worker performed 1,148 bounded
input reads; nested wrappers/polls explain the amplification. Its suggestion to
retain mountinfo descriptors would also need explicit namespace/freshness and
lifecycle qualification. Neither suggestion is implemented or accepted here.

The local review boundary was reached and summarized. DEC-151 now permits the
existing PR workflow to proceed after tests; no fourth local review or new
performance architecture changes are part of this stage.

Cloud Codex round 1 found an EOF-to-flush boundary omission: an empty gathered
read bypassed the append-loop destination check, permitting the final flush
before the next full proof. The fix adds the full boundary immediately before
that flush. Regression cases cover successful completion, detached destination
and changed destination after a prior append; all three failed on the unfixed
code because flush preceded the check. Failure cases must not flush, publish
the output or create a receipt. This closes a missed transition in DEC-150's
existing contract, not a new guard architecture. The 80.252-second physical
measurement predates this added final check and has not been remeasured.
Post-fix local validation: transfer/transaction/guarded-decoding suites 200 passed,
11 skipped; native/FAT32 transaction and public-gate suites 91 passed. Repository
lint and diff whitespace checks passed. Full remote CI must pass the pushed fix.
Subsequent exact-head review state is kept in the ignored operator evidence.

Operator evidence: `claudedocs/operator-scratch/slice-performance-2MUAJY/` in the
primary checkout, including per-run JSON, profiler data, review prompts/state,
and qualification harness. USB outputs use fresh `modelark-observation-2MUAJY-N`
children under `/media/phaze/PRINTER1`; NAS `/mnt/drive-00` remains read-only.
No cache eviction was forced: these are attended workflow timings, not cold-NAS
or sustained-device bandwidth claims. The initial parser-only wheel is retained
under `/tmp/modelark-observation-wheel.Be0Mtf/parser-only/`.
