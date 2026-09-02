# ModelArk

<p align="center">
  <a href="https://github.com/Auspex-Aerie/modelark/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Auspex-Aerie/modelark/actions/workflows/ci.yml/badge.svg"></a>
  <a href="CHANGELOG.md#031---2026-09-02"><img alt="Release: v0.3.1 Public Alpha" src="https://img.shields.io/badge/release-v0.3.1%20Public%20Alpha-orange"></a>
  <a href="pyproject.toml"><img alt="Python 3.10–3.12" src="https://img.shields.io/badge/python-3.10%E2%80%933.12-3776AB?logo=python&amp;logoColor=white"></a>
  <a href="docs/deployment.md"><img alt="Linux" src="https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&amp;logoColor=black"></a>
  <a href="LICENSE"><img alt="Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
</p>

> **ModelArk 0.3.1 is a public alpha.** It is usable today as an operator-attended archive and
> disaster-recovery system for open model artifacts. Storage work remains explicit and reviewable,
> and interfaces can still change before 1.0.

ModelArk helps you **preserve selected model artifacts, prove what you have, and recover a verified
working tree later**. It catalogs broadly, archives a curated set across offline or attached drives,
keeps distinct evidence about every copy, and restores Hugging Face-compatible trees only after the
complete result verifies.

ModelArk is an evidence-driven storage and recovery system. It tracks model artifacts from discovery
through ingestion, replication, and physical verification while preserving the distinction between
historical records and current storage state. That trustworthy evidence trail is the foundation of
the product.

```text
catalog → curate → plan → review → approve → fill → verify → restore
```

Approval and execution are separate. Approving an exact placement never starts Fill.

## Start here

| I want to… | Use this guide |
|---|---|
| Install ModelArk on a new Linux host and create the first archive | [Fresh install and first archive](docs/getting-started.md) |
| Operate plans, drives, Fill, verification, and restore | [Operating ModelArk](docs/operations.md) |
| Upgrade a 0.2.0 or other pre-v7 catalog | [Upgrading ModelArk](docs/upgrading.md) |
| Install or update the supervised user service | [Deploying ModelArk](docs/deployment.md) |
| Perform the stopped side-by-side provenance migration | [Live cutover procedure](docs/provenance-live-cutover.md) |

The portal is local-only at `http://127.0.0.1:8077`. ModelArk deliberately has no remote-listen mode
until authentication exists.

## What ModelArk protects

- A broad metadata catalog and an explicitly curated archive plan.
- Supported model artifacts placed across a finite git-annex drive fleet.
- Original and stored-byte hashes, copy locations, and physical-verification results without
  collapsing them into one vague “available” state.
- Verified restores published only after the requested tree is complete.

## Evidence, not assumptions

| Evidence | What it actually means |
|---|---|
| Catalog metadata | The repository and its declared artifacts are known. |
| Remote-header verification | The declared safetensors layout or basic GGUF structure was checked without downloading every tensor byte. |
| Ingestion evidence | Downloaded original bytes matched available digests; compressed representations also passed a round-trip canary. |
| Copy/location evidence | git-annex records that a specific annex remote should hold the content. |
| Physical verification | A mounted copy was read and verified again. |
| Verified restore | The full requested tree was reconstructed and passed its recorded original-byte hashes before publication. |

See [capacity evidence](docs/capacity-evidence.md) and the [Fill pipeline](docs/fill_pipeline.md) for
the detailed contracts.

## Safety and recovery

- **No in-place legacy upgrade.** Pre-v7 catalogs are migrated through a stopped, backup-first,
  side-by-side procedure.
- **No inferred disk identity.** Filesystem and annex identity—not an enclosure label—authorize a
  registered drive.
- **No silent under-replication.** Missing capacity, identity, or provenance evidence remains a
  typed blocker.
- **No automatic approval or Fill.** The operator reviews one immutable proposal, approves its exact
  placement, and starts execution separately.
- **No canary, no drop.** A compressed artifact cannot replace its original until decompression
  reproduces the original digest.
- **No automatic deletion of extras.** Reconciliation reports unclaimed content but does not adopt
  or remove it.

## What changed in v0.3.1

Version 0.3.1 is an application-only patch over the schema-v7 0.3.0 release. It separates planned
work from archived occupancy in the Fill display, marks lost/excluded drives unambiguously, and
hardens per-drive live occupancy refresh. Existing 0.3.0 catalogs need no migration, and updating
still does not start or resume Fill automatically.

Read the [v0.3.1 release notes](docs/releases/v0.3.1.md) or the
[changelog](CHANGELOG.md) for the complete patch record.

## What changed in v0.3.0

There are **a lot of backend disk-safety and workflow additions** in this release. If you run into an
issue, ModelArk is designed to stop with evidence, preserve completed work, and give you a documented
recovery, retry, migration, or rollback path instead of guessing.

In brief, v0.3.0 adds:

- backup-first schema-v7 migration with rehearsal, no-clobber publication, and retained rollback;
- identity-bound capacity admission, drive lifecycle handling, and safer reconciliation;
- canonical placement planning with exact proposal review and explicit approval;
- resumable, fenced Fill sessions with attended drive-loading workflow;
- stronger provenance, hash repair, physical verification, and staged restore contracts; and
- focused install, operations, deployment, upgrade, migration, and recovery guides.

Read the [v0.3.0 release notes](docs/releases/v0.3.0.md) or the
[changelog](CHANGELOG.md) for the complete release record.

## Becoming a storage primitive

Model weights are the first demanding workload, but the durable core is more general: artifact
identity, manifests, placement, copy evidence, resumable movement, and verified materialization.
ModelArk is becoming an evidence-preserving storage primitive for later local-model tools rather
than another cache tied to one inference engine.

That same boundary supports two future directions:

- **Usable Slice:** ask for a named subset of the existing catalog, then materialize and verify it
  for a specific consumer or machine.
- **Peer-to-peer transport:** exchange sealed artifact sets while preserving evidence instead of
  treating successful transport as proof of usability.

Usable Slice, local delivery adapters, scratch transfer, and P2P transport are future work—not
shipped features.

## Project and support

The [roadmap](docs/roadmap.md), [architecture plan](docs/plans/placement-capacity-hardening.md), and
append-only [decision ledger](docs/decision_log.md) carry deeper status and rationale. See
[Contributing](contributing/contributions.md) before opening a change, and report vulnerabilities
through the [security policy](SECURITY.md).

If ModelArk helps preserve the models you care about, you can
[support AuspexLabs](https://buymeacoffee.com/auspexlabs?new=1). Testing and thoughtful failure
reports are equally valuable.

ModelArk is licensed under [Apache-2.0](LICENSE). The standalone
[`modelark/streamznn.py`](modelark/streamznn.py) module retains its embedded MIT license. Model
weights are not distributed by this repository.
