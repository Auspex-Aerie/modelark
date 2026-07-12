# Security and Bug Policy

ModelArk is pre-1.0 and built in public.

Keep in mind that at this stage ModelArk is **entirely locally hosted** — the portal binds to
localhost, and downloads are strictly between you and Hugging Face. Adversarial behavior against the
system, from the machine it runs on, isn't a likely scenario. That said, a vulnerability that lets
something **arbitrary and genuinely damaging** happen from a security standpoint is welcome to be
reported, and **we will address it**.

The confusing category — where we expect a lot of reports filed as "security" that are really severe
**bugs** — looks like:

- **Silent data loss** or a **false integrity pass** via some unusual interaction with the local
  portal (an archive that reports "verified" but isn't actually restorable, because of a trick played
  against the local webserver).

A bug like that is serious and we treat it as such — but on its own it is **not adversarial**. It
becomes a *security* issue only if you can show a real attack path: e.g. a download or input that
makes the local app do something arbitrary and harmful (kick off more downloads, reach into the
system beyond its remit, etc.). If you can demonstrate that, treat it as security.

## Reporting

**If it's genuinely adversarial** (an actual attack path, not just a severe bug) — do both:

1. **Strongly preferred:** open a private security advisory on GitHub (repo → **Security** →
   **Advisories** → *Report a vulnerability*).
2. **Then** email `dev@auspexlabs.sh` with a link to your advisory.

Please include reproduction steps and the affected version/commit. We'll acknowledge and work a fix.
There is no bounty program at this time; should we spin up public-facing services, we'll initiate one.

**If it's not adversarial** — a bug, especially a serious one — file it in the repo's **Issues** and
tag **@auspexlabs**; we'll review.

**Anything with data loss or potential corruption** — even if it's situational and hasn't actually
happened yet — mark the issue **P1**.

## What matters most (restated)

ModelArk's core promise is integrity: every compression is gated by a round-trip **canary** before
the uncompressed original is ever dropped (`DEC-003`), and copy counts come from a durable record,
not a guess. Security reports are welcome — but please keep the *security* channel to actual
adversarial behavior. For bugs affecting the core promise, use **Issues** (and P1 them).
