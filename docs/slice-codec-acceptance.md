# Slice codec acceptance — Stage E

This is an evidence map, not permission to deploy or a physical USB certificate.
The implemented representation is `hf-tree-v1`: original paths and original
bytes, regardless of raw/compressed archive storage. Stored-byte export and
future P2P remain separate deferred work. Synthetic weight payloads establish
byte delivery, not model loadability.

## Public command qualification

Run from the checkout with the dev environment:

```sh
python -m pytest -q tests/test_slice_public_codec_qualification.py
```

The test invokes `modelark.cli.main` with actual `slice preview`, `approve`,
`start`, and `status` arguments. Its dispatch is not mocked. Each case creates
a real private SQLite catalog and git archive fixture, raw/encoded source
files, private transaction state, and a new native output folder. Actual source
fences, descriptor checks, codec dispatch, guarded ZipNN children, atomic
publication, original hashes and final receipts are exercised.

Synthetic inputs are explicitly limited to host inventory/providers, a unique
test launch-socket address, and deterministic available-memory samples. These
tests do not start a separate installed console-script process, measure current
host RAM availability, or prove physical device admission. ZipNN children still
install the real guard. The fixture writer calls the low-level codec API;
production ingestion/writer parity is separate Stage D qualification evidence.

Success cases cover raw, whole ZipNN and StreamZNN with all three supported
byte dtypes, plus optional qualified zstd. A raw JSON sidecar accompanies each
weight file. The checks compare complete original byte strings, sizes and
SHA256s with both output and receipt, ensure no renamed compressed exports,
preserve source/catalog/siblings, and check completed Start is idempotent even
without attachments. Missing optional zstd is a reported skip, not a pass.

## Failure and lifecycle matrix

| Boundary | Evidence | Required outcome |
|---|---|---|
| Public Start, missing attachment / low sampled RAM / truncated or trailing frame | `test_slice_public_codec_qualification.py` | Nonzero JSON result; no output root, claimed-attempt events or completion receipt |
| Public Start, wrong destination | Same | Exact destination refusal; no output mutation |
| Public Start, original digest mismatch / killed real decoder child | Same | No completed weight or success receipt; source and catalog unchanged; child reaped |
| Bounded transport, malformed protocol, blocked pipes, parent death | `test_codec_supervisor.py` | Refusal/cancellation; bounded buffering; child cleanup |
| Stop/source/destination changes while decoding; preflight/claim boundary | `test_slice_guarded_decoding.py` | No detached read authority or orphan; preclaim refusal does not spend FAT attempt |
| Source identity/generation fences | `test_slice_fence_integration.py` | Stale evidence rejected under the shared archive fence |
| Native crash, write failure and authenticated restart | `test_slice_direct_integration.py` | Safe authenticated recovery or refusal; no false receipt |
| FAT postclaim faults and Stop | `test_slice_fat32_transactions.py` | Consumed attempt remains non-resumable; unrelated siblings preserved |

These rows distinguish public-entry-point coverage from lower-layer injections.
They do not assert every fault was reproduced on a physical USB or via the CLI.
The kernel-vfat runner `scripts/qualify_fat32_slice.py` is separately attended;
its disposable loop image is still not a physical USB or archive test.

## Retained limits

- DEC-147 intentionally leaves the legacy zstd excessive-window refusal
  classification edge unchanged. Stage D's local Grok verdict was NOT ACCEPT on
  that finding; the operator retained it, and Codex accepted PR #79 afterward.
- Legacy readers and Slice share RAM methodology, not an identical allowlist.
  Slice's sealed framing/window/version constraints remain in effect.
- Prior 64/100/410MB qualification establishes separately recorded large-input
  behavior; the small Stage E CLI cases do not repeat that measurement.
- A merged PR is not a live deployment. No physical compressed-source USB
  acceptance, all-historical-archive compatibility, or P2P feature is claimed.

Current run and review results are recorded in
`plans/codec-stage-e-progress.md`; unfinished or skipped checks are not accepted.
