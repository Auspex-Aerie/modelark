# FAT32 session export: approved protocol and qualification

Status: intent/live-authority distinction approved by the operator, 2026-09-09; no additional
DEC requested. DEC-130 remains the scope authority. FAT32 plan, live descriptor port,
one-attempt private state, observer and public operator routing are implemented locally.
Both attended kernel-vfat runs passed, including the supplemental public-flow/alias checks below.
No physical USB or real archive acceptance is claimed.

## Approved intent versus live authority

Existing Preview, Approve and Start are separate guarded application launches. Native ext4 can
reprove the approved parent's persistent identity. Gate A's FAT32 `FolderTarget.parent_identity`
instead names a retained live parent binding. That descriptor is gone when preview exits; saved
JSON cannot turn its token back into a capability. Do not change that token's meaning in place.

Approved public contract: retain the three-command workflow, with a **new versioned FAT32
pre-creation intent** binding the exact destination path, qualified backing filesystem, source
closure, capacity policy and explicit one-attempt/no-resume warning. It does not promise that
the parent is the same directory inode seen at Preview. At Start, revalidate path/backing/roles,
bind a fresh parent descriptor, confirm the child is unused, acquire/consume authority, then
exclusively create and retain the output child. No existing directory is adopted, even empty.

Alternative: preview, interactive approval and Start in one process retaining the same parent
descriptor. This preserves live-parent continuity but introduces a different attended CLI flow.
The operator selected separate durable intent and fresh Start authority. The combined interactive
alternative is not being implemented. Native identity, archive source evidence, no-clobber
publication and the launch singleton are unchanged.

## Live-only ownership and resource bounds

The dedicated port holds every owned directory/file descriptor necessary to authenticate later
operations and final layout. Tokens index that in-memory capability table; FAT inode numbers,
birth-like timestamps, paths, marker contents and matching hashes never authorize fresh-process
ownership. Reads/writes use descriptors; resolving a name checks it against the still-retained
object. Parent/root/mount/backing checks remain active at mutation and publication boundaries.

