"""Load and expose the declarative curation rules in wishlist.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

from modelark.core import db

WISHLIST_PATH = db.REPO_ROOT / "wishlist.yaml"


def load() -> dict:
    return yaml.safe_load(WISHLIST_PATH.read_text())


def orgs() -> list[str]:
    return load()["always_include"]["orgs"]


def scope_categories() -> list[str]:
    return load()["scope"]["include_categories"]


# DEC-022 compression gate. Defaults are conservative so an older wishlist.yaml (no `compression:`
# section) still runs safely; an explicit section overrides key-by-key.
_COMPRESSION_DEFAULTS = {"max_compress_ram_gb": 4.0, "stream_compress": True, "threads": 1}


def compression() -> dict:
    """The compression gate config: {max_compress_ram_gb, stream_compress, threads}."""
    cfg = dict(_COMPRESSION_DEFAULTS)
    cfg.update(load().get("compression") or {})
    return cfg


# Download rate cap. The fill self-throttles once `max_24h_gb` of ORIGINAL bytes have been archived
# within a ROLLING 24h window (fetch._bytes_last_24h); 0 = unlimited. Default 1 TB/day if wishlist.yaml
# has no `download:` section — deliberately conservative; the user opts UP, not down.
_DOWNLOAD_DEFAULTS = {"max_24h_gb": 1000.0}


def download() -> dict:
    """The download rate config: {max_24h_gb} (0 = no cap)."""
    cfg = dict(_DOWNLOAD_DEFAULTS)
    cfg.update(load().get("download") or {})
    return cfg


# Logging (DEC-023 / #26). Defaults are safe if wishlist.yaml has no `logging:` section. `file` is
# resolved relative to the repo root; set it null to disable the file sink (console only).
_LOGGING_DEFAULTS = {"level": "INFO", "file": "logs/modelark.log", "max_mb": 20, "backups": 5, "console": True}


def logging_config() -> dict:
    """Kwargs for telemetry.configure(): {level, file_path (abs or None), max_bytes, backups, to_console}."""
    cfg = dict(_LOGGING_DEFAULTS)
    cfg.update(load().get("logging") or {})
    file = cfg["file"]
    if file:
        p = Path(file)
        file = str(p if p.is_absolute() else db.REPO_ROOT / p)
    return {"level": str(cfg["level"]), "file_path": file or None,
            "max_bytes": int(float(cfg["max_mb"]) * 1_000_000), "backups": int(cfg["backups"]),
            "to_console": bool(cfg["console"])}
