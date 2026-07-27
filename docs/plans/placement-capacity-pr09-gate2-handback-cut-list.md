# PR-09 Gate-2 handback — closed cut-list remediation

**Date:** 2026-07-26
**Status:** Remediation complete — **not accepted**. Gate 3 unauthorized. PR-10 unauthorized.
**From:** implementer
**To:** human Gate-2 reviewer
**Implements:** `docs/plans/placement-capacity-pr09-gate2-passback-cut-list.md`

## Tips

| Item | Value |
|------|--------|
| **Remediation tip (Greptile/CI)** | `10a9be9e6548d0df1640aada39ca06924a79346f` |
| **Parent of remediation tip** | `74698fbe3e3060b6c7f9ceffd5f449845ca9ea9d` (DEC-051 code) |
| Pin released from | `1939786b0742352df96563cea0d438d9eb6f5e7b` |
| Draft PR | https://github.com/Auspex-Aerie/modelark/pull/54 |
| Branch | `fix/placement-capacity-pr09-execution-projection` |

This handback commit is docs-only after a green Greptile/CI tip; if present as HEAD, its parent is the remediation tip above.

## Five items

| # | Item | Disposition |
|---|------|-------------|
| 1 | DEC-051 fail-closed in `_archive_content_satisfies` | Done. Both-null fails closed on source and target. Cite DEC-051; DEC-022 removed from satisfaction docstring. Regressions: pure matrix + present/equal, present/unequal, present/null-archive, absent/present, absent/absent on both paths (`tests/test_gate2_findings_33_38.py`). |
| 2 | Independently verify impact counts | **Matches reviewer table exactly** (production join: `archived` on `(repo_id, rfilename, drive_label)`, `repo_id`/`target_drive` from `proposal_tasks`, approved proposal only). See table below. |
| 3 | Reconcile refresh statements | Live JSON is authority: source `fill._drain_projection`, total **3** = 2 batch + 1 typed, generator `gate2-b12-rfc001-copy-v4`. Superseded handbacks/passbacks: header only pointing at cut-list after pin `1939786`; historical numbers left intact. |
| 4 | B−1 in artifact | Evidence JSON + harness emit: equality `total_refreshes = batch_boundary_refreshes + typed_event_refreshes`; bound `batch_boundary_refreshes ≤ transport_batches` (often B−1: final loop re-entry exits terminal without another batch-boundary refresh). |
| 5 | Whitespace + citation | `git diff --check bc33a066..10a9be9` clean. Passback-38-cadence `schema.sql:145` → `:46` for tiny-git-blob note. No self-SHA in commits. |

## Item 2 — independent DEC-051 impact (fixture on disk)

Fixture bytes still local at
`bac9bea888843c47765550239d808977ddc5142d8d38425c74ed51ee06c1522f`
(untracked; not deleted). Approved proposal `824fce33-…`, 9,520 `proposal_files` rows.

| target drive | no archive (unhashed) | archive hashed (unhashed pf) | archive NULL → fails closed | hashed approved |
|---|---|---|---|---|
| `drive-00` | 1,112 | 69 | **265** | 1,190 |
| `drive-01` | 1,926 | 213 | 0 | 1,998 |
| `drive-02` | 1,141 | 0 | 0 | 1,034 |
| `drive-03` | 141 | 0 | 0 | 136 |
| `drive-06` | 12 | 0 | 0 | 283 |
| **all** | 4,332 | 282 | **265** | 4,641 |

Replica source side (`source_drive` join): **497** of **1,887** replica-task file rows are unhashed against a null source digest and hold at `waiting_dependency`.

No material disagreement with the cut-list / DEC-051 ledger figures.

## Known limitation (handback statement)

DEC-051 parks **265** target rows on `drive-00` (both digests null) rather than waving them through. Replica side parks **497** source-unproven file rows. INC-017 defect class; fail-safe and visible. Whether `repair-hashes` covers them is **PR-10** — not investigated here. DEC-052 implementation deferred to PR-10 (decision_log status already says so).

## Commits this cycle (no amends)

1. `72e8257` — cut-list verbatim (**note:** already-staged fixture `git rm --cached` hitchhiked into this commit; bytes remain on disk)
2. `204795f` — `.gitignore` + DEC-051/052/DEF-032 ledger
3. `74698fb` — DEC-051 code + regressions
4. `10a9be9` — evidence JSON, harness cadence emit, supersede headers, citation fix

## Verification

| Check | Result |
|-------|--------|
| Focused F35/F37/F38 + v6 + projection performance | pass |
| Full pytest | **602 passed** |
| `python tests/test_replan.py` | all passed |
| Ruff (touched) | clean |
| `git diff --check bc33a066..10a9be9` | clean |
| Exact-tip CI (`10a9be9`) | test 3.10 ✅ · test 3.12 ✅ · e2e ✅ |
| Greptile (`10a9be9`) | **pass**, **0** inline comments on tip (round 1; no further rounds) |

## PR meta

Title/body updated to Gate-2 production scope (projection, session lifecycle, content-satisfaction authority, schema v6, B12 evidence, DEC-051 limitation). Stale "tests-only / 44 failed" framing removed.

## Untracked operator files (untouched)

- `docs/plans/placement-capacity-reviewer-handoff.md`
- `docs/plans/placement-capacity-pr09-review-restart-primer.md`
- `docs/plans/placement-capacity-pr10-charter.md`
- `iscsi-login.sh`

## Stop

Do **not** mark ready, merge, begin Gate 3, or begin PR-10.
Pin re-engages on this handback: **no further pushes** until human Gate-2 disposition.
