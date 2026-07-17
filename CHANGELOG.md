# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) while it is pre-1.0.

## Unreleased

### Added

- First-class staged restore with replica fallback, original-path reconstruction, final hash checks,
  and atomic publication.
- Dry-run-first `systemd --user` deployment and loopback health checks.
- Installed-wheel, standalone, and isolated Playwright acceptance coverage.
- Library repository search and clickable multi-drive filters.

### Changed

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

## 0.1.0 - 2026-07-11

- Initial sanitized release-candidate snapshot; subsequent work remains under **Unreleased**.
