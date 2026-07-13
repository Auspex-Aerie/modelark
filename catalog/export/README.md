# Catalog export provenance and licensing

`models.jsonl` is a sanitized export of ModelArk's catalog, generated from public Hugging Face Hub
repository metadata and ModelArk's derived classification fields. It contains metadata and links,
not model weights, private drive records, annex locations, credentials, or local machine details.

Auspex-Aerie licenses its scripts and its copyrightable selection, arrangement, and derived fields
in this export under the repository's [Apache-2.0 license](../../LICENSE). Repository identifiers,
upstream metadata, trademarks, and other third-party material remain subject to their source terms;
the export does not relicense them. A row's `license` field is informational metadata reported for
the corresponding model repository, not a license grant by ModelArk. Consult the linked upstream
repository before downloading or using model artifacts.

Generate a fresh sanitized export with:

```bash
modelark export
```
