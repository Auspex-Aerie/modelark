# Folder projection review

Scope: folder-projection candidate based on PR #70 head `6fd87c3`, including native ext4 and
FAT32 single-attempt folder delivery. The operator authorized local Grok review first, then a
fast-forward push to PR #70 (`codex/usable-slice-direct-usb`) and up to three Greptile/Codex web
review rounds. No merge, deployment, archive repair or physical-device writes are authorized.

## Qualification evidence

- Full pre-review Slice suite: 1,287 passed, 5 optional-codec skips, two upstream Torch warnings.
- Independently installed wheel: 622 passed with import-location assertion and source match.
- Operator-reported expanded kernel-vfat disposable-image run: passed, retained at
  `/tmp/modelark-fat32-qualification-ru4sibe7`. Includes public Preview/Approve/Start with synthetic
  source/backing, real case/numbered-short-alias refusal, no-replace/flush, verified original
  bytes/report, all 30 interruption boundaries and sibling preservation.
- No physical USB or real archive acceptance. These evidence levels remain distinct.

## Local review

Pass 1: Grok returned **REQUEST_CHANGES** with two P2 findings and no established P1. Local
session `01a0853d-6a61-7733-bcd4-dd3b0e7deb3c`; no tests were run by Grok itself.

- FAT caught aborts dropped the lease before publishing ended state. Four corrected fault-hook
  tests reproduced the stale `transferring` state at real creation/payload boundaries, for Ctrl+C
  and RuntimeError. A FAT-only shared abort-close helper now requests/acknowledges Stop before
  lease release, then always closes the destination. The public wrapper avoids a duplicate Stop
  request after engine acknowledgment. Native/legacy abort behavior is unchanged. Four tests pass.
- Native inode checks required only one free inode. The plan now computes the total file/directory
  inode budget; Preview compares fresh known free inodes, and runtime subtracts distinct live
  authenticated inodes. The shared allocation auditor counts identities once across temporary/final
  hard links while preserving the legacy bytes-only interface. Tests cover pre-root refusal/retry,
  partially published links, and zero free inodes after the final receipt exists.

Both Grok corrections plus affected native/FAT paths: **240 source tests passed**. The legacy
destination/transaction/authority/operator/direct-integration/probe subset passed **343 tests**.
No schema or serialized plan field changed.
The final rebuilt wheel in `/tmp/modelark-grok-review-wheel.MjCxF8/package` passed **642 native/
FAT32 folder tests**, including installed-import-location proof and exact packaged-source match.

Pass 2: Grok returned **ACCEPT**, closing both P2s and finding no new supported P1/P2 in the
focused follow-up, including the independent IO corrections below. No third local pass needed.
Residual limits explicitly retained: SIGKILL/private-state failure cannot guarantee a durable Stop;
an interrupt inside acquisition after claim but before handoff can retain last-known active state.
The consumed-attempt fence still forbids resumed FAT writes. Grok treated that narrow handoff
window as non-blocking; it is not covered by the creation/payload fault-hook tests. Review acceptance
is not physical-media acceptance or a claim that every abort publishes a final state.

Independent Codex finding: both new Preview assemblies lacked the outer IO boundary after
observer admission. Parent reopen, recheck or descriptor close failure could escape as raw
`OSError`, bypassing the CLI's structured refusal. Six regressions reproduced this before the
fix. Both assemblies now classify these failures as `DESTINATION_UNPROVEN`, preserving narrower
annotated probe failures and leaving the destination untouched. Validation: 56 affected source
tests passed; 57 passed against a freshly built, independently installed wheel (including import
location proof). Ruff and diff whitespace checks pass. No writer protocol changed for this fix.

Independent Codex finding: FAT32's IO error classifier rechecked only retained parent identity,
omitting backing-device evidence from the supplied observer. A disconnect/replacement between
the pre-IO boundary and an allocation error could be misreported as capacity failure. Two failing
regressions reproduced this; two controls preserve the original error for unavailable/inconclusive
probes. The adapter now uses the same full read-only recheck for normal boundaries and error
classification. No new admission policy, resumed authority or destination mutation is introduced.
The related reader setup path also left its initial live/owned-object probes outside its IO
boundary. Two regressions reproduce raw setup errors; those probes now classify before yielding
the reader, without catching or reclassifying consumer exceptions across the context-manager yield.

Combined correction validation: **169 source tests passed**. A rebuilt wheel installed into
`/tmp/modelark-review-final-wheel.tfg8JC/package` passed **634 native/FAT32 folder tests**, including
installed-import-location proof; packaged Slice sources match the checkout. Ruff and the complete
staged diff whitespace check pass. The existing real-vfat result predates these read-only error
classification corrections; no additional physical-media result is claimed.

## Web review rounds

Use the PR's exact-head trigger/result comments as the live round tracker. Count only newly
requested rounds for this folder-projection extension; earlier
PR #70 direct-USB reviews do not constitute review of the new code. Tie every result to the
exact pushed head. Request with `@greptileai review` and `@codex review`.

After the third completed round, stop further fixes/triggers and summarize residual findings,
common causes and any architectural recommendation. Do not infer acceptance from silence,
an old thumbs-up, an outdated review, or green CI alone.
