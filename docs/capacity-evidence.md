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

Synthetic bf16-like round-trip and incompressible raw-fallback tests pass. A high-water run against an
operator-approved representative real bf16 shard remains required before Phase 3 executor activation.
