"""Fetch pipeline (DEC-003) — download the finalized wishlist onto a drive.

Per file: hf download → compute sha256 (and verify it against HF when supplied) →
ZipNN-compress (float
weights) + mandatory round-trip canary → drop the original → git-annex add →
record in `archived`. Peak transient footprint is ~one shard, so a 1.4TB model
streams through without needing 1.4TB of scratch.

The fetch set is `selection` rows with finalized_at set (the "Finish" button in
the portal). Companion files (config/tokenizer/index) ride along uncompressed so
restore can reconstruct the original Hugging Face layout. Functional loadability
is not claimed by this pipeline (Tier B is unimplemented).

Execution context (DEC-019/020, task #22). `run`/`fetch_model`/`run_replica` take an
optional `RunCtx` so the SAME code serves the CLI and the portal's background worker.
`ctx=None` (the default) reproduces today's behaviour exactly: each entry point opens
its own connection, never locks (nullcontext), never emits, never stops. The worker
injects the portal's shared connection + `data._lock` so its per-file writes are
BRIEF-locked (the multi-day download/compress runs lock-free → the portal never
freezes), plus a progress callback and a cooperative stop checked at file boundaries.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from huggingface_hub import HfApi, get_token
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

from modelark.core import db
from modelark import archive_manifest, compress, register, wishlist

# Download-stall resilience (INC-004 + the DEC-023 stage-2 watchdog). The built-in read timeout did
# NOT catch a multi-hour hang (hf blocked/retried internally and never returned control; on 2026-07-09
# it wasn't even reached — the download sat in poll() for hours), so downloads run in a killable child
# (download_worker.py, which keeps its own belt-and-suspenders socket timeout) and the parent:
#   1) KILLS the child if its .incomplete stops growing (the no-progress watchdog — the real guard);
#   2) bounded retries per shard — classic HTTP may resume .incomplete; hf_xet restarts that shard
#      from zero (INC-010). Completed files remain durable and are never fetched again;
#   3) a circuit-breaker: clustered stalls → a full cooldown instead of hammering a flaky network;
#   4) per-repo isolation in run() — one repo's failure can't wedge the whole fill.
_DL_RETRIES = 4                 # attempts per shard before giving up on it
_DL_BACKOFF = 15                # base backoff seconds for an ISOLATED stall, grows per attempt
_STALL_WINDOW = 20 * 60         # sec: rolling window for counting clustered stalls
_STALL_COOLDOWN = 120           # sec: >1 stall in the window → a flaky network; pause this long to let it recover

# Stage-2 hang watchdog (DEC-023 / #27). The socket timeout above did NOT catch the 2026-07-09
# Falcon-H1 hang (the fill sat blocked in poll() for hours), so the heavy native ops run as MONITORED
# child processes: if the op's on-disk output stops growing for the window below, the parent KILLS the
# child — the only way to break a wedged native download/compress — then retries (download) or falls
# back to raw (compress). This also makes Stop interrupt mid-download/compress, not just at boundaries.
_DL_STALL_SECS = 180            # no .incomplete growth this long → the download is hung; kill + retry
_COMPRESS_STALL_SECS = 300      # FLOOR for the compress window (small shards); scaled up per-shard below
_COMPRESS_STALL_PER_GB = 60     # +sec of window per shard-GB: the child's canary (decompress+hash the
#                                 whole shard to certify restore) READS the .znn — the temp stops growing,
#                                 so the watchdog is blind to canary progress. A 29 GB canary is ~13 min
#                                 over iSCSI (DIS-002: 8 GB ≈ 3.5 min), which blew past a flat 300 s and
#                                 false-killed a working canary → stored raw (INC-011). 60 s/GB ≈ 2.3× the
#                                 measured ~26 s/GB, so the window covers the canary with margin; compress
#                                 itself grows the temp every few sec so it never approaches the window.
_MONITOR_POLL = 5               # sec between progress samples / stop checks while a child runs
_GIT_PROBE_TIMEOUT = 30         # bounded proof lookup on removable/network-backed archive worktrees


class _StopRequested(Exception):
    """Raised inside the download loop when a stop is requested mid-shard, so run() can stop cleanly."""


class CapacityPreflightError(RuntimeError):
    """The fresh per-file ledger check refuses an operation before any download/write."""

    def __init__(self, failure):
        super().__init__(f"{failure.code.value}: short by {failure.shortfall_bytes} bytes")
        self.failure = failure


class FetchTerminalError(RuntimeError):
    """A deterministic operator boundary that must not enter repo/network retry loops."""

    def __init__(self, code: str, message: str, *, evidence: dict | None = None,
                 actions: Sequence[str] = (), gate: str = "C"):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}
        self.actions = tuple(actions)
        self.gate = gate

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "evidence": dict(self.evidence),
            "actions": list(self.actions),
            "gate": self.gate,
        }


class DownloadLocalError(FetchTerminalError):
    """The isolated download child hit local filesystem state, not a transient network stall."""

    def __init__(self, detail: str, error_number: int | None = None):
        super().__init__(
            "DOWNLOAD_LOCAL_IO",
            f"local archive staging failed: {detail}",
            evidence={"errno": error_number},
            actions=("inspect_target_filesystem", "check_free_space", "retry_fill"),
        )


class TargetPathConflictError(FetchTerminalError):
    """A staged verified file cannot safely replace the existing worktree entry."""

    def __init__(self, rfilename: str, reason: str):
        super().__init__(
            "TARGET_PATH_CONFLICT",
            f"archive target for {rfilename} is not safely replaceable: {reason}",
            evidence={"rfilename": rfilename, "reason": reason},
            actions=("inspect_annex_placeholder", "verify_existing_path", "retry_fill"),
        )


ArchivePolicyError = archive_manifest.ArchivePolicyError


def _noop(ev: dict) -> None:            # default progress sink (CLI relies on the print()s below)
    pass


def _never() -> bool:                   # default stop check — a bare `run`/`fetch` never self-cancels
    return False


def _timeout_action(prompt: dict, timeout_seconds: float) -> str:
    """Non-portal callers cannot answer an operator prompt; park it without sleeping."""
    return "timeout"


@dataclass
class RunCtx:
    """Injected execution strategy (task #22). `con` is the DB connection every DB touch uses;
    `lock` is held ONLY around those touches (nullcontext on the CLI, `data._lock` in the worker)
    so downloads/compression stay lock-free; `on_progress` receives flat status dicts (a `"say"`
    key marks a human line the CLI prints); `should_stop` is polled at file/repo boundaries;
    `stats` accumulates session totals (bytes, ratio, per-drive) for the live rate readout."""
    con: Any
    lock: Any = field(default_factory=nullcontext)
    on_progress: Callable[[dict], None] = _noop
    should_stop: Callable[[], bool] = _never
    stats: dict = field(default_factory=dict)
    read_connection_factory: Callable[[], Any] | None = None
    check_hf_auth: bool = False
    request_action: Callable[[dict, float], str] = _timeout_action

    def q1(self, sql: str, params: list | None = None):
        with self.lock:
            return self.con.execute(sql, params if params is not None else []).fetchone()

    def write(self, fn: Callable[[Any], Any]):
        with self.lock:
            return fn(self.con)


def finalized(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL ORDER BY repo_id").fetchall()]


def plan(con, repo_id: str, *, allow_pickle: bool | None = None) -> list[dict]:
    """Compatibility adapter over the canonical archive manifest.

    ``allow_pickle=None`` applies the configured acquisition policy. Restore passes
    ``True`` because a later policy change must never make already-archived inert bytes
    unrecoverable.
    """
    policy = archive_manifest.acquisition_policy(allow_pickle=allow_pickle)
    return [item.as_fetch_record() for item in archive_manifest.manifest_for_repo(con, repo_id, policy)]


def _is_annex(dest: Path) -> bool:
    return (dest / ".git").exists() and subprocess.run(
        ["git", "-C", str(dest), "annex", "version"], capture_output=True).returncode == 0


_INCOMPLETE_MIN_AGE = 30    # only sweep a .incomplete idle ≥ this — belt-and-suspenders vs an active writer


def _sweep_incomplete(model_dir: Path) -> int:
    """Delete orphaned `.incomplete` download leftovers — a killed/stalled attempt that did NOT resume
    (hf_xet restarts a fresh reconstruction rather than resuming the partial; INC-010). Called right
    after a shard STORES: shards are fetched SEQUENTIALLY, so at that moment no `.incomplete` is being
    written (the just-stored shard's was consumed on completion; the next hasn't started) → every
    leftover is an orphan. The idle-age guard is defensive against a hypothetical concurrent writer.
    Returns bytes reclaimed. Never touches the shared hf_xet chunk cache — only this model's dl cache."""
    dl_cache = model_dir / ".cache" / "huggingface" / "download"
    if not dl_cache.exists():
        return 0
    now, freed = time.time(), 0
    # rglob, not glob: a nested rfilename's partial lands in download/<subdir>/<hash>.incomplete, so a
    # top-level glob would leave nested orphans (e.g. transformer/…) unreclaimed on the archive drive.
    for f in dl_cache.rglob("*.incomplete"):
        try:
            st = f.stat()
            if now - st.st_mtime >= _INCOMPLETE_MIN_AGE:
                f.unlink()
                freed += st.st_size
        except OSError:
            pass
    return freed


def _dest_writable(dest: Path) -> bool:
    """Probe that `dest` still accepts writes. A USB enclosure can drop MID-fill, leaving a mounted
    device that EIOs on every write (drive-01, 2026-07-10); without this, run() would churn its whole
    remaining batch logging one error per repo. Write+read+delete a tiny hidden file; OSError → dead."""
    probe = dest / ".modelark-write-probe"
    try:
        probe.write_bytes(b"ok")
        ok = probe.read_bytes() == b"ok"
        probe.unlink()
        return ok
    except OSError:
        try:
            probe.unlink()
        except OSError:
            pass
        return False


def _stored_relative_path(stored: Path, model_dir: Path) -> str:
    """Lexical POSIX path below a model root; never allow an archive record to escape that root."""
    try:
        rel = stored.relative_to(model_dir)
    except ValueError as e:
        raise RuntimeError(f"stored file escaped its model directory: {stored}") from e
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise RuntimeError(f"unsafe stored relative path: {rel}")
    return rel.as_posix()


def _hf_auth_invalid_failure() -> dict:
    """One operator contract for both preflight and mid-run credential rejection."""
    return {
        "code": "HF_AUTH_INVALID",
        "message": (
            "the configured Hugging Face credential is invalid; correct or unset HF_TOKEN, "
            "or run `hf auth login --force`, then retry"
        ),
        "evidence": {
            "credential_source": "environment" if os.environ.get("HF_TOKEN") else "cached"
        },
        "actions": ["hf_auth_login_force", "check_hf_token_environment", "retry_fill"],
        "gate": "A",
    }


def _hf_auth_failure() -> dict | None:
    """Return typed evidence only when a configured credential is definitively rejected.

    No token is valid for public repositories. Transient whoami/network failures are left to the
    download worker's bounded retry path; only HTTP 401 is an authentication blocker. ``get_token``
    also gives huggingface_hub a chance to refresh browser OAuth credentials before validation.
    """
    try:
        token = get_token()
    except Exception:
        return None
    if not token:
        return None
    try:
        HfApi().whoami()
    except HfHubHTTPError as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code != 401:
            return None
        return _hf_auth_invalid_failure()
    except Exception:
        return None
    return None


def hf_auth_preflight(ctx: RunCtx) -> dict | None:
    """Validate a configured HF credential once per execution context, before archive writes."""
    if ctx.stats.get("hf_auth_checked"):
        return None
    failure = _hf_auth_failure()
    if failure is None:
        ctx.stats["hf_auth_checked"] = True
    return failure


def _download_stage_dir(dest: Path, repo_id: str, annex: bool) -> Path:
    """Stable private same-filesystem staging so retries resume without touching worktree links."""
    token = hashlib.sha256(repo_id.encode("utf-8")).hexdigest()[:24]
    root = (dest / ".git" / "annex" / "tmp" / "modelark-downloads" if annex
            else dest / ".modelark-downloads")
    return root / token


def _annex_key_for_path(dest: Path, target: Path) -> str | None:
    try:
        rel = target.relative_to(dest).as_posix()
    except ValueError:
        return None
    try:
        tracked = subprocess.run(
            ["git", "-C", str(dest), "ls-files", "--error-unmatch", "--", rel],
            capture_output=True, text=True, timeout=_GIT_PROBE_TIMEOUT,
        )
        if tracked.returncode != 0:
            return None
        result = subprocess.run(
            ["git", "-C", str(dest), "annex", "lookupkey", "--", rel],
            capture_output=True, text=True, timeout=_GIT_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadLocalError(
            f"git-annex placeholder proof exceeded {_GIT_PROBE_TIMEOUT}s"
        ) from exc
    key = result.stdout.strip()
    return key if result.returncode == 0 and key else None


def _publish_staged(dest: Path, staged: Path, target: Path, digest: str,
                    rfilename: str, annex: bool) -> Path:
    """Atomically publish verified bytes, preserving every unproven conflicting worktree path."""
    try:
        if staged.is_symlink() or not staged.is_file():
            raise DownloadLocalError("download worker did not produce a regular staged file")
        target.parent.mkdir(parents=True, exist_ok=True)
        lexists = os.path.lexists(target)
        if lexists:
            if target.is_symlink() and not target.exists():
                if not annex or _annex_key_for_path(dest, target) is None:
                    raise TargetPathConflictError(
                        rfilename, "broken symlink is not a proven git-annex placeholder"
                    )
            elif target.is_file():
                if compress.sha256_file(target) == digest:
                    staged.unlink()
                    return target
                raise TargetPathConflictError(rfilename, "existing file has different verified bytes")
            else:
                raise TargetPathConflictError(rfilename, "existing path is not a regular file")
        if staged.stat().st_dev != target.parent.stat().st_dev:
            raise DownloadLocalError("staging and archive target are on different filesystems")
        os.replace(staged, target)
    except FetchTerminalError:
        raise
    except OSError as exc:
        raise DownloadLocalError(str(exc), exc.errno) from exc
    return target


class _HttpResp:
    """Minimal response stand-in so a download error reconstructed from the child process still carries
    the status_code + Retry-After that run() inspects (the real response object lived in the child)."""

    def __init__(self, status_code: int, retry_after: float | None = None):
        self.status_code = status_code
        self.headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        self.request = None                          # HfHubHTTPError.__init__ stores response.request


def _run_monitored(cmd: list[str], progress: Callable[[], int], stall_secs: float,
                   should_stop: Callable[[], bool]) -> dict:
    """Run `cmd` as a child, watching `progress()` — a monotonically-growing byte count for the current
    operation (the `.incomplete` for a download, the `.znn` temp for a compress). KILL the child if it
    makes no progress for `stall_secs` (the hang the socket timeout can't catch) or a stop is requested.
    Returns {"outcome": "exited"|"stalled"|"stopped", "rc": int|None, "stderr": str}. The child writes
    its real result to a file; here we only track liveness + the exit/kill disposition + a stderr tail."""
    err_fd, err_path = tempfile.mkstemp(suffix=".stderr")
    os.close(err_fd)
    outcome, rc = "exited", None
    try:
        with open(err_path, "w") as errf:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf)
            best, last_grow = -1, time.monotonic()
            while True:
                try:
                    rc = proc.wait(timeout=_MONITOR_POLL)   # NOT a deadline — just the sample interval
                    break                                    # the child exited on its own
                except subprocess.TimeoutExpired:
                    pass
                if should_stop():
                    proc.kill(); proc.wait(); outcome = "stopped"; break
                cur = progress()
                now = time.monotonic()
                if cur > best:
                    best, last_grow = cur, now
                elif now - last_grow >= stall_secs:
                    proc.kill(); proc.wait(); outcome = "stalled"; break
        stderr = Path(err_path).read_text(errors="replace").strip()[-2000:]   # after errf closed → flushed
    finally:
        Path(err_path).unlink(missing_ok=True)
    return {"outcome": outcome, "rc": rc, "stderr": stderr}


def _compress_isolated(local: Path, dtype: str, codec: str, threads: int,
                       expected_sha256: str | None, should_stop: Callable[[], bool]) -> dict:
    """Compress + canary in a MONITORED child process (DEC-023 stage 3 + the stage-2 watchdog). A
    native compressor abort (ZipNN's double-free, INC-005) kills the child, not the portal; a HUNG
    compress is killed by the no-progress watchdog. Returns a dict whose `status` is one of:
        "ok"      — canary certified; keys znn_path/znn_sha256/stored_bytes are set
        "canary"  — round-trip did NOT match the original-byte sha256 (keep original, drop nothing)
        "crash"   — child died from a signal (e.g. SIGABRT double-free) → caller stores the shard RAW
        "stalled" — child made no progress for the (size-scaled) stall window, killed → caller stores RAW
        "over-cap"— compressed output would exceed the guaranteed disk ceiling → caller stores RAW
        "error"   — child exited non-zero without a signal (unexpected; surface it)
    Raises _StopRequested if a stop is requested mid-compress (the child is killed first)."""
    dst = local.with_name(local.name + compress.ZNN_SUFFIX)
    res_fd, res_path = tempfile.mkstemp(dir=str(dst.parent), prefix=dst.name + ".", suffix=".result")
    os.close(res_fd)                                     # the child writes it; we just reserve the name
    request = json.dumps({"src": str(local), "dst": str(dst), "dtype": dtype, "codec": codec,
                          "threads": threads, "expected_sha256": expected_sha256, "result": res_path})

    def progress() -> int:                               # the growing .znn temp = compress liveness
        try:
            return sum(p.stat().st_size for p in dst.parent.glob(dst.name + ".*.tmp"))
        except OSError:
            return 0

    # Window must cover the child's canary (which READS the .znn → temp flat, watchdog blind), so scale
    # it to the shard size; floor at _COMPRESS_STALL_SECS for small shards. See INC-011.
    stall = max(_COMPRESS_STALL_SECS, int(local.stat().st_size / 1e9 * _COMPRESS_STALL_PER_GB))
    mon = _run_monitored([sys.executable, "-m", "modelark.compress_worker", request],
                         progress, stall, should_stop)
    try:
        if mon["outcome"] == "exited" and mon["rc"] == 0:
            result = json.loads(Path(res_path).read_text())
            result["status"] = ("ok" if result["ok"] else
                                ("over-cap" if result.get("over_cap") else "canary"))
            return result
        for tmp in dst.parent.glob(dst.name + ".*.tmp"):  # sweep the half-written temp the dead/killed child left
            tmp.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        if mon["outcome"] == "stopped":
            raise _StopRequested()
        if mon["outcome"] == "stalled":
            return {"status": "stalled", "stderr": mon["stderr"][-300:]}
        if mon["rc"] is not None and mon["rc"] < 0:      # killed by signal N (-6 = SIGABRT = the double-free)
            return {"status": "crash", "signal": -mon["rc"], "stderr": mon["stderr"][-300:]}
        return {"status": "error", "returncode": mon["rc"], "stderr": mon["stderr"][-2000:]}
    finally:
        Path(res_path).unlink(missing_ok=True)


def _annex_add(dest: Path, path: Path) -> str | None:
    rel = str(path.relative_to(dest))
    subprocess.run(["git", "-C", str(dest), "annex", "add", rel], check=True, capture_output=True)
    r = subprocess.run(["git", "-C", str(dest), "annex", "lookupkey", rel], capture_output=True, text=True)
    return r.stdout.strip() or None


def _annex_metadata(dest: Path, key: str | None, repo_id: str, params, fmt: str, quant) -> None:
    """#14: tag an archived blob's git-annex key with its model identity (model / params / format /
    quant), so the fleet is queryable (`git annex find --metadata model=…`) and a shelved drive is
    self-describing. Best-effort — a metadata failure never blocks the archive record."""
    if not key:
        return
    fields = ["-s", f"model={repo_id}", "-s", f"format={fmt}", "-s", f"quant={quant or 'none'}"]
    if params is not None:
        fields += ["-s", f"params={params}"]
    subprocess.run(["git", "-C", str(dest), "annex", "metadata", f"--key={key}", *fields],
                   capture_output=True)


def _download_shard(ctx: RunCtx, repo_id: str, rfilename: str, download_dir: Path, base: dict) -> Path:
    """Download one file in a KILLABLE child process (DEC-023 stage-2 watchdog + INC-004). The parent
    kills the child if its `.incomplete` stops growing for _DL_STALL_SECS — the hang the socket timeout
    can't catch (the 2026-07-09 Falcon-H1 stall, blocked in poll() for hours) — then retries. Classic
    HTTP may resume the on-disk `.incomplete`; hf_xet restarts that file (INC-010). Terminal errors
    (gated / not-found / 4xx
    incl 429), reconstructed from the child's result, propagate unretried so run() classifies them as
    before. Local filesystem failures propagate immediately and never enter the network-stall circuit
    breaker. A stop mid-download kills the child and raises _StopRequested."""
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadLocalError(str(exc), exc.errno) from exc
    dl_cache = download_dir / ".cache" / "huggingface" / "download"

    def progress() -> int:                              # the growing .incomplete = download liveness
        try:
            # rglob, not glob: hf writes a nested rfilename's partial to download/<subdir>/<hash>.incomplete
            # (e.g. transformer/…). A top-level glob would miss it and false-kill a healthy nested download.
            return sum(p.stat().st_size for p in dl_cache.rglob("*.incomplete")) if dl_cache.exists() else 0
        except OSError:
            return 0

    last_detail = "download failed"
    for attempt in range(1, _DL_RETRIES + 1):
        if ctx.should_stop():
            raise _StopRequested()
        try:
            res_fd, res_path = tempfile.mkstemp(
                dir=str(download_dir), prefix=".dl-", suffix=".result"
            )
        except OSError as exc:
            raise DownloadLocalError(str(exc), exc.errno) from exc
        os.close(res_fd)
        request = json.dumps({"repo_id": repo_id, "rfilename": rfilename,
                              "local_dir": str(download_dir), "result": res_path})
        try:
            mon = _run_monitored([sys.executable, "-m", "modelark.download_worker", request],
                                 progress, _DL_STALL_SECS, ctx.should_stop)
            if mon["outcome"] == "stopped":
                raise _StopRequested()
            if mon["outcome"] == "exited" and mon["rc"] == 0:
                result = json.loads(Path(res_path).read_text())
                if result["ok"]:
                    downloaded = Path(result["path"])
                    try:
                        downloaded.relative_to(download_dir)
                    except ValueError as exc:
                        raise DownloadLocalError("download worker returned a path outside staging") from exc
                    return downloaded
                et, code, ra, detail = (result["error_type"], result["status_code"],
                                        result["retry_after"], result["detail"])
                if et == "gated":
                    raise GatedRepoError(detail, response=_HttpResp(403))
                if et == "not_found":
                    raise RepositoryNotFoundError(detail, response=_HttpResp(404))
                if et == "http" and code is not None and 400 <= code < 500:
                    raise HfHubHTTPError(detail, response=_HttpResp(code, ra))    # incl 429 → run() stops the run
                if et == "local_io":
                    raise DownloadLocalError(detail, result.get("errno"))
                last_detail = detail                    # http 5xx / transient → retry
            elif mon["outcome"] == "stalled":
                last_detail = f"no download progress for {_DL_STALL_SECS}s — killed the hung child"
            else:                                       # child exited non-zero unexpectedly
                last_detail = f"download child exited rc={mon['rc']}: {mon['stderr'][-200:]}"
        finally:
            Path(res_path).unlink(missing_ok=True)
        # Circuit-breaker (INC-004): count this stall; >1 in the last _STALL_WINDOW → flaky network,
        # pause a full cooldown instead of hammering. An isolated stall just backs off and retries.
        stalls = ctx.stats.setdefault("stalls", [])
        now = time.monotonic()
        stalls.append(now)
        stalls[:] = [t for t in stalls if now - t <= _STALL_WINDOW]
        clustered = len(stalls) > 1
        wait = _STALL_COOLDOWN if clustered else _DL_BACKOFF * attempt
        why = (f"{len(stalls)} stalls in {_STALL_WINDOW // 60}m → {wait}s cooldown" if clustered
               else f"retry {attempt}/{_DL_RETRIES} in {wait}s")
        print(f"    [dl-retry] {repo_id}/{rfilename}: {last_detail[:80]} — {why}")
        ctx.on_progress({**base, "file_phase": "download-retry", "stall_cooldown": clustered,
                         "retry_attempt": attempt, "retry_limit": _DL_RETRIES,
                         "retry_reason": "transient_network",
                         "say": f"    transient download retry {attempt}/{_DL_RETRIES}; {why}"})
        for _ in range(wait):                           # interruptible backoff
            if ctx.should_stop():
                raise _StopRequested()
            time.sleep(1)
    raise RuntimeError(last_detail)                     # exhausted retries → run() isolates the repo


def fetch_model(
    ctx: RunCtx,
    repo_id: str,
    dest: Path,
    drive_label: str,
    annex: bool,
    compress_cfg: dict,
    *,
    manifest: Sequence[archive_manifest.ManifestFile] | None = None,
    before_file: Callable[[archive_manifest.ManifestFile], None] | None = None,
) -> dict:
    """Execute only the explicit task manifest, with a stale-work DB check per file.

    ``manifest=None`` retains the standalone fetch CLI behavior.  The reconciled fill always passes
    an exact missing manifest and a live-capacity guard, so the executor cannot silently broaden a
    task back to every repository file.
    """
    con = ctx.con
    with ctx.lock:                                      # brief: read the plan + resume set
        task_manifest = tuple(manifest) if manifest is not None else tuple(
            archive_manifest.manifest_for_repo(con, repo_id)
        )
        files = [item.as_fetch_record() for item in task_manifest]
        have = {r[0] for r in con.execute(
            "SELECT rfilename FROM archived WHERE repo_id=? AND drive_label=?",
            [repo_id, drive_label]).fetchall()}
        params = (con.execute("SELECT params_b FROM models WHERE repo_id=?",   # #14: for key metadata
                              [repo_id]).fetchone() or [None])[0]
    todo = [f for f in files if f["rfilename"] not in have]      # file-level resume after a crash
    model_dir = dest / repo_id
    stage_dir = _download_stage_dir(dest, repo_id, annex)
    try:
        model_dir.mkdir(parents=True, exist_ok=True)
        stage_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadLocalError(str(exc), exc.errno) from exc
    done = dl_bytes = 0
    st = ctx.stats
    st.setdefault("t0", time.monotonic())
    for k in ("bytes", "comp_orig", "comp_stored"):
        st.setdefault(k, 0)
    st.setdefault("by_drive", {})
    n = len(todo)
    # True shard position/count for the UI: only safetensors are "shards" (aux files — config, tokenizer,
    # index — aren't); sorted so the number matches the zero-padded `-XXXXX-of-YYYYY` filename, and absolute
    # (from the full file list) so it reads e.g. 7/9 on a resumed model rather than restarting at 1/remaining.
    shard_names = sorted(x["rfilename"] for x in files if x["fmt"] == "safetensors")
    n_shards = len(shard_names)
    for i, f in enumerate(todo):
        if ctx.should_stop():                           # clean boundary — no half-written file
            break
        base = {"drive": drive_label, "repo": repo_id, "file": f["rfilename"],
                "file_index": i + 1, "n_files": n, "n_shards": n_shards,
                "shard_no": (shard_names.index(f["rfilename"]) + 1) if f["fmt"] == "safetensors" else None}
        if before_file is not None:
            item = next(entry for entry in task_manifest if entry.rfilename == f["rfilename"])
            if before_file(item) is False:               # another durable writer satisfied stale work
                continue
        ctx.on_progress({**base, "file_phase": "download"})
        local = _download_shard(ctx, repo_id, f["rfilename"], stage_dir, base)
        # Every archived original needs durable restore evidence. Hugging Face does not publish a
        # sha256 for ordinary Git-tracked files such as .gitattributes, so checking only when the
        # catalog supplied one stranded those files at restore time (INC-017). Always hash the
        # downloaded original; when HF did supply a canonical digest, require it to agree.
        ctx.on_progress({**base, "file_phase": "verify"})
        got = compress.sha256_file(local)
        if f["sha256"]:
            canonical = f["sha256"].lower()
            if got != canonical:
                raise RuntimeError(f"{repo_id}/{f['rfilename']}: sha256 mismatch (download corrupt)")
        if f["mode"] == "compress" and compress.should_compress(f["rfilename"]):
            # DEC-022 gate: pick the codec from the shard size + config; log the decision + canary loudly.
            codec = compress.plan_codec(f["size"] or 0, compress_cfg)
            gb = (f["size"] or 0) / 1e9
            if codec == compress.CODEC_RAW:             # over budget, streaming off, no zstd → keep uncompressed
                stored, znn_sha, compressed = local, None, False
                print(f"    [raw] {f['rfilename']} ({gb:.2f} GB) — over {compress_cfg['max_compress_ram_gb']}GB "
                      f"compress budget, streaming off: stored uncompressed")
                ctx.on_progress({**base, "file_phase": "stored", "codec": codec})
            else:
                ctx.on_progress({**base, "file_phase": "compress", "codec": codec})
                dtype = compress.zipnn_dtype(f["quant"])
                res = _compress_isolated(local, dtype, codec, compress_cfg["threads"], got, ctx.should_stop)
                if res["status"] in ("crash", "stalled", "over-cap"):
                    # INC-005: the compressor died natively (ZipNN double-free) or hung on this shard. The
                    # child absorbed it — store the shard RAW so the fill routes around it instead of
                    # core-dumping the portal or looping. (A stop mid-compress raises _StopRequested.)
                    stored, znn_sha, compressed = local, None, False
                    if res["status"] == "crash":
                        why = f"CRASHED (signal {res['signal']})"
                    elif res["status"] == "stalled":
                        why = f"HUNG ({_COMPRESS_STALL_SECS}s no progress)"
                    else:
                        why = f"OUTPUT CAP ({res.get('detail', 'compressed data expanded')})"
                    print(f"    [raw-fallback] {f['rfilename']} ({gb:.2f} GB) — compressor {why}, "
                          f"stored uncompressed :: {res['stderr']}")
                    ctx.on_progress({**base, "file_phase": "compress-crashed", "codec": "raw-fallback"})
                    ctx.write(lambda c: _event(c, repo_id, "compress-fallback",    # DEF-021: a disruption boundary
                              detail=f"{f['rfilename']}: compressor {why} → stored raw"))
                elif res["status"] == "canary":
                    raise RuntimeError(f"{repo_id}/{f['rfilename']}: canary FAILED — keeping original, not dropping")
                elif res["status"] == "error":
                    raise RuntimeError(f"{repo_id}/{f['rfilename']}: compress subprocess failed "
                                       f"(rc={res['returncode']}) :: {res['stderr']}")
                else:                                       # ok — the child's canary certified the round-trip
                    znn = Path(res["znn_path"])
                    local.unlink()                          # safe: canary proved the round-trip
                    stored, znn_sha, compressed = znn, res["znn_sha256"], True
                    st["comp_orig"] += f["size"] or 0
                    st["comp_stored"] += stored.stat().st_size
                    ratio = 100 * stored.stat().st_size / (f["size"] or stored.stat().st_size or 1)
                    print(f"    [{codec}] {f['rfilename']}  {gb:.2f}→{stored.stat().st_size/1e9:.2f} GB "
                          f"({ratio:.0f}%)  canary OK")
                    ctx.on_progress({**base, "file_phase": "canary-ok", "codec": codec, "ratio_pct": round(ratio, 1)})
        else:
            stored, znn_sha, compressed = local, None, False
        stored_digest = znn_sha if compressed else got
        final_relpath = f["rfilename"] + (compress.ZNN_SUFFIX if compressed else "")
        ctx.on_progress({**base, "file_phase": "publish"})
        stored = _publish_staged(
            dest, stored, model_dir / final_relpath, stored_digest,
            f["rfilename"], annex,
        )
        if annex:
            ctx.on_progress({**base, "file_phase": "annex"})
            key = _annex_add(dest, stored)
            _annex_metadata(dest, key, repo_id, params, f["fmt"], f["quant"])   # #14: self-describing key
        else:
            key = None
        stored_sz = stored.stat().st_size
        stored_relpath = _stored_relative_path(stored, model_dir)
        ctx.write(lambda c: db.upsert(c, "archived", {
            "repo_id": repo_id, "rfilename": f["rfilename"], "stored_name": stored.name,
            "stored_relpath": stored_relpath,
            "drive_label": drive_label, "orig_sha256": got, "znn_sha256": znn_sha,
            "orig_bytes": f["size"], "stored_bytes": stored_sz,
            "compressed": compressed, "annex_key": key,
        }, pk=["repo_id", "rfilename", "drive_label"], touch=["verified_at"]))
        done += 1
        dl_bytes += f["size"] or 0
        st["bytes"] += f["size"] or 0
        if drive_label not in st["by_drive"]:
            st["by_drive"][drive_label] = 0
        st["by_drive"][drive_label] += stored_sz
        elapsed = max(1e-6, time.monotonic() - st["t0"])
        ctx.on_progress({**base, "file_phase": "stored",
                         "session_bytes": st["bytes"], "rate_bps": st["bytes"] / elapsed,
                         "ratio": (st["comp_stored"] / st["comp_orig"]) if st["comp_orig"] else None,
                         "done_by_drive": dict(st["by_drive"])})
        # Reclaim current staging leftovers plus the legacy final-directory cache used before
        # DEC-046. The age guard keeps this safe even if an unexpected second writer exists.
        freed = _sweep_incomplete(stage_dir) + _sweep_incomplete(model_dir)
        if freed:
            print(f"    [swept] {freed/1e9:.1f} GB orphaned .incomplete reclaimed")
            ctx.on_progress({**base, "file_phase": "swept", "reclaimed": freed})
    # A task may be only the missing suffix of a copy.  Mark the model archived only when the target
    # now contains the complete canonical manifest; task completion itself is never inferred from
    # this presentation status.
    with ctx.lock:
        canonical = archive_manifest.manifest_for_repo(con, repo_id)
        present = {row[0] for row in con.execute(
            "SELECT rfilename FROM archived WHERE repo_id=? AND drive_label=?",
            [repo_id, drive_label],
        ).fetchall()}
        if {item.rfilename for item in canonical} <= present:
            con.execute("UPDATE models SET status='archived' WHERE repo_id=?", [repo_id])
    if done == len(todo) and stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)
    return {"repo_id": repo_id, "files": done, "skipped": len(files) - len(todo), "bytes": dl_bytes}


