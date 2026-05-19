"""Tests for GitHub issue reporting."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from alchemist.config import Config
from alchemist.reporter import report_self_issue, report_tool_failure

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _config(tmp_path: Path, *, dry_run: bool = False) -> Config:
    return Config(
        org="autumngarage",
        dispatch_label="alchemist-dispatch",
        default_provider="openrouter",
        default_budget="$2",
        poll_interval_minutes=5,
        state_dir=tmp_path,
        dry_run=dry_run,
        max_issues_per_tick=1,
        max_per_repo_per_tick=1,
        max_concurrent_repos=1,
        conductor_effort="low",
        conductor_timeout_sec=600,
        review_timeout_sec=900,
        github_token_env="GITHUB_TOKEN",
        assignee_user="@me",
        repo_blocklist=(),
        app_id=None,
        app_installation_id=None,
        app_private_key=None,
        app_private_key_path=None,
        stall_escalate_after=3,
    )


def _stub_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing_duplicate: bool = False,
    active_self_work: bool = False,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            if "--label" in cmd and "alchemist-working" in cmd:
                stdout = "1\n" if active_self_work else "0\n"
            else:
                stdout = "1\n" if existing_duplicate else "0\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if cmd[:3] == ["gh", "issue", "create"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/example/repo/issues/1\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr("alchemist.reporter.subprocess.run", fake_run)
    return calls


def test_self_issue_can_enter_normal_dispatch_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls = _stub_run(monkeypatch)

    report_self_issue(
        _config(tmp_path),
        title="docs: clarify automatic dispatch",
        body="Tiny docs fix.",
        fingerprint="docs-auto-dispatch",
        safe_to_dispatch=True,
    )

    create = [cmd for cmd in calls if cmd[:3] == ["gh", "issue", "create"]][0]
    assert create[create.index("--repo") + 1] == "autumngarage/alchemist"
    assert create.count("--label") == 2
    assert "bug" in create
    assert "alchemist-dispatch" in create
    body = create[create.index("--body") + 1]
    assert "alchemist-fingerprint:docs-auto-dispatch" in body
    assert "same branch, Conductor, PR, and Touchstone merge-gate path" in body


def test_self_issue_skips_dispatch_when_self_work_already_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls = _stub_run(monkeypatch, active_self_work=True)

    report_self_issue(
        _config(tmp_path),
        title="fix: handled errors should not crash cron",
        body="Narrow runtime guard.",
        fingerprint="cron-exit-status",
        safe_to_dispatch=True,
    )

    create = [cmd for cmd in calls if cmd[:3] == ["gh", "issue", "create"]][0]
    assert create.count("--label") == 1
    assert "bug" in create
    assert "alchemist-dispatch" not in create
    body = create[create.index("--body") + 1]
    assert "Self-dispatch skipped" in body


def test_self_issue_dedupes_by_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls = _stub_run(monkeypatch, existing_duplicate=True)

    report_self_issue(
        _config(tmp_path),
        title="fix: duplicate",
        body="duplicate",
        fingerprint="same-bug",
        safe_to_dispatch=True,
    )

    assert not any(cmd[:3] == ["gh", "issue", "create"] for cmd in calls)
    assert any(
        "alchemist-fingerprint:same-bug" in cmd[cmd.index("--search") + 1]
        for cmd in calls
        if "--search" in cmd
    )


def test_tool_failure_self_report_requires_explicit_dispatch_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls = _stub_run(monkeypatch)

    report_tool_failure(
        _config(tmp_path),
        "alchemist",
        "handled tick marked Railway crashed",
        repo_context="autumngarage/alchemist",
        issue_number=100,
    )

    create = [cmd for cmd in calls if cmd[:3] == ["gh", "issue", "create"]][0]
    assert "bug" in create
    assert "alchemist-dispatch" not in create
    assert "was not auto-dispatched" in create[create.index("--body") + 1]


def test_reporter_logs_failures_instead_of_swallowing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    def fake_run(*args: Any, **kwargs: Any):
        raise FileNotFoundError("gh missing")

    monkeypatch.setattr("alchemist.reporter.subprocess.run", fake_run)

    report_tool_failure(_config(tmp_path), "conductor", "boom")

    err = capsys.readouterr().err
    assert "reporter warning" in err
    assert "could not report conductor failure" in err
