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


def _config(
    state_dir: Path,
    *,
    dry_run: bool = True,
    max_issues: int = 10,
    max_concurrent: int = 1,
    repo_blocklist: tuple[str, ...] = (),
) -> Config:
    return Config(
        org="autumngarage",
        dispatch_label="alchemist-test",
        default_provider="kimi",
        default_budget="$1",
        poll_interval_minutes=5,
        state_dir=state_dir,
        dry_run=dry_run,
        max_issues_per_tick=max_issues,
        max_per_repo_per_tick=1,
        max_concurrent_repos=max_concurrent,
        conductor_timeout_sec=60,
        review_timeout_sec=60,
        github_token_env="GITHUB_TOKEN",
        assignee_user="@me",
        repo_blocklist=repo_blocklist,
        app_id=None,
        app_installation_id=None,
        app_private_key=None,
        app_private_key_path=None,
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
    git_auth_failure: bool = False,
    label_transition_fail_on: str | None = None,
    # "merged" | "blocked" | "preflight-failed" | "infra-error" | "missing" |
    # "timeout-but-actually-merged" | "timeout-and-not-merged"
    merge_outcome: str = "merged",
):
    """Patch every subprocess.* + scanner + doctor used by the runner.

    Returns a captured-call tracker (dict) the test can inspect to verify
    push / PR / label transitions did or did not happen.
    """
    if issues is None:
        issues = [_issue()]
    label_state: dict[str, set[str]] = {
        str(issue.number): set(issue.labels) for issue in issues
    }

    captured: dict[str, list[Any]] = {
        "subprocess_run_args": [],
        "subprocess_run_kwargs": [],
        "label_transitions": [],
        "label_creates": [],
        "assignee_changes": [],
        "activity_comments": [],
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
        captured["subprocess_run_kwargs"].append(kwargs)

        if "label" in cmd and "create" in cmd:
            captured["label_creates"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "issue" in cmd and "edit" in cmd and (
            "--add-assignee" in cmd or "--remove-assignee" in cmd
        ):
            captured["assignee_changes"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "issue" in cmd and "view" in cmd and "--json" in cmd and "labels" in cmd:
            issue_number = str(cmd[cmd.index("view") + 1])
            stdout = "\n".join(sorted(label_state.get(issue_number, set())))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        if "issue" in cmd and "edit" in cmd:
            if (
                label_transition_fail_on
                and "--add-label" in cmd
                and cmd[cmd.index("--add-label") + 1] == label_transition_fail_on
            ):
                captured["label_transitions"].append(cmd)
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="label update failed"
                )
            issue_number = str(cmd[cmd.index("edit") + 1])
            labels = label_state.setdefault(issue_number, set())
            if "--remove-label" in cmd:
                raw = cmd[cmd.index("--remove-label") + 1]
                for label in str(raw).split(","):
                    labels.discard(label)
            if "--add-label" in cmd:
                raw = cmd[cmd.index("--add-label") + 1]
                for label in str(raw).split(","):
                    labels.add(label)
            captured["label_transitions"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "issue" in cmd and "comment" in cmd:
            captured["activity_comments"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "repo" in cmd and "view" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")

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

        if git_auth_failure and "git" in cmd and ("clone" in cmd or "fetch" in cmd):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=128,
                stdout="",
                stderr=(
                    "fatal: Authentication failed with "
                    "Authorization: Basic Z2hwX1NFQ1JFVA== ghp_SECRETLEAK"
                ),
            )

        if "bash" in cmd and any("merge-pr.sh" in str(c) for c in cmd):
            if merge_outcome in ("timeout-but-actually-merged", "timeout-and-not-merged"):
                raise subprocess.TimeoutExpired(cmd, timeout=10)
            if merge_outcome == "merged":
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if merge_outcome == "blocked":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="PUSH BLOCKED\nConductor flagged issues\nCODEX_REVIEW_BLOCKED\n",
                    stderr="",
                )
            if merge_outcome == "preflight-failed":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="deterministic preflight failed\nruff check failed\n",
                    stderr="",
                )
            if merge_outcome == "infra-error":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="gh: GraphQL: API rate limit exceeded\n",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

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


def test_merge_preflight_failure_is_fatal_with_pr_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch, merge_outcome="preflight-failed")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    assert r.merged is False
    assert r.error is not None
    assert "merge-pr: preflight failed" in r.error
    assert len(captured["pr_creates"]) == 1
    labels_added = [
        cmd[cmd.index("--add-label") + 1]
        for cmd in captured["label_transitions"]
        if "--add-label" in cmd
    ]
    assert "alchemist-test-error" in labels_added


