# Stage 1: conservative SHA256E compatibility qualification

Date: 2026-09-14. Scope: compatibility of existing SHA256E content identities with
the shared publication validators. This is passing qualification evidence, not a
Grok review, completed Stage 1 acceptance, live migration, or release approval.

## Native evidence

`scripts/qualify_annex_sha256e.py` passed **52 native cases** in fresh `/tmp`
repositories using the pinned Git, git-annex 8.20210223 and shell hashes inherited
from the accepted Stage 0 profile. Final annex repository format remained **8**.
The fixtures use tiny synthetic bytes only and never access a live archive or
catalog, configured remote, or network destination. They are retained.

- Retained run: `/tmp/modelark-sha256e-qualification-57zmmb6m/sha256e-result.json`.
- Exact copied result: [annex-sha256e-stage1-2026-09-14.json](annex-sha256e-stage1-2026-09-14.json).
- Result SHA256: `2ad1e6d6439c87ce032c0bb39644fdea3dd3781e3da61b05f458c3176b40515e`.
- Script SHA256: `9724a059d50899485d1b40e18df46c3f374a9107075e864640eafc1b82c63cb2`.

The result records exact native keys, stored byte lengths and SHA256 digests,
native `examinekey --format=${objectpath}` output, and locked/unlocked committed
pointer bytes for each case. Lock → unlock → relock preserved exact bytes,
key identity and the original locked representation.

## Observed behavior and admitted scope

- `.safetensors`, `.model`, `.vocab`, `.tiktoken`, `.jinja`, `.tokenizer`,
  `.gitignore` and `.gitattributes` examples produced SHA256E keys without an
  extension suffix. `.safetensors.znn` produced `.znn`.
- Compound suffixes are real: tested examples retained `.q4.gguf`, `.json.znn`,
  `.txt.znn`, `.gguf.znn`, `.tar.gz` and `.json.gz` in their full key identities.
- Native case is preserved (`.JSON`). Native also retained tested Unicode
  suffixes; those remain deliberately **unqualified** in production.
- The production parser admits only the **31 exact observed ASCII suffixes**,
  including the empty suffix, listed in `publication_policy.SHA256E_SUFFIXES`.
  A regression compares this set directly with the native observations. This is
  not an inferred general extension-length, character-class or case-folding rule.
  Unknown suffixes fail explicitly; files are not silently remapped to other keys.
- Locked committed pointers remain exact relative native object paths. Unlocked
  committed blobs remain exactly `/annex/objects/<full-key>\n`. Original filenames,
  stored filenames and full annex identities are not interchanged.
- Newline-containing filenames passed native add, Git index/tree inspection,
  object-byte verification and lock/unlock/relock. However, the pinned native
  argument-form `lookupkey` returned no key for that case. A successful add or
  this lookup command is not the proof: the qualification independently reads
  the exact NUL-framed Git index pointer and native object path. The production
  publication boundary must retain that independent proof.

## Shared-validator integration and regression

Runtime changes are confined to `publication_policy.py`,
`publication_map_policy.py`, and `publication_map_tree.py`:

- `parse_sha256_key` retains its compatible API name and returns size/digest for
  qualified SHA256-family keys; `sha256_key` still creates **SHA256**, not SHA256E.
- Pointer, object-path, annotation and location-map validation preserve and
  compare the **complete key**, including SHA256E suffixes. Equal content digests
  with different suffixes do not authorize substituting one key for another.
- Metadata buckets use the native MD5 distribution of the **complete ASCII key**,
  not just its payload digest. MD5 is a native directory convention, not content
  integrity evidence. Actual qualified native `hashdirlower` observations matched
  this calculation for every admitted suffix; a wrong suffix's bucket is refused.

Tests exercise actual pinned tools, real filesystem locks and qualified readers,
with synthetic catalog/attachment evidence in disposable fixtures. They validate
existing locked and unlocked SHA256E payload bytes, committed trees, metadata
snapshots and selected positive location claims. This does not qualify all
unfinished publisher coordination or map mutation/recovery work.

Validation: **330 scoped tests passed in 94.87 seconds**, covering publication
policy, map policy/tree, SHA256E qualification/integration, payload, tree and
qualified native-reader modules. An additional targeted rerun passed after adding
the different-suffix/wrong-bucket assertion. Ruff and `git diff --check` passed.
No review, commit, push, catalog-floor change or live-data mutation was performed.
