"""Discover models on the Hugging Face Hub and record metadata in the catalog.

Metadata is cheap, so we catalog broadly; bytes are downloaded later only after
an operator selects and finalizes repositories. Walk scope is decided by our own
`category` (architecture-first), not HF's unreliable `pipeline_tag` — see DEC-002.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import huggingface_hub
from huggingface_hub import HfApi
from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError, HfHubHTTPError

from modelark.core import db
from modelark import formats, ggufmeta

# Modalities we now collect (DEC-010/011: text + audio-speech + world + image-gen).
# Vision, multimodal, and video generation stay deferred (DEF-002).
_COLLECTED_DOMAINS = {"text", "audio", "world", "image-gen"}


def _backoff(fn, *args, tries: int = 8, **kwargs):
    """Call an HF API fn, sleeping through 429 rate-limit windows (1000 req / 5 min)."""
    for attempt in range(tries):
        try:
            return fn(*args, **kwargs)
        except HfHubHTTPError as e:
            resp = getattr(e, "response", None)
            if getattr(resp, "status_code", None) == 429 and attempt < tries - 1:
                try:
                    wait = int(resp.headers.get("Retry-After", "60"))
                except (ValueError, TypeError, AttributeError):
                    wait = 60
                print(f"  [rate-limited] sleeping {wait + 3}s (attempt {attempt + 1}/{tries})", flush=True)
                time.sleep(wait + 3)
                continue
            raise


def _license_of(info) -> str | None:
    if info.card_data and getattr(info.card_data, "license", None):
        return info.card_data.license
    for t in info.tags or []:
        if t.startswith("license:"):
            return t.split(":", 1)[1]
    return None


def _architecture(info) -> str | None:
    if info.config and isinstance(info.config, dict):
        archs = info.config.get("architectures")
        if archs:
            return archs[0]
    return None


def classify(info) -> tuple[str | None, str | None, str | None, str | None]:
    """Authoritative (domain, category, variant, architecture) from full model info."""
    arch = _architecture(info)
    mt = info.config.get("model_type") if (info.config and isinstance(info.config, dict)) else None
    domain, category = formats.classify_category(
        arch, info.pipeline_tag, info.tags, info.library_name, info.id, mt)
    # GGUF repos have no config.architectures — read the arch from the GGUF header.
    if domain is None or category == "unknown":
        gguf = next((s.rfilename for s in (info.siblings or [])
                     if s.rfilename.lower().endswith(".gguf")), None)
        if gguf:
            ga = ggufmeta.architecture(info.id, gguf)
            gc = formats.gguf_category(ga)
            if gc:
                domain, category, arch = "text", gc, (arch or f"gguf:{ga}")
    variant = formats.parse_variant(info.id, info.tags)
    return domain, category, variant, arch


def _model_row(info) -> dict:
    repo_id = info.id
    domain, category, variant, arch = classify(info)
    total = sum(s.size for s in info.siblings if s.size) if info.siblings else None
    return {
        "repo_id": repo_id,
        "author": info.author,
        "model_name": repo_id.rsplit("/", 1)[-1],
        "params_b": formats.parse_params_b(info, repo_id),
        "architecture": arch,
        "modality": domain,
        "category": category,
        "variant": variant,
        "pipeline_tag": info.pipeline_tag,
        "library": info.library_name,
        "license": _license_of(info),
        "gated": str(info.gated).lower(),
        "private": info.private,
        "likes": info.likes,
        "downloads_30d": info.downloads,
        "downloads_all": info.downloads_all_time,
        "trending_score": info.trending_score,
        "tags": json.dumps(list(info.tags or [])),   # SQLite has no array type → store as JSON text
        "total_size_bytes": total,
        "hf_last_modified": info.last_modified,
        "hf_created_at": info.created_at,
    }


def _file_rows(info) -> list[dict]:
    repo_dtype = formats.repo_dtype_from_info(info)
    tags = tuple(info.tags or [])
    rows = []
    for s in info.siblings or []:
        fmt, quant, bits, safety = formats.classify_file(s.rfilename, repo_dtype, tags)
        rows.append({
            "repo_id": info.id,
            "rfilename": s.rfilename,
            "size_bytes": s.size,
            "is_lfs": bool(s.lfs),
            "sha256": s.lfs.sha256 if s.lfs else None,
            "format": fmt,
            "quant": quant,
            "quant_bits": bits,
            "safety": safety,
        })
    return rows


def persist_info(con, info) -> None:
    """Upsert a model + its files from a fetched ModelInfo."""
    db.upsert(con, "models", _model_row(info), pk=["repo_id"], touch=["updated_at"])
    db.replace_files(con, info.id, _file_rows(info))


def discover_one(api: HfApi, con, repo_id: str) -> str:
    """Fetch + catalog one repo unconditionally (explicit discovery bypasses scope)."""
    try:
        info = api.model_info(repo_id, files_metadata=True)
    except GatedRepoError:
        db.upsert(con, "models",
                  {"repo_id": repo_id, "status": "skip", "gated": "yes",
                   "notes": "gated: needs accepted license + token"},
                  pk=["repo_id"], touch=["updated_at"])
        return "gated"
    except RepositoryNotFoundError:
        return "notfound"
    except HfHubHTTPError as e:
        return f"http-error: {e}"
    persist_info(con, info)
    return "ok"


def discover_repos(repo_ids: list[str], con=None) -> dict[str, str]:
    api = HfApi()
    own = con is None
    con = con or db.connect()
    results = {}
    try:
        for rid in repo_ids:
            status = discover_one(api, con, rid)
            results[rid] = status
            print(f"  [{status:>10}] {rid}")
    finally:
        if own:
            con.close()
    return results


def discover_top(n: int, task: str = "text-generation", con=None) -> dict[str, str]:
    api = HfApi()
    own = con is None
    con = con or db.connect()
    try:
        ids = [m.id for m in api.list_models(sort="downloads", limit=n, pipeline_tag=task)]
        print(f"Discovering top {len(ids)} '{task}' models by downloads...")
        return discover_repos(ids, con=con)
    finally:
        if own:
            con.close()


def _write_exclusions(records: list[dict], path=None) -> Path:
    """Persist every skipped repo (with reason) so nothing is dropped silently."""
    exclusions_dir = db.CATALOG_DIR / "exclusions"
    exclusions_dir.mkdir(parents=True, exist_ok=True)
    if path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = exclusions_dir / f"discover-{ts}.jsonl"
    else:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def discover_orgs(orgs, include_categories, con=None, limit_per_org: int = 40,
                  exclusions_path=None) -> tuple[dict, Path, list[dict]]:
    """Walk each org, catalog repos whose (architecture-derived) category is in scope.

    Cheap path: a repo whose pipeline tag already resolves to a non-text domain is
    excluded without a fetch. Everything else (text or tag-less) is fetched so its
    authoritative category comes from config.architectures. Returns
    (per-org stats, exclusions file path, exclusion records).
    """
    api = HfApi()
    own = con is None
    con = con or db.connect()
    include = set(include_categories)
    existing = {r[0] for r in con.execute("SELECT repo_id FROM models").fetchall()}
    excluded: list[dict] = []
    stats: dict[str, dict] = {}
    try:
        for org in orgs:
            lim = limit_per_org if limit_per_org and limit_per_org > 0 else None
            try:
                listed = _backoff(lambda: list(
                    api.list_models(author=org, sort="downloads", limit=lim)))
            except Exception as e:  # bad handle / transient — record, never crash the walk
                print(f"  {org:24} ORG-ERROR: {e}")
                excluded.append({"repo_id": None, "org": org, "reason": "org-error", "detail": str(e)})
                stats[org] = {"listed": 0, "cataloged": 0, "excluded": 0, "error": str(e)}
                continue

            cataloged = exc = skipped = 0
            for m in listed:
                if m.id in existing:  # resume: already cataloged, don't re-fetch
                    skipped += 1
                    continue
                # Cheap pre-filter: a present, clearly non-text tag → skip without a fetch.
                cheap_domain, _ = formats.classify_category(
                    None, m.pipeline_tag, m.tags, getattr(m, "library_name", None), m.id)
                if cheap_domain and cheap_domain not in _COLLECTED_DOMAINS:
                    excluded.append({"repo_id": m.id, "org": org,
                                     "reason": f"domain:{cheap_domain}", "pipeline_tag": m.pipeline_tag})
                    exc += 1
                    continue
                # Text or tag-less → fetch for authoritative architecture-based category.
                try:
                    info = _backoff(api.model_info, m.id, files_metadata=True)
                except GatedRepoError:
                    db.upsert(con, "models",
                              {"repo_id": m.id, "status": "skip", "gated": "yes", "notes": "gated"},
                              pk=["repo_id"], touch=["updated_at"])
                    excluded.append({"repo_id": m.id, "org": org, "reason": "gated"})
                    exc += 1
                    continue
                except (RepositoryNotFoundError, HfHubHTTPError) as e:
                    excluded.append({"repo_id": m.id, "org": org,
                                     "reason": "fetch-error", "detail": str(e)[:120]})
                    exc += 1
                    continue
                domain, category, _, arch = classify(info)
                if category in include:
                    persist_info(con, info)
                    cataloged += 1
                else:
                    excluded.append({"repo_id": m.id, "org": org,
                                     "reason": f"category:{category or domain or 'unknown'}",
                                     "pipeline_tag": info.pipeline_tag,
                                     "architecture": arch, "library": info.library_name})
                    exc += 1
            capped = " (capped)" if lim and len(listed) == lim else ""
            stats[org] = {"listed": len(listed), "cataloged": cataloged,
                          "excluded": exc, "skipped": skipped}
            skip_note = f"  skipped={skipped:>3}" if skipped else ""
            print(f"  {org:24} listed={len(listed):>4}{capped:<9} "
                  f"cataloged={cataloged:>3}  excluded={exc:>3}{skip_note}", flush=True)
        path = _write_exclusions(excluded, exclusions_path)
    finally:
        if own:
            con.close()
    return stats, path, excluded


def tool_versions() -> str:
    return f"huggingface_hub={huggingface_hub.__version__}"
