"""Installed DEC-059 provenance rehearsal and publication (DEF-035).

Never binds the live catalog. Paths are explicit. Rehearse is the safe verb.
Publication requires ``--confirm-stopped MODELARK-STOPPED``.

This is not ``modelark-migrate`` (legacy-runtime data-directory cutover).
Do not point it at the operator live catalog; use disposable copies only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from modelark.core import db

_CONFIRMATION = "MODELARK-STOPPED"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    rehearse = sub.add_parser("rehearse", help="clone-first rehearsal; never mutates source")
    rehearse.add_argument("--source-data-dir", type=Path, required=True)
    rehearse.add_argument("--work-dir", type=Path, required=True)
    rehearse.add_argument("--run-id", required=True)
    rehearse.set_defaults(_run=_cmd_rehearse)

    publish = sub.add_parser(
        "publish",
        help="publish a rehearsed clone (requires stopped-writer confirmation)",
    )
    publish.add_argument("--work-dir", type=Path, required=True)
    publish.add_argument("--dest-dir", type=Path, required=True)
    publish.add_argument(
        "--confirm-stopped",
        metavar="TEXT",
        required=True,
        help=f"must be exactly {_CONFIRMATION}",
    )
    publish.set_defaults(_run=_cmd_publish)
    return parser


def _cmd_rehearse(args: argparse.Namespace) -> dict:
    return db.rehearse_provenance_migration(
        args.source_data_dir, args.work_dir, run_id=args.run_id
    )


def _cmd_publish(args: argparse.Namespace) -> dict:
    return db.publish_provenance_migration(
        args.work_dir,
        args.dest_dir,
        confirm_stopped=args.confirm_stopped,
        writers_stopped=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cmd == "publish" and args.confirm_stopped != _CONFIRMATION:
        parser.error(f"publish --confirm-stopped must be exactly {_CONFIRMATION}")
    try:
        result = args._run(args)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    except (RuntimeError, FileExistsError) as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
