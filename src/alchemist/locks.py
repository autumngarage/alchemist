"""Per-repo file locking on the persistent state volume.

Two alchemist workers must not operate on the same target repo at the same
time — they would collide on the local clone, branch state, and racing PR
opens. The lock is the lockfile's *existence* — atomic O_CREAT|O_EXCL on
`<state_dir>/locks/<repo-slug>.lock`. Lighter than fcntl/flock and adequate
at our cron cadence (`*/5 * * * *`).

Cross-repo parallelism is the desired swarm shape: many workers in different
repos run in parallel; multiple workers in one repo never do. The runner
groups issues by repo and fans out across groups, never within them.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import socket
from contextlib import contextmanager
from datetime import UTC, datetime
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


@contextmanager
def acquire(state_dir: Path, repo: str, *, holder_note: str = "") -> Iterator[Path]:
    """Acquire the per-repo lock. Yields the lockfile path while held.

    The lockfile holds JSON describing the holder (pid, host, started_at,
    optional note) so an operator inspecting `<state_dir>/locks` can tell
    what's running and why.
    """
    path = _lock_path(state_dir, repo)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise LockBusyError(str(path)) from exc

    try:
        payload = {
            "pid": os.getpid(),
            "host": platform.node() or socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(),
            "repo": repo,
            "note": holder_note,
        }
        os.write(fd, json.dumps(payload).encode())
    finally:
        os.close(fd)

    try:
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