def _event(con, repo_id, outcome, bytes=None, wait_seconds=None, detail=None) -> None:
    con.execute("INSERT INTO fetch_events (repo_id, outcome, bytes, wait_seconds, detail) "
                "VALUES (?,?,?,?,?)", [repo_id, outcome, bytes, wait_seconds, detail])


def _retry_after(e) -> float | None:
    resp = getattr(e, "response", None)
    ra = resp.headers.get("Retry-After") if resp is not None else None
    try:
        return float(ra) if ra else None            # seconds form; HTTP-date form ignored in v1
    except (TypeError, ValueError):
        return None


def _bytes_last_24h(con) -> int:
    return con.execute("SELECT coalesce(sum(orig_bytes), 0) FROM archived "
                       "WHERE verified_at > datetime('now', '-1 day')").fetchone()[0]


def run(dest=None, drive_label=None, limit=None, repos=None, dry_run=False, max_24h_gb=1000,
        ctx: RunCtx | None = None, fits: Callable[[str], bool] | None = None,
        task_manifests: Mapping[str, Sequence[archive_manifest.ManifestFile]] | None = None,
        before_file: Callable[[str, archive_manifest.ManifestFile], None] | None = None,
        on_gated: Callable[[str], str] | None = None) -> dict:
    """`fits(repo_id) -> bool` (optional, #37): a per-model boundary check the caller (fill.execute)
    supplies — 'does this repo still fit the target drive's LIVE free under the plan's capacity
    mode?'. On a non-fit, break the batch (emit `plan-capacity`) so fill re-plans instead of
    ENOSPC-ing mid-shard. None (the CLI/plain-fetch path) → no capacity gating, as before."""
    result = {"stored_repos": [], "failed_repos": [], "capacity_failure": None,
              "terminal_failure": None, "terminal_repo": None,
              "throttled": False, "stopped": False, "drive_unwritable": False,
              "gated_repos": [], "gated_retry": None}
    own = ctx is None                               # CLI/plain-fetch path owns its connection
    con = db.connect() if own else ctx.con
    if own:
        ctx = RunCtx(con=con, check_hf_auth=True)
    try:
        # Resolve the on-drive archive dir (DEC-006): an explicit --dest wins; otherwise a
        # registered --drive label resolves to <mount>/modelark via the drives table.
        if dest:
            dest = Path(dest).resolve()
        elif drive_label:
            with ctx.lock:
                dest = register.archive_path(con, drive_label)

        with ctx.lock:
            ids = repos or finalized(con)
        if not ids:
            print("Nothing to fetch — finalize a set in the portal (Finish) or pass --repo.")
            return result
        if limit:
            ids = ids[:limit]

        if dry_run:
            grand = 0
            print(f"DRY RUN — {len(ids)} model(s) → {dest or f'<drive {drive_label}>'} (drive {drive_label}):")
            for rid in ids:
                try:
                    with ctx.lock:
                        files = plan(con, rid)
                except ArchivePolicyError as exc:
                    print(f"  {rid:48} BLOCKED · {exc}")
                    continue
                comp = sum(f["size"] or 0 for f in files if f["mode"] == "compress")
                raw = sum(f["size"] or 0 for f in files if f["mode"] == "raw")
                grand += comp + raw
                print(f"  {rid:48} {len(files):>3} files · {(comp+raw)/1e9:7.1f} GB raw "
                      f"(~{comp*0.67/1e9:.1f} GB compressed + {raw/1e9:.1f} GB)")
            print(f"\nTotal: ~{grand/1e12:.2f} TB raw → ~{grand*0.7/1e12:.2f} TB on disk (bf16 ZipNN est).")
            return result

        if dest is None:
            print(f"drive '{drive_label}' is not registered or not mounted — run "
                  f"`modelark drive register --dev /dev/sdX --label {drive_label or 'drive-01'}`, "
                  f"or pass --dest PATH.")
            return result
        if not drive_label:
            print("give --drive LABEL so the archive is recorded against a fleet drive.")
            return result

        if ctx.check_hf_auth:
            auth_failure = hf_auth_preflight(ctx)
            if auth_failure is not None:
                result["terminal_failure"] = auth_failure
                print(f"  [auth    ] {auth_failure['message']}")
                ctx.on_progress({
                    "phase": "auth-invalid", "code": auth_failure["code"],
                    "evidence": auth_failure["evidence"], "actions": auth_failure["actions"],
                    "say": f"🔴 {auth_failure['message']}",
                })
                return result

        annex = _is_annex(dest)
        if not annex:
            print(f"WARNING: {dest} is not a git-annex repo — storing verified files raw, "
                  f"not annex-tracked. (Run drive registration to enable annex.)")
        cap = (max_24h_gb or 0) * 1e9
        compress_cfg = wishlist.compression()       # DEC-022 codec gate config (loaded once per run)
        for k, rid in enumerate(ids):
            if ctx.should_stop():
                break
            with ctx.lock:
                used = _bytes_last_24h(con) if cap else 0
            if cap and used >= cap:
                print(f"  throttle: {used/1e12:.2f} TB downloaded in last 24h ≥ {cap/1e12:.2f} TB cap "
                      f"— stopping at repo boundary (resumable, re-run to continue).")
                ctx.write(lambda c: _event(c, None, "throttled", detail=f"{used/1e9:.0f} GB in 24h"))
                ctx.on_progress({"phase": "throttled", "say":
                                 f"  throttle: {used/1e12:.2f} TB in 24h ≥ cap — stopping (resumable)."})
                result["throttled"] = True
                break
            if fits is not None and not fits(rid):
                # #37 per-model failsafe: this drive's LIVE free can no longer hold `rid` in the plan's
                # capacity mode (actual > estimate, or a compression-aware forecast coming up short).
                # Break the batch so fill.execute re-plans — it re-homes rid onto another plan drive, or
                # (nothing fits anywhere) stops cleanly as plan-capacity-stop. Prevents an ENOSPC mid-shard.
                print(f"  [plan-capacity] {drive_label} full for {rid} — breaking batch to re-plan.")
                ctx.on_progress({"phase": "plan-capacity", "drive": drive_label, "repo": rid,
                                 "say": f"  {drive_label} full for {rid} — re-planning (add a drive if nothing else fits)."})
                break
            ctx.on_progress({"drive": drive_label, "repo": rid, "repo_index": k + 1, "n_repos": len(ids),
                             "used_24h": used, "cap_24h": cap})
            try:
                task_args = {}
                if task_manifests is not None:
                    task_args["manifest"] = task_manifests.get(rid)
                if before_file is not None:
                    task_args["before_file"] = lambda item, _rid=rid: before_file(_rid, item)
                r = fetch_model(ctx, rid, dest, drive_label, annex, compress_cfg, **task_args)
                tag = f"{r['files']} files" + (f" (+{r['skipped']} already had)" if r["skipped"] else "")
                print(f"  [archived] {rid}  ({tag})")
                ctx.write(lambda c: _event(c, rid, "archived", bytes=r["bytes"], detail=tag))
                result["stored_repos"].append(rid)
            except _StopRequested:
                result["stopped"] = True
                break                                    # clean stop requested mid-shard (INC-004)
            except CapacityPreflightError as exc:
                result["capacity_failure"] = exc.failure
                ctx.on_progress({"phase": "plan-capacity", "drive": drive_label, "repo": rid,
                                 "code": exc.failure.code.value,
                                 "say": f"  {drive_label} lacks safe workspace for {rid} — re-planning."})
                break
            except FetchTerminalError as exc:
                result["terminal_failure"] = exc.as_dict()
                result["terminal_repo"] = rid
                print(f"  [{exc.code.lower():8}] {rid}: {exc}")
                event_detail = f"{exc.code}: {str(exc)[:150]}"
                ctx.write(lambda c: _event(c, rid, "terminal", detail=event_detail))
                ctx.on_progress({
                    "phase": "fetch-blocked", "repo": rid, "code": exc.code,
                    "evidence": exc.evidence, "actions": list(exc.actions),
                    "say": f"🔴 {exc}",
                })
                break
            except ArchivePolicyError as e:
                print(f"  [policy  ] {rid}: {e}")
                detail = str(e)[:200]
                ctx.write(lambda c: _event(c, rid, "policy", detail=detail))
                result["failed_repos"].append(rid)
            except GatedRepoError:
                print(f"  [gated   ] {rid}  — needs accepted Hugging Face repository access")
                ctx.write(lambda c: _event(c, rid, "auth", detail="gated / needs accepted access"))
                if on_gated is None:                    # plain fetch/CLI compatibility: no prompt broker
                    result["failed_repos"].append(rid)
                    continue
                action = on_gated(rid)
                if action == "retry":
                    result["gated_retry"] = rid
                    break                               # reconcile and retry this repo immediately
                if action in {"skip", "timeout"}:
                    detail = json.dumps({
                        "type": "access-gated", "resolution": action,
                        "url": f"https://huggingface.co/{rid}",
                    }, sort_keys=True)
                    ctx.write(lambda c: _event(c, rid, "auth", detail=detail))
                    result["gated_repos"].append({"repo": rid, "resolution": action})
            except HfHubHTTPError as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code == 401:
                    failure = _hf_auth_invalid_failure()
                    result["terminal_failure"] = failure
                    result["terminal_repo"] = rid
                    ctx.write(lambda c: _event(c, rid, "auth", detail="configured HF credential rejected"))
                    ctx.on_progress({
                        "phase": "auth-invalid", "repo": rid, "code": failure["code"],
                        "evidence": failure["evidence"], "actions": failure["actions"],
                        "say": f"🔴 {failure['message']}",
                    })
                    break
                if code == 429:
                    ra = _retry_after(e)
                    print(f"  [429     ] {rid} — HF rate limit"
                          + (f", Retry-After={ra:.0f}s" if ra else "") + "; stopping (resumable).")
                    ctx.write(lambda c: _event(c, rid, "rate_limited", wait_seconds=ra, detail="429; stopped run"))
                    ctx.on_progress({"phase": "rate_limited", "say": f"  [429] {rid} — HF rate limit; stopping."})
                    result["throttled"] = True
                    break
                print(f"  [error   ] {rid}: {str(e)[:100]}")
                detail = str(e)[:200]
                ctx.write(lambda c: _event(c, rid, "error", detail=detail))
                result["failed_repos"].append(rid)
            except RepositoryNotFoundError as e:
                print(f"  [error   ] {rid}: {str(e)[:100]}")
                ctx.write(lambda c: _event(c, rid, "error", detail="repo not found"))
                result["failed_repos"].append(rid)
            except Exception as e:                       # INC-004: isolate ANY other repo failure (stalled
                print(f"  [error   ] {rid}: {type(e).__name__}: {str(e)[:100]}")   # download exhausted retries,
                detail = f"{type(e).__name__}: {str(e)[:180]}"
                ctx.write(lambda c: _event(c, rid, "error", detail=detail))
                result["failed_repos"].append(rid)
                if not _dest_writable(dest):             # the DRIVE went unwritable mid-batch (USB drop), not just this
                    ctx.write(lambda c: _event(c, rid, "awaiting-drive",           # DEF-021: a disruption boundary
                              detail=f"{drive_label} went unwritable mid-fill"))
                    ctx.on_progress({"phase": "awaiting-drive", "awaiting_drive": drive_label,   # repo → bail; the guided
                                     "say": f"⚠ {drive_label} stopped accepting writes mid-fill — re-seat it."})
                    result["drive_unwritable"] = True
                    break                                # re-plan loop re-awaits + write-probes it (no silent churn)
                if ctx.should_stop():                    # canary fail, etc.) — log + move on, don't wedge the fill
                    break
        # DEC-006: propagate to the central map — sync the drive (commit + push the
        # location log) then sync the map (merge the file tree into its index) so the map
        # is both the authoritative "where" (location log) and a browsable "what".
        if annex:
            s = subprocess.run(["git", "-C", str(dest), "annex", "sync"], capture_output=True, text=True)
            m = subprocess.run(["git", "-C", str(register.library_root()), "annex", "sync"],
                               capture_output=True, text=True)
            if s.returncode == 0 and m.returncode == 0:
                print("  synced drive + map (location log + index)")
            else:
                print(f"  sync warning: {((s.stderr or s.stdout) + ' ' + (m.stderr or m.stdout)).strip()[:160]}")
        return result
    finally:
        if own:
            con.close()


