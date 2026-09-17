# Stage 1 native annotation integration qualification

Date: 2026-09-14. Scope: tiny synthetic fixtures only; no live archive/catalog access.

Result: **8 grouped checks passed**, all four final repository formats 8. This is
targeted evidence for preserving existing Fill tags, **not Stage 1 acceptance**.
Local Grok Stage 1 review has not started (0/3).

Run: `scripts/qualify_annex_annotations.py` using the top-level `.venv-dev/bin/python`.
The script inherits the accepted Stage 0 pinned installed-tool/filter profile and its
Git-only input quarantine plus isolated native annex merger. It invokes no newer client.

Retained fixture: `/tmp/modelark-annex-payload-stage0-1b1yx9ne` (the shared harness
supplies the directory prefix; this run is Stage 1 integration work).
Result: [exact recorded JSON](annex-annotations-stage1-2026-09-14.json), SHA256
`d86d50ba59b14904403681aeb0ae5cd45b52dafcb28d719fdbac31ac615aed23`.
Script SHA256: `9f4bef07f33cf3a0035dd1b9119adc217657756c1e74afc16a230c1f91c0b99d`.
The result records all validator/harness source hashes; preserve them as evidence of
what actually ran. Later code changes require a fresh run, not rewritten old evidence.

## What was learned and checked

Existing `fetch._annex_metadata` uses native `-s` assignments for model, format,
quantization and optional parameter count. Native `.log.met` records differ from
location logs: superseded timestamped field/value cells can be compacted away,
replacement records carry removals, and repeated assignments may add a timestamp.
Values needing escaping use `!base64` with UTF-8. A missing parameter assignment
preserves its previous value, matching existing Fill behavior.

Four source-write cases checked initial assignment, replacement, repeated assignment,
and encoded Unicode/space values with omitted parameters. Every parsed effective
field set was compared with the actual native JSON metadata reader.

An independent map tag for another repository sharing the same annex key survived
the staged native merge alongside the source's model tag. Neither tag claims payload
presence on the map. The map remained unchanged and contentless; only its isolated
candidate was merged. Unexpected source tag changes after the admitted snapshot
were refused before the native merger ran. A separately introduced unplanned same-key
field was refused by the source-write validator without relying on a wrong model value
as a confounding negative control.

Additional pure tests cover malformed/ambiguous grammar, tied conflicting clocks,
unencoded control characters, future/nonadvancing clocks, unrelated changes, missing
tombstones, no-op assumptions and loss of unrelated same-key tags during merging.

## Limits

The fixture's selected-key snapshot is synthetic authority. Production still needs
the approved profile/fence/authority-bound source proof factories, durable obligations,
catalog CAS, map/ref/index replay and generation closure. `parse_annotations` does not
admit arbitrary incoming records; `check_annotation_merge` requires an independently
admitted source snapshot. Tags remain advisory, never content identity or copy proof.
No production schema floor, conversion command, Fill behavior, physical drive or live
map was changed by this experiment. Existing best-effort tag policy is not silently
replaced by this qualification result.

Earlier retained developmental runs also passed eight grouped checks:
`/tmp/modelark-annex-payload-stage0-h0946p7m` and
`/tmp/modelark-annex-payload-stage0-ngtj_xjm`. The final run above strengthens the
independent same-key tag and isolated unplanned-field checks; use its exact source
hashes/result for the current qualification claim.
