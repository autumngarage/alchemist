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
    # "merged" | "blocked" | "missing" |
    # "timeout-but-actually-merged" | "timeout-and-not-merged"
    merge_outcome: str = "merged",
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
        "label_creates": [],
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

        if "label" in cmd and "create" in cmd:
            captured["label_creates"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

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
                stdout="https://github.com/autumngarage/touchstone/pull/9001\n",
                stderr="",
            )

        if "pr" in cmd and "view" in cmd and "--json" in cmd and "mergedAt" in cmd:
            if merge_outcome == "timeout-but-actually-merged":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="2026-05-07T11:16:50Z\n",
                    stderr="",
                )
            if merge_outcome == "timeout-and-not-merged":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="\n", stderr="")
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="not found"
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

        if "bash" in cmd and any("merge-pr.sh" in str(c) for c in cmd):
            if merge_outcome in ("timeout-but-actually-merged", "timeout-and-not-merged"):
                raise subprocess.TimeoutExpired(cmd, timeout=10)
            rc = 0 if merge_outcome == "merged" else 1
            return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout="", stderr="")

        # Everything else (git clone/checkout/fetch/reset/clean/add/commit, brew prefix)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="/opt/homebrew/opt/touchstone\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    if merge_outcome == "missing":
        def fake_resolve():
            from alchemist.runner import _ToolError
            raise _ToolError("touchstone scripts/merge-pr.sh not found")
        monkeypatch.setattr("alchemist.runner._resolve_touchstone_root", fake_resolve)
    else:
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
    assert r.pr_url is None
    assert r.merged is None
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
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    assert r.merged is True
    assert len(captured["push_calls"]) == 1
    assert len(captured["pr_creates"]) == 1
    # working transition + shipped transition
    assert len(captured["label_transitions"]) == 2
    # Verify the PR title carries the [alchemist] audit prefix.
    pr_create = captured["pr_creates"][0]
    title_idx = pr_create.index("--title") + 1
    assert pr_create[title_idx].startswith("[alchemist]")


def test_conductor_timeout_records_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_all_external(monkeypatch, conductor_outcome="timeout")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is not None
    assert "timeout" in r.error.lower()
    assert captured["pr_creates"] == []


def test_merge_blocked_leaves_pr_open_for_human_triage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch, merge_outcome="blocked")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is None
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    assert r.merged is False  # touchstone blocked the merge; PR stays open
    assert len(captured["pr_creates"]) == 1


def test_merge_pr_script_missing_records_nonfatal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Touchstone install missing → PR is open but merge-pr.sh couldn't run.
    Surface this as an error string but keep pr_url so a human can pick up."""
    captured = _stub_all_external(monkeypatch, merge_outcome="missing")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    assert r.merged is False
    assert r.error is not None and "merge-pr" in r.error
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
        merged=None,
        error=None,
        elapsed_sec=0.0,
        dry_run=True,
    )
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        r.repo = "mutated"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Post-timeout PR state recheck (alchemist#22)                                #
# --------------------------------------------------------------------------- #


def test_post_timeout_recheck_detects_actual_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """merge-pr.sh subprocess timed out, but the PR actually merged.
    The recheck queries gh and reports merged=True correctly."""
    captured = _stub_all_external(monkeypatch, merge_outcome="timeout-but-actually-merged")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.merged is True, f"expected merged=True after recheck; got {r}"
    assert r.error is None, f"expected no error after successful recheck; got {r.error!r}"
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    # Both label transitions fired: working AND shipped.
    assert len(captured["label_transitions"]) == 2


def test_post_timeout_recheck_confirms_genuine_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """merge-pr.sh times out AND the PR is genuinely not merged.
    recheck returns False; alchemist correctly reports merged=False with
    the timeout error preserved."""
    captured = _stub_all_external(monkeypatch, merge_outcome="timeout-and-not-merged")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.merged is False
    assert r.error is not None
    assert "merge-pr" in r.error
    assert "timeout" in r.error.lower()
    # The PR was opened — keep the URL so a human can pick it up.
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    # Only the working transition fired; no shipped transition because not merged.
    assert len(captured["label_transitions"]) == 1


# --------------------------------------------------------------------------- #
# Label auto-creation (alchemist#19)                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_labels_cache():
    """The per-process cache must be empty between tests so each test exercises
    the full create-or-skip code path independently."""
    from alchemist import runner as _runner_mod
    _runner_mod._LABELS_ENSURED.clear()
    yield
    _runner_mod._LABELS_ENSURED.clear()


def test_ensure_labels_creates_all_four_expected(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _ensure_labels

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _ensure_labels("autumngarage/touchstone", "alchemist-test")

    label_names = [cmd[cmd.index("create") + 1] for cmd in calls if "label" in cmd]
    assert sorted(label_names) == sorted([
        "alchemist-test",
        "alchemist-test-working",
        "alchemist-test-shipped",
        "alchemist-test-error",
    ])


def test_ensure_labels_uses_force_so_already_existing_is_no_op(
    monkeypatch: pytest.MonkeyPatch,
):
    from alchemist.runner import _ensure_labels

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _ensure_labels("autumngarage/touchstone", "alchemist-test")
    assert all("--force" in cmd for cmd in calls if "label" in cmd)


def test_ensure_labels_cached_within_process(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _ensure_labels

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _ensure_labels("autumngarage/touchstone", "alchemist-test")
    first_count = len(calls)
    _ensure_labels("autumngarage/touchstone", "alchemist-test")  # second call
    assert len(calls) == first_count, "second call should hit the cache, no new gh invocations"


def test_ensure_labels_raises_on_gh_failure(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _ensure_labels, _GhError

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="permission denied"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(_GhError, match="could not ensure label"):
        _ensure_labels("autumngarage/touchstone", "alchemist-test")


def test_ensure_labels_skipped_in_dry_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """run_tick in dry-run mode must not mutate the target repo's labels."""
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=True)

    run_tick(config)

    # No label-create gh invocations should fire in dry-run mode.
    assert captured["label_creates"] == []


def test_ensure_labels_runs_before_clone_in_live_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Order matters: labels must exist before any branch transitions try to add them."""
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    # The label-create calls should appear before any git clone.
    flat = captured["subprocess_run_args"]
    label_create_indices = [i for i, cmd in enumerate(flat) if "label" in cmd and "create" in cmd]
    git_clone_indices = [
        i for i, cmd in enumerate(flat)
        if "git" in cmd and "clone" in cmd
    ]
    assert label_create_indices, "expected at least one label create"
    assert git_clone_indices, "expected at least one git clone"
    assert max(label_create_indices) < min(git_clone_indices), (
        "labels must be ensured before any git clone"
    )