def test_merge_infra_failure_is_fatal_with_pr_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch, merge_outcome="infra-error")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    assert r.merged is False
    assert r.error is not None
    assert "merge-pr: github api failure" in r.error
    assert "API rate limit exceeded" in r.error
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


def test_no_diff_produced_results_in_decline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The agent correctly judging an issue non-actionable (no diff) is a
    DECLINE, not an error. Label transitions to `<dispatch>-declined`,
    comment frames it as a deliberate decline."""
    captured = _stub_all_external(monkeypatch, conductor_outcome="no-diff")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is not None  # populated for log visibility
    assert "no diff" in r.error.lower()
    assert captured["pr_creates"] == []
    # Label transition is to declined, not error.
    transitions = captured["label_transitions"]
    add_labels = [
        cmd[cmd.index("--add-label") + 1] for cmd in transitions if "--add-label" in cmd
    ]
    assert "alchemist-test-declined" in add_labels, (
        f"expected -declined transition; got {add_labels}"
    )
    assert "alchemist-test-error" not in add_labels
    # Comment frames the outcome as a deliberate decline.
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("declined" in b for b in bodies), f"expected 'declined' comment; got {bodies}"


def test_shipped_label_failure_is_visible_in_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(
        monkeypatch,
        label_transition_fail_on="alchemist-test-shipped",
    )
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].merged is True
    assert results[0].error is not None
    assert results[0].error.startswith("label-transition:")
    assert len(captured["pr_creates"]) == 1


def test_declined_label_failure_is_visible_as_fatal_label_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _stub_all_external(
        monkeypatch,
        conductor_outcome="no-diff",
        label_transition_fail_on="alchemist-test-declined",
    )
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].error.startswith("label-transition:")
    assert "decline: conductor produced no diff" in results[0].error


def test_error_label_failure_is_visible_in_bail_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _stub_all_external(
        monkeypatch,
        conductor_outcome="timeout",
        label_transition_fail_on="alchemist-test-error",
    )
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert "conductor: timeout" in results[0].error
    assert "label-transition: label update failed" in results[0].error


def test_ensure_labels_creates_declined_label(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _ensure_labels

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _ensure_labels("autumngarage/touchstone", "alchemist-test")

    label_names = [cmd[cmd.index("create") + 1] for cmd in calls if "label" in cmd]
    assert "alchemist-test-declined" in label_names
    # Now five expected labels (added 'declined' to the original four).
    assert len(label_names) == 5


def test_repo_blocklist_skips_listed_repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Repos in the blocklist are filtered out of the tick — labelled issues
    on those repos are silently skipped, not bailed/declined."""
    issues = [
        _issue(num=1, repo="autumngarage/touchstone"),
        _issue(num=2, repo="autumngarage/vesper"),  # blocklisted
        _issue(num=3, repo="autumngarage/cortex"),
    ]
    _stub_all_external(monkeypatch, issues=issues)
    config = _config(
        tmp_path,
        dry_run=True,
        repo_blocklist=("autumngarage/vesper",),
    )

    results = run_tick(config)

    repos_processed = sorted({r.repo for r in results})
    assert "autumngarage/vesper" not in repos_processed
    assert repos_processed == ["autumngarage/cortex", "autumngarage/touchstone"]


