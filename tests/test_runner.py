"""Tests for the simplified issue dispatcher runner."""

from __future__ import annotations

import json
import subprocess
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import TYPE_CHECKING, Any

import pytest

from alchemist.config import Config
from alchemist.doctor import Check
from alchemist.runner import RunResult, run_tick
from alchemist.scanner import DispatchIssue

if TYPE_CHECKING:
    from pathlib import Path


def _config(
    state_dir: Path,
    *,
    dry_run: bool = False,
    provider: str = "codex",
    max_issues: int = 10,
    auto_merge: bool = False,
) -> Config:
    return Config(
        org="autumngarage",
        intake_label="agent-ready",
        state_label_prefix="alchemist",
        agent_provider=provider,
        poll_interval_minutes=5,
        state_dir=state_dir,
        dry_run=dry_run,
        max_issues_per_tick=max_issues,
        max_per_repo_per_tick=1,
        max_concurrent_repos=1,
        agent_stale_after_hours=24,
        auto_merge=auto_merge,
        devin_api_key_env="DEVIN_API_KEY",
        devin_org_id="org_123",
        github_token_env="GITHUB_TOKEN",
        assignee_user="@me",
        repo_blocklist=(),
        app_id=None,
        app_installation_id=None,
        app_private_key=None,
        app_private_key_path=None,
    )


def _issue(
    num: int = 7,
    *,
    labels: tuple[str, ...] = ("agent-ready",),
    updated_at: str = "2026-06-16T10:00:00Z",
) -> DispatchIssue:
    return DispatchIssue(
        number=num,
        title=f"Fix issue {num}",
        body="Please fix this.",
        url=f"https://github.com/autumngarage/touchstone/issues/{num}",
        repository="autumngarage/touchstone",
        updated_at=updated_at,
        labels=labels,
    )


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")


def _stub_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    issues: list[DispatchIssue],
    pr_list: list[dict[str, Any]] | None = None,
    pr_state: dict[str, Any] | None = None,
    issue_comments: list[dict[str, str]] | None = None,
) -> dict[str, list[Any]]:
    labels_by_issue = {str(issue.number): set(issue.labels) for issue in issues}
    captured: dict[str, list[Any]] = {
        "scan_labels": [],
        "label_creates": [],
        "issue_edits": [],
        "issue_comments": [],
        "pr_comments": [],
        "pr_merges": [],
    }
    pr_list = pr_list or []
    issue_comments = issue_comments or []
    pr_state = pr_state or {}

    monkeypatch.setattr(
        "alchemist.runner.run_doctor",
        lambda config: [Check(name="gh", ok=True, detail="fake")],
    )

    def fake_scan(*, org: str, label: str = "", **_: Any):
        assert org == "autumngarage"
        captured["scan_labels"].append(label)
        return [issue for issue in issues if label in issue.labels]

    monkeypatch.setattr("alchemist.runner.scan", fake_scan)

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any):
        _ = args, kwargs
        if cmd[:3] == ["gh", "label", "create"]:
            captured["label_creates"].append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        if cmd[:3] == ["gh", "issue", "view"] and "labels" in cmd:
            issue_num = cmd[3]
            stdout = "\n".join(sorted(labels_by_issue.get(issue_num, set())))
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        if cmd[:3] == ["gh", "issue", "view"] and "comments" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps({"comments": issue_comments}),
                "",
            )

        if cmd[:3] == ["gh", "issue", "edit"]:
            issue_num = cmd[3]
            labels = labels_by_issue.setdefault(issue_num, set())
            if "--remove-label" in cmd:
                for label in cmd[cmd.index("--remove-label") + 1].split(","):
                    labels.discard(label)
            if "--add-label" in cmd:
                labels.add(cmd[cmd.index("--add-label") + 1])
            captured["issue_edits"].append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        if cmd[:3] == ["gh", "issue", "comment"]:
            captured["issue_comments"].append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(pr_list), "")

        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(pr_state), "")

        if cmd[:3] == ["gh", "pr", "comment"]:
            captured["pr_comments"].append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        if cmd[:3] == ["gh", "pr", "merge"]:
            captured["pr_merges"].append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def _linked_pr(number: int = 99) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Fix issue 7",
        "body": "Closes #7",
        "updatedAt": "2026-06-16T10:05:00Z",
    }


