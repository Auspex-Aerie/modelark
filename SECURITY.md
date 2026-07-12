# Security Policy

ModelArk is pre-1.0 and built in public. If you find a vulnerability — especially anything that
could cause **silent data loss** or a **false integrity pass** (an archive that reports "verified"
but isn't actually restorable) — please report it privately rather than opening a public issue.

## Reporting

- **Preferred:** open a private security advisory on GitHub (repo → **Security** → **Advisories** →
  *Report a vulnerability*).
- **Or** contact the maintainers at: `[INSERT CONTACT]`.

Please include repro steps and the affected version/commit. We'll acknowledge and work a fix. There
is no bounty program.

## What matters most

ModelArk's core promise is integrity: every compression is gated by a round-trip **canary** before
the uncompressed original is ever dropped (`DEC-003`), and copy counts come from a durable record,
not a guess. Reports that undermine those guarantees — a canary that can be fooled, a path where an
original is dropped before its restore is proven, an under-replication that goes unreported — are the
highest priority.
