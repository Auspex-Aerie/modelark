# Contributing to ModelArk

ModelArk is built in public and pre-1.0 — bug reports, fixes, docs, and curation ideas are all
welcome. Expect rough edges, and read the honest gaps in the [README](README.md#status) first.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .          # installs modelark + deps (huggingface_hub, duckdb, httpx, zipnn)
```

System packages for the full pipeline: `git-annex` (byte storage), `smartmontools` (Disk Health),
and optionally `open-iscsi` (NAS LUN). See the README's **Setup** section.

## Running the tests

Each test file is a self-contained harness (no pytest needed) — run one, or all:

```bash
.venv/bin/python tests/test_plan.py
for t in tests/test_*.py; do .venv/bin/python "$t"; done
```

A file prints `all passed` on success and raises (non-zero exit) on the first failing assertion.
CI runs the whole suite on every push and PR.

## How the project records decisions

Architecture and policy decisions live in [`docs/decision_log.md`](docs/decision_log.md) — an
append-only, [ADRLight](https://github.com/Indubitable-Industries/ADRLight)-style ledger. If your
change makes an architectural or policy call, add an entry following the format at the top of that
file. **Append only** — never rewrite past entries (status updates excepted). The ledger is also the
project's narrative; skim it to understand *why* things are the way they are.

## Code conventions

- **Match the surrounding style** — naming, comment density, idioms.
- **Fail loud, not silent.** Direct access is the default; a missing value should crash at the
  source, not be papered over by a silent fallback that defers the bug to distant code.
- **Respect the safety invariants** (README → *Safety invariants*): canary-before-drop, no silent
  under-replication, the codec gate. Don't weaken an integrity guarantee for speed.
- **Keep the large-shard paths O(chunk).** The streaming compressor (StreamZNN) exists so a giant
  shard never buffers whole-file; don't reintroduce that.

## Pull requests

Work on a feature branch and open a PR. Keep changes focused, and include a test when you fix a bug
or add behavior. Explain *why*, not just *what* — the reasoning is what the ledger captures.

## Reporting bugs

Open an issue with what you ran, what you expected, and the relevant log lines
(`journalctl -u modelark` if you run the supervised service). For anything security-sensitive, see
[`SECURITY.md`](SECURITY.md).
