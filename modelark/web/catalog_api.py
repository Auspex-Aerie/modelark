"""Catalog read API: facets (filter options) and a filtered/sorted model list."""
from __future__ import annotations

from modelark.web import data

_SORT = {"id": "repo_id", "p": "params_b", "bucket": "bucket", "cat": "category",
         "v": "variant", "gb": "bytes", "dl": "downloads_30d", "lic": "license"}
_LIMIT = 2000


def facets() -> dict:
    cats = data.q("SELECT category, count(*) FROM ui_cache GROUP BY 1 ORDER BY 2 DESC")
    lics = data.q("SELECT coalesce(license,'—'), count(*) FROM ui_cache GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
    return {
        "categories": [{"name": c, "n": n} for c, n in cats],
        "variants": ["base", "instruct", "reasoning", "finetune", "quant"],
        "buckets": data.BUCKETS,
        "licenses": [{"name": l, "n": n} for l, n in lics],
        "budget": data.DEFAULT_BUDGET_TB,
    }


def models(p: dict) -> dict:
    where, params = ["1=1"], []
    sel = p.get("sel", [""])[0]                     # mutually-exclusive checked/unchecked filter
    if sel != "checked":                            # "only checked" shows the WHOLE cart; hide-toggles are for browsing
        if p.get("hide_quant", ["1"])[0] == "1":
            where.append("variant != 'quant'")
        if p.get("hide_gated", ["0"])[0] == "1":
            where.append("NOT gated")
    if sel == "checked":
        where.append("sel")
    elif sel == "unchecked":
        where.append("NOT sel")
    for field, col in (("cat", "category"), ("v", "variant"), ("bucket", "bucket")):
        if p.get(field, [""])[0]:
            vals = p[field][0].split(",")
            where.append(f"{col} IN ({','.join(['?'] * len(vals))})")
            params += vals
    if p.get("q", [""])[0].strip():
        where.append("lower(repo_id) LIKE ?")
        params.append(f"%{p['q'][0].strip().lower()}%")
    clause = " AND ".join(where)
    sort = _SORT.get(p.get("sort", ["dl"])[0], "downloads_30d")
    direction = "ASC" if p.get("dir", ["desc"])[0] == "asc" else "DESC"

    base = ("(SELECT ui_cache.*, (sel.repo_id IS NOT NULL) AS sel "
            "FROM ui_cache LEFT JOIN selection sel USING(repo_id))")
    rows = data.q(
        f"SELECT repo_id,params_b,bucket,category,variant,license,downloads_30d,gated,bytes,sel "
        f"FROM {base} WHERE {clause} ORDER BY {sort} {direction} NULLS LAST LIMIT {_LIMIT}", params)
    matched, filtered_bytes = data.q(
        f"SELECT count(*), coalesce(sum(bytes),0) FROM {base} WHERE {clause}", params)[0]
    keys = ["id", "p", "bucket", "cat", "v", "lic", "dl", "g", "bytes", "sel"]
    return {"rows": [dict(zip(keys, r)) for r in rows], "matched": matched,
            "filtered_bytes": filtered_bytes, "total": data.total, "capped": matched > _LIMIT}