@pytest.mark.parametrize("stale_label", ["alchemist-test-error", "alchemist-test-declined"])
def test_working_transition_removes_stale_state_labels_on_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stale_label: str
):
    issue = _issue(num=7, repo="autumngarage/touchstone")
    issue = DispatchIssue(
        number=issue.number,
        title=issue.title,
        body=issue.body,
        url=issue.url,
        repository=issue.repository,
        updated_at=issue.updated_at,
        labels=("alchemist-test", stale_label),
    )
    captured = _stub_all_external(monkeypatch, issues=[issue])
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    first_transition = captured["label_transitions"][0]
    assert first_transition[first_transition.index("--add-label") + 1] == "alchemist-test-working"
    removed = first_transition[first_transition.index("--remove-label") + 1].split(",")
    assert "alchemist-test" in removed
    assert stale_label in removed


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


def test_global_tick_cap_limits_total_issues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    issues = [
        _issue(num=1, repo="autumngarage/touchstone"),
        _issue(num=2, repo="autumngarage/cortex"),
        _issue(num=3, repo="autumngarage/vesper"),
    ]
    _stub_all_external(monkeypatch, issues=issues)
    config = _config(tmp_path, dry_run=True, max_issues=1)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].repo == "autumngarage/touchstone"


def test_doctor_failure_returns_visible_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr("alchemist.runner.scan", lambda **_: [_issue()])
    monkeypatch.setattr(
        "alchemist.runner.run_doctor",
        lambda config: [Check(name="github auth", ok=False, detail="missing")],
    )
    config = _config(tmp_path)
    results = run_tick(config)
    assert len(results) == 1
    assert results[0].issue_number == 0
    assert results[0].error == "doctor: github auth: missing"


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


def test_ensure_labels_creates_all_five_expected(monkeypatch: pytest.MonkeyPatch):
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
        "alchemist-test-declined",
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


# --------------------------------------------------------------------------- #
# Issue claiming (alchemist#23)                                               #
# --------------------------------------------------------------------------- #


def test_live_run_assigns_and_comments_on_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Live run posts a 'starting work' comment + assigns the issue to the
    configured assignee_user."""
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)
    run_tick(config)
    assert any(
        "--add-assignee" in cmd for cmd in captured["assignee_changes"]
    ), "expected an --add-assignee call"
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("claiming this issue" in b for b in bodies), (
        f"expected a 'claiming' comment; got {bodies}"
    )


def test_live_run_comments_on_ship(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """After successful merge, alchemist posts a 'shipped' comment with the PR url."""
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)
    run_tick(config)
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("shipped" in b and "/pull/9001" in b for b in bodies), (
        f"expected a 'shipped' comment with the PR url; got {bodies}"
    )


def test_dry_run_skips_assignee_and_activity_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=True)
    run_tick(config)
    assert captured["assignee_changes"] == []
    assert captured["activity_comments"] == []


def test_assignee_failure_does_not_block_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """If `gh issue edit --add-assignee` fails (e.g., user not in org), the run
    continues. Comment claim is the backup."""
    from alchemist import runner as _runner_mod

    real_set_assignee = _runner_mod._set_assignee

    def failing_set_assignee(repo, issue_number, action, assignee, config):
        if action == "add":
            raise _runner_mod._GhError("not in org")
        # remove-action would never fire in this test
        return real_set_assignee(repo, issue_number, action, assignee, config)

    monkeypatch.setattr(_runner_mod, "_set_assignee", failing_set_assignee)
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)
    results = run_tick(config)
    assert len(results) == 1
    r = results[0]
    assert r.error is None, f"assignee failure should not block the run; got error={r.error!r}"
    assert r.merged is True
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    # The comment claim still posted (backup signal) even though assign failed.
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("claiming" in b for b in bodies)


def test_unexpected_issue_exception_transitions_to_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch)
    monkeypatch.setattr(
        "alchemist.runner.render_brief",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("brief exploded")),
    )
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].error.startswith("unhandled: brief exploded")
    labels_added = [
        cmd[cmd.index("--add-label") + 1]
        for cmd in captured["label_transitions"]
        if "--add-label" in cmd
    ]
    assert "alchemist-test-error" in labels_added
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("unhandled: brief exploded" in b for b in bodies)


# --------------------------------------------------------------------------- #
# Credential hygiene (alchemist#38)                                           #
# --------------------------------------------------------------------------- #


def test_token_never_appears_in_git_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The GITHUB_TOKEN must never appear cleartext in URLs passed to git,
    because git prints URLs on stdout/stderr and persists them to .git/config.
    Auth must come via `-c http.extraheader=Authorization: Basic <encoded>`
    instead — the encoded value is base64 of `x-access-token:<token>`, so
    the raw token must never show up in any non-header argument."""
    # Use a recognizable fake token.
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_TESTLEAK0123456789abcdef")
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    # Walk every captured subprocess invocation, looking for the token.
    for cmd in captured["subprocess_run_args"]:
        for part in cmd:
            part_str = str(part)
            # Cleartext token must NOT appear anywhere — not even in the
            # Authorization header, since we now base64 it. The encoded
            # form (which contains the token bytes embedded in base64)
            # is fine because it's not the literal "ghp_TESTLEAK..."
            # string.
            assert "ghp_TESTLEAK" not in part_str, (
                f"raw token leaked into argument: {part_str!r}"
            )

    # Belt-and-suspenders: explicitly verify no URL contains the token prefix.
    url_args = [
        part for cmd in captured["subprocess_run_args"]
        for part in cmd
        if isinstance(part, str) and part.startswith("https://")
    ]
    for url in url_args:
        assert "ghp_" not in url, f"token leaked into URL: {url!r}"
        assert "x-access-token" not in url, (
            f"URL still uses old x-access-token embedding: {url!r}"
        )


