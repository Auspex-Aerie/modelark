"""Load discovery scope, archive policy, and operational settings from wishlist.yaml."""
from __future__ import annotations

import os
import sys
from importlib import resources
from pathlib import Path

import yaml

from modelark.core import db

_CONFIG_OVERRIDE: Path | None = None


def configure(path: str | Path | None = None) -> None:
    """Select an explicit wishlist file. Omitted: user config, source checkout, then packaged default."""
    global _CONFIG_OVERRIDE
    _CONFIG_OVERRIDE = Path(path).expanduser().resolve() if path is not None else None


def _user_config_path() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "modelark" / "wishlist.yaml"


def config_source():
    """Return the selected config as a Path or importlib Traversable."""
    if _CONFIG_OVERRIDE is not None:
        if not _CONFIG_OVERRIDE.is_file():
            raise FileNotFoundError(f"ModelArk config does not exist: {_CONFIG_OVERRIDE}")
        return _CONFIG_OVERRIDE
    user = _user_config_path()
    if user.is_file():
        return user
    source = db.REPO_ROOT / "wishlist.yaml"
    if source.is_file():                                  # editable/source checkout compatibility
        return source
    return resources.files("modelark").joinpath("default_wishlist.yaml")


def load() -> dict:
    return yaml.safe_load(config_source().read_text(encoding="utf-8"))


def orgs() -> list[str]:
    return load()["always_include"]["orgs"]


def scope_categories() -> list[str]:
    return load()["scope"]["include_categories"]


def exclude_pickle_only() -> bool:
    """Whether archive planning must refuse repositories whose only usable weights are pickle."""
    return bool((load().get("exclude") or {}).get("pickle_only", True))


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


# Logging (DEC-023 / #26). Relative paths live under the writable state directory; set `file` null
# to disable the file sink (console only).
_LOGGING_DEFAULTS = {"level": "INFO", "file": "logs/modelark.log", "max_mb": 20, "backups": 5, "console": True}


def logging_config() -> dict:
    """Kwargs for telemetry.configure(): {level, file_path (abs or None), max_bytes, backups, to_console}."""
    cfg = dict(_LOGGING_DEFAULTS)
    cfg.update(load().get("logging") or {})
    file = cfg["file"]
    if file:
        p = Path(file)
        file = str(p if p.is_absolute() else db.STATE_DIR / p)
    return {"level": str(cfg["level"]), "file_path": file or None,
            "max_bytes": int(float(cfg["max_mb"]) * 1_000_000), "backups": int(cfg["backups"]),
            "to_console": bool(cfg["console"])}
