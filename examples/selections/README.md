# Sample selection sets

A **selection set** is just a list of model repo IDs — the things you'd archive. These are starter
sets, each a different *angle* on the catalog (`catalog/export/models.jsonl`), to show how you might
curate. Use one as a jumping-off point, then refine in the portal's **Catalog** tab or on the CLI.

| File | Angle | How it's built |
|------|-------|----------------|
| `flagship-llms.jsonl`   | The essentials         | top generative LLMs by all-time downloads |
| `fits-24gb.jsonl`       | Runs locally           | generative LLMs ≤ 14B params, by downloads (fit a 24 GB GPU) |
| `frontier-giants.jsonl` | Archive the unrunnable | generative LLMs ≥ 100B params, largest first |

Each line is `{"repo_id", "model_name", "params_b"}`.

## Using a set

Curate visually in the portal (`modelark serve` → **Catalog**), or fetch a set's repos directly:

```bash
# fetch everything in one angle
xargs -a <(python -c "import json;[print(json.loads(l)['repo_id']) for l in open('examples/selections/fits-24gb.jsonl')]") \
      -I{} modelark fetch --repo {}
```

Mark anything you want a redundant second copy of with `modelark protect --repo <id>`.

> These sets were generated from a catalog snapshot and are **illustrative** — regenerate against a
> fresh `modelark discover --walk` for current models and numbers.
