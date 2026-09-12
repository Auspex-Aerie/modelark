# 0.4 Beta release checklist

Decision: DEC-152. Prerequisite PR #82 merged as
`e3c8b84d480febeb9b9ca1c884d472daaf3705af`.

## Release identity and scope

Use package/runtime version **0.4.0**, Git tag **v0.4.0**, and title
**ModelArk v0.4.0 — Public Beta**. Beta is the product maturity label and package
development classifier; this does not introduce a `0.4.0b1` version. The GitHub
release is marked **pre-release**, consistent with the pre-1.0 Beta designation.
Do not backfill or move historical version tags.

The release change synchronizes README, package/runtime metadata, release tests,
changelog, release notes, upgrade instructions and current operator-guide status.
Preserve historical release notes and ledger entries. It changes no catalog
schema, executable policy, codec behavior, archive bytes or deployed service.

## Before merge

- Verify the version and Beta classifier agree in the checkout and built wheel;
  check installed distribution metadata, CLI version and packaged public assets.
- Run release/CLI regressions and repository lint; require full CI on the final head.
- Review documentation links, evidence scope and independent catalog/private-state
  compatibility. Do not promote a fixture result to universal hardware support.
- Local Grok CLI up to three review/fix rounds for this release scope, then a fresh
  PR with cloud Codex up to three rounds. No Greptile under the current exclusion.
  Review each pushed head; do not reset counters per commit. Mutable counters and
  outputs belong in the operator handoff, not status-only PR commits.
- Ask the operator explicitly in bold to merge when current-head review and CI pass.

## After the operator merges

1. Verify the actual merge commit and a clean source tree; rebuild release wheel
   and source archive from that commit. Recheck installed identity and artifacts;
   record SHA-256 checksums. Never label a pre-merge artifact as a merge-commit build.
2. Verify that `v0.4.0` is absent remotely. Create the annotated tag against the
   verified release commit, never a moving branch name; never overwrite a tag.
3. Publish the GitHub **pre-release** with the versioned release notes, tested
   wheel/source archive and checksums; verify tag, target commit, title and assets.
4. Record the release URL and exact artifact identities. Keep the existing runtime,
   journals and data unchanged. PyPI publishing and live service rollout are separate
   operations, not part of this release checklist's authority.

No Git tag or release has been published by preparation of this file. If merge or
publication authority is unclear at execution time, ask rather than infer it.
