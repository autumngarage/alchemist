"""Tests for the runner — the transmute loop top-level."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from alchemist.config import Config
from alchemist.doctor import Check
from alchemist.runner import RunResult, run_tick
from alchemist.scanner import DispatchIssue


def _config(state_dir: Path, *, dry_run: bool = True, max_concurrent: int = 1) -> Config:
    return Config(
        org="autumngarage",
        dispatch_label="alchemist-test",
        default_provider="kimi",
        default_budget="$1",
        poll_interval_minutes=5,
        state_dir=state_dir,
        dry_run=dry_run,
        max_per_repo_per_tick=1,
        max_concurrent_repos=max_concurrent,
        conductor_timeout_sec=60,
        review_timeout_sec=60,
        github_token_env="GITHUB_TOKEN",
    )


def _issue(num: int = 7, repo: str = "autumngarage/touchstone") -> DispatchIssue:
    return DispatchIssue(
        number=num,
        title=f"Fix typo in README #{num}",
        body="Tiny readme typo.",
        url=f"https://github.com/{repo}/issues/{num}",
        repository=repo,
        updated_at=f"2026-05-06T{20 + num % 4:02d}:00:00Z",
        labels=("alchemist-test",),
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch):
    """Pretend GITHUB_TOKEN is set so doctor's auth check passes inside tests."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")


def _stub_all_external(
    monkeypatch: pytest.MonkeyPatch,
    *,
    issues: list[DispatchIssue] | None = None,
    conductor_outcome: str = "ok",  # "ok" | "no-diff" | "timeout" | "error"
    review_verdict: str = "CLEAN",
):
    """Patch every subprocess.* + scanner + doctor used by the runner.

    Returns a captured-call tracker (dict) the test can inspect to verify
    push / PR / label transitions did or did not happen.
    """
    if issues is None:
        issues = [_issue()]

    captured: dict[str, list[Any]] = {
        "subprocess_run_args": [],
        "label_transitions": [],
        "push_calls": [],
        "pr_creates": [],
    }

    monkeypatch.setattr("alchemist.runner.scan", lambda **_: list(issues))
    monkeypatch.setattr(
        "alchemist.runner.run_doctor",
        lambda config: [Check(name=n, ok=True, detail="fake") for n in ("gh", "git", "conductor")],
    )

    def fake_run(cmd, *args, **kwargs):
        captured["subprocess_run_args"].append(cmd)

        if "issue" in cmd and "edit" in cmd:
            captured["label_transitions"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "repo" in cmd and "view" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")

        if "issue" in cmd and "comment" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "pr" in cmd and "create" in cmd:
            captured["pr_creates"].append(cmd)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="https://github.com/fake/pr/1\n",
                stderr="",
            )

        if "git" in cmd and "push" in cmd:
            captured["push_calls"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "git" in cmd and "status" in cmd and "--porcelain" in cmd:
            stdout = "" if conductor_outcome == "no-diff" else " M something.txt\n"
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        if "conductor" in cmd and "exec" in cmd:
            if conductor_outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, timeout=10)
            if conductor_outcome == "error":
                return subprocess.CompletedProcess(args=cmd, returncode=2, stdout="", stderr="boom")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "bash" in cmd and any("codex-review.sh" in str(c) for c in cmd):
            stdout = f"CODEX_REVIEW_{review_verdict}\n"
            rc = 0 if review_verdict in ("CLEAN", "FIXED") else 1
            return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=stdout, stderr="")

        # Everything else (git clone/checkout/fetch/reset/clean/add/commit, brew prefix)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="/opt/homebrew/opt/touchstone\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "alchemist.runner._resolve_touchstone_root",
        lambda: Path("/opt/homebrew/opt/touchstone/libexec"),
    )
    return captured


def test_dry_run_happy_path_skips_push_and_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=True)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.repo == "autumngarage/touchstone"
    assert r.error is None
    assert r.review_verdict == "CLEAN"
    assert r.pr_url is None
    assert r.dry_run is True
    assert captured["push_calls"] == []
    assert captured["pr_creates"] == []
    assert captured["label_transitions"] == []  # dry-run also skips label mutations


def test_live_happy_path_pushes_and_opens_pr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is None
    assert r.pr_url == "https://github.com/fake/pr/1"
    assert r.review_verdict == "CLEAN"
    assert len(captured["push_calls"]) == 1
    assert len(captured["pr_creates"]) == 1
    # working transition + shipped transition
    assert len(captured["label_transitions"]) == 2


def test_conductor_timeout_records_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_all_external(monkeypatch, conductor_outcome="timeout")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is not None
    assert "timeout" in r.error.lower()
    assert captured["pr_creates"] == []


def test_review_blocked_still_opens_pr_with_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch, review_verdict="BLOCKED")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.review_verdict == "BLOCKED"
    assert r.pr_url == "https://github.com/fake/pr/1"
    assert len(captured["pr_creates"]) == 1


def test_no_diff_produced_results_in_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_all_external(monkeypatch, conductor_outcome="no-diff")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is not None
    assert "no diff" in r.error.lower()
    assert captured["pr_creates"] == []


def test_grouping_takes_one_per_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Two issues in same repo + one issue in another — second-of-same-repo gets dropped."""
    issues = [
        _issue(num=1, repo="autumngarage/touchstone"),
        _issue(num=2, repo="autumngarage/touchstone"),
        _issue(num=3, repo="autumngarage/cortex"),
    ]
    _stub_all_external(monkeypatch, issues=issues)
    config = _config(tmp_path, dry_run=True)

    results = run_tick(config)

    repos_processed = sorted({r.repo for r in results})
    assert repos_processed == ["autumngarage/cortex", "autumngarage/touchstone"]
    # Exactly two issues processed (one per repo, max_per_repo_per_tick=1)
    assert len(results) == 2


def test_doctor_failure_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr("alchemist.runner.scan", lambda **_: [_issue()])
    monkeypatch.setattr(
        "alchemist.runner.run_doctor",
        lambda config: [Check(name="github auth", ok=False, detail="missing")],
    )
    config = _config(tmp_path)
    assert run_tick(config) == []


def test_run_result_is_frozen():
    r = RunResult(
        repo="x/y",
        issue_number=1,
        pr_url=None,
        review_verdict=None,
        error=None,
        elapsed_sec=0.0,
        dry_run=True,
    )
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        r.repo = "mutated"  # type: ignore[misc]