def _remote_name_for_uuid(lib, uuid: str) -> str | None:
    """Map a git-annex uuid to its remote name in the map repo (for special remotes)."""
    out = subprocess.run(["git", "-C", str(lib), "config", "--get-regexp", r"remote\..*\.annex-uuid"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == uuid:
            return parts[0].split(".", 2)[1]        # remote.<name>.annex-uuid
    return None


def _annex_key_on_uuid(repo: Path, key: str, target_uuid: str) -> bool:
    """Require git-annex's per-key location evidence for the concrete target UUID."""
    result = subprocess.run(
        ["git", "-C", str(repo), "annex", "whereis", f"--key={key}", "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return False
    return any(
        location.get("uuid") == target_uuid
        for record in records
        for location in (record.get("whereis") or [])
    )


def run_replica_tasks(tasks: Sequence[Any], ctx: RunCtx | None = None) -> dict:
    """Execute exact replica tasks and record only per-key target-UUID proofs.

    The task graph may choose a different source for each repository.  A stale target row is checked
    before every key, copy publication is verified through ``annex whereis --key`` against the
    registered target UUID, and only that verified file is then mirrored into ``archived``.
    """
    own = ctx is None
    con = db.connect() if own else ctx.con
    if own:
        ctx = RunCtx(con=con)
    result = {
        "deferred": False,
        "source_offline": False,
        "deferred_targets": [],
        "copied_targets": [],
        "copied_files": 0,
        "failed": [],
    }
    try:
        grouped: dict[tuple[str | None, str], list[Any]] = {}
        for task in tasks:
            grouped.setdefault((task.source_drive, task.target_drive), []).append(task)
        lib = register.library_root()
        copied_any = False
        for (source, target), group in sorted(grouped.items(), key=lambda item: (item[0][1], item[0][0] or "")):
            if source is None:
                result["failed"].append({
                    "code": "SOURCE_INCOMPLETE",
                    "target": target,
                    "requirements": [task.requirement_id for task in group],
                })
                continue
            with ctx.lock:
                source_path = register.archive_path(con, source)
                target_path = register.archive_path(con, target)
                target_uuid = (con.execute(
                    "SELECT annex_uuid FROM drives WHERE drive_label=?", [target]
                ).fetchone() or [None])[0]
            if source_path is None or not _dest_writable(Path(source_path)):
                result.update(deferred=True, source_offline=True)
                result["deferred_targets"].append(target)
                ctx.on_progress({
                    "phase": "awaiting-drive", "awaiting_drive": source,
                    "say": f"⏳ replica source {source} is offline/read-only — copy #2 deferred; re-seat it.",
                })
                continue
            if target_path is None or not _dest_writable(Path(target_path)):
                result["deferred"] = True
                result["deferred_targets"].append(target)
                ctx.on_progress({
                    "phase": "awaiting-drive", "awaiting_drive": target,
                    "say": f"⏳ replica target {target} is offline/unwritable — copy #2 deferred; re-seat it.",
                })
                continue
            if not target_uuid:
                result["failed"].append({
                    "code": "TARGET_ANNEX_UUID_MISSING", "target": target,
                    "requirements": [task.requirement_id for task in group],
                })
                continue
            source_repo, target_repo = Path(source_path), Path(target_path)
            remote = subprocess.run(
                ["git", "-C", str(source_repo), "remote", "set-url", target, str(target_repo)],
                capture_output=True,
                text=True,
            )
            if remote.returncode != 0:
                subprocess.run(
                    ["git", "-C", str(source_repo), "remote", "add", target, str(target_repo)],
                    capture_output=True,
                    text=True,
                )
            ctx.on_progress({
                "phase": "replica", "drive": target, "n_repos": len(group),
                "say": f"-- replica {target} ← {source} ({len(group)} task(s)) --",
            })
            group_deferred = False
            for task in sorted(group, key=lambda item: item.requirement_id):
                for rfilename in task.budget.missing_files:
                    with ctx.lock:
                        if con.execute(
                            "SELECT 1 FROM archived WHERE repo_id=? AND rfilename=? AND drive_label=?",
                            [task.repo_id, rfilename, target],
                        ).fetchone():
                            continue
                        source_row = con.execute(
                            "SELECT stored_name,stored_relpath,orig_sha256,znn_sha256,orig_bytes,"
                            "stored_bytes,compressed,annex_key FROM archived "
                            "WHERE repo_id=? AND rfilename=? AND drive_label=?",
                            [task.repo_id, rfilename, source],
                        ).fetchone()
                    if source_row is None or not source_row[7]:
                        result["failed"].append({
                            "code": "SOURCE_KEY_MISSING", "requirement_id": task.requirement_id,
                            "repo": task.repo_id, "file": rfilename, "source": source,
                        })
                        continue
                    key = source_row[7]
                    copied = subprocess.run(
                        ["git", "-C", str(source_repo), "annex", "copy", "--to", target, f"--key={key}"],
                        capture_output=True,
                        text=True,
                    )
                    if copied.returncode != 0:
                        if not _dest_writable(target_repo):
                            result["deferred"] = True
                            result["deferred_targets"].append(target)
                            group_deferred = True
                            break
                        result["failed"].append({
                            "code": "ANNEX_COPY_FAILED", "requirement_id": task.requirement_id,
                            "repo": task.repo_id, "file": rfilename, "target": target,
                        })
                        continue
                    if not _annex_key_on_uuid(source_repo, key, target_uuid):
                        result["failed"].append({
                            "code": "TARGET_KEY_UNVERIFIED", "requirement_id": task.requirement_id,
                            "repo": task.repo_id, "file": rfilename, "target": target,
                        })
                        continue
                    stored_name, stored_relpath, orig_sha, znn_sha, orig_bytes, stored_bytes, compressed, _ = source_row
                    ctx.write(lambda c, _task=task, _file=rfilename, _target=target, _values=(
                        stored_name, stored_relpath, orig_sha, znn_sha, orig_bytes, stored_bytes,
                        compressed, key,
                    ): db.upsert(c, "archived", {
                        "repo_id": _task.repo_id, "rfilename": _file,
                        "stored_name": _values[0], "stored_relpath": _values[1],
                        "drive_label": _target, "orig_sha256": _values[2],
                        "znn_sha256": _values[3], "orig_bytes": _values[4],
                        "stored_bytes": _values[5], "compressed": _values[6],
                        "annex_key": _values[7],
                    }, pk=["repo_id", "rfilename", "drive_label"], touch=["verified_at"]))
                    result["copied_files"] += 1
                    copied_any = True
                if group_deferred:
                    break
            if group_deferred:
                ctx.on_progress({
                    "phase": "awaiting-drive", "awaiting_drive": target,
                    "say": f"⏳ replica target {target} went unwritable mid-copy — deferred; re-seat it.",
                })
            elif target not in result["deferred_targets"]:
                result["copied_targets"].append(target)
        if copied_any:
            subprocess.run(["git", "-C", str(lib), "annex", "sync"], capture_output=True, text=True)
        result["deferred_targets"] = sorted(set(result["deferred_targets"]))
        result["copied_targets"] = sorted(set(result["copied_targets"]))
        return result
    finally:
        if own:
            con.close()


def run_replica(replica_assign: dict, source: str | None, ctx: RunCtx | None = None) -> dict:
    """Realize must-have COPY #2+ as LOCAL copies (DEC-017): transfer the already-fetched copy#1
    (on `source` — the RAID/primary home) to each replica drive, no HF re-download. git-annex 8.x
    has no one-shot `copy --from A --to B` (and clones only know `origin`), so we teach the SOURCE
    clone about the target remote and run a plain `copy --to` — a direct clone→clone transfer, no
    map staging. Records the landed copy in `archived` (mirroring source's rows) only on success.

    DEF-022 fail-soft: PROBE the source (copy#2 reads from it) and each target BEFORE copying. An
    offline / read-only source or target is DEFERRED — emit an awaiting-drive prompt and bail, never
    churn a failed `annex copy` per repo (INC-009: a dead RAID source failed every copy → GATE-C red).
    Returns {deferred, source_offline, deferred_targets, copied_targets} so GATE-C can PAUSE (resumable)
    instead of hard-erroring a run whose copy #1 is all safe."""
    own = ctx is None
    con = db.connect() if own else ctx.con
    if own:
        ctx = RunCtx(con=con)
    result = {"deferred": False, "source_offline": False, "deferred_targets": [], "copied_targets": []}
    try:
        targets = [(label, [i["repo"] for i in items]) for label, items in replica_assign.items() if items]
        if not targets:
            return result
        if source is None:
            print("  [replica] no copy#1 source placed (no RAID/primary home) — skipping replica copies.")
            return result
        lib = register.library_root()
        with ctx.lock:
            src_archive = register.archive_path(con, source)
        # DEF-022: the source must be mounted AND healthy — INC-009's RAID went read-only + EIO'd, so a
        # mere "mounted" check isn't enough. A dead source can serve NO copy#2 → defer the whole tier.
        if src_archive is None or not _dest_writable(Path(src_archive)):
            result.update(deferred=True, source_offline=True, deferred_targets=[l for l, _ in targets])
            print(f"  [replica] source {source} offline/read-only — deferring copy #2 (resumable).")
            ctx.on_progress({"phase": "awaiting-drive", "awaiting_drive": source,
                             "say": f"⏳ replica source {source} is offline/read-only — copy #2 deferred; re-seat it."})
            return result
        for label, repos in targets:
            with ctx.lock:
                tgt_archive = register.archive_path(con, label)
            if tgt_archive is None or not _dest_writable(Path(tgt_archive)):
                result.update(deferred=True)
                result["deferred_targets"].append(label)
                print(f"  [replica] target {label} offline/unwritable — deferring (resumable).")
                ctx.on_progress({"phase": "awaiting-drive", "awaiting_drive": label,
                                 "say": f"⏳ replica target {label} offline/unwritable — copy #2 deferred; re-seat it."})
                continue
            print(f"\n-- replica {label} ← local copy from {source} ({len(repos)} must-have(s)) --")
            ctx.on_progress({"phase": "replica", "drive": label, "n_repos": len(repos),
                             "say": f"-- replica {label} ← local copy from {source} ({len(repos)} must-have(s)) --"})
            # teach the source clone where the target lives (idempotent), then a plain copy --to
            if subprocess.run(["git", "-C", str(src_archive), "remote", "set-url", label, str(tgt_archive)],
                              capture_output=True, text=True).returncode != 0:
                subprocess.run(["git", "-C", str(src_archive), "remote", "add", label, str(tgt_archive)],
                               capture_output=True, text=True)
            r = subprocess.run(["git", "-C", str(src_archive), "annex", "copy", "--to", label, *repos],
                               capture_output=True, text=True)
            if r.returncode != 0:
                # A copy failure with a HEALTHY source/target is a real per-repo failure (record nothing).
                # But if the TARGET just went unwritable mid-copy, treat it as deferred (re-seat), not churn.
                if not _dest_writable(Path(tgt_archive)):
                    result.update(deferred=True)
                    result["deferred_targets"].append(label)
                    ctx.on_progress({"phase": "awaiting-drive", "awaiting_drive": label,
                                     "say": f"⏳ replica target {label} went unwritable mid-copy — deferred; re-seat it."})
                    continue
                print(f"    ✗ copy failed — not recording. {(r.stderr or r.stdout).strip()[:180]}")
                ctx.on_progress({"phase": "replica", "drive": label,
                                 "say": f"    ✗ replica {label} copy failed — not recording."})
                continue
            print("    ok")
            subprocess.run(["git", "-C", str(lib), "annex", "sync"], capture_output=True, text=True)
            ctx.write(lambda c: c.execute(   # mirror source's archived rows onto this replica label
                "INSERT INTO archived (repo_id, rfilename, stored_name, stored_relpath, drive_label, orig_sha256, "
                "znn_sha256, orig_bytes, stored_bytes, compressed, annex_key, verified_at) "
                "SELECT repo_id, rfilename, stored_name, stored_relpath, ?, orig_sha256, znn_sha256, orig_bytes, "
                "stored_bytes, compressed, annex_key, CURRENT_TIMESTAMP FROM archived WHERE drive_label=? AND "
                f"repo_id IN ({','.join(['?']*len(repos))}) "
                "ON CONFLICT (repo_id, rfilename, drive_label) DO NOTHING",
                [label, source, *repos]))
            result["copied_targets"].append(label)
            ctx.on_progress({"phase": "replica", "drive": label, "say": f"    ✓ replica {label} ok"})
        return result
    finally:
        if own:
            con.close()
