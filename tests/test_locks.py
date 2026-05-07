"""Tests for per-repo file locking."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from alchemist.locks import LockBusyError, acquire

if TYPE_CHECKING:
    from pathlib import Path


def test_acquire_creates_and_removes_lockfile(tmp_path: Path):
    repo = "autumngarage/touchstone"
    with acquire(tmp_path, repo) as lock_path:
        assert lock_path.exists()
        payload = json.loads(lock_path.read_text())
        assert payload["repo"] == repo
        assert "pid" in payload
        assert "started_at" in payload
    assert not lock_path.exists()


def test_acquire_busy_raises(tmp_path: Path):
    repo = "autumngarage/touchstone"
    with acquire(tmp_path, repo), pytest.raises(LockBusyError), acquire(tmp_path, repo):
        pass  # pragma: no cover — should never enter


def test_two_different_repos_can_lock_simultaneously(tmp_path: Path):
    """The whole point of per-repo locking — different repos don't block each other."""
    with (
        acquire(tmp_path, "autumngarage/touchstone"),
        acquire(tmp_path, "autumngarage/cortex") as inner,
    ):
        assert inner.exists()


def test_lockfile_cleaned_up_after_exception(tmp_path: Path):
    repo = "autumngarage/touchstone"
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
