# ModelArk (modelark)

An ark for open model weights: catalog model metadata broadly, archive supported artifacts from a
curated set across an offline git-annex drive library, and record distinct remote-header, ingestion,
copy, and physical-verification evidence. Do not collapse those evidence levels into a claim that
every catalog entry is loadable or every offline copy is currently verified. See `README.md` for the
current product contract and `catalog_discussions.md` for non-normative catalog research.

Package: `modelark.core` (shared catalog/db) + `modelark` (discovery, archive, portal, restore).
Tooling: `.venv` for runtime, `.venv-dev` for tests/builds, `hf` CLI for Hub auth, and
`git-annex` for bytes. DuckDB is optional and used only for legacy migration.

## Decision log

Decisions, deferrals, and hypotheses for this project are recorded in
`docs/decision_log.md` — an append-only,
[ADRLight](https://github.com/Indubitable-Industries/ADRLight)-style ledger (this
repo is `Auspex-Aerie/modelark`). Record architecture/policy decisions there as
you make them, following the format at the top of that file; append only, never
rewrite past entries (status updates excepted).