Require an explicit descriptor budget for the whole closure before the first output write.
Do not evade that bound by closing earlier descriptors and later treating FAT inode numbers as
persistent certificates. Mount permissions must establish the trusted-account writer boundary;
FAT's mount-wide uid/masks are not per-directory ext4 mode/ACL enforcement. No chmod/remount to
force eligibility. [Linux VFAT mount documentation](https://docs.kernel.org/filesystems/vfat.html).

All destination control, staging and report objects are inside the new child. Named staging
uses exclusive creation, not O_TMPFILE or hardlinks. Publish only through qualified no-replace
rename; unsupported primitives refuse. The first disposable kernel-vfat test passed successful
and colliding RENAME_NOREPLACE plus required directory flushes on this host.
[rename(2)](https://man7.org/linux/man-pages/man2/rename.2.html).

## Authority lifecycle and no-resume

A schema-8 `consumed_attempt` record commits inside the fenced host claim transaction
**before root creation**. Commit failure means no destination writes. A committed claim is spent
even if the process dies before mkdir; this conservatively trades a retry for unambiguous lifetime.
Any later fresh claim refuses regardless of whether the old state says starting, stopped, waiting
or transferring. A repeated Start may observe an already-live attempt, never construct a new port.

Any ended attempt requires a new folder: graceful Stop, process exit/death, destination loss,
source wait, capacity wait or error. Source alternatives can still be tried within the same live
attempt. The native retained-wait/resume behavior is not inherited by the FAT profile.

The implementation retains old claims and compares component ancestry under the candidate
ASCII/case-insensitive profile. A disjoint new sibling intent is permitted; the old root remains
claimed and cannot be adopted. The dedicated observer requires exact enumerated parent spelling,
rejects requested short aliases and refuses unqualified name/mount options. The supplemental driver
run passed those alias test vectors for the narrowly admitted profile; pure Gate A FAT overlap
remains conservative and is not relabeled as that qualification. No stale-claim/media cleanup is added.

## Required persistence/failure table

No branch below permits adoption, overwriting, automatic residue deletion or false completion.

| Boundary reached | Required ordering and failure outcome |
| --- | --- |
| Read-only preview / unconsumed Start | No destination writes. Requalification/retry possible. |
| Host one-attempt claim committed | Claim is irrevocably consumed; failure before mkdir still requires a new intent/root. |
| Root intent, exclusive mkdir, live capture | Keep root and parent retained; flush required directory metadata. Uncertain capture/flush leaves residue and ends attempt. |
| Control/file intent and exclusive named staging | Persist host intent first; retain created file. Creation/write/source failure may leave staging; no fresh-process recovery. |
| Content verified and prepared | Flush file, then persist prepared evidence; failure is not prepared success. No capacity guarantee is inferred. |
| No-replace publication | Authenticate retained source and parents; rename without replacement; collision/unsupported result ends attempt. |
| Published name / parent flush | Required directory flush must succeed before publication is called durable. Failure ends attempt with residue. |
| Final original hashes and exact live-owned layout | Verify all payloads again and record exact observed evidence; extra/changed objects refuse. |
| Destination report publication | Flush report and parent. Report asserts verified export evidence, **not host transaction commit**. |
| Host export-evidence journal append | Persist the non-final report evidence; failure ends the attempt without host completion. |
| Final retained-descriptor close barrier | Attempt every close once; any close error prevents host completion. Never retry an already-closed descriptor number. |
| Host completion commit | Only committed host state or its successful command result asserts transaction completion. Failure leaves host completion unproven. |
| Host commit succeeded, response lost | Read-only status may report complete. Never reopen the output to retry writes. |

An export-evidence report may survive when the host commit did not. It must identify that limit
in its versioned schema instead of copying the native receipt's top-level completion assertion.
No future Start infers committed completion or writable authority from that media report.
Successful flush syscalls are observed evidence, not proof of arbitrary power-loss survival.

## Layout preflight

`modelark.slice.fat32_layout.admit_layout` takes relative payload path/size pairs, one output child,
the canonical absolute host parent spelling, and exact control/report payload sizes. It validates
the full directory/file tree and generated `.slice-<32 hex>` staging path lengths. Its return
value always has `execution_ready=False` and cannot enter the execution store.

Candidate v1 grammar is deliberately conservative ASCII: letters, digits, underscore, hyphen,
dot and internal spaces. Refuse non-ASCII, control/forbidden characters, surrounding whitespace,
trailing dots, DOS device stems, requested tilde/short-alias spellings, reserved staging prefixes,
duplicate/case-equivalent names, inconsistent case in shared directories and file/directory
collisions. Do not rename or split to fit. Each payload/control/report must be at most 2^32-1
bytes, components at most 255 ASCII characters, and generated relative/absolute path byte lengths
strictly below 4096. This is an admission proposal, not a claim to support every legal FAT name.

These rules **do not prove driver alias disjointness**. In particular, `nonumtail` can generate
short aliases without a tilde, so a tilde blacklist alone is insufficient. Mount-option
qualification must establish the actual alias rules (or refuse the mount), and must not change
them silently. Existing parent aliases are not validated by this pure layout function.
[Linux VFAT name options](https://docs.kernel.org/filesystems/vfat.html).

## Qualification evidence and remaining checks

On 2026-09-09 the operator reported PASS from the first attended script. Retained artifacts:
`/tmp/modelark-fat32-qualification-reohetp0`. It covered real kernel-vfat no-replace and directory
flush, original-byte delivery/report, all 30 injected engine interruptions with no resumed
authority, and sibling preservation. `physical_USB_or_archive_test: false` is intentional.
The public workflow and parent case/short-alias checks were not in that first script.

The operator subsequently reported PASS from the expanded script, with artifacts retained at
`/tmp/modelark-fat32-qualification-ru4sibe7`. This includes the real parent case/numbered-short-alias
checks and public Preview/Approve/Start with synthetic source/backing, in addition to every earlier
writer/fault/sibling check. The planned disposable-image gate is complete, not physical acceptance.

- Use a new disposable FAT32 image with the kernel vfat driver, not an ext4 test pretending to
  be FAT and not mtools/FUSE as proof of kernel syscall behavior. Local FAT formatting and loop
  facilities exist, but noninteractive sudo requires a password. The attended script below
  requests sudo interactively for reruns; it never accepts an existing device.
- Cover no-replace collision and successful rename with retained descriptors; directory/file
  flush support and failure; exact long-name enumeration; case and short-name collisions;
  mount options; zero-byte and size/name/path limits; descriptor budget and permission refusal.
- Inject interruption before/after every row above, including SQLite FULL/busy/commit failure,
  and assert no resumed authority, no unrelated writes and no false host-complete result.
- Regress native and strict legacy protocols and a freshly installed wheel after write changes.
- Only afterward: reviewed exact physical USB/source acceptance. Source clean-anchor
  reconciliation remains a separate authorization requirement. No deliberate unplug/power loss,
  formatting existing media, permission rewriting, archive repair or live deployment is included.

## Attended disposable-image command

Run as your ordinary user (not by putting sudo before Python):

```text
/home/phaze/PycharmProjects/modelark/.venv-dev/bin/python /tmp/modelark-folder-projection/scripts/qualify_fat32_slice.py
```

It creates a fresh private `/tmp/modelark-fat32-qualification-*` directory and a new 64 MiB regular
image, formats only that new image, and requests sudo solely to mount/unmount it. It accepts no
device or destination argument. The real FAT port checks include no-replace rename, directory
flush, original-byte delivery/report, 30 interrupted engine boundaries and no resumed authority.
The updated script also checks exact long-parent spelling, actual case and numbered-short-alias
resolution/refusal, case-equivalent creation collision, and public Preview/Approve/Start using the
real mount-routing hint and real vfat I/O. Its source and backing evidence are explicitly synthetic;
it does not qualify physical USB observation or real archive eligibility. Admission is restricted
to `codepage=437,iocharset=iso8859-1,utf8,shortname=mixed`; other codecs are not inferred safe.
The image, synthetic private state and output residue remain available after unmount for inspection.
Broader failure cases and physical acceptance remain separate checks.
An unsuccessful unmount is reported, never hidden with lazy or forced detach.
