# Stage A — shared codec resource contract

DEC-138 requires one RAM-admission methodology for compression, canary, restore
and Slice. Stage A implements an operation-neutral `CodecMemoryPolicy` and a
disposable qualification runner. **No live caller adopts it yet.** This is not a
claim that Slice's 64 MiB incompatibility is fixed.

## What the policy means

The same versioned record controls all three qualification operations. It has an
explicit address-space ceiling and a headroom reserve; no separate compression
multiplier, decoder cap, implicit environment variable or Slice default.
It requires observed available memory of at least ceiling + reserve. The Linux
runner uses `MemAvailable` and all visible ancestor cgroup hard-limit headrooms,
including a constrained visible root, taking the minimum. Unknown accounting
refuses. This is conservative sampled admission, **not a memory reservation**.
Unrelated processes can still consume memory after the observation.

Fresh workers install `RLIMIT_AS` before importing the native codec, set their
core-file limit to zero and refuse a lower inherited limit instead of relaxing it. This is a hard
virtual address-space ceiling, not an RSS cap or protection against arbitrary
concurrent host pressure. Existing native file mappings make virtual and resident
usage differ substantially. A successful run cannot certify all future workloads.

Qualification uses an **8 GiB AS ceiling plus 2 GiB observed headroom reserve**.
These are experimental parameters for this runtime, not newly deployed defaults.
Whole and StreamZNN operations are performed in separate fresh processes with the
same policy. Native diagnostics go to separate files; results use exclusive JSON
files rather than stdout. Temporary synthetic bytes are removed on exit, while
results and diagnostic files remain in the reported private qualification directory.

## Running

From the checkout, with its dev Python:

```sh
python -m pytest -q tests/test_codec_resources.py tests/test_codec_qualification.py
python -m scripts.qualify_codec_resources --large --output-parent /tmp
```

Without `--large`, the runner uses 2 MiB inputs. Large mode uses exactly
99,630,640 and 409,993,344 bytes of synthetic valid BF16 safetensors data. Each size
is compressed through real whole-ZipNN and StreamZNN writers, independently
canary-verified, and restored with complete original-size/hash checking. Misleading
`.blob` filenames ensure the restore routes by actual magic rather than suffix.
The runner asserts writer format headers too. It also tests a refused allocation
larger than the AS ceiling without trying to touch that much resident memory.

Reports include exact policy, source-code digests, Python/ZipNN/Torch versions,
format magic, original/stored identities, and peak RSS/virtual memory by phase.
Code changes during a run invalidate it. No source/encoded/runtime fixture is
read from the live archive, and no application service is started or stopped.

## Limits and next gates

- A raw allocation refusal and an injected native abort test are not a complete
  adversarial native-decoder qualification. Header validation, cancellation,
  parent death, bounded pipes and per-read attachments belong to later stages.
- The corpus is deterministic BF16-like synthetic data, not every dtype, data
  distribution, archive format or historical whole-file size.
- Visible cgroup limits and a fresh host sample cannot reserve RAM or prove the
  absence of hidden outer constraints. Run only in a understood Linux host
  environment; production discovery/admission must be reviewed before adoption.
- Successful new-fixture round trips do not migrate old archives, prove physical
  USB delivery, or change existing seals, source-read authority or Fill policy.
- Shared resource admission must be adopted by production compression/canary and
  readers before declaring DEC-138 parity complete. Module existence alone is
  not integration. See the approved representation plan for staged adoption.

## Stage results and reviews

Local targeted regression run after review hardening: **71 passed, 5 skipped**
(optional zstd cases in this environment); full-project Ruff passed.

Unchanged-code large runs passed all **13** checks in both dependency stacks:

| Runtime | Code revision | Python / ZipNN / Torch | Result |
|---|---|---|---|
| Development | f336101 | 3.10.12 / 0.5.4 / 2.13.0 | 13/13 |
| Installed dependencies, checkout code | 4311547 | 3.10.12 / 0.5.4 / 2.14.0 | 13/13 |

Final installed-dependency run: `/tmp/modelark-codec-qualification-b8fo65k8/result.json`.
Its maximum measured RSS across phases was 1,765,400,576 bytes (about 1.64 GiB);
maximum virtual-address peak was 6,774,444,032 bytes (about 6.31 GiB), under the
same 8 GiB AS policy. Source-code fingerprints are in the report. Package
versions and these source hashes are not native-binary attestation.

Local Grok CLI review round 1 found no blockers, with hardening suggestions.
Accounting-error typing, malformed membership rejection and precise test wording
were addressed. Round 2 **ACCEPTED 4311547** with no blockers. Child-process-group
lifecycle, native binary identity and general-purpose worker API remain outside
this unadopted qualification stage; they must not be inferred from its result.

The full core suite is also being run in a disposable user/network namespace:
ordinary sandbox execution blocks localhost sockets, and ordinary host execution
collides with the live portal's intentionally host-wide singleton. Namespace
isolation preserves that guard and does not stop the live service. CI validates
the PR revision independently. These remain HYP-002 evidence, not a blanket
production go decision or a completed physical Slice test.
