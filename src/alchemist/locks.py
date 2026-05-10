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
    started_at = payload.get("started_at")
    if isinstance(started_at, str):
        try:
            return now - datetime.fromisoformat(started_at.replace("Z", "+00:00"))
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


def is_locked(state_dir: Path, repo: str, *, stale_after_sec: int | None = None) -> bool:
    """Return True when another process currently holds the repo lock."""
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
            return True
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

    try:
        payload = {
            "pid": os.getpid(),
            "host": platform.node() or socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(),
            "repo": repo,
            "note": holder_note,
            "lock_mode": "flock",
        }
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(payload).encode())
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
