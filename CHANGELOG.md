# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) while it is pre-1.0.

## Unreleased

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
