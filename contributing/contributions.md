# Contributing to ModelArk

ModelArk is built in public and pre-1.0 — bug reports, fixes, docs, and curation ideas are all
welcome. Expect rough edges, and read the honest gaps in the [README](../README.md#status) first.

By taking part you agree to the (one-line) [Code of Conduct](code_of_conduct.md).

## Development setup

```bash
python3 -m venv .venv-dev
.venv-dev/bin/pip install -e ".[dev]"
```

The `dev` extra installs the test/build tools and Playwright Python package. Normal users get a
non-editable checkout-local install through `python3 scripts/deploy.py`; editable installs are for
development only.

System packages for the full pipeline: `git-annex` (byte storage), `smartmontools` (Disk Health),
and optionally `open-iscsi` (NAS LUN) or `xfsprogs` (XFS formatting). See the README's
**Deploy** and **Manual setup** sections.

## Running the tests

Each test file is a self-contained harness (no pytest needed) — run one, or all:

```bash
.venv-dev/bin/python tests/test_plan.py
for t in tests/test_*.py; do
  [ "$t" = tests/test_e2e_portal.py ] && continue
  .venv-dev/bin/python "$t"
done
```

A file prints `all passed` on success and raises (non-zero exit) on the first failing assertion.
CI runs the core suite on every push and PR; the browser E2E is a separate job (below).
Run the correctness-oriented static checks with `.venv-dev/bin/ruff check modelark scripts tests`.

### End-to-end tests

The portal has a headless Playwright smoke test (`tests/test_e2e_portal.py`): it seeds a throwaway
catalog in a temporary data directory, boots the portal, and drives a real browser (select a plan →
catalog → the over-cap banner). It never moves, replaces, or opens your normal catalog. Install the
browser once in the development environment:

```bash
.venv-dev/bin/playwright install chromium
.venv-dev/bin/python tests/test_e2e_portal.py
```

CI runs it as a separate job. **If you touch the portal UI, add an E2E assertion like it** — this test
already caught a real banner bug the unit tests missed.

## How the project records decisions

Architecture and policy decisions live in [`docs/decision_log.md`](../docs/decision_log.md) — an
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

At this alpha stage the project moves quickly, so the smoothest way to contribute is a **fork PR** —
carrying local changes tends to get hard to merge as things shift under you:

1. **Fork** the repo and branch on your fork.
2. **Contribute** — keep changes focused; include a test when you fix a bug or add behavior, and
   explain *why*, not just *what* (the reasoning is what the ledger captures).
3. **Push** your branch and open a PR back here. CI runs the suite on every PR; I review and apply
   them quickly.

## Reporting bugs

Open an issue with what you ran, what you expected, and the relevant log lines
(`journalctl --user -u modelark.service` if you run the supervised service). For anything security-sensitive, see
[`SECURITY.md`](../SECURITY.md).
