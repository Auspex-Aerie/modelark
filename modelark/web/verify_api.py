"""Portal endpoints for the Verifier (DEF-021): auto-surfaced disruption suspects + on-demand re-verify
of archived copies. Uses its OWN read-only connection (WAL allows concurrent readers) so a slow
decompress-canary never holds the portal's write lock / freezes the UI."""
from __future__ import annotations

from modelark.core import db


def suspects() -> dict:
    from modelark import verifier
    con = db.connect(read_only=True)
    try:
        return {"suspects": verifier.suspects(con)}
    finally:
        con.close()


def run(body: dict) -> dict:
    from modelark import verifier
    repos = body.get("repos") or []
    if not repos:
        return {"ok": False, "error": "no repos given"}
    deep = bool(body.get("deep", True))
    con = db.connect(read_only=True)
    try:
        return {"ok": True, "results": verifier.reverify_many(con, repos, deep=deep)}
    finally:
        con.close()