def test_git_auth_prefix_uses_basic_header_with_x_access_token():
    """The auth prefix uses HTTP Basic with `x-access-token:<token>` so it
    works for both PATs and App installation tokens (GitHub's git endpoint
    rejects Bearer for installation tokens)."""
    import base64

    from alchemist.runner import _git_auth_prefix

    prefix = _git_auth_prefix("ghs_install123")
    assert prefix[:2] == ["git", "-c"]
    expected_encoded = base64.b64encode(b"x-access-token:ghs_install123").decode()
    assert prefix[2] == f"http.extraheader=Authorization: Basic {expected_encoded}"


def test_git_auth_failures_are_sanitized_before_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SECRETLEAK0123456789abcdef")
    captured = _stub_all_external(monkeypatch, git_auth_failure=True)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is not None
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    combined = "\n".join([results[0].error, *bodies])
    assert "ghp_SECRETLEAK" not in combined
    assert "Z2hwX1NFQ1JFVA==" not in combined
    assert "[redacted" in combined


def test_conductor_worker_env_excludes_orchestrator_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_orchestrator")
    monkeypatch.setenv("GH_TOKEN", "ghp_gh_cli")
    monkeypatch.setenv("ALCHEMIST_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-provider")
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=True)

    run_tick(config)

    conductor_envs = [
        kwargs.get("env")
        for cmd, kwargs in zip(
            captured["subprocess_run_args"], captured["subprocess_run_kwargs"], strict=False
        )
        if "conductor" in cmd and "exec" in cmd
    ]
    assert len(conductor_envs) == 1
    env = conductor_envs[0]
    assert env is not None
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "ALCHEMIST_APP_PRIVATE_KEY" not in env
    assert env["OPENROUTER_API_KEY"] == "sk-provider"


# --------------------------------------------------------------------------- #
# Diff validation (alchemist#32)                                              #
# --------------------------------------------------------------------------- #


def test_validate_diff_returns_none_for_clean_python(tmp_path: Path):
    """Valid Python file → no error returned."""
    from alchemist.runner import _validate_diff

    # Stage a fake diff: simulate `git diff --name-only` returning one file.
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "diff" in cmd and "--name-only" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="good.py\n", stderr=""
            )
        return real_run(cmd, *args, **kwargs)

    (tmp_path / "good.py").write_text("def hello():\n    return 'world'\n")
    import unittest.mock as _m
    with _m.patch.object(subprocess, "run", side_effect=fake_run):
        result = _validate_diff(tmp_path)
    assert result is None


