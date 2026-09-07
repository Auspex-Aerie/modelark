# Usable Slice domain API

Slice 1 implements an internal, pure proposal contract and an explicit read-only catalog reader.
There is no CLI, portal endpoint, hardware preflight, state store, Start, or byte-transfer path.
The accepted implementation sequence is in [the charter](plans/usable-slice-implementation-charter.md).

```python
from modelark.slice.catalog import read_catalog
from modelark.slice.domain import SliceSpec, approve, preview, validate_approval

spec = SliceSpec(
    repo_ids=("organization/model",),
    destination_id="operator-selected-usb-identity",
    destination_root="models",
)
snapshot = read_catalog("/explicit/path/catalog.sqlite", spec)
proposal = preview(spec, snapshot)
# Present proposal.closure, proposal.gaps, proposal.required_drives and proposal.seal for review.
# After explicit approval, supply the seal the operator actually reviewed and a fresh snapshot:
approval = approve(
    proposal,
    expected_seal=proposal.seal,
    current_snapshot=read_catalog("/explicit/path/catalog.sqlite", spec),
)
validate_approval(proposal, approval)
```

The example illustrates the internal call sequence, not an automatic-approval workflow. Callers
must obtain operator intent before `approve`. Pure approval is a value transformation: it cannot
authenticate a client, persist ownership, perform a CAS, or start work. `SliceApproval.stage` is
`domain`; `SlicePreview.execution_ready` is always false. Later slices must seal the actual
destination preflight and persist execution authority before a real transfer can be approved.
They must never treat this domain artifact alone as permission to write.

`hf-tree-v1` freezes the existing recovery manifest with repository-relative paths, sizes, original
SHA-256 digests, and format/quant metadata. It is a layout profile, not a functional model-loading
claim. If current acquisition formats cannot describe a repository with positive ONNX/MLX weight
evidence, its full declared catalog file set becomes the recovery scope; this retains
declared-but-unarchived gaps instead of reducing the closure to the subset already archived.
A manifest error alone is not legacy-format evidence. Accept ONNX/MLX catalog classification or,
for legacy `other`/NULL classifications, exact `.onnx`, `.npz`, or `.npy` filename extensions.
Directory names and prefixes are not weight evidence. Auxiliary-only repositories and unknown
formats without that evidence retain a blocking `MANIFEST_UNAVAILABLE` gap. Catalog annotations
are preserved, not rewritten. No acquisition policy is changed.
Catalog-only files yield exact gaps. Missing file sizes stay unknown instead of becoming
zero. A missing catalog digest may be supplied by unambiguous qualifying archive evidence; a
missing repository commit SHA is never inferred from a remote head.

Each source carries its recorded copy, physical identity, exact current clean anchor, and the
digest evidence used. Identity follows the existing reconciliation contract: at least one proven
filesystem or annex UUID, with every recorded UUID bound by the matching fingerprint and epoch;
a serial alone is insufficient. Lifecycle-active excluded drives remain readable. Dirty/unanchored,
lost/retired, explicitly absent, identity-mismatched, or conflicting copies cannot satisfy a file.
Replica metadata alone is insufficient. A safe stored path on the proven drive may qualify using
a valid original digest with durable `hub_confirmed`, `ingestion_computed`, `annex_key`, or
`archive-head-blob` provenance even if its current annex key is absent or uses another backend.
Durable original-byte provenance does not disappear when current catalog metadata omits its digest.
For raw SHA256 annex objects, the key is independent
original-byte evidence, even when the stored provenance is absent; the derived proof is recorded
in the proposal without repairing the catalog. A compressed object's annex hash cannot stand in
for its original-byte digest; a separately recorded original digest with durable provenance still
qualifies. Multiple qualifying but disagreeing originals block closure when
the catalog does not disambiguate them.

Offline sources need no filesystem access at this stage. Preview validates lexical paths; source
attachment, descriptor confinement, and digest verification are execution requirements. The domain
core accepts immutable facts and performs no SQL, filesystem operations, subprocesses, or network
access. The catalog adapter opens only the explicitly supplied SQLite path with `mode=ro` and
`query_only`, reads schema v7 in one transaction, and closes it on every exit. It neither calls
the globally configured catalog connector nor creates/migrates a missing/older database.

Canonical JSON is versioned as `modelark.slice.domain.v1` and hashed with SHA-256. Ordering of input
facts does not affect the result. The seal binds the spec, relevant catalog snapshot content and
identity, exact closure, alternatives, and gaps. Only requested manifests, their recorded source
candidates, and those candidates' current drive/anchor facts affect approval freshness. Unrelated
fleet activity and historical anchors do not. Placement eligibility is retained as captured
annotation but excluded from read-source seal authority. `approve` detects tampering, rejects blocked
proposals and mismatched reviewed seals, and rederives against freshly supplied evidence to reject
stale proposals. The hash detects accidental modification; it is not a signature or an untrusted
client's authorization credential. Durable concurrency control belongs to Slice 2.

`source_ready` means every frozen manifest file has a qualifying source. On a blocked preview,
`closure` and `total_bytes` contain only resolved files; they are not the total requested size.
`required_drives` is the deterministic preferred-source schedule, with all approved alternatives
retained on each artifact. `validate_approval` checks the original immutable pair and does not
replan it when a source later changes: execution must apply the charter's per-use source gates.

Validation uses synthetic evidence and disposable SQLite databases. The concurrent-WAL test proves
that a committing writer does not mix catalog generations inside a preview, and that the reader
refuses writes. No test or development action needs a live archive or USB destination.
