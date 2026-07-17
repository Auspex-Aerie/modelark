# Sample selection sets

A **selection set** is just a list of model repo IDs — the things you'd archive. These are starter
sets, each a different *angle* on the catalog (`catalog/export/models.jsonl`), to show how you might
curate. Use one as a jumping-off point, then refine in the portal's **Catalog** tab or on the CLI.

| File | Angle | How it's built |
|------|-------|----------------|
| `fits-24gb.jsonl`       | Runs locally           | ~10 LLMs ≤ 14B, one per org, by 30-day downloads (fit a 24 GB GPU) |
| `flagship-llms.jsonl`   | The notable ones       | ~10 LLMs by community likes, one per org (size-mixed, ~3B→large) |
| `frontier-giants.jsonl` | Archive the unrunnable | a *handful* of the biggest — a taste of the use case, not a grab-list |

Each line is `{"repo_id", "model_name", "params_b", "category"}`.

## Using a set

Curate visually in the portal (`modelark serve` → **Catalog**). The CLI accepts repeatable
`--repo` values for an explicit, read-only placement preview:

```bash
modelark library plan --repo org/model-a --repo org/model-b
```

There is not yet a bulk-import command for these JSONL samples. Do not pipe them into bare
`modelark fetch`: direct fetch also requires an explicit registered drive, and bypassing the active
Plan is not the recommended fleet workflow. Copy the repositories you want into the Catalog cart,
then review the Fill plan before execution.

Mark anything you want a redundant second copy of with `modelark protect --repo <id>`.

> These sets are **illustrative** and deliberately small — regenerate against a fresh
> `modelark discover --walk` for current numbers. Keep your own sets modest: the default 1 TB/day cap
> will pace you, and ModelArk is an archive/DR tool, not a way to mirror large swaths of Hugging Face.
