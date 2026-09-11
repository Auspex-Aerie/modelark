# Guarded codec worker — Stage C1

This is the worker/transport prerequisite of DEC-139 Stage C, not a Slice rollout.
No production caller imports `codec_supervisor`. Existing direct/native/FAT seals,
64 MiB decoding, zstd behavior, restore and compression remain unchanged.

## Boundary

`codec_supervisor.guarded_zipnn_frame` is a context-managed iterator for one
ZipNN work unit. The caller retains its source and must enclose this context in
its source/fence lifetime. It checks the shared standalone 32-byte header before
payload IO or spawning, observes current host/visible-cgroup headroom with the
same function used by compression qualification, and applies the explicit
`CodecMemoryPolicy` admission. There is no production default numeric profile.

The fresh worker receives only frame data on stdin and a dedicated protocol FD;
stdout/stderr share a separately drained diagnostics pipe. No source path,
archive/catalog/destination handle or fence FD is passed. `close_fds=True` closes
unrelated inheritable handles. This is descriptor confinement, not a filesystem
or credential sandbox: the worker still runs under the caller's OS account.
Python starts with an isolated search path and an explicit root for the package
the parent imported. The caller's current directory/PYTHONPATH cannot substitute
a different `modelark.codec_worker` and bypass the intended guard/protocol.

The child installs Linux parent-death SIGKILL and the irreversible AS/core-file
limits before importing ZipNN. It validates the frame again and requires input
EOF before native decoding. Native code holds the indivisible frame in the
child; the parent transfers at most 64 KiB at a time. Only a checked 9-byte
status/length header controls output: success must match the original size;
errors are capped at 4096 bytes. Diagnostic reads are bounded and retain at most
8192 bytes; arbitrary native diagnostics are not reflected into refusal text.

Nonblocking input/output/diagnostic pipes share one selector. Attachment/stop
checks run between source reads, IO events and output chunks, and every 50 ms
while native work or a pipe is idle. This is a polling interval, not a job
deadline. Synchronous caller-owned source reads can still block in the kernel;
the helper does not promise to interrupt a stalled filesystem syscall.

Iterator EOF requires exact output length, protocol EOF and zero child exit.
Context exit alone is never success. Early close or any consumer/source failure
kills and reaps the worker before returning; source ownership is not transferred.
The caller must still verify the complete original digest and control publication.
Linux parent-death signaling follows the spawning **thread**, which must stay
alive throughout the context. No persistent worker, nested canary worker, scratch
file, GPU workflow or unbounded parent accumulation is introduced.

## Qualification

Command (from the checkout, using the installed candidate's dependency Python):

```text
python -m scripts.qualify_codec_resources --large --guarded-reader
```

Final local report: `/tmp/modelark-codec-qualification-_8of6ito/result.json`.
All **22 checks passed** on Python 3.10.12 / ZipNN 0.5.4 / Torch 2.14.0:

- Deliberate allocation refusal under the shared guard.
- Compression, canary and restore of whole/StreamZNN fixtures at 67,108,864,
  99,630,640 and 409,993,344 original bytes under 8 GiB AS + 2 GiB sampled headroom.
- The new parent/worker path decoded each whole fixture to the original digest,
  delivering pieces no larger than 65,536 bytes. The 64 MiB fixture exercises a
  default StreamZNN-sized native work unit, not integrated container traversal.

Parent live RSS snapshots were approximately 19–24 MB and unchanged across each
guarded decode; current-exec virtual peak stayed below 48 MB. These are not a
sampling proof of peak resident allocation. `ru_maxrss` also includes inherited
pre-exec history and is not attributed to this worker transport. The report
fingerprints all six relevant implementation files and refuses changes during
the run. This is measured support for these fixtures/runtime, not arbitrary
input safety, host-wide memory reservation or a physical USB qualification.

Transport tests cover short input, noisy diagnostics, malformed/oversize/short/
trailing worker messages, exit failure after output, native abort, resource
refusal, cancellation during input/compute/output, consumer ENOSPC, source EIO,
early close, descriptor inheritance, spawn failure and parent death. Existing
standalone/Slice codec tests remain the compatibility gate.

Local tests: **170 passed** with optional zstd installed, including 28 supervisor/
worker tests. The complete existing Slice suite passed **1346**, with 5 optional
skips. Ruff passed across the project. A rebuilt wheel exercises the real isolated
worker bootstrap outside the checkout as well as the neutral/standalone readers.

Grok CLI round 1 accepted its inspected C1 boundary and reported two low-severity
failure-reporting observations: appending a second status after output begins,
and labelling parent-guard initialization as RAM refusal. Both were corrected by
separating decoding from response emission and distinguishing initialization
failure. An independent local probe also found cwd package shadowing at worker
launch; the pinned isolated bootstrap and its regression address it. The final
report above was rerun after all three corrections. Round 2 reviews these changes;
no final local/remote approval or complete Stage C acceptance is implied here.

## Remaining Stage C2 gate

Before any larger Slice approval becomes executable:

- Integrate bounded frame chunks into the shared StreamZNN traversal and neutral
  dispatcher; do not accumulate a whole frame back in the parent.
- Bind a qualified versioned policy into all direct/native/FAT canonical seals.
  Keep legacy approvals and their legacy zstd-window conversion unchanged.
- Qualify the corrected zstd units (INC-064) under the new policy.
- Inspect every relevant attached candidate/frame before destination mutation or
  FAT attempt consumption, preserving alternatives, waiting and native resume.
- Prove helper lifetime stays inside real source fences on stop, unplug and
  destination failure; then exercise the full transaction/public CLI path.

Production writer/canary/restore RAM-policy adoption and the separately gated
decoder consolidation remain required before claiming the overall parity arc is
complete. Stage C1 alone is not permission to retest compressed Slice on the USB.
