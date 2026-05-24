"""Per-repo file locking on the persistent state volume.

Two alchemist workers must not operate on the same target repo at the same
time — they would collide on the local clone, branch state, and racing PR
opens. The lock is an OS advisory exclusive lock on
`<state_dir>/locks/<repo-slug>.lock`; the file stores holder metadata for
operators, but file existence alone does not imply the lock is active. This
keeps crashed workers reclaimable because the OS releases the advisory lock
when the process exits.

Cross-repo parallelism is the desired swarm shape: many workers in different
repos run in parallel; multiple workers in one repo never do. The runner
groups issues by repo and fans out across groups, never within them.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform
import socket
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class LockBusyError(RuntimeError):
    """Raised when a lockfile already exists for the target repo."""


def _slug(repo: str) -> str:
    return repo.replace("/", "-")


def _lock_path(state_dir: Path, repo: str) -> Path:
    return state_dir / "locks" / f"{_slug(repo)}.lock"


def _lock_payload(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _lock_age(path: Path, now: datetime) -> timedelta | None:
    payload = _lock_payload(path)
    for key in ("last_heartbeat", "started_at"):
        stamp = payload.get(key)
        if isinstance(stamp, str):
            try:
                return now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                pass
    try:
        return now - datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def _legacy_lock_is_fresh(path: Path, stale_after_sec: int | None) -> bool:
    """Protect active pre-flock lockfiles during rollout.

    New-style locks write `lock_mode=flock`; if the process crashes, the OS
    releases the lock and the leftover file can be reused immediately. Legacy
    files have no advisory lock, so treat them as active until their metadata
    ages past the configured stale threshold.
    """
    if _lock_payload(path).get("lock_mode") == "flock":
        return False
    if stale_after_sec is None or stale_after_sec <= 0:
        return True
    age = _lock_age(path, datetime.now(UTC))
    return age is None or age < timedelta(seconds=stale_after_sec)


def stale_flock_lock_age(
    state_dir: Path,
    repo: str,
    *,
    heartbeat_timeout_sec: int,
) -> timedelta | None:
    """Return stale-age for a flock lock, or None when not stale/not flock/unknown."""
    if heartbeat_timeout_sec <= 0:
        return None
    path = _lock_path(state_dir, repo)
    payload = _lock_payload(path)
    if payload.get("lock_mode") != "flock":
        return None
    age = _lock_age(path, datetime.now(UTC))
    if age is None:
        return None
    return age if age >= timedelta(seconds=heartbeat_timeout_sec) else None


def is_locked(
    state_dir: Path,
    repo: str,
    *,
    stale_after_sec: int | None = None,
    heartbeat_timeout_sec: int | None = None,
) -> bool:
    """Return True when another process currently holds the repo lock.

    `heartbeat_timeout_sec` is an optional stale-holder escape hatch for
    flock-mode lockfiles: if the holder keeps the advisory lock but stops
    refreshing `last_heartbeat` beyond the timeout, treat it as reclaimable.
    """
    path = _lock_path(state_dir, repo)
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if heartbeat_timeout_sec is None or heartbeat_timeout_sec <= 0:
                return True
            age = _lock_age(path, datetime.now(UTC))
            return age is None or age < timedelta(seconds=heartbeat_timeout_sec)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return _legacy_lock_is_fresh(path, stale_after_sec)
    finally:
        os.close(fd)


@contextmanager
def acquire(
    state_dir: Path,
    repo: str,
    *,
    holder_note: str = "",
    stale_after_sec: int | None = None,
    heartbeat_interval_sec: int | None = None,
) -> Iterator[Path]:
    """Acquire the per-repo lock. Yields the lockfile path while held.

    The lockfile holds JSON describing the holder (pid, host, started_at,
    optional note) so an operator inspecting `<state_dir>/locks` can tell
    what's running and why.
    """
    path = _lock_path(state_dir, repo)
    path.parent.mkdir(parents=True, exist_ok=True)

    while True:
        created = False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
            created = True
            break
        except FileExistsError:
            try:
                fd = os.open(path, os.O_RDWR)
                break
            except FileNotFoundError:
                continue
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise LockBusyError(str(path)) from exc
    if not created and _legacy_lock_is_fresh(path, stale_after_sec):
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise LockBusyError(str(path))

    write_lock = threading.Lock()

    def _write_payload(payload: dict[str, str | int]) -> None:
        encoded = json.dumps(payload).encode()
        with write_lock:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, encoded)
            os.fsync(fd)

    started_at = datetime.now(UTC).isoformat()
    payload: dict[str, str | int] = {
        "pid": os.getpid(),
        "host": platform.node() or socket.gethostname(),
        "started_at": started_at,
        "last_heartbeat": started_at,
        "repo": repo,
        "note": holder_note,
        "lock_mode": "flock",
    }
    _write_payload(payload)

    stop_heartbeat = threading.Event()
    heartbeat_thread: threading.Thread | None = None

    if heartbeat_interval_sec is not None and heartbeat_interval_sec > 0:
        interval = max(1, heartbeat_interval_sec)

        def _heartbeat_loop() -> None:
            while not stop_heartbeat.wait(interval):
                payload["last_heartbeat"] = datetime.now(UTC).isoformat()
                try:
                    _write_payload(payload)
                except OSError as exc:
                    print(f"alchemist: lock heartbeat write failed for {repo}: {exc}", file=sys.stderr)
                    break

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        heartbeat_thread.start()

    try:
        yield path
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
