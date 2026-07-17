# Security and Bug Policy

ModelArk is pre-1.0 and built in public.

During alpha, security fixes target the latest reviewed `main`; include the exact commit in every
report. Older commits are not maintained as separate supported release lines.

ModelArk has no Auspex-operated application backend. The portal binds only to loopback; Hub fetches
go from the operator's machine to Hugging Face, while git-annex retrieval and replication use the
operator's configured local or network storage. Localhost is still a security boundary: a hostile
webpage may try to send requests to a service on the visitor's machine, and catalog or operator text
must not become executable HTML.

The portal therefore validates the loopback Host and port on every request. Mutations require an
exact same-origin `Origin`, `application/json`, a bounded body, and a per-process CSRF capability.
Structured views escape remote/operator data, responses carry a restrictive CSP, and the server
refuses non-loopback binds because remote authentication is not implemented. Bypasses of any of
these controls are security issues and **we will address them**.

The confusing category — where we expect a lot of reports filed as "security" that are really severe
**bugs** — looks like:

- **Silent data loss** or a **false integrity pass** via some unusual interaction with the local
  portal (an archive that reports "verified" but isn't actually restorable, because of a trick played
  against the local webserver).
- **Cross-origin mutation or script execution** through Host/Origin/CSRF validation gaps, unsafe
  rendering, or a CSP bypass.

A bug like that is serious and we treat it as such — but on its own it is **not adversarial**. It
becomes a *security* issue only if you can show a real attack path: e.g. a download or input that
makes the local app do something arbitrary and harmful (kick off more downloads, reach into the
system beyond its remit, etc.). If you can demonstrate that, treat it as security.

## Reporting

**If it is genuinely adversarial** (an actual attack path, not just a severe bug):

1. Email `dev@auspexlabs.sh` with the subject `ModelArk security report`.
2. If GitHub shows **Security → Advisories → Report a vulnerability**, you may open a private
   advisory and include its link in the email. The email path remains valid when GitHub private
   vulnerability reporting is unavailable.

Please include reproduction steps and the affected version/commit. We'll acknowledge and work a fix.
There is no bounty program at this time; should we spin up public-facing services, we'll initiate one.

**If it's not adversarial** — a bug, especially a serious one — file it in the repo's **Issues** and
tag **@auspexlabs**; we'll review.

**Anything with data loss or potential corruption** — even if it is situational and has not actually
happened yet — prefix the issue title with **`[P1]`** so it is visible even when the repository has no
priority-label taxonomy.

## What matters most (restated)

ModelArk's core promise is integrity: every compression is gated by a round-trip **canary** before
the uncompressed original is ever dropped (`DEC-003`), and copy counts come from a durable record,
not a guess. Security reports are welcome — but please keep the *security* channel to actual
adversarial behavior. For bugs affecting the core promise, use **Issues** and prefix the title
`[P1]`.
