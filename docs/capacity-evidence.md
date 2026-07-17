# Capacity-ledger empirical evidence

This file records sanitized, reproducible evidence required by DEC-045. It contains no archive paths,
drive identifiers, or model data.

## git-annex directory-remote target staging

- Date: 2026-07-14
- Host stack: Linux; git-annex `8.20210223`; local filesystem-backed `directory` special remote
- Fixture: one newly generated 8 MiB blob in disposable `/tmp` source and target directories
- Method: `strace -ff` over `git annex copy <key> --to <directory-remote>`, tracing file opens,
  writes, links, renames, and unlinks

Observed publication sequence:

```text
open target/tmp/<key>/<key> for write
write 8 MiB object
rename target/tmp/<key>/ -> target/<hash>/<hash>/<key>/
```

The temporary directory is renamed into its final object directory. The trace showed no interval in
which both a complete target temporary object and a separate complete final object coexisted; final
target content was exactly 8 MiB. This supports a zero *additional* target workspace term for this
specific directory-remote/filesystem path.

Phase 2 deliberately retains the reviewed conservative bound—per-drive durable sum plus the maximum
single replica task—until implementation review accepts whether this trace is representative of every
supported target. The executor therefore does not rely on the observed optimization.

## StreamZNN output ceiling

Automated tests enforce the proof boundary for every compression path:

- StreamZNN rejects an expanded chunk in memory before writing its frame;
- whole-file ZipNN rejects an expanded blob before its first output write; and
- zstd checks accumulated output plus the next chunk before each write.

The guaranteed StreamZNN ceiling is:

```text
raw_size + len(SZNN_MAGIC) + 4 * ceil(raw_size / chunk_size)
```

Synthetic bf16-like round-trip and incompressible raw-fallback tests pass. The required real-shard
gate was run on 2026-07-14 against an operator-approved archived safetensors shard restored into
disposable scratch. Before measurement, the 29,359,329,168-byte restore matched the catalog's
canonical SHA-256 exactly. The source repository, filename, drive label, and local paths are omitted.

```text
tensor dtypes:                 BF16, F32
chunk bytes:                   67,108,864
input bytes:                   29,359,329,168
StreamZNN output bytes:        19,472,559,872
filesystem high-water bytes:   19,472,559,872
enforced cap bytes:            29,359,330,925
cap headroom bytes:             9,886,771,053
compression ratio:                       0.663249
compression duration seconds:                 68.289
round-trip SHA-256 verified:                    yes
```

The measured high-water remained 9.89 GB below the enforced raw-plus-framing ceiling and no expanded
write occurred. The output size also matched the independently archived StreamZNN object's recorded
size, providing a reproducibility cross-check. This closes the real-bf16 gate for Phase 3.

The repository provides a sanitized, self-cleaning collector. It validates that the input is a BF16
safetensors shard, writes only below the explicit scratch directory, verifies the restored SHA-256,
and omits the source filename and path from its JSON:

```bash
.venv-dev/bin/python scripts/phase3_gate_evidence.py streamznn \
  /path/to/representative/model-00001-of-N.safetensors \
  --scratch-dir /path/to/disposable/scratch
```

## Copied-catalog release-host replay

Phase 3 also requires a read-only replay of an operator-approved copied or sanitized catalog. The
collector opens the supplied catalog with SQLite URI `mode=ro`, performs no bootstrap or journal-mode
mutation, times the complete graph/ledger/legacy comparison, and exercises concurrent-reader behavior
only on an automatically removed scratch clone:

```bash
.venv-dev/bin/python scripts/phase3_gate_evidence.py catalog \
  /path/to/copied/catalog.sqlite \
  --scratch-dir /path/to/disposable/scratch
```

The output contains aggregate counts, graph hash, comparison status, and p95/max latency only. It
does not contain repository ids, filenames, drive labels, or local paths. The release-host gate is
500 ms p95 over 20 measured graph-plus-ledger runs after one warm-up. The legacy planner comparison
is executed once and reported separately because it is a review seam, not part of the Phase 3
production executor path.

The required release-host replay was run on 2026-07-14. The live legacy source was opened with SQLite
URI `mode=ro` and copied through SQLite's consistent backup API; only the disposable copy received
current schema migrations. The migrated copy passed `integrity_check` and `foreign_key_check` and
contained 2,311 archived rows across 444 selected repositories.

```text
executor graph + ledger samples:      20
executor p95 milliseconds:       271.724
executor maximum milliseconds:   329.878
release-host budget milliseconds:    500
concurrent-writer clone read:          yes
capacity byte failures:                  0
shadow legacy comparison ms:       552.885
```

The production graph-plus-ledger path passes its latency/locking gate. An initial collector revision
incorrectly timed the shadow-only legacy planner too and reported about 601 ms; separating that review
seam produced the figures above. The replay also exposed 54 blocking manifest-policy diagnostics (50
pickle-only selections under the safe default and four unsupported artifact formats) plus 95 policy-
drift warnings. Those are not byte-capacity failures. The operator subsequently retained the safe
pickle default, parked the 54 unsupported selections in `docs/deferred-artifact-support.md`, and
approved Phase 3 adoption; the decision and reviewed rollout are recorded under `DEC-045`.