def test_validate_diff_catches_python_syntax_error(tmp_path: Path):
    """Malformed Python file → error message describing the file and issue."""
    from alchemist.runner import _validate_diff

    (tmp_path / "broken.py").write_text(
        "def hello(\n    return 'unterminated paren'\n"
    )
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "diff" in cmd and "--name-only" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="broken.py\n", stderr=""
            )
        return real_run(cmd, *args, **kwargs)

    import unittest.mock as _m
    with _m.patch.object(subprocess, "run", side_effect=fake_run):
        result = _validate_diff(tmp_path)

    assert result is not None
    assert "broken.py" in result
    assert "SyntaxError" in result


def test_validate_diff_skips_non_python_files(tmp_path: Path):
    """README.md edits should pass through; we don't validate Markdown."""
    from alchemist.runner import _validate_diff

    (tmp_path / "README.md").write_text("# Random text\nNot Python; should not be parsed.\n")
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "diff" in cmd and "--name-only" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="README.md\n", stderr=""
            )
        return real_run(cmd, *args, **kwargs)

    import unittest.mock as _m
    with _m.patch.object(subprocess, "run", side_effect=fake_run):
        result = _validate_diff(tmp_path)
    assert result is None


def test_validate_diff_handles_corrupted_unicode_in_python(tmp_path: Path):
    """The conductor#254 corruption pattern: hallucinated training-data
    fragments (Chinese + Devanagari + tool-call syntax leakage) at module
    scope. Python's parser rejects this as SyntaxError."""
    from alchemist.runner import _validate_diff

    (tmp_path / "runner.py").write_text(
        "def hello():\n"
        "    return 'ok'\n"
        "\n"
        # The actual corruption captured in conductor#254:
        "'} +#+#+#+#+#+assistant to=functions.Edit კომენტary 彩神争霸破解_code 大发游戏json {\n"
    )
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "diff" in cmd and "--name-only" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="runner.py\n", stderr=""
            )
        return real_run(cmd, *args, **kwargs)

    import unittest.mock as _m
    with _m.patch.object(subprocess, "run", side_effect=fake_run):
        result = _validate_diff(tmp_path)
    assert result is not None, "expected validation to catch corruption"
    assert "SyntaxError" in result


# --------------------------------------------------------------------------- #
# Agent-summary extraction (alchemist#36)                                     #
# --------------------------------------------------------------------------- #


def test_extract_agent_summary_returns_tail(tmp_path: Path):
    from alchemist.runner import _extract_agent_summary
    transcript = tmp_path / "transcript.log"
    transcript.write_text(
        "Lots of tool-call noise here.\n" * 50
        + "I read the README, found the typo, fixed it on line 12.\n"
    )
    summary = _extract_agent_summary(transcript, max_chars=200)
    assert summary is not None
    assert "found the typo" in summary


def test_extract_agent_summary_handles_missing_file(tmp_path: Path):
    from alchemist.runner import _extract_agent_summary
    summary = _extract_agent_summary(tmp_path / "nonexistent.log")
    assert summary is None


def test_extract_agent_summary_returns_none_for_whitespace_only(tmp_path: Path):
    from alchemist.runner import _extract_agent_summary
    transcript = tmp_path / "blank.log"
    transcript.write_text("   \n\n\n   ")
    summary = _extract_agent_summary(transcript)
    assert summary is None


def test_extract_agent_summary_caps_to_max_chars(tmp_path: Path):
    from alchemist.runner import _extract_agent_summary
    transcript = tmp_path / "long.log"
    transcript.write_text("x" * 5000)
    summary = _extract_agent_summary(transcript, max_chars=500)
    assert summary is not None
    assert len(summary) <= 500


# --------------------------------------------------------------------------- #
# Stuck-state sweep (alchemist#34)                                            #
# --------------------------------------------------------------------------- #


