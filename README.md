# ModelArk

<p align="center">
  <a href="https://github.com/Auspex-Aerie/modelark/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Auspex-Aerie/modelark/actions/workflows/ci.yml/badge.svg"></a>
  <a href="CHANGELOG.md#030---2026-08-30"><img alt="Release: v0.3.0 Public Alpha" src="https://img.shields.io/badge/release-v0.3.0%20Public%20Alpha-orange"></a>
  <a href="pyproject.toml"><img alt="Python 3.10–3.12" src="https://img.shields.io/badge/python-3.10%E2%80%933.12-3776AB?logo=python&amp;logoColor=white"></a>
  <a href="docs/deployment.md"><img alt="Linux" src="https://img.shields.io/badge/platform-Linux-FCC624?logo=linux&amp;logoColor=black"></a>
  <a href="LICENSE"><img alt="Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://buymeacoffee.com/auspexlabs?new=1"><img alt="Buy Me a Coffee" src="https://img.shields.io/badge/Buy_Me_a_Coffee-support-AuspexLabs-FFDD00?logo=buymeacoffee&amp;logoColor=black"></a>
  <a href="https://www.greptile.com/?utm_source=oss_badge&amp;utm_medium=readme&amp;utm_campaign=greptile_for_open_source"><img alt="Greptile: The War on Bugs" src="https://www.greptile.com/badge.svg"></a>
</p>

> **ModelArk 0.3.0 is a public alpha.** It is usable today as an operator-attended archive and
> disaster-recovery system for open model artifacts. Interfaces can still change before 1.0, and
> consequential storage work remains explicit and reviewable.

ModelArk helps you **store now, prove what you have, and recover later**. It catalogs open model
artifacts broadly, archives a curated set across offline or attached git-annex storage, records
distinct evidence about those copies, and restores verified Hugging Face-compatible trees when you
need them.

It does not turn a catalog row into a claim that a model is loadable, or a stale location record into
a claim that a disk is healthy. That evidence boundary is the point of the project.

## What it does

- Seeds an offline catalog of roughly 4,100 classified models, or discovers current metadata from
  the Hugging Face Hub.
- Curates explicit plans and shows the exact placement proposal before an operator approves it.
- Archives supported artifacts across a finite drive fleet with capacity admission, resumable work,
  content hashes, and lossless compression where it is safe and useful.
- Tracks physical locations through git-annex while keeping catalog, remote-header, ingestion,
  copy-location, and physical-verification evidence distinct.
- Reconciles attached drives against stable filesystem and annex identity without silently adopting
  unrelated files.
- Restores into a hidden staging tree, verifies original-byte hashes, and publishes the recovered
  model only after the complete result passes.

The normal lifecycle is:

```text
catalog → curate → plan → review → approve → fill → verify → restore
```

Approval and execution are separate. Approving a placement never starts Fill.

## A storage primitive, not only an archive app

Model weights are the first demanding workload, but the durable core is more general: artifact
identity, manifests, placement, copy evidence, resumable movement, and verified materialization.
That makes ModelArk a storage primitive for later local-model work rather than another cache tied to
one inference engine.

The same boundary can support future peer-to-peer work: exchange a sealed artifact set, preserve its
evidence, and materialize a verified destination without pretending transport alone proves
usability. **Usable Slice**, local delivery adapters, and P2P transport are future work, not shipped
0.3.0 features.

## Start here

Detailed commands live in focused guides so this page can stay readable:

- [Fresh install and first archive](docs/getting-started.md)
- [Operating ModelArk: plans, drives, Fill, verification, and restore](docs/operations.md)
- [Upgrade from 0.2.0 or another pre-v7 catalog](docs/upgrading.md)
- [Supervised Linux deployment and rollback](docs/deployment.md)
- [Stopped side-by-side provenance migration](docs/provenance-live-cutover.md)

ModelArk runs locally and the portal binds to `http://127.0.0.1:8077`. There is deliberately no
remote-listen mode until authentication exists.

## The evidence contract

ModelArk keeps several statements separate because they answer different questions:

| Evidence | What it means |
|---|---|
| Catalog metadata | The repository and declared artifacts are known. |
| Remote-header verification | The declared safetensors layout or basic GGUF header structure was checked without downloading all tensor bytes. |
| Ingestion evidence | Downloaded original bytes matched the available digest and any compressed representation passed a round-trip canary. |
| Copy/location evidence | git-annex records that a particular annex remote should hold the content. |
| Physical verification | A mounted copy was read and verified again. |
| Verified restore | The complete requested tree was reconstructed and passed its recorded original-byte hashes before publication. |

See [capacity evidence](docs/capacity-evidence.md) and the [Fill pipeline](docs/fill_pipeline.md) for
the detailed contracts.

## Safety by construction

- **No in-place catalog upgrades.** ModelArk 0.3.0 refuses pre-v7 catalogs and directs the operator
  through a stopped, backup-first, side-by-side migration.
- **No canary, no drop.** A compressed artifact cannot replace its original until decompression
  reproduces the original digest.
- **No silent under-replication.** Required copies remain explicit requirements; missing capacity or
  identity evidence blocks work.
- **No inferred drive identity.** A stable filesystem UUID plus annex UUID is authoritative;
  enclosure serials are supporting observations.
- **No automatic approval or Fill.** The operator reviews one immutable proposal, approves it with
  its exact phrase, and starts work separately.
- **No automatic deletion of extras.** Reconciliation reports unclaimed content but does not adopt
  or remove it.

## 0.3.0 status

Version 0.3.0 is the first schema-v7 release line. It adds the backup-first provenance migration,
identity-bound capacity evidence, canonical placement and approval, execution-session fencing,
drive lifecycle handling, and the operator-facing exact proposal review.

The release path is covered by the full test suite, installed-wheel migration smoke, packaging,
hostile-web checks, and standalone browser acceptance. Alpha still means operator attention matters:
large downloads can be expensive to retry, some USB bridges report weak SMART identity, functional
model loading is not a verification tier yet, and supported artifact policy remains intentionally
conservative.

The [changelog](CHANGELOG.md), [roadmap](docs/roadmap.md), and append-only
[decision ledger](docs/decision_log.md) carry the detailed status and rationale.

## Project map

```text
modelark.core        catalog and database primitives
modelark             discovery, planning, archive, verification, and restore
git-annex map        private byte/location authority
drive fleet          the actual offline, attached, or network-backed content
```

Useful deeper references:

- [Architecture and placement hardening](docs/plans/placement-capacity-hardening.md)
- [Migration acceptance record](docs/rfcs/001-migrated-runtime-acceptance.md)
- [First-class placement and approval design](docs/rfcs/002-first-class-placement-approval.md)
- [Deferred artifact-format support](docs/deferred-artifact-support.md)
- [Security policy](SECURITY.md)

## Contributing and support

Bug reports, migration feedback, documentation fixes, curation ideas, and code contributions are
welcome. See [Contributing](contributing/contributions.md) before opening a change.

If ModelArk helps preserve the models you care about, you can
[support AuspexLabs](https://buymeacoffee.com/auspexlabs?new=1). Testing and thoughtful failure
reports are equally valuable.

ModelArk is licensed under [Apache-2.0](LICENSE). The standalone
[`modelark/streamznn.py`](modelark/streamznn.py) module retains its embedded MIT license. Model
weights are not distributed by this repository.
