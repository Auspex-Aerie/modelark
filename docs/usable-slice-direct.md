# Attended direct USB delivery

Slice 3 joins the domain preview, private transaction engine, local archive reader and Linux USB
adapter. It never fetches missing content, modifies archives, formats/mounts drives or deploys a
service. The first attended real-device trial and merge remain separate operator approval gates.

## Operator workflow

Stop any running ModelArk portal before invoking another CLI command (DEC-124). Each command is a
separate guarded application launch. These examples explain the interface; they do not authorize
writing an unreviewed physical drive.

```text
modelark slice preview --catalog /explicit/catalog.sqlite --repo org/model \
  --destination /mounted/usb --root delivery
modelark slice approve TRANSACTION --seal REVIEWED_SEAL
modelark slice start TRANSACTION --destination /mounted/usb \
  --source drive-a=/mounted/archive/modelark
modelark slice status TRANSACTION
modelark slice stop TRANSACTION
```

Results are JSON. Preview reports the exact closure, gaps, source alternatives, destination identity,
capacity policy and reserve. A blocked closure creates no transaction and does not probe hardware.
A ready preview persists private state; approval and Start are separate actions. Global runtime
overrides are rejected for Slice: the explicit preview catalog is sealed, and transaction state uses
one operator-wide namespace outside the catalog.

Start runs synchronously until completion, wait or refusal. **Ctrl+C** requests and acknowledges Stop
while running. A separate `stop` launch records a durable request; if the old attempt already ended,
the next Start first acknowledges it, and a subsequent explicit Start can resume. No background
worker/control server is introduced. An exiting CLI releases its process descriptors while retaining
completed work and the durable reservation. Unreviewed source labels are rejected on resume.

Completion means original-byte SHA-256 verification plus authenticated layout and receipt publication,
not that every catalog entry is archived, loadable or currently physically verified. Completed status
is readable without the USB. New transactions require unused output/control paths; previous control
records are not adopted or automatically cleaned up.

## Identity, IO and recovery

Writable admission requires a direct USB disk/partition with unique filesystem and parent-disk
identity, at its exact mount root. System/swap disks and their siblings, all registered archive
identities, stacked/ambiguous devices, aliases and nested mounts are excluded. Filesystem capacity is
distinct from block-device capacity. Stable capabilities are sealed separately from transient Linux
mount IDs, which are checked through retained descriptors.
Each fresh observation must match the retained descriptor's mount ID and attachment path before use.

Descendant IO uses `openat2` confinement with no symlink/mount crossing. A vanished pinned mount waits
and never redirects writes into an uncovered host mountpoint; the old binding cannot reacquire.
Fresh Start must prove the sealed identity again. Replacement of a still-attached root refuses.
Each mutation also passes the existing transaction authority and capacity gate.

Host certificates bind transaction, seal, filesystem, path, token and inode birth identity. Files
are certified while unnamed, then linked without replacement using the retained inode. Directories
cannot be created and certified atomically: DEC-123 requires operator intervention for uncertified
crash residue, without adoption/deletion. DEC-124 trusts the operator account and does not introduce
adversarial same-account namespace isolation. Ordinary collisions still refuse.

Source reads retain the shared archive fence, refresh the explicit read-only catalog and validate
filesystem/serial/annex identity. Stored paths are repository-relative. Only a final annex link to the
sealed key beneath the pinned object store is permitted. No annex command or retrieval runs. Raw,
bounded ZipNN 0.5 byte-format, StreamZNN and optional zstd streams yield original bytes. The default
64-MiB limit bounds individual frames/windows, not the native codec's entire working set; oversized
legacy whole-ZipNN blobs and unsupported modes refuse. Consumer errors are not relabeled as source IO.

## Initial ext4 capacity profile

This deliberately conservative profile is not a general ext4 estimator. Read-only superblock proof
comes from an opened block-device handle whose device number matches the mounted filesystem. Read
permission must already exist: there is no automatic sudo or permissive override. An unsupported
volume refuses; do not reformat an archive or a drive with wanted data to satisfy this profile.

Require 4-KiB blocks/clusters, internal journal, supported inode/xattr capabilities and an initially
one-block nonindexed mount root. Quotas, bigalloc, EA-inodes, inline data, encryption/casefold/verity,
large-directory extensions and unknown/newer optional features are excluded. Allowed feature masks:
compat `0x023c`, incompat `0x22c6`, ro-compat `0x047b`; required masks respectively `0x000c`, `0x0042`,
`0x000a`. Conflicting GDT/metadata checksums refuse. Large/huge-file features must already exist so the
transfer cannot change its own sealed profile or admit files beyond the filesystem's limit.

For `B=4096`, `F=artifacts+2` includes control/receipt, `D` is new directories, `N` sums each file's
rounded payload blocks, and `E` counts planned root-level names including temporary links:

```text
required bytes  = B × [6N + F + 2D + 6(2 + 4E)]
required inodes = F + D
```

The `6N` term conservatively charges data plus five extent-mapping levels; it is not expected actual
usage. Files get one external xattr block each; new directories get one data and one xattr block.
Every new directory's complete live name set must fit one block including dot entries and checksum
tail. Names are limited by UTF-8 bytes, not characters. Root growth separately covers conversion and
splits, without assuming preexisting dirent slots are compact. Control/receipt serialization includes
the full sealed plan and largest eligible source records, bounding reserve-number width before seal.
Thus required capacity can substantially exceed model payload sizes.

Ownership markers exceed the supported inode's inline-xattr capacity. Each external xattr block
therefore contains a unique token, preventing ext4's shared-xattr optimization from causing duplicate
charges across different owned inodes. Live inode identities are counted once across publication
hardlinks. Settled allocation includes xattrs/directories and root growth. Free bytes/inodes must
reconcile with the sealed baseline; unexplained usage is not hidden inside a tolerance.

The derivation follows kernel [extent trees](https://www.kernel.org/doc/html/latest/filesystems/ext4/ifork.html),
[directory records](https://www.kernel.org/doc/html/latest/filesystems/ext4/directory.html),
[non-largedir insertion](https://github.com/torvalds/linux/blob/v6.12/fs/ext4/namei.c#L2352), and
[feature definitions](https://github.com/torvalds/linux/blob/v6.12/fs/ext4/ext4.h#L1883).
Media, fragmentation and IO faults may still stop execution; none authorizes a successful receipt.

## Private state and qualification

Private schema v6 atomically stores each plan with its direct-admission capsule. Catalog path,
filesystem policy, root evidence and budgets are hashed into the sealed destination binding. Legacy
internal plans remain readable without invented direct-admission evidence. No catalog migration occurs.
The separate certificate registry uses schema v2: durable identities retain inode number and birth
time within the sealed disk/filesystem scope, not Linux's transient device number. Trusted v1
certificates migrate atomically; malformed or ambiguous identities refuse without partial migration.
This permits the same USB filesystem to return under a different kernel device number while live
descriptor checks continue to enforce the current attachment.

Tests exercise real disposable IO, certificates, process fences and journals with synthetic physical
device/free-space observations. They do not prove a completed attended physical USB trial. Deployment,
that trial and merge are not performed by this implementation work.
