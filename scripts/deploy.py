"""Minimal, operator-visible deployment for the ModelArk portal.

This intentionally owns only the unprivileged application surface: a checkout-local
virtual environment, explicit runtime directories, and a systemd user unit. System
packages, SMART sudoers access, storage mounts, and data migration remain separate,
operator-visible steps.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


UNIT_NAME = "modelark.service"
UNIT_TEMPLATE = Path(__file__).with_name(UNIT_NAME)


def _xdg_path(env_name: str, fallback: Path) -> Path:
    return Path(os.environ.get(env_name, fallback)).expanduser().resolve()


def _default_source() -> Path:
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / "pyproject.toml").is_file():
        return checkout
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").is_file():
        return cwd
    raise RuntimeError("cannot locate a ModelArk checkout; pass --source PATH")


def _unit_quote(value: str | Path) -> str:
    """Quote one systemd directive/ExecStart token and escape specifier expansion."""
    text = str(value)
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError("systemd unit values cannot contain NUL or newlines")
    return '"' + text.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unit_path(value: str | Path) -> str:
    """Escape a path directive, whose parser does not remove shell-style quotes."""
    text = str(value)
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError("systemd unit paths cannot contain NUL or newlines")
    escaped = []
    for char in text:
        if char == "%":
            escaped.append("%%")
        elif char.isascii() and (char.isalnum() or char in "/_.-"):
            escaped.append(char)
        elif char.isascii():
            escaped.append(f"\\x{ord(char):02x}")
        else:
            escaped.append(char)
    return "".join(escaped)


def render_unit(
    source: Path,
    executable: Path,
    data_dir: Path,
    state_dir: Path,
    config: Path | None,
    port: int,
    resume_fill: bool,
) -> str:
    argv = [
        executable,
        "--data-dir", data_dir,
        "--state-dir", state_dir,
    ]
    if config is not None:
        argv.extend(("--config", config))
    argv.extend(("serve", "--port", str(port), "--no-open"))
    if resume_fill:
        argv.append("--resume")
    exec_start = " ".join(_unit_quote(arg) for arg in argv)
    template = UNIT_TEMPLATE.read_text(encoding="utf-8")
    return (template
            .replace("@WORKING_DIRECTORY@", _unit_path(source))
            .replace("@EXEC_START@", exec_start))


def _run(argv: list[str | Path], dry_run: bool) -> None:
    printable = shlex.join(str(arg) for arg in argv)
    print(f"+ {printable}")
    if not dry_run:
        subprocess.run([str(arg) for arg in argv], check=True)


def _mkdir_private(path: Path, dry_run: bool) -> None:
    print(f"+ install -d -m 700 {shlex.quote(str(path))}")
    if not dry_run:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)


def _write_unit(path: Path, content: str, dry_run: bool) -> None:
    print(f"+ install systemd user unit {path}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _guard_legacy_catalog(source: Path, data_dir: Path) -> None:
    """Never let a non-editable install silently ignore checkout-local runtime data."""
    legacy_dir = (source / "catalog").resolve()
    found = [path for path in (
        legacy_dir / "catalog.sqlite",
        legacy_dir / "catalog.duckdb",
    ) if path.is_file()]
    target = data_dir / "catalog.sqlite"
    target_ready = False
    if target.is_file():
        try:
            con = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
            try:
                target_ready = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='models'"
                ).fetchone() is not None
            finally:
                con.close()
        except sqlite3.Error:
            pass
    if found and data_dir.resolve() != legacy_dir and not target_ready:
        names = ", ".join(str(path) for path in found)
        raise RuntimeError(
            f"checkout-local catalog found ({names}) but deployment target {data_dir} has no valid "
            "migrated catalog.sqlite; pass --data-dir to use existing SQLite data or follow "
            "docs/legacy-cutover.md before deploying")


def _validate_unit(
    unit_path: Path,
    source: Path,
    executable: Path,
    data_dir: Path,
    state_dir: Path,
    config: Path | None,
    port: int,
) -> None:
    content = unit_path.read_text(encoding="utf-8")
    expected = [
        f"WorkingDirectory={_unit_path(source)}",
        " ".join((_unit_quote(executable), _unit_quote("--data-dir"), _unit_quote(data_dir))),
        " ".join((_unit_quote("--state-dir"), _unit_quote(state_dir))),
        " ".join((_unit_quote("--port"), _unit_quote(str(port)))),
    ]
    if config is not None:
        expected.append(" ".join((_unit_quote("--config"), _unit_quote(config))))
    missing = [item for item in expected if item not in content]
    if missing:
        raise RuntimeError(
            "installed unit does not match the requested deployment: " + ", ".join(missing))


def _check(
    source: Path,
    venv: Path,
    unit_path: Path,
    data_dir: Path,
    state_dir: Path,
    config: Path | None,
    port: int,
) -> None:
    executable = venv / "bin" / "modelark"
    if not executable.is_file():
        raise RuntimeError(f"deployed executable is missing: {executable}")
    if not unit_path.is_file():
        raise RuntimeError(f"systemd user unit is missing: {unit_path}")
    _validate_unit(unit_path, source, executable, data_dir, state_dir, config, port)
    subprocess.run([str(executable), "--help"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT_NAME], check=True)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/meta",
        headers={"Host": f"127.0.0.1:{port}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"portal health check returned HTTP {response.status}")
        payload = json.load(response)
    if "os" not in payload:
        raise RuntimeError(f"portal health check returned an unexpected response: {payload!r}")
    print(f"deployment healthy: {source} · {executable} · portal os={payload['os']} port={port}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modelark-deploy",
        description="Install ModelArk into a checkout-local venv and render its systemd user service.",
    )
    parser.add_argument("--source", type=Path, help="ModelArk checkout (default: this checkout)")
    parser.add_argument("--venv", type=Path, help="virtual environment (default: SOURCE/.venv)")
    parser.add_argument("--data-dir", type=Path, help="catalog/runtime data directory")
    parser.add_argument("--state-dir", type=Path, help="logs/state directory")
    parser.add_argument("--config", type=Path, help="explicit wishlist.yaml; packaged default if omitted")
    parser.add_argument("--port", type=int, default=8077, help="loopback portal port (default: 8077)")
    parser.add_argument("--resume-fill", action="store_true",
                        help="auto-resume unfinished fill work whenever the service starts")
    parser.add_argument("--enable", action="store_true",
                        help="enable the user service for future login sessions")
    parser.add_argument("--start", action="store_true",
                        help="start/restart the portal after installing it")
    parser.add_argument("--skip-install", action="store_true",
                        help="reuse an existing venv and only update the unit")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    parser.add_argument("--check", action="store_true",
                        help="only verify the installed binary, active service, and portal response")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        source = (args.source.expanduser().resolve() if args.source else _default_source())
    except RuntimeError as exc:
        parser.error(str(exc))
    if not (source / "pyproject.toml").is_file():
        parser.error(f"not a ModelArk source checkout: {source}")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if hasattr(os, "geteuid") and os.geteuid() == 0 and not args.dry_run:
        parser.error("do not deploy ModelArk as root; run this as the service user")

    home = Path.home()
    data_dir = (args.data_dir.expanduser().resolve() if args.data_dir else
                _xdg_path("XDG_DATA_HOME", home / ".local" / "share") / "modelark")
    state_dir = (args.state_dir.expanduser().resolve() if args.state_dir else
                 _xdg_path("XDG_STATE_HOME", home / ".local" / "state") / "modelark")
    config = args.config.expanduser().resolve() if args.config else None
    if config is not None and not config.is_file():
        parser.error(f"--config does not exist: {config}")
    venv = args.venv.expanduser().resolve() if args.venv else source / ".venv"
    executable = venv / "bin" / "modelark"
    unit_root = _xdg_path("XDG_CONFIG_HOME", home / ".config") / "systemd" / "user"
    unit_path = unit_root / UNIT_NAME

    if args.check:
        if any((args.enable, args.start, args.resume_fill, args.skip_install, args.dry_run)):
            parser.error("--check cannot be combined with deployment actions")
        try:
            _check(source, venv, unit_path, data_dir, state_dir, config, args.port)
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
            raise SystemExit(f"deployment check failed: {exc}") from exc
        return

    if not sys.platform.startswith("linux") and not args.dry_run:
        parser.error("the supervised deploy surface currently supports Linux/systemd only")
    if shutil.which("systemctl") is None and not args.dry_run:
        parser.error("systemctl is required for the supervised deploy surface")
    try:
        _guard_legacy_catalog(source, data_dir)
    except RuntimeError as exc:
        if args.dry_run:
            print(f"WARNING: live deployment would stop: {exc}", file=sys.stderr)
        else:
            parser.error(str(exc))

    print("ModelArk deployment plan")
    print(f"  source:  {source}")
    print(f"  venv:    {venv}")
    print(f"  data:    {data_dir}")
    print(f"  state:   {state_dir}")
    print(f"  config:  {config or 'packaged/user default'}")
    print(f"  unit:    {unit_path}")
    print(f"  resume:  {'enabled' if args.resume_fill else 'disabled'}")

    if not args.skip_install:
        if not (venv / "bin" / "python").is_file():
            _run([sys.executable, "-m", "venv", venv], args.dry_run)
        _run([venv / "bin" / "python", "-m", "pip", "install", source], args.dry_run)
        _run([executable, "--help"], args.dry_run)
    elif not args.dry_run and not executable.is_file():
        parser.error(f"--skip-install requested but {executable} does not exist")

    _mkdir_private(data_dir, args.dry_run)
    _mkdir_private(state_dir, args.dry_run)
    unit = render_unit(source, executable, data_dir, state_dir, config, args.port, args.resume_fill)
    _write_unit(unit_path, unit, args.dry_run)
    _run(["systemctl", "--user", "daemon-reload"], args.dry_run)
    if args.enable:
        _run(["systemctl", "--user", "enable", UNIT_NAME], args.dry_run)
    if args.start:
        _run(["systemctl", "--user", "restart", UNIT_NAME], args.dry_run)

    if not args.dry_run:
        print("deployment installed")
        if not args.start:
            print(f"  start: systemctl --user start {UNIT_NAME}")
        print(f"  logs:  journalctl --user -u {UNIT_NAME} -f")
        print(f"  check: {executable.parent / 'modelark-deploy'} --source {source} --check")


if __name__ == "__main__":
    main()
