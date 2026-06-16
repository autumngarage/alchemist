"""Tests for per-repo file locking."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from alchemist.locks import LockBusyError, acquire, is_locked

if TYPE_CHECKING:
    from pathlib import Path


def test_acquire_creates_and_removes_lockfile(tmp_path: Path):
    repo = "autumngarage/widgets"
    with acquire(tmp_path, repo) as lock_path:
        assert lock_path.exists()
        payload = json.loads(lock_path.read_text())
        assert payload["repo"] == repo
        assert "pid" in payload
        assert "started_at" in payload
        assert payload["lock_mode"] == "flock"
    assert not lock_path.exists()


def test_acquire_busy_raises(tmp_path: Path):
    repo = "autumngarage/widgets"
    with acquire(tmp_path, repo), pytest.raises(LockBusyError), acquire(tmp_path, repo):
        pass  # pragma: no cover — should never enter


def test_two_different_repos_can_lock_simultaneously(tmp_path: Path):
    """The whole point of per-repo locking — different repos don't block each other."""
    with (
        acquire(tmp_path, "autumngarage/widgets"),
        acquire(tmp_path, "autumngarage/cortex") as inner,
    ):
        assert inner.exists()


def test_lockfile_cleaned_up_after_exception(tmp_path: Path):
    repo = "autumngarage/widgets"
    expected_path = tmp_path / "locks" / f"{repo.replace('/', '-')}.lock"
    with pytest.raises(RuntimeError, match="boom"), acquire(tmp_path, repo):
        raise RuntimeError("boom")
    assert not expected_path.exists()


def test_acquire_holder_note_is_persisted(tmp_path: Path):
    with acquire(tmp_path, "autumngarage/sentinel", holder_note="2 issues; first=#7") as lock_path:
        payload = json.loads(lock_path.read_text())
        assert payload["note"] == "2 issues; first=#7"


def test_repo_slug_replaces_slash(tmp_path: Path):
    """Lockfile name uses dashes so it's safe across filesystems."""
    with acquire(tmp_path, "henrymodisett/private-repo") as lock_path:
        assert lock_path.name == "henrymodisett-private-repo.lock"


def test_fresh_existing_lock_is_locked(tmp_path: Path):
    repo = "autumngarage/widgets"
    with (
        acquire(tmp_path, repo),
        pytest.raises(LockBusyError),
    ):
        assert is_locked(tmp_path, repo, stale_after_sec=60) is True
        with acquire(tmp_path, repo, stale_after_sec=60):
            pass


def test_orphaned_lockfile_is_reclaimed_on_acquire(tmp_path: Path):
    repo = "autumngarage/widgets"
    lock_path = tmp_path / "locks" / "autumngarage-widgets.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({"repo": repo, "started_at": "old", "pid": 999999, "lock_mode": "flock"})
    )

    with acquire(tmp_path, repo, holder_note="new holder", stale_after_sec=60) as new_lock:
        payload = json.loads(new_lock.read_text())
        assert payload["note"] == "new holder"
        assert payload["pid"] == os.getpid()


def test_fresh_legacy_lockfile_is_busy_during_migration(tmp_path: Path):
    repo = "autumngarage/widgets"
    lock_path = tmp_path / "locks" / "autumngarage-widgets.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({
            "repo": repo,
            "started_at": datetime.now(UTC).isoformat(),
            "pid": 999999,
        })
    )

    assert is_locked(tmp_path, repo, stale_after_sec=60) is True
    with pytest.raises(LockBusyError), acquire(tmp_path, repo, stale_after_sec=60):
        pass


def test_stale_legacy_lockfile_is_reclaimed_on_acquire(tmp_path: Path):
    repo = "autumngarage/widgets"
    lock_path = tmp_path / "locks" / "autumngarage-widgets.lock"
    lock_path.parent.mkdir(parents=True)
    old_started = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    lock_path.write_text(json.dumps({"repo": repo, "started_at": old_started, "pid": 999999}))

    assert is_locked(tmp_path, repo, stale_after_sec=60) is False
    with acquire(tmp_path, repo, holder_note="new holder", stale_after_sec=60) as new_lock:
        payload = json.loads(new_lock.read_text())
        assert payload["note"] == "new holder"
        assert payload["lock_mode"] == "flock"


def test_acquire_retries_when_legacy_lock_disappears_mid_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    real_open = os.open
    calls = 0

    def flaky_open(path, flags, mode=0o777):
        nonlocal calls
        if str(path).endswith("autumngarage-widgets.lock"):
            calls += 1
            if calls == 1:
                raise FileExistsError(path)
            if calls == 2:
                raise FileNotFoundError(path)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", flaky_open)

    with acquire(tmp_path, "autumngarage/widgets") as lock_path:
        assert lock_path.exists()
    assert calls >= 3


def test_corrupt_stale_legacy_lockfile_is_reclaimed(tmp_path: Path):
    repo = "autumngarage/widgets"
    lock_path = tmp_path / "locks" / "autumngarage-widgets.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("not json")
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(lock_path, (old, old))

    assert is_locked(tmp_path, repo, stale_after_sec=60) is False
    with acquire(tmp_path, repo, stale_after_sec=60) as new_lock:
        assert json.loads(new_lock.read_text())["repo"] == repo
