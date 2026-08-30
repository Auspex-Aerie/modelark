# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) while it is pre-1.0.

## Unreleased

## 0.3.0 - 2026-08-30

### Added

- The Fill portal now authors one immutable placement proposal, presents every exact assignment for
  human review, and requires the backend-issued confirmation phrase before approval. Approval never
  starts Fill, and Fill remains gated when that approval is absent or stale.
- An explicit `modelark-provenance-migrate` rehearsal/publication workflow for existing pre-v7 SQLite
  catalogs, with copied-runtime acceptance and a stopped side-by-side cutover guide.
- Operator-confirmed missing-drive loss/exclusion in the portal, followed by one canonical replan;
  unregistered attached hardware remains advisory and never inherits the lost identity.
- Preview-bound replacement-drive registration now shows the service identity, required write
  authority, exact attended permission commands when blocked, and ModelArk's temporary-to-final
  archive directory transition before any storage or catalog mutation.
- Drive reconciliation now emits bounded annex-membership and filesystem-scan progress.

### Changed

- Planning now has one first-class authority shared by CLI, Library, portal preview, proposal
  construction, and approval adapters. Capacity modes remain centrally managed policies rather than
  alternate planners.
- Existing pre-v7 catalogs are no longer auto-migrated on open. Users upgrading from 0.2.0 run the
  documented backup-first provenance migration once; fresh installs require no action.
- Provenance publication preserves and validates the optional `library.json` git-annex map locator in
  the new data directory, preventing a custom archive map from silently falling back to the default.
- Passive attached-drive identity now prefers the exact filesystem/annex UUID pair; serial is
  supporting/fallback evidence and bridge discrepancies are shown without rewriting the catalog.

### Fixed

- Lost/excluded historical plan members now remain visible with their lifecycle/eligibility reason
  while contributing no targets or admitted capacity.
- Proposal preview can no longer bypass identity-bound capacity admission through stale legacy
  `free_bytes`; all planning surfaces report the same typed root blocker and executable task set.
- A mounted, empty filesystem is no longer considered registration-ready unless the running
  service account can traverse and write its root; the physical preparation helper repeats that
  check before creating the hidden git-annex staging directory.
- Full drive reconciliation no longer launches one `git annex whereis` process per archived row or
  descends into `.git/annex/objects`; one target-UUID membership query and a pruned worktree walk
  preserve the same missing-copy refusal and fresh-anchor boundary.
- Passive drive inventory now records protected or otherwise unreadable unrelated mount topology as
  inaccessible evidence instead of allowing one system mount to abort the entire Drives API.
- Successful drive reconciliation now retains its classified inventory in the domain result and
  prints bounded present/missing/debris/extra counts, including explicit notice that unclaimed
  content is retained rather than catalogued or deleted.

## 0.2.0 - 2026-07-20

### Added

- `modelark import` seeds the catalog from a bundled starter export (~4,100 pre-classified models),
  offline and with no Hugging Face token — a fresh install no longer has to re-walk the whole Hub
  before there is anything to curate. Insert-only by default (`--refresh` to overwrite; `--from` to
  point at another export). The sanitized `models.jsonl` is now packaged into the wheel.
- A "Getting started" walkthrough in the README that orders install → seed → drive registration →
  plan → curate/fill, with drive registration surfaced early.

### Changed

- Gated repositories are handled as interactive per-session operator follow-ups (retained notice,
  then one prompt with a fixed-origin Hub link and retry/skip) rather than generic fetch-task
  failures; a plan whose only remaining work is parked gated repos completes as
  `PLAN_COMPLETE_WITH_FOLLOWUPS` (DEC-047 / INC-020).
- Fetch publication now stages every download in verified, same-filesystem staging before it crosses
  into the archive worktree; dangling annex placeholders are recovered by proof, and a systemic
  credential rejection stops the batch immediately instead of churning repositories
  (DEC-046 / INC-018 / INC-019).

### Fixed

- The download no-progress watchdog and the orphaned-partial sweep now recurse into per-file
  subdirectories (`rglob`), so a healthy nested-path shard (e.g. `transformer/…`) larger than the
  stall window is no longer repeatedly false-killed as a transient stall, and its partials no longer
  leak on the archive drive (INC-021).

## 0.1.0 - 2026-07-16

### Added

- First-class staged restore with replica fallback, original-path reconstruction, final hash checks,
  and atomic publication.
- Dry-run-first `systemd --user` deployment and loopback health checks.
- Installed-wheel, standalone, and isolated Playwright acceptance coverage.
- Library repository search and clickable multi-drive filters.

### Changed

- Published the canonical repository on 2026-07-16 after a reachable-history content scan and
  hardened its public settings: accurate archive-integrity metadata, no unused Wiki, dependency and
  secret scanning with push protection, and private vulnerability reporting.
- Reconciled work-graph execution and exact capacity accounting: completed copies reserve no bytes,
  partial copies reserve only missing files, and replica completion requires target-UUID evidence.
- Schema-v2 capacity terminology: `guaranteed` and `compression_aware` replace the ambiguous
  provisioning names, with a backup-first migration and one-release CLI/API compatibility aliases.
- Pickle-only acquisition now fails closed by default; unsupported artifact repositories remain
  visible as typed policy blockers and in the public deferred-support backlog.

### Fixed

- Installed packages use explicit user data/state paths and package all required defaults/assets.
- Physical verification fails on absent mounted bytes and preserves nested archive paths.
- Every newly ingested file records an original-byte SHA-256. `repair-hashes` can audit legacy gaps
  and, only with explicit apply, backfill Git-object-proven bytes after a consistent catalog backup.
- Fill terminals are typed and persistent, planning is read-only unless explicitly applied, and the
  portal no longer hides policy-blocked selections or misaligns planned drive occupancy.

### Security

- The loopback portal enforces Host, Origin, content type, request size, per-process CSRF, output
  escaping, and a restrictive Content Security Policy.
