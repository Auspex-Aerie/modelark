# ModelArk (modelark)

An ark for open model weights: catalog open models broadly, archive curated
language-model weights across an offline git-annex drive library, and verify each
is intact and loadable. See `README.md` for architecture and `catalog_discussions.md`
for the evolving catalog scope (what we collect and why).

Package: `modelark.core` (shared catalog/db) + `modelark` (discover/verify/cli).
Tooling: `.venv` (hf 1.x, duckdb, httpx), `hf` CLI for Hub auth, `git-annex` for bytes.

## Decision log

Decisions, deferrals, and hypotheses for this project are recorded in
`docs/decision_log.md` — an append-only,
[ADRLight](https://github.com/Indubitable-Industries/ADRLight)-style ledger (this
repo is `Auspex-Aerie/modelark`). Record architecture/policy decisions there as
you make them, following the format at the top of that file; append only, never
rewrite past entries (status updates excepted).
