"""Minimal attended Slice commands; explicit inputs and JSON results only.

The operator is imported only after parsing and launch exclusion. No catalog,
Store, device observation, or global runtime configuration is opened here.
"""
import argparse
import importlib
import json
import re


class _Attachments(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        label, separator, path = value.partition("=")
        if (not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", label)
                or not path.strip() or path != path.strip() or "\0" in path):
            raise argparse.ArgumentError(self, "expected LABEL=ARCHIVE with nonempty label and archive path")
        sources = dict(getattr(namespace, self.dest, None) or {})
        if label in sources:
            raise argparse.ArgumentError(self, f"duplicate source label: {label}")
        sources[label] = path
        setattr(namespace, self.dest, sources)


def add_subparser(subparsers):
    parser = subparsers.add_parser("slice", help="preview and execute attended direct Slice delivery")
    commands = parser.add_subparsers(dest="slice_command", required=True)
    preview = commands.add_parser("preview", help="review explicit catalog, repositories and USB destination")
    preview.add_argument("--catalog", required=True, help="explicit source catalog path (read-only)")
    preview.add_argument("--destination", required=True, help="attached destination mount root")
    preview.add_argument("--repo", required=True, action="append", help="repository ID (repeatable)")
    preview.add_argument("--root", required=True, help="new relative destination delivery root")
    approval = commands.add_parser("approve", help="approve the exact reviewed transaction seal")
    approval.add_argument("transaction")
    approval.add_argument("--seal", required=True)
    start = commands.add_parser("start", help="run synchronously until completion or an attended wait/refusal")
    start.add_argument("transaction")
    start.add_argument("--destination", required=True, help="attached destination mount root")
    start.add_argument("--source", action=_Attachments, default=None, metavar="LABEL=ARCHIVE",
                       help="explicit archive attachment (repeatable; no retrieval)")
    for name in ("status", "stop"):
        command = commands.add_parser(name, help=f"{name} of a private Slice transaction")
        command.add_argument("transaction")
    parser.set_defaults(func=dispatch)
    return parser


def dispatch(args):
    from .domain import SliceRefusal
    from .transaction import TransferRefusal
    operator = importlib.import_module("modelark.slice.operator")
    try:
        if args.slice_command == "preview":
            result = operator.preview(args.catalog, args.destination, tuple(args.repo), args.root)
        elif args.slice_command == "approve":
            result = operator.approve(args.transaction, args.seal)
        elif args.slice_command == "start":
            result = operator.start(args.transaction, args.destination, args.source or {})
        elif args.slice_command == "status":
            result = operator.status(args.transaction)
        elif args.slice_command == "stop":
            result = operator.stop(args.transaction)
        else:
            raise ValueError("unknown Slice command")
    except (SliceRefusal, TransferRefusal) as exc:
        print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail}, sort_keys=True, allow_nan=False))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    if result.get("ok") is False:
        raise SystemExit(1)
