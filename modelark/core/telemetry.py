"""Application logging for ModelArk — ported from Bayence-Certus's telemetry_manager (pure stdlib),
with the gaps that matter for a long-lived systemd service filled in:

  • a ROTATING file sink (Bayence used a bare, unbounded FileHandler) so the log can't fill the disk;
  • BOTH a file and a stdout sink — a logging StreamHandler flushes per record, so the file AND
    journald stay current *while the process is alive*. That's the whole point: the 2026-07-09 hang
    was invisible because the fill's block-buffered print()s never flushed to journald (INC-006/DEC-023).
  • config from wishlist.yaml (no env vars, per project rule) rather than Bayence's pydantic coupling.

Usage:
    from modelark.core import telemetry
    telemetry.configure(level="INFO", file_path="logs/modelark.log")   # once, at process start
    log = telemetry.get_logger("fetch", drive="drive-00")
    log.info("shard stored", repo=rid, shard="3/8", codec="streamznn", ratio=0.67)
    #   → 2026-07-09 12:00:01 [INFO    ] modelark-fill modelark.fetch: shard stored | repo="…" shard="3/8" …
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from threading import Lock
from typing import Any

_ROOT = "modelark"
_LOCK = Lock()
# threadName distinguishes the portal (MainThread) from the fill worker (modelark-fill) in one file;
# child compress/download processes log via their captured stderr, surfaced by the parent as a message.
_FMT = "%(asctime)s [%(levelname)-8s] %(threadName)s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class TaggedLogger:
    """A logging.Logger wrapper carrying static context, rendered Bayence-style as `msg | k="v"`."""

    def __init__(self, logger: logging.Logger, context: dict[str, Any] | None = None):
        self._logger = logger
        self._context = context or {}

    def with_context(self, **context: Any) -> "TaggedLogger":
        merged = dict(self._context)
        merged.update(context)
        return TaggedLogger(self._logger, merged)

    # `message` is positional-only (the `/`) so a context kwarg named `message` (or any method-param
    # name) lands in **ctx instead of colliding — e.g. log.info("done", message=res["message"]) is safe.
    def debug(self, message: str, /, **ctx: Any) -> None:
        self._emit(logging.DEBUG, message, ctx)

    def info(self, message: str, /, **ctx: Any) -> None:
        self._emit(logging.INFO, message, ctx)

    def warning(self, message: str, /, **ctx: Any) -> None:
        self._emit(logging.WARNING, message, ctx)

    def error(self, message: str, /, **ctx: Any) -> None:
        self._emit(logging.ERROR, message, ctx)

    def exception(self, message: str, /, **ctx: Any) -> None:
        self._emit(logging.ERROR, message, ctx, exc_info=True)

    def _emit(self, level: int, message: str, ctx: dict, exc_info: bool = False) -> None:
        merged = dict(self._context)
        merged.update(ctx)
        self._logger.log(level, _format(message, merged), exc_info=exc_info)


def _format(message: str, context: dict[str, Any]) -> str:
    if not context:
        return message
    suffix = " ".join(f"{k}={_render(v)}" for k, v in context.items())
    return f"{message} | {suffix}"


def _render(value: Any) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value)


def _level_num(level: str) -> int:
    num = logging.getLevelName(level.upper())     # str -> int for standard names
    if not isinstance(num, int):
        raise ValueError(f"invalid log level: {level!r}")
    return num


def configure(level: str = "INFO", file_path: str | Path | None = None,
              max_bytes: int = 20_000_000, backups: int = 5, to_console: bool = True) -> None:
    """Set up the `modelark` logger with a rotating file sink (+ stdout). Idempotent — safe to call
    again (handlers are rebuilt). `file_path=None` → console only."""
    with _LOCK:
        root = logging.getLogger(_ROOT)
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)
        root.setLevel(_level_num(level))
        root.propagate = False                    # our own namespace; don't double-emit via the python root
        fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)
        if to_console:
            ch = logging.StreamHandler(sys.stdout)   # flushes per record → journald/terminal current even while alive
            ch.setFormatter(fmt)
            root.addHandler(ch)
        if file_path is not None:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        if not root.handlers:
            raise ValueError("telemetry.configure: no sinks enabled (file_path=None and to_console=False)")


def get_logger(name: str | None = None, **context: Any) -> TaggedLogger:
    """A tagged logger under the `modelark` namespace. Any `name` is reparented to `modelark.<name>`
    so records propagate to the configured handlers. Extra kwargs are static context on every line."""
    if not name or name == _ROOT:
        logger = logging.getLogger(_ROOT)
    else:
        short = name.rsplit(".", 1)[-1]           # e.g. "modelark.fetch" -> "fetch"
        logger = logging.getLogger(f"{_ROOT}.{short}")
    return TaggedLogger(logger, context)
