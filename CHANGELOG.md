# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) while it is pre-1.0.

## Unreleased

- Public-release hardening, packaging, portal security, verified restore, catalog integrity, and
  operator-safe migration/cutover preparation.
- Minimal dry-run-first `systemd --user` deployment and loopback health-check surface.
- Reconciled work-graph execution and exact capacity accounting: completed copies reserve no bytes,
  partial copies reserve only missing files, and replica completion requires target-UUID evidence.
- Schema-v2 capacity terminology: `guaranteed` and `compression_aware` replace the ambiguous
  provisioning names, with a backup-first migration and one-release CLI/API compatibility aliases.

## 0.1.0 - 2026-07-11

- Initial sanitized public snapshot.