def _pr_state(
    *,
    state: str = "OPEN",
    merged_at: str | None = None,
    review_decision: str | None = None,
    checks: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "url": "https://github.com/autumngarage/touchstone/pull/99",
        "number": 99,
        "state": state,
        "mergedAt": merged_at,
        "reviewDecision": review_decision,
        "isDraft": False,
        "statusCheckRollup": checks or [],
        "comments": comments or [],
    }


def test_dry_run_dispatch_skips_mutations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_runner(monkeypatch, issues=[_issue()])

    results = run_tick(_config(tmp_path, dry_run=True))

    assert len(results) == 1
    assert results[0].status == "dispatched"
    assert results[0].dry_run is True
    assert captured["issue_comments"] == []
    assert captured["issue_edits"] == []
    assert captured["scan_labels"] == ["agent-ready", "alchemist-dispatched", "alchemist-pr-open"]


def test_live_codex_dispatch_comments_and_marks_dispatched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_runner(monkeypatch, issues=[_issue()])

    results = run_tick(_config(tmp_path))

    assert len(results) == 1
    assert results[0].status == "dispatched"
    body = captured["issue_comments"][0][captured["issue_comments"][0].index("--body") + 1]
    assert "@codex" in body
    assert "Closes #7" in body
    added = [cmd[cmd.index("--add-label") + 1] for cmd in captured["issue_edits"]]
    assert "alchemist-dispatched" in added


def test_live_devin_dispatch_uses_api_session_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _AgentDispatch

    monkeypatch.setattr(
        "alchemist.runner._create_devin_session",
        lambda issue, config: _AgentDispatch(
            provider="devin",
            session_id="devin-123",
            session_url="https://app.devin.ai/sessions/devin-123",
        ),
    )
    captured = _stub_runner(monkeypatch, issues=[_issue()])

    results = run_tick(_config(tmp_path, provider="devin"))

    assert len(results) == 1
    assert results[0].status == "dispatched"
    body = captured["issue_comments"][0][captured["issue_comments"][0].index("--body") + 1]
    assert "session_id=devin-123" in body
    assert "https://app.devin.ai/sessions/devin-123" in body


def test_devin_session_create_posts_prompt_to_configured_org(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _create_devin_session

    monkeypatch.setenv("DEVIN_API_KEY", "cog_secret")
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"session_id":"devin-123","url":"https://app.devin.ai/sessions/devin-123"}'

    def fake_urlopen(request, *, timeout: int):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("alchemist.runner.urllib.request.urlopen", fake_urlopen)

    dispatch = _create_devin_session(_issue(), _config(tmp_path, provider="devin"))

    assert dispatch.session_id == "devin-123"
    assert dispatch.session_url == "https://app.devin.ai/sessions/devin-123"
    assert captured["url"].endswith("/organizations/org_123/sessions")
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer cog_secret"
    assert captured["timeout"] == 30
    assert "Repository: `autumngarage/touchstone`" in captured["payload"]["prompt"]
    assert "Closes #7" in captured["payload"]["prompt"]


def test_devin_followup_posts_session_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from alchemist.runner import _send_devin_message

    monkeypatch.setenv("DEVIN_API_KEY", "cog_secret")
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    def fake_urlopen(request, *, timeout: int):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("alchemist.runner.urllib.request.urlopen", fake_urlopen)

    _send_devin_message(
        "devin-123", "Please fix failing checks.", _config(tmp_path, provider="devin")
    )

    assert captured["url"].endswith("/organizations/org_123/sessions/devin-123/messages")
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer cog_secret"
    assert captured["payload"] == {"message": "Please fix failing checks."}
    assert captured["timeout"] == 30


