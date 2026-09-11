# Folder projection

Approved scope: DEC-130 and [the implementation scope](plans/folder-projection-scope.md).
Native ext4 and separate FAT32 session execution are implemented. Disposable kernel-vfat writer,
public-flow and alias qualification passed. Reviewed merge `16e8b3f` was deployed on 2026-09-10;
the attended real-source FAT32 USB smoke test passed under the qualified mount profile described
below. This is evidence for that tested configuration, not arbitrary USB or filesystem support.
See the [acceptance record](acceptance/source-identity-usb-2026-09-10.md) for live repair,
raw-source completion and the still-incomplete compressed-source follow-up.

## Operator workflow

Without `--root`, `--destination` names the **exact new output folder**. Its parent must exist.
Preview makes no destination objects; it records a plan in private host state. Approval does not
start copying. These examples describe the interface, not authorization to write a real device.

```text
modelark slice preview --catalog /explicit/catalog.sqlite --repo org/model \
  --destination /home/operator/exports/demo
modelark slice approve TRANSACTION --seal REVIEWED_SEAL
modelark slice start TRANSACTION --destination /home/operator/exports/demo \
  --source drive-a=/mounted/archive/modelark
modelark slice status TRANSACTION
modelark slice stop TRANSACTION
```

Review the exact closure, target, profile, advisory capacity and seal before approval. Start creates
the child exclusively; an existing empty directory is not adoptable. Payload, staging, control and
receipt files stay inside that child. Host-private journals remain in the existing fixed
`~/.local/state/modelark/slice` namespace. No siblings are owned, cleaned up or overwritten.

The existing application-launch singleton is unchanged. Stop a running portal before invoking the
CLI. Start is synchronous; Ctrl+C acknowledges Stop while the attempt lease is still retained.
Native fresh Start can resume only the same authenticated transaction/root. A pending Stop from an
earlier launch must first be acknowledged; a subsequent explicit Start permits resume.

Explicit preview `--root` retains the [strict direct-USB interface](usable-slice-direct.md), where
`--destination` is the mount root. Existing seals keep their original policy. Start selects the
adapter from the sealed plan, not a flag that can weaken old approvals.

## Native eligibility and protection

`native-ext4-folder.v1` supports qualified case-sensitive Linux ext4 folders, including ordinary
system-backed encrypted/LVM storage with an unambiguous single-parent block chain. It does not
require raw-device reads or exclusive use of the filesystem. The actual host test covered ext4
on LUKS/LVM; that does not qualify every device-mapper target or kernel/filesystem combination.

Admission checks stable backing identities, mount uniqueness, retained parent inode/birth identity,
effective permissions, directory flags and xattr access. Runtime operations require the native
confinement, durable certificates, unnamed staging, no-replace hardlink publication and flush
interfaces. Missing capabilities refuse; no overwriting or path-based fallback is used.

Initially refused: bind/duplicate mount aliases, symlink ancestors, nested output mounts,
ambiguous/multi-parent storage, registered archive backing devices, casefold/fscrypt/inline-data
directories, remote/FUSE storage, XFS and exFAT. A FAT32 type string never selects the ext4 writer.

