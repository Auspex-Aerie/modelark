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

Round 1 reviewed `163f60bf14df9e120fd23827a3e36c8c0db200c5`, requested in
[comment 5599186847](https://github.com/Auspex-Aerie/modelark/pull/70#issuecomment-5599186847).
Greptile completed with 5/5 and a trigger thumbs-up; Codex completed with two P2s.
All three CI jobs passed on that head. Both findings were independently confirmed:

- **Native inherited ACL/marker footprint:** a real disposable ext4 parent with 200 named-user
  default ACL entries admitted successfully, then directory ownership-marker publication failed
  with ENOSPC. The pre-mkdir check now budgets two full inherited ACL values, the actual marker
  length and 256 bytes of xattr headers/entries/alignment/slack against one filesystem block.
  It credits neither in-inode space nor ACL sharing/EA-inode support. Preview and each runtime
  parent check enforce it. Ordinary small ACLs still complete real local transfers; large ACLs
  refuse before creating the root, with parent ACL and sibling bytes unchanged. The same bound
  refuses 1 KiB blocks even without ACLs because they cannot hold the padded marker. This remains
  a conservative compatibility bound, not a promise against quota, concurrent allocation or
  other security attributes. Ext4 layout basis: [kernel xattr documentation](https://www.kernel.org/doc/html/latest/filesystems/ext4/attributes.html).
- **FAT owner masks:** fmask must preserve owner read/write and dmask owner read/write/search,
  in addition to excluding group/other writes. Five newly added bad-mask cases reproduced prior
  admission, including Codex's fmask=0477. Good 0077 and file-only 0177 masks remain admitted;
  live recheck rejects a newly incompatible file mask. These are synthetic mount-observation
  tests, not new mounted-vfat or physical-USB qualification. Mount-mask basis:
  [kernel VFAT documentation](https://www.kernel.org/doc/html/latest/filesystems/vfat.html).

The common cause is incomplete admission of the writer's **whole operation sequence**: creation
permission alone did not establish later certification/read-back capability. The corrections
extend existing shared Preview/runtime admission functions; they do not introduce a new adapter,
permission rewriting, rollback/adoption policy, schema, seal version or archive behavior.

Post-fix installed wheel `/tmp/modelark-web-round1-wheel.F5Tdqa/package`: **173 tests passed**
(113 admission/import-location tests and 60 native transaction/public-flow tests). Packaged Slice
sources exactly match the checkout. Ruff passes across `modelark`, `scripts` and `tests`; diff
whitespace checks pass. No additional mounted-vfat or physical-media result is claimed.
The full post-fix Slice suite passed **1,322 tests**, with five optional-codec skips and two
upstream Torch deprecation warnings (509 seconds).

Use the PR's exact-head trigger/result comments as the live round tracker. Count only newly
requested rounds for this folder-projection extension; earlier
PR #70 direct-USB reviews do not constitute review of the new code. Tie every result to the
exact pushed head. Request with `@greptileai review` and `@codex review`.

After the third completed round, stop further fixes/triggers and summarize residual findings,
common causes and any architectural recommendation. Do not infer acceptance from silence,
an old thumbs-up, an outdated review, or green CI alone.