def _stuck_sweep_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stuck_items: list[dict],
    label_transition_fails: bool = False,
):
    """Stub gh + label calls for testing _sweep_stuck in isolation."""
    captured: dict[str, list] = {
        "label_transitions": [],
        "comments": [],
    }
    import json as _json

    def fake_run(cmd, *args, **kwargs):
        if "search" in cmd and "issues" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=_json.dumps(stuck_items), stderr=""
            )
        if "issue" in cmd and "edit" in cmd:
            captured["label_transitions"].append(cmd)
            if label_transition_fails:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="label update failed"
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "issue" in cmd and "comment" in cmd:
            captured["comments"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def test_sweep_stuck_transitions_old_working_issues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An issue 60 minutes old in `-working` should be swept (threshold = 30 min)."""
    from datetime import UTC, datetime, timedelta

    from alchemist.runner import _sweep_stuck

    old_ts = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    stuck = [{
        "number": 42,
        "title": "Old stuck issue",
        "repository": {"nameWithOwner": "autumngarage/touchstone"},
        "updatedAt": old_ts,
    }]
    captured = _stuck_sweep_stub(monkeypatch, stuck_items=stuck)

    config = _config(tmp_path, dry_run=False)
    results = _sweep_stuck(config)

    assert len(results) == 1
    r = results[0]
    assert r.repo == "autumngarage/touchstone"
    assert r.issue_number == 42
    assert r.error is not None
    assert "stuck-sweep" in r.error
    # Comment posted + label transition fired.
    assert len(captured["comments"]) == 1
    assert len(captured["label_transitions"]) == 1


def test_sweep_stuck_label_failure_is_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from datetime import UTC, datetime, timedelta

    from alchemist.runner import _sweep_stuck

    old_ts = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    stuck = [{
        "number": 42,
        "title": "Old stuck issue",
        "repository": {"nameWithOwner": "autumngarage/touchstone"},
        "updatedAt": old_ts,
    }]
    _stuck_sweep_stub(monkeypatch, stuck_items=stuck, label_transition_fails=True)

    config = _config(tmp_path, dry_run=False)
    results = _sweep_stuck(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert "stuck-sweep" in results[0].error
    assert "label-transition: label update failed" in results[0].error


def test_sweep_stuck_respects_repo_blocklist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from datetime import UTC, datetime, timedelta

    from alchemist.runner import _sweep_stuck

    old_ts = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    stuck = [
        {
            "number": 42,
            "title": "Blocked repo",
            "repository": {"nameWithOwner": "autumngarage/vesper"},
            "updatedAt": old_ts,
        },
        {
            "number": 43,
            "title": "Allowed repo",
            "repository": {"nameWithOwner": "autumngarage/touchstone"},
            "updatedAt": old_ts,
        },
    ]
    captured = _stuck_sweep_stub(monkeypatch, stuck_items=stuck)
    config = _config(
        tmp_path,
        dry_run=False,
        repo_blocklist=("autumngarage/vesper",),
    )

    results = _sweep_stuck(config)

    assert [r.repo for r in results] == ["autumngarage/touchstone"]
    assert len(captured["comments"]) == 1
    assert captured["comments"][0][captured["comments"][0].index("--repo") + 1] == (
        "autumngarage/touchstone"
    )


def test_sweep_stuck_skips_repo_with_active_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from datetime import UTC, datetime, timedelta

    from alchemist.locks import acquire
    from alchemist.runner import _sweep_stuck

    old_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    stuck = [{
        "number": 42,
        "title": "Actively processing",
        "repository": {"nameWithOwner": "autumngarage/touchstone"},
        "updatedAt": old_ts,
    }]
    captured = _stuck_sweep_stub(monkeypatch, stuck_items=stuck)
    config = _config(tmp_path, dry_run=False)

    with acquire(tmp_path, "autumngarage/touchstone", stale_after_sec=3600):
        results = _sweep_stuck(config)

    assert results == []
    assert captured["comments"] == []
    assert captured["label_transitions"] == []


def test_sweep_stuck_skips_recent_working_issues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An issue 5 minutes old in `-working` is below threshold — leave alone."""
    from datetime import UTC, datetime, timedelta

    from alchemist.runner import _sweep_stuck

    recent_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    recent = [{
        "number": 7,
        "title": "Recent in-flight issue",
        "repository": {"nameWithOwner": "autumngarage/touchstone"},
        "updatedAt": recent_ts,
    }]
    captured = _stuck_sweep_stub(monkeypatch, stuck_items=recent)

    config = _config(tmp_path, dry_run=False)
    results = _sweep_stuck(config)

    assert results == []
    assert captured["label_transitions"] == []
    assert captured["comments"] == []


def test_sweep_stuck_noop_when_no_working_issues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _sweep_stuck

    captured = _stuck_sweep_stub(monkeypatch, stuck_items=[])
    config = _config(tmp_path, dry_run=False)
    results = _sweep_stuck(config)

    assert results == []
    assert captured["label_transitions"] == []


# --------------------------------------------------------------------------- #
# Cortex T1.6-style journal integration (alchemist#7)                         #
# --------------------------------------------------------------------------- #


def _journal_issue() -> DispatchIssue:
    return DispatchIssue(
        number=7,
        title="Fix README typo",
        body="There's a typo on line 12.",
        url="https://github.com/autumngarage/touchstone/issues/7",
        repository="autumngarage/touchstone",
        updated_at="2026-05-07T10:00:00Z",
        labels=("alchemist-test",),
    )


def test_cortex_journal_written_when_dot_cortex_exists(tmp_path: Path):
    """If `.cortex/` is present in the cloned repo, alchemist appends a
    journal entry before staging so it ships in the same PR."""
    from alchemist.runner import _maybe_write_cortex_journal

    (tmp_path / ".cortex").mkdir()
    _maybe_write_cortex_journal(
        tmp_path,
        "autumngarage/touchstone",
        _journal_issue(),
        "alchemist/issue-7-fix-typo",
        "openrouter",
        "Read the README, fixed the typo on line 12.",
    )

    journal_dir = tmp_path / ".cortex" / "journal"
    entries = list(journal_dir.glob("*-alchemist-7.md"))
    assert len(entries) == 1
    body = entries[0].read_text()
    assert "trigger: T1.6-alchemist" in body
    assert "Fix README typo" in body
    assert "alchemist/issue-7-fix-typo" in body
    assert "Read the README, fixed the typo" in body


def test_cortex_journal_skipped_when_no_dot_cortex(tmp_path: Path):
    """No `.cortex/` directory → no journal entry written, no error."""
    from alchemist.runner import _maybe_write_cortex_journal

    _maybe_write_cortex_journal(
        tmp_path,
        "autumngarage/touchstone",
        _journal_issue(),
        "alchemist/issue-7-fix-typo",
        "openrouter",
        None,
    )
    # No directory created, no error raised — silent skip.
    assert not (tmp_path / ".cortex").exists()


def test_cortex_journal_handles_missing_agent_summary(tmp_path: Path):
    """Without an agent summary, the entry still writes but omits that section."""
    from alchemist.runner import _maybe_write_cortex_journal

    (tmp_path / ".cortex").mkdir()
    _maybe_write_cortex_journal(
        tmp_path,
        "autumngarage/touchstone",
        _journal_issue(),
        "alchemist/issue-7-fix-typo",
        "openrouter",
        None,
    )

    entries = list((tmp_path / ".cortex" / "journal").glob("*.md"))
    assert len(entries) == 1
    body = entries[0].read_text()
    assert "## Agent summary" not in body  # section absent
    assert "Fix README typo" in body  # rest still rendered


def test_cortex_journal_creates_journal_dir_if_missing(tmp_path: Path):
    """`.cortex/` exists but `.cortex/journal/` doesn't — alchemist creates it."""
    from alchemist.runner import _maybe_write_cortex_journal

    (tmp_path / ".cortex").mkdir()
    # No journal subdir.
    _maybe_write_cortex_journal(
        tmp_path,
        "autumngarage/touchstone",
        _journal_issue(),
        "alchemist/issue-7-fix-typo",
        "openrouter",
        None,
    )
    assert (tmp_path / ".cortex" / "journal").is_dir()


def test_sweep_stuck_skipped_in_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Dry-run mode never mutates external state — sweep is a no-op."""
    from datetime import UTC, datetime, timedelta

    from alchemist.runner import _sweep_stuck

    old_ts = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
    stuck = [{
        "number": 42,
        "title": "would be swept in live mode",
        "repository": {"nameWithOwner": "autumngarage/touchstone"},
        "updatedAt": old_ts,
    }]
    captured = _stuck_sweep_stub(monkeypatch, stuck_items=stuck)

    config = _config(tmp_path, dry_run=True)
    results = _sweep_stuck(config)

    assert results == []
    # Dry-run skips the gh search entirely.
    assert captured["label_transitions"] == []
    assert captured["comments"] == []


# --------------------------------------------------------------------------- #
# Budget cap (alchemist#33)                                                   #
# --------------------------------------------------------------------------- #


def test_parse_budget_handles_dollar_sign_and_decimal():
    from alchemist.runner import _parse_budget
    assert _parse_budget("$2") == 2.0
    assert _parse_budget("$1.50") == 1.5
    assert _parse_budget("0.25") == 0.25
    assert _parse_budget("  $3 ") == 3.0


def test_parse_budget_returns_none_when_disabled():
    """Empty / zero / non-numeric → None means 'fail open, no cap.'"""
    from alchemist.runner import _parse_budget
    assert _parse_budget("") is None
    assert _parse_budget("$0") is None
    assert _parse_budget("$") is None
    assert _parse_budget("nope") is None


def test_extract_total_cost_sums_usage_events(tmp_path: Path):
    from alchemist.runner import _extract_total_cost
    log = tmp_path / "session.ndjson"
    log.write_text(
        '{"event": "route_decision", "data": {"provider": "claude"}}\n'
        '{"event": "usage", "data": {"provider": "claude", "cost_usd": 0.18}}\n'
        '{"event": "tool_call", "data": {"name": "Read"}}\n'
        '{"event": "usage", "data": {"provider": "claude", "cost_usd": 0.07}}\n'
    )
    assert _extract_total_cost(log) == pytest.approx(0.25)


def test_extract_total_cost_returns_none_when_no_usage(tmp_path: Path):
    """No usage events → None ('we don't know'), distinct from 0.0."""
    from alchemist.runner import _extract_total_cost
    log = tmp_path / "session.ndjson"
    log.write_text('{"event": "route_decision", "data": {}}\n')
    assert _extract_total_cost(log) is None


def test_extract_total_cost_returns_none_when_missing(tmp_path: Path):
    from alchemist.runner import _extract_total_cost
    assert _extract_total_cost(tmp_path / "missing.ndjson") is None


def test_extract_total_cost_skips_malformed_lines(tmp_path: Path):
    from alchemist.runner import _extract_total_cost
    log = tmp_path / "session.ndjson"
    log.write_text(
        "not-json\n"
        '{"event": "usage", "data": {"cost_usd": 0.10}}\n'
        '{"event": "usage", "data": {"cost_usd": "not-a-number"}}\n'
    )
    assert _extract_total_cost(log) == pytest.approx(0.10)


def test_check_budget_returns_problem_when_over(tmp_path: Path):
    from alchemist.runner import _check_budget
    log = tmp_path / "session.ndjson"
    log.write_text('{"event": "usage", "data": {"cost_usd": 5.20}}\n')
    problem = _check_budget(log, "$2")
    assert problem == "$5.20 spent vs $2.00 budgeted"


def test_check_budget_returns_none_when_within(tmp_path: Path):
    from alchemist.runner import _check_budget
    log = tmp_path / "session.ndjson"
    log.write_text('{"event": "usage", "data": {"cost_usd": 0.50}}\n')
    assert _check_budget(log, "$2") is None


def test_check_budget_disabled_when_budget_empty(tmp_path: Path):
    """Empty budget means no enforcement, regardless of cost."""
    from alchemist.runner import _check_budget
    log = tmp_path / "session.ndjson"
    log.write_text('{"event": "usage", "data": {"cost_usd": 999.00}}\n')
    assert _check_budget(log, "") is None
    assert _check_budget(log, "$0") is None


def test_check_budget_fails_open_when_cost_unknown(tmp_path: Path):
    """Missing NDJSON or no usage events → don't bail. Better than blocking
    the whole flow on a logging glitch."""
    from alchemist.runner import _check_budget
    assert _check_budget(tmp_path / "missing.ndjson", "$2") is None
    log = tmp_path / "empty.ndjson"
    log.write_text('{"event": "route_decision"}\n')
    assert _check_budget(log, "$2") is None