def test_devin_http_error_redacts_custom_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from alchemist.runner import _AgentDispatchError, _create_devin_session

    monkeypatch.setenv("CUSTOM_DEVIN_KEY", "custom-secret")
    config = replace(
        _config(tmp_path, provider="devin"),
        devin_api_key_env="CUSTOM_DEVIN_KEY",
    )

    def fake_urlopen(request, *, timeout: int):
        _ = request, timeout
        raise urllib.error.HTTPError(
            url="https://api.devin.ai/v3/organizations/org_123/sessions",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=BytesIO(b'{"error":"custom-secret"}'),
        )

    monkeypatch.setattr("alchemist.runner.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(_AgentDispatchError) as excinfo:
        _create_devin_session(_issue(), config)

    assert "custom-secret" not in str(excinfo.value)
    assert "[redacted-secret]" in str(excinfo.value)


def test_dispatched_issue_waits_when_no_pr_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _stub_runner(monkeypatch, issues=[_issue(labels=("alchemist-dispatched",))])

    results = run_tick(_config(tmp_path))

    assert len(results) == 1
    assert results[0].status == "waiting"
    assert results[0].error is None


def test_dispatched_issue_marks_merged_pr_shipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_runner(
        monkeypatch,
        issues=[_issue(labels=("alchemist-dispatched",))],
        pr_list=[_linked_pr()],
        pr_state=_pr_state(merged_at="2026-06-16T11:00:00Z"),
    )

    results = run_tick(_config(tmp_path))

    assert len(results) == 1
    assert results[0].merged is True
    assert results[0].status == "merged"
    added = [cmd[cmd.index("--add-label") + 1] for cmd in captured["issue_edits"]]
    assert "alchemist-shipped" in added


def test_open_pr_with_failed_checks_gets_one_codex_nudge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_runner(
        monkeypatch,
        issues=[_issue(labels=("alchemist-dispatched",))],
        pr_list=[_linked_pr()],
        pr_state=_pr_state(checks=[{"conclusion": "FAILURE", "status": "COMPLETED"}]),
    )

    results = run_tick(_config(tmp_path))

    assert len(results) == 1
    assert results[0].status == "nudged"
    body = captured["pr_comments"][0][captured["pr_comments"][0].index("--body") + 1]
    assert "<!-- alchemist-nudge: checks-failed -->" in body
    assert "@codex" in body


def test_existing_nudge_is_not_repeated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_runner(
        monkeypatch,
        issues=[_issue(labels=("alchemist-pr-open",))],
        pr_list=[_linked_pr()],
        pr_state=_pr_state(
            checks=[{"conclusion": "FAILURE", "status": "COMPLETED"}],
            comments=[{"body": "<!-- alchemist-nudge: checks-failed -->\n@codex already"}],
        ),
    )

    results = run_tick(_config(tmp_path))

    assert len(results) == 1
    assert results[0].status == "pr-open"
    assert captured["pr_comments"] == []


def test_stale_dispatch_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    captured = _stub_runner(
        monkeypatch,
        issues=[_issue(labels=("alchemist-dispatched",), updated_at=old)],
    )

    results = run_tick(_config(tmp_path))

    assert len(results) == 1
    assert results[0].status == "blocked"
    assert "No linked PR" in (results[0].error or "")
    added = [cmd[cmd.index("--add-label") + 1] for cmd in captured["issue_edits"]]
    assert "alchemist-blocked" in added


def test_ready_pr_can_queue_auto_merge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_runner(
        monkeypatch,
        issues=[_issue(labels=("alchemist-pr-open",))],
        pr_list=[_linked_pr()],
        pr_state=_pr_state(
            review_decision="APPROVED",
            checks=[{"conclusion": "SUCCESS", "status": "COMPLETED"}],
        ),
    )

    results = run_tick(_config(tmp_path, auto_merge=True))

    assert len(results) == 1
    assert results[0].status == "merge-queued"
    assert captured["pr_merges"]


def test_run_result_is_frozen():
    result = RunResult(
        repo="x/y",
        issue_number=1,
        pr_url=None,
        merged=None,
        error=None,
        elapsed_sec=0.0,
        dry_run=True,
        status="waiting",
    )
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        result.repo = "mutated"  # type: ignore[misc]