Protected subtrees are `/boot`, `/usr`, `/etc`, `/var`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/dev`,
`/proc`, `/sys`, `/run` and `/root`; ModelArk state/config/cache/share directories, the selected
catalog and actual runtime code are protected too. File roles protect the file, not every sibling
in its parent. Output cannot contain or be contained by a protected role. System-disk identity
alone does not prohibit an ordinary home/data folder. Pseudo-filesystem role classification needs
current inode/mount evidence, not persistent birth identity; owned destination certificates still
require birth identity.

The parent must be owned by the operator or root. Non-owner-writable parents require sticky-bit
protection. A default ACL must preserve owner rwx; creating the owned root with mode 0700 masks
inherited non-owner permissions. Both inherited ACL values plus the ownership marker and overhead
must fit a conservative single-block xattr budget; oversized ACLs and 1 KiB blocks refuse before
root creation. This check does not reserve space against quota or other security attributes.
The implementation never changes existing ownership, modes or
ACLs to force admission. This assumes a trusted operator account, not protection from malicious
code running as that same UID or root.

## Ownership, capacity and recovery

`FolderTarget` seals the canonical filesystem scope, existing parent identity and intended child.
It excludes free-space observations and the not-yet-created root identity. `NativePlan` adds the
approved closure, backing/admission evidence, catalog, advisory snapshot and metadata allowance
under `modelark.slice.native-transaction.v1` for legacy approvals. Fresh previews use v2 and
also bind the explicit decode/resource policy. `FolderReview` remains non-executable; parsing it
cannot produce source approval, ownership or execution authority.

Durable claims compare canonical same-filesystem paths: exact and ancestor/descendant roots
conflict; disjoint native siblings do not. Stopped claims persist. Cross-profile checks also
exclude a legacy whole-device claim that intersects a native backing identity. Authority is
checked inside the durable claim transaction as well as before it; changing a target ID cannot
erase an earlier overlapping path claim. FAT32 uses its separate case-insensitive claim rules;
actual-driver alias checks passed for the narrowly admitted mount/name profile.

Start revalidates parent, protected roles and authority, records root-creation intent, exclusively
creates the child, and certifies its identity. Uncertain residue after interrupted certification
is left untouched, never adopted or deleted. Resume must reprove created-root and descendant
ownership. A missing/replaced certified root is not permission to create another one.

Space is shared and **not reserved**. Metadata allowance includes serialized evidence and a
per-object estimate; fresh checks compare available bytes/inodes with remaining requirements.
Sibling allocation or parent-directory growth does not invalidate identity. Allocation inside
the owned tree is still audited. Another process can consume space after any check.

Destination ENOSPC/EDQUOT returns `waiting_destination` and ends that attempt. After restoring
space, a fresh Start rebuilds authenticated allocation and may resume. Conclusive attachment
loss/replacement takes precedence over a space diagnosis. Private-journal/certificate persistence
errors are not relabeled as destination capacity waits or fabricated durable outcomes. There is
no automatic uncertain-object cleanup.

Mapped storage is conservatively re-observed at recheck boundaries to detect mapping changes;
this can cost more than the direct-device fast path. Performance qualification for large mapped
deliveries remains separate from the tiny correctness smoke test.

Completion requires original-byte hashes, exact owned layout, required flushes and successful
receipt/host-state publication. Receipts identify the native folder profile, shared-space policy
and authenticated resume. A delivery receipt is not newly verified archive-copy evidence, nor
proof of arbitrary power-loss survival. Source readers and clean-anchor/archive fences are
unchanged; synthetic fixture success does not qualify a real source.

## Compatibility

Private store schema **8** upgrades prior development schemas while preserving legacy seals,
approvals, journals and claims. It prevents older binaries from interpreting new executable
folder authority and FAT's irrevocable consumed-attempt field. This is private Slice state,
not a catalog schema migration. Legacy
`TransferPlan` v1–v3 serialization, receipts and strict device/capacity policy remain unchanged;
golden tests pin their seal hashes against reviewed commit `6fd87c3`. Unknown/mixed envelopes
refuse before destination observation or writer authority. No public flag weakens an old seal.

Fresh direct/native/FAT approval envelopes now carry a versioned original-byte decoding policy.
ZipNN frames run in the C1-qualified child: 8 GiB AS ceiling plus 2 GiB sampled headroom, with
individual buffer ceilings derived from that same envelope and at most 64 KiB output pieces.
This is not an RSS reservation or a guarantee every smaller frame fits; unknown headroom and
child allocation failures refuse. zstd stays bounded streaming with a 64 MiB byte-valued window;
this policy currently qualifies only zstandard 0.25.0's C extension. Old approvals retain their
old limits and conversion. Fresh preview and approval are necessary to use the new behavior.

Before first output mutation, preflight checks fresh sealed source candidates under their read
fences, including every StreamZNN header. It reads/discards payload in bounded pieces, so a large
archive may take time to inspect. It is not a full decode/hash verification. A usable attached
alternative can proceed; absent alternatives remain unchecked, and absent required sources wait.
Actual decoding repeats header/resource checks and still verifies the sealed original hash.
Native partial resume authenticates completed output without requiring its sources online.

No private schema migration is added by these v2 envelopes. An older binary rejects them, including
while checking overlapping ownership; stores with v2 records require the upgraded reader. The
[C2 tracker](plans/slice-c2-progress.md) separates synthetic implementation qualification from
the still-required public CLI, live deployment and physical compressed-source acceptance gates.

## FAT32 session workflow

The same Preview → Approve → Start commands select the FAT32 adapter for a vfat parent;
the selected observer must still admit the destination. There is no fallback to the native writer.
The approved intent binds the exact path/backing, not parent inode continuity across launches.
Start retains fresh live parent/object descriptors and exclusively creates the new child.

Only direct USB with explicit FAT32 evidence and the tested name settings is admitted:
`codepage=437,iocharset=iso8859-1,utf8,shortname=mixed`, current-user ownership, and masks excluding
non-owner writes while preserving owner directory rwx and file rw. Non-ASCII/short-alias spellings
and unsupported full layouts refuse. Each file
must fit FAT32's 2^32−1-byte limit. The application does not remount or change permissions.
Protected paths still include all of `/run`, so `/run/media/...` is currently refused too.

FAT32 has **one attempt, no resume**. Once the host claim is consumed, Stop, Ctrl+C, source or
capacity waits and failures require a new intent and different output folder. Residue remains
untouched. Known capacity/source/header/resource refusals before claim consumption are retryable;
so is a Stop acknowledged before the one-shot attempt is consumed. The media report
says export verified, not host transaction committed; committed host status supplies that evidence.

## Completed qualification gates and evidence limits

- Gate C: qualification of the implemented `fat32-folder-session.v1` passed. The operator's first
  disposable-image run passed no-replace/flush, original-byte/report delivery, 30 interruption
  boundaries and sibling preservation. The updated [attended script](plans/fat32-session-protocol.md)
  additionally exercises exact/case/numbered-short-alias parent checks and public Preview/Approve/
  Start with synthetic source/backing evidence. The operator reported all additions passed in
  `/tmp/modelark-fat32-qualification-ru4sibe7`; the planned disposable-image gate is complete.
- Gate D: the separately authorized source-evidence repair and attended FAT32 USB smoke test passed
  on 2026-09-10 using deployed merge `16e8b3f`. Public Preview → Approve → Start → Status delivered
  `facebook/wav2vec2-base-960h` from the repaired drive-07 archive: nine uncompressed source files,
  377,615,574 original bytes. Every delivered size/SHA-256 matched the sealed closure, the exact
  output-file layout matched, the FAT export report was published, and host status was `complete`.
  All eight pre-existing USB files retained their sizes/hashes; the live catalog was logically
  unchanged. The portal was restored with Fill idle.
  The operator temporarily mounted the USB with the qualified FAT32 name/permission options and
  `noexec`, without desktop `showexec`; no formatting or existing-file deletion was performed.
  This successful raw-file transfer does not qualify compressed-source decoding on physical media,
  interruption/power-loss survival, arbitrary desktop mount options, or functional model loading.
  Private evidence: candidate `16e8b3f/usb-slice-acceptance-k7bol08o/result.json`;
  transaction `a40ac9f588c847f682d0b2f08939318f`. Output and receipts remain on the USB.

No reformat, permission rewriting, live deployment, archive reconciliation or deliberate media
removal was performed as part of the native implementation.

## Deferred destination onboarding

DEF-044 records the operator-requested follow-up from attended USB testing: a UI to select the
destination device/folder, explain readiness and mount-policy refusals, and generate exact commands
for the freshly verified target. Commands must account for mount-directory removal after unmount,
privileges and restoration of temporary settings while preserving existing files and archive
protection. Common desktop FAT32 settings such as `showexec` need qualification before admission;
the current implementation still refuses them. Revisit in the next Slice/USB onboarding UI scope,
before describing FAT32 delivery as self-service without attended CLI assistance.
