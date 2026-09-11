# Stage B — reusable original-byte reading

Implements DEC-139's reader extraction without changing archive bytes, Slice
seals, production RAM policy, destination transactions or filesystem admission.
Stage A merged in PR #74. Stage B does **not** remove the large-frame Slice limit.

## Shared mechanism, separate caller contracts

- `streamznn.iter_decompress` owns the one StreamZNN container-framing loop.
  It accepts a caller-owned binary stream, including non-seekable short-reading
  streams, and yields one restored frame at a time. Input requests are bounded.
  Iterator closure does not close the source; callback and source exceptions
  retain their identity. No paths, subprocesses or ModelArk imports are added.
- Standalone `decompress_to`, `decompress_file` and `verify_sha256` reuse that
  loop. Their default frame decoder remains the historical permissive ZipNN
  decoder, with its existing native exceptions and atomic publication wrappers.
  No Slice allowlist or 64 MiB cap is imposed on these existing callers.
- `streamznn.read_zipnn_frame` extracts Slice's narrower validated lossless-byte
  frame interpretation. It checks encoded/decoded sizes, mode fields and framed
  length agreement before payload/native decoding. Whole-ZipNN and StreamZNN
  use the same parser. A neutral optional decoder hook receives a validated blob;
  it does not itself create a worker or make arbitrary whole blobs streamable.
- `modelark.artifact_io` dispatches raw/whole/StreamZNN/zstd input without source
  acquisition or transport knowledge. Encoded-frame, decoded-frame, consumer-read
  and zstd-window ceilings are explicit separate fields. Expected original length
  is enforced; the consuming transaction still verifies the original digest.
- Slice's `decoding.original_stream` is now a small compatibility/error adapter.
  It maps neutral failures to the existing `SOURCE_DECODE_*` codes and supplies
  the same legacy bound to every limit. Source confinement, per-read attachment
  checks, source/destination IO attribution and fence ownership remain external.

The standalone default iterator's optional decoded/total bounds are post-decode
checks, **not native RAM protection**. Slice injects the strict frame reader so
its declared frame bounds are checked before native decoding. Callback-provided
frame readers are trusted code responsible for consuming exactly their frame and
honoring their own IO/resource bounds. The same distinction applies to injected
frame decoders: exceptions propagate; byte type/actual byte count are verified.
The MIT header, standalone dependency boundary and on-disk container are retained.

## Known zstd units mismatch — Stage C gate

Expanded optional-codec coverage reproduced a pre-existing refusal, not a Stage B
regression: a 256 KiB unknown-content-size zstd frame passes Slice's 64 MiB header
ceiling but fails native decoding. The old `max_decode_bytes // 1024` setting
becomes a 64 KiB native window ceiling with zstandard 0.25.0. Supplying a bytes-
valued 64 MiB ceiling to that same native decoder restores the fixture completely.
The [dependency implementation](https://github.com/indygreg/python-zstandard/blob/0.25.0/c-ext/decompressor.c)
passes the setting directly to `ZSTD_DCtx_setMaxWindowSize`; its documentation's
KiB description is inconsistent with the measured behavior.

Stage B intentionally preserves the old native setting and has a regression
asserting that known refusal. **Stage C must qualify and correct the units under
the new explicit policy without reinterpreting old seals.** Add known/unknown
content-size cases at/below/above the real window ceiling and exercise every
applicable supported dependency backend. Track this as INC-064 in the ledger.
Do not claim that zstd window compatibility or shared RAM admission is complete.

## Validation and review

- Codec-focused suite with optional zstandard 0.25.0: **105 passed**, no skips.
  The dependency was installed only into a disposable test directory, not a live
  runtime. Tests cover short/non-seekable IO, real writer dtype variants, exact
  bounds, malformed/unsupported headers, total length, callback/source failures,
  atomic wrapper preservation, caller ownership and dependency separation.
- Installed-dependency synthetic qualification: **13/13 passed** at both observed
  failure sizes, whole and streamed, compression/canary/full restoration. Report:
  `/tmp/modelark-codec-qualification-mu8c_x8m/result.json`; source hashes identify
  the implementation tested. This requalifies existing StreamZNN wrapper reuse,
  not large-frame admission through Slice.
- Full Slice regression: **1,346 passed, 5 optional-codec skips** in 520.58 seconds.
  The separate zstd-enabled run above covers the optional codec paths.
- Fresh wheel, installed without dependencies into a disposable directory:
  **93 passed** with zstd enabled and tests copied outside the checkout. Runtime
  import locations were asserted; all three changed modules matched source bytes.
- Full-project Ruff and diff whitespace checks passed.
- Local Grok CLI round 1 **ACCEPTED c272891** with no actionable findings. It
  confirmed that INC-064 stays at the Stage C policy gate, not a refactor fix.
  Session: `f2d466e1-014e-452f-8e06-9b81ee841275`. The first CLI invocation ended
  after review commentary; a same-session follow-up supplied its explicit verdict.
  This is one review round, not a second code-review iteration. This evidence
  update changes documentation only; remote review covers the pushed PR head.
  At most three Greptile/Codex rounds remain available for Stage B, per DEC-140.

No live deployment, archive/catalog access, physical USB writes, RAM-policy
adoption, stored-export switch, P2P service or compression fallback change.
