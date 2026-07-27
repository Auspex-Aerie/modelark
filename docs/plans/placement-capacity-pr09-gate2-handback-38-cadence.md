# PR-09 Gate-2 handback — F35/F37 fail-open + F38 cadence

> **SUPERSEDED** by `docs/plans/placement-capacity-pr09-gate2-passback-cut-list.md` (after pin `1939786`). F37-a presence-for-null rule below is historical; live authority is DEC-051 (both-null fails closed). Live refresh instrumentation and generator version remain as in `docs/plans/evidence/b12_390_acceptance_wall_clock.json` (`gate2-b12-rfc001-copy-v4`, source `fill._drain_projection`, 3 = 2 batch + 1 typed).

**Date:** 2026-07-26
**Status:** Remediation complete — not accepted. Gate 3 unauthorized.
**Parent tip:** `768741f74e6c3f7533b83d6f30e3b0db47e8d286`
**Code remediation tip:** `fadc937a549482d445aa17b17f72bfd3f0174458`
**Passback implemented:** `docs/plans/placement-capacity-pr09-gate2-passback-38-cadence.md`

## Closures

### F35-a
Removed `derivation_mode=ecfg:` start fallback. NULL/empty/short `execution_config_hash` refuses unconditionally. Regression: `test_finding_35a_ecfg_derivation_mode_does_not_authorize_start`.

### F35-b
`mark_proposal_pre_pr09_unbound` clears only `execution_config_hash`. `require_bound_execution_config` reads `execution_config_hash`. Regression: `test_finding_35b_unbind_helper_clears_only_config_hash`.

### F37-a — content satisfaction rule (both sides)
- Approved file **with** content hash: archive must match exact non-null hash.
- Approved file **without** content hash (schema: NULL for tiny git blobs): **presence** is durable satisfaction on source and target.
- Acceptance catalog: 4879/9520 proposal_files null sha; 1122/5567 archived null sha — presence rule keeps operation feasible.
- Regressions: null-null both sides; multi-file stale/absent source.

### F37-b
Multi-file source authority regressions for stale second file and absent second file.

### F38-a
Refresh measured only via `fill._drain_projection` with transport mocked:
- 2+ batch_boundary refreshes
- 1 typed_event:gated_retry refresh
- total = sum of breakdown
- Adversarial: blocking drain raises rather than inventing a count

### F38-b / F38-d evidence (this tip's measurement)
| Field | Value |
|-------|--------|
| Generator | `gate2-b12-rfc001-copy-v4` |
| Source | `fill._drain_projection` |
| Refresh total | 3 (2 batch_boundary + 1 typed_event:gated_retry) |
| Transport batches | 3 |
| Pure / full p95 (this tip) | ~0.365 s / ~0.770 s |
| Prior accepted p95 (`00ba101`) | ~0.327 s / ~0.698 s |
| Contract | provisional harness targets (not RFC/decision authority) |
| Fixture path | repo-relative `docs/plans/evidence/b12_390_approved_fixture.sqlite` |
| Fixture SHA | `bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f` |
| Archived / baseline / executable | 5567 / 65 / 429 |
| integrity_check / FK | ok / 0 |
| user_version | 6 |

Supersedes refresh claims in `placement-capacity-pr09-gate2-handback-35-38.md` (claimed 6 synthetic) and `passback-35-38.md`.

### Self-verification class fixes
- v6 short-hash probe requires IntegrityError; success without CHECK failure raises.
- F38 measure fails closed on insufficient batches/typed events / drain not invoked.

## Verification commands (re-runnable)

```text
.venv-dev/bin/python -m pytest tests/test_gate2_findings_33_38.py tests/test_catalog_v6_execution_config_hash.py tests/test_projection_performance_contract.py -q
.venv-dev/bin/python tests/test_replan.py
.venv-dev/bin/python -m pytest -q
.venv-dev/bin/ruff check modelark tests
git diff --check bc33a066 HEAD
```

## Stop

Do not mark ready, merge, Gate 3, or PR-10. Fixture sqlite untouched. Untracked operator files preserved.
