"""Tests for the runner — the transmute loop top-level."""

from __future__ import annotations

import re
import subprocess
from dataclasses import replace
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
        conductor_effort="low",
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
    # Most runner tests predate self-file behavior and assert only the primary
    # issue lifecycle. Keep that loop muted by default; dedicated tests opt in.
    monkeypatch.setenv("ALCHEMIST_DISABLE_SELF_FILE", "1")


def _stub_all_external(
    monkeypatch: pytest.MonkeyPatch,
    *,
    issues: list[DispatchIssue] | None = None,
    conductor_outcome: str = "ok",  # "ok" | "no-diff" | "timeout" | "error"
    git_auth_failure: bool = False,
    remote_branch_exists: bool = True,
    push_create_failure: bool = False,
    pr_create_failure: bool = False,
    label_transition_fail_on: str | None = None,
    # "merged" | "blocked" | "dirty-conflict" | "preflight-failed" |
    # "infra-error" | "missing" | "timeout-but-actually-merged" |
    # "timeout-and-not-merged"
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
        "meta_issue_lists": [],
        "meta_issue_creates": [],
        "meta_issue_comments": [],
        "remote_branch_fetches": [],
        "push_calls": [],
        "pr_creates": [],
    }
    remote_branch_oid = "0123456789abcdef0123456789abcdef01234567"

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
            if "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "autumngarage/alchemist":
                captured["meta_issue_comments"].append(cmd)
            else:
                captured["activity_comments"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if (
            "issue" in cmd and "list" in cmd
            and "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "autumngarage/alchemist"
        ):
            captured["meta_issue_lists"].append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]", stderr="")

        if (
            "issue" in cmd and "create" in cmd
            and "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "autumngarage/alchemist"
        ):
            captured["meta_issue_creates"].append(cmd)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="https://github.com/autumngarage/alchemist/issues/123\n",
                stderr="",
            )

        if "repo" in cmd and "view" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")

        if "pr" in cmd and "create" in cmd:
            captured["pr_creates"].append(cmd)
            if pr_create_failure:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="pr create failed",
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="https://github.com/autumngarage/touchstone/pull/9001\n",
                stderr="",
            )

        if "pr" in cmd and "list" in cmd and "--json" in cmd and "url,number,state" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]", stderr="")

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

        if "git" in cmd and "fetch" in cmd and any(
            str(part).startswith("+refs/heads/alchemist/") for part in cmd
        ):
            captured["remote_branch_fetches"].append(cmd)
            if not remote_branch_exists:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="fatal: couldn't find remote ref",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "git" in cmd and "rev-parse" in cmd and any(
            str(part).startswith("refs/remotes/origin/alchemist/") for part in cmd
        ):
            if remote_branch_exists:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=f"{remote_branch_oid}\n", stderr=""
                )
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

        if "git" in cmd and "update-ref" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        if "git" in cmd and "push" in cmd:
            captured["push_calls"].append(cmd)
            if "--set-upstream" in cmd and push_create_failure:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=128,
                    stdout="",
                    stderr=(
                        "fatal: Authentication failed with "
                        "Authorization: Basic Z2hwX1NFQ1JFVA== ghp_SECRETLEAK"
                    ),
                )
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
            captured["merge_env"] = kwargs.get("env", {})
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
            if merge_outcome == "dirty-conflict":
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout=(
                        "=== > Checking merge state for PR #196 ...\n"
                        "attempt 1: mergeStateStatus=DIRTY mergeable=CONFLICTING\n"
                        "ERROR: PR #196 is DIRTY — has conflicts or is out of date with base.\n"
                    ),
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
    assert len(captured["remote_branch_fetches"]) == 1
    assert len(captured["push_calls"]) == 1
    assert "--set-upstream" in captured["push_calls"][0]
    assert any(
        str(part).startswith("--force-with-lease=refs/heads/alchemist/")
        for part in captured["push_calls"][0]
    )
    assert len(captured["pr_creates"]) == 1
    # working transition + shipped transition
    assert len(captured["label_transitions"]) == 2
    # Verify the PR title carries the [alchemist] audit prefix.
    pr_create = captured["pr_creates"][0]
    title_idx = pr_create.index("--title") + 1
    assert pr_create[title_idx].startswith("[alchemist]")


def test_missing_remote_branch_pushes_without_force_with_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch, remote_branch_exists=False)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is None
    assert results[0].pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    assert len(captured["remote_branch_fetches"]) == 1
    assert len(captured["push_calls"]) == 1
    assert "--set-upstream" in captured["push_calls"][0]
    assert not any(
        str(part).startswith("--force-with-lease=")
        for part in captured["push_calls"][0]
    )
    assert len(captured["pr_creates"]) == 1


def test_push_create_failure_is_sanitized_before_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SECRETLEAK0123456789abcdef")
    captured = _stub_all_external(monkeypatch, push_create_failure=True)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].error.startswith("push:")
    assert captured["pr_creates"] == []
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    combined = "\n".join([results[0].error, *bodies])
    assert "ghp_SECRETLEAK" not in combined
    assert "Z2hwX1NFQ1JFVA==" not in combined
    assert "[redacted" in combined


def test_conductor_timeout_records_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _stub_all_external(monkeypatch, conductor_outcome="timeout")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is not None
    assert "timeout" in r.error.lower()
    assert captured["pr_creates"] == []
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    error_body = next(body for body in bodies if "alchemist hit an error" in body)
    assert "Alchemist will retry this issue on a future tick." in error_body
    assert "remove the `alchemist-error` label to retry" not in error_body


def test_issue_body_conductor_timeout_override_reaches_conductor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    issue = _issue()
    issue = DispatchIssue(
        number=issue.number,
        title=issue.title,
        body=f"{issue.body}\n\nalchemist-timeout: 25m\n",
        url=issue.url,
        repository=issue.repository,
        updated_at=issue.updated_at,
        labels=issue.labels,
    )
    captured = _stub_all_external(monkeypatch, issues=[issue])
    seen_timeouts: list[int] = []

    def run_conductor(*, timeout: int, **_: object) -> None:
        seen_timeouts.append(timeout)

    monkeypatch.setattr("alchemist.runner._run_conductor", run_conductor)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is None
    assert seen_timeouts == [1500]
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("picked this up" in body for body in bodies)


def test_invalid_conductor_timeout_override_bails_before_conductor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    issue = _issue()
    issue = DispatchIssue(
        number=issue.number,
        title=issue.title,
        body=f"{issue.body}\n\nalchemist-conductor-timeout: 12parsecs\n",
        url=issue.url,
        repository=issue.repository,
        updated_at=issue.updated_at,
        labels=issue.labels,
    )
    captured = _stub_all_external(monkeypatch, issues=[issue])
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].error.startswith("conductor-timeout:")
    assert captured["pr_creates"] == []
    assert not any("conductor" in cmd and "exec" in cmd for cmd in captured["subprocess_run_args"])


def test_conductor_timeout_override_is_bounded():
    from alchemist.runner import _parse_conductor_timeout_override

    assert _parse_conductor_timeout_override("alchemist-timeout: 60s") == 60
    assert _parse_conductor_timeout_override("alchemist-timeout: 1h") == 3600
    assert _parse_conductor_timeout_override("no override") is None
    with pytest.raises(ValueError, match="between 60s and 3600s"):
        _parse_conductor_timeout_override("alchemist-timeout: 59s")
    with pytest.raises(ValueError, match="between 60s and 3600s"):
        _parse_conductor_timeout_override("alchemist-timeout: 2h")


def test_tail_text_returns_last_lines(tmp_path: Path):
    from alchemist.runner import _tail_text

    transcript = tmp_path / "transcript.log"
    transcript.write_text("\n".join(f"line-{i}" for i in range(50)), encoding="utf-8")

    tail = _tail_text(transcript)

    assert "line-49" in tail
    assert "line-20" in tail
    assert "line-19" not in tail


def test_tail_text_empty_and_missing(tmp_path: Path):
    from alchemist.runner import _tail_text

    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")

    assert _tail_text(empty) == ""
    assert _tail_text(tmp_path / "missing.log") == ""


def test_tail_text_truncates_to_max_chars(tmp_path: Path):
    from alchemist.runner import _tail_text

    transcript = tmp_path / "transcript.log"
    transcript.write_text("\n".join(["x" * 50 for _ in range(10)]), encoding="utf-8")

    tail = _tail_text(transcript, max_lines=30, max_chars=120)

    assert len(tail) == 120


def test_run_conductor_failure_includes_transcript_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from alchemist.runner import _run_conductor, _ToolError

    brief = tmp_path / "brief.md"
    brief.write_text("brief", encoding="utf-8")
    transcript = tmp_path / "transcript.log"
    ndjson = tmp_path / "transcript.ndjson"

    def fake_run(cmd, *args, **kwargs):
        _ = cmd
        kwargs["stdout"].write("first line\n")
        kwargs["stdout"].write("root cause line\n")
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(_ToolError) as exc_info:
        _run_conductor(
            brief_path=brief,
            cwd=tmp_path,
            provider="kimi",
            timeout=60,
            transcript_path=transcript,
            ndjson_path=ndjson,
        )

    message = str(exc_info.value)
    assert "transcript tail:" in message
    assert "root cause line" in message
    assert f"see {transcript}" in message


def test_run_conductor_uses_configured_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _run_conductor

    brief = tmp_path / "brief.md"
    brief.write_text("brief", encoding="utf-8")
    transcript = tmp_path / "transcript.log"
    ndjson = tmp_path / "transcript.ndjson"
    seen_cmd: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        _ = args, kwargs
        seen_cmd.extend(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _run_conductor(
        brief_path=brief,
        cwd=tmp_path,
        provider="openrouter",
        timeout=60,
        effort="medium",
        transcript_path=transcript,
        ndjson_path=ndjson,
    )

    effort_index = seen_cmd.index("--effort")
    assert seen_cmd[effort_index + 1] == "medium"


def test_run_conductor_sanitizes_transcript_tail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from alchemist.runner import _run_conductor, _ToolError

    brief = tmp_path / "brief.md"
    brief.write_text("brief", encoding="utf-8")
    transcript = tmp_path / "transcript.log"
    ndjson = tmp_path / "transcript.ndjson"

    def fake_run(cmd, *args, **kwargs):
        _ = cmd
        kwargs["stdout"].write("token leak ghp_xxxxxxxxxxx\n")
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(_ToolError) as exc_info:
        _run_conductor(
            brief_path=brief,
            cwd=tmp_path,
            provider="kimi",
            timeout=60,
            transcript_path=transcript,
            ndjson_path=ndjson,
        )

    assert "ghp_xxxxxxxxxxx" not in str(exc_info.value)
    assert "[redacted-token]" in str(exc_info.value)


def test_run_conductor_subprocess_timeout_scales_with_conductor_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from alchemist.runner import _run_conductor

    brief = tmp_path / "brief.md"
    brief.write_text("brief", encoding="utf-8")
    transcript = tmp_path / "transcript.log"
    ndjson = tmp_path / "transcript.ndjson"
    seen_timeouts: list[int] = []

    def fake_run(cmd, *args, **kwargs):
        _ = cmd
        seen_timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _run_conductor(
        brief_path=brief,
        cwd=tmp_path,
        provider="kimi",
        timeout=600,
        transcript_path=transcript,
        ndjson_path=ndjson,
    )

    assert seen_timeouts == [660]


def test_run_conductor_subprocess_timeout_is_capped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from alchemist.runner import _run_conductor

    brief = tmp_path / "brief.md"
    brief.write_text("brief", encoding="utf-8")
    transcript = tmp_path / "transcript.log"
    ndjson = tmp_path / "transcript.ndjson"
    seen_timeouts: list[int] = []

    def fake_run(cmd, *args, **kwargs):
        _ = cmd
        seen_timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _run_conductor(
        brief_path=brief,
        cwd=tmp_path,
        provider="kimi",
        timeout=3600,
        transcript_path=transcript,
        ndjson_path=ndjson,
    )

    assert seen_timeouts == [3720]


def test_conductor_error_comment_uses_collapsed_details_for_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _ToolError

    captured = _stub_all_external(monkeypatch)

    def fail_conductor(*, transcript_path: Path, **_: object) -> None:
        raise _ToolError(
            "exit 2; transcript tail:\n"
            "noisy line\n"
            "token ghp_fake\n"
            f"see {transcript_path}"
        )

    monkeypatch.setattr("alchemist.runner._run_conductor", fail_conductor)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert "transcript tail:" in results[0].error
    assert "<details>" not in results[0].error
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    error_body = next(body for body in bodies if "exit 2" in body)
    assert "<details>" in error_body
    assert "<summary>Conductor transcript tail</summary>" in error_body
    assert "[redacted-token]" in error_body
    assert "ghp_fake" not in error_body


def test_dependency_prepare_failure_bails_before_conductor_and_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _ToolError

    captured = _stub_all_external(monkeypatch)

    def fail_prepare(_repo_dir: Path) -> None:
        raise _ToolError(".venv/bin/python: No module named pytest")

    monkeypatch.setattr("alchemist.runner._prepare_target_repo", fail_prepare)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is not None
    assert r.error.startswith("dependency-prepare:")
    assert "No module named pytest" in r.error
    assert not any("conductor" in cmd and "exec" in cmd for cmd in captured["subprocess_run_args"])
    assert captured["pr_creates"] == []


def test_merge_blocked_sets_blocked_label_and_posts_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch, merge_outcome="blocked")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is None
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    assert r.merged is False
    assert len(captured["pr_creates"]) == 1
    labels_added = [
        cmd[cmd.index("--add-label") + 1]
        for cmd in captured["label_transitions"]
        if "--add-label" in cmd
    ]
    assert "alchemist-test-blocked" in labels_added
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("Touchstone blocked the merge" in b for b in bodies)


def test_merge_dirty_conflict_sets_blocked_label_and_posts_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch, merge_outcome="dirty-conflict")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    r = results[0]
    assert r.error is None
    assert r.pr_url == "https://github.com/autumngarage/touchstone/pull/9001"
    assert r.merged is False
    labels_added = [
        cmd[cmd.index("--add-label") + 1]
        for cmd in captured["label_transitions"]
        if "--add-label" in cmd
    ]
    assert "alchemist-test-blocked" in labels_added
    assert "alchemist-test-error" not in labels_added


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


def test_merge_preflight_failure_does_not_self_file_meta_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("ALCHEMIST_DISABLE_SELF_FILE", raising=False)
    captured = _stub_all_external(monkeypatch, merge_outcome="preflight-failed")
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    assert captured["meta_issue_lists"] == []
    assert captured["meta_issue_creates"] == []
    assert captured["meta_issue_comments"] == []


def test_iteration_cap_conductor_failure_does_not_self_file_meta_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _ToolError

    monkeypatch.delenv("ALCHEMIST_DISABLE_SELF_FILE", raising=False)
    captured = _stub_all_external(monkeypatch)

    def fail_conductor(**_: object) -> None:
        raise _ToolError(
            "exit 1; transcript tail:\n"
            "[conductor] iteration cap hit at 20. Tool usage: Read=1 Edit=6 Bash=13\n"
            "[conductor] Detected unfinished items:\n"
            "  - Validation calls for `uv run ruff check`; not invoked in this session.\n"
        )

    monkeypatch.setattr("alchemist.runner._run_conductor", fail_conductor)
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    assert captured["meta_issue_lists"] == []
    assert captured["meta_issue_creates"] == []
    assert captured["meta_issue_comments"] == []


def test_agent_loop_iteration_cap_failure_does_not_self_file_meta_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _ToolError

    monkeypatch.delenv("ALCHEMIST_DISABLE_SELF_FILE", raising=False)
    captured = _stub_all_external(monkeypatch)

    def fail_conductor(**_: object) -> None:
        raise _ToolError(
            "exit 1; transcript tail:\n"
            "[conductor] agent loop iteration cap: 20\n"
            "[conductor] Detected unfinished items:\n"
            "  - Validation calls for `uv run pytest`; not invoked in this session.\n"
        )

    monkeypatch.setattr("alchemist.runner._run_conductor", fail_conductor)
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    assert captured["meta_issue_lists"] == []
    assert captured["meta_issue_creates"] == []
    assert captured["meta_issue_comments"] == []


def test_non_external_failure_self_files_meta_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("ALCHEMIST_DISABLE_SELF_FILE", raising=False)
    captured = _stub_all_external(monkeypatch, conductor_outcome="timeout")
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    assert len(captured["meta_issue_lists"]) == 1
    assert len(captured["meta_issue_creates"]) == 1
    assert captured["meta_issue_comments"] == []


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


def test_no_diff_decline_does_not_self_file_meta_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Declines are expected outcomes and should not open alchemist-meta issues."""
    monkeypatch.setenv("ALCHEMIST_DISABLE_SELF_FILE", "0")
    captured = _stub_all_external(monkeypatch, conductor_outcome="no-diff")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error == "conductor produced no diff"
    assert captured["meta_issue_lists"] == []
    assert captured["meta_issue_creates"] == []
    assert captured["meta_issue_comments"] == []


def test_budget_exceeded_transitions_to_blocked_and_does_not_self_file_meta_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("ALCHEMIST_DISABLE_SELF_FILE", "0")
    captured = _stub_all_external(monkeypatch)
    monkeypatch.setattr(
        "alchemist.runner._check_budget",
        lambda *_args, **_kwargs: "$2.07 spent vs $2.00 budgeted",
    )
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error == "budget-exceeded: $2.07 spent vs $2.00 budgeted"
    add_labels = [
        cmd[cmd.index("--add-label") + 1]
        for cmd in captured["label_transitions"]
        if "--add-label" in cmd
    ]
    assert "alchemist-test-blocked" in add_labels
    assert "alchemist-test-error" not in add_labels
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("Further retries tend to increase spend" in body for body in bodies)
    assert captured["meta_issue_lists"] == []
    assert captured["meta_issue_creates"] == []
    assert captured["meta_issue_comments"] == []


def test_expected_nonfailure_error_matches_embedded_budget_exceeded():
    from alchemist.runner import _is_expected_nonfailure_error

    assert _is_expected_nonfailure_error("budget-exceeded: $2.34 spent vs $2.00 budgeted")
    assert _is_expected_nonfailure_error(
        "unhandled: budget-exceeded: $2.34 spent vs $2.00 budgeted"
    )
    assert _is_expected_nonfailure_error("budget exceeded: $4.04 spent vs $2.00 budgeted")


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
    assert "alchemist-test-blocked" in label_names
    assert len(label_names) == 5


def test_repo_blocklist_skips_listed_repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Repos in the blocklist are filtered out of the tick — issues on those repos
    are silently skipped, not bailed/declined."""
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


@pytest.mark.parametrize("stale_label", ["alchemist-test-declined"])
def test_state_label_filter_dispatches_only_unlabeled_issues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stale_label: str
):
    issues = [
        _issue(num=1, repo="autumngarage/touchstone"),
        DispatchIssue(
            number=2,
            title="Already being worked",
            body="",
            url="https://github.com/autumngarage/touchstone/issues/2",
            repository="autumngarage/touchstone",
            updated_at="2026-05-06T22:00:00Z",
            labels=("alchemist-test-working",),
        ),
        DispatchIssue(
            number=3,
            title="Operator opted out",
            body="",
            url="https://github.com/autumngarage/touchstone/issues/3",
            repository="autumngarage/touchstone",
            updated_at="2026-05-06T23:00:00Z",
            labels=("alchemist-skip",),
        ),
        DispatchIssue(
            number=4,
            title=f"Carries a stale {stale_label}",
            body="",
            url="https://github.com/autumngarage/touchstone/issues/4",
            repository="autumngarage/touchstone",
            updated_at="2026-05-06T23:30:00Z",
            labels=(stale_label,),
        ),
    ]
    captured = _stub_all_external(monkeypatch, issues=issues)
    config = _config(tmp_path, dry_run=True, max_issues=10)

    results = run_tick(config)

    assert [r.issue_number for r in results] == [1]
    assert captured["pr_creates"] == []


def test_error_labeled_issue_is_retried_via_working_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    issue = DispatchIssue(
        number=5,
        title="Retry after tool recovery",
        body="",
        url="https://github.com/autumngarage/touchstone/issues/5",
        repository="autumngarage/touchstone",
        updated_at="2026-05-06T21:00:00Z",
        labels=("alchemist-test-error",),
    )
    captured = _stub_all_external(monkeypatch, issues=[issue])
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert [r.issue_number for r in results] == [5]
    transitions = [
        cmd for cmd in captured["label_transitions"]
        if "--add-label" in cmd
    ]
    assert len(transitions) >= 2
    first_add = transitions[0][transitions[0].index("--add-label") + 1]
    first_remove = transitions[0][transitions[0].index("--remove-label") + 1]
    assert first_add == "alchemist-test-working"
    assert first_remove == "alchemist-test-error"


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


def test_merge_gate_uses_configured_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("TOUCHSTONE_CONDUCTOR_WITH", raising=False)
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].merged is True
    assert captured["merge_env"]["TOUCHSTONE_CONDUCTOR_WITH"] == config.default_provider


def test_merge_gate_respects_explicit_touchstone_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch)
    monkeypatch.setenv("TOUCHSTONE_CONDUCTOR_WITH", "codex")
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].merged is True
    assert captured["merge_env"]["TOUCHSTONE_CONDUCTOR_WITH"] == "codex"


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


def test_make_branch_tracks_origin_default_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from alchemist.runner import _make_branch

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        seen_cmds.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _make_branch(tmp_path, "alchemist/issue-7", "main")

    assert seen_cmds == [["git", "checkout", "-B", "alchemist/issue-7", "origin/main"]]


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


def test_make_pr_timeout_reconciles_existing_pr(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _make_pr

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(["gh", "pr", "create"], timeout=60)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "alchemist.runner._find_pr_for_head",
        lambda repo, head: ("https://github.com/autumngarage/touchstone/pull/99", 99),
    )

    assert _make_pr("autumngarage/touchstone", "main", "alchemist/issue-7", "t", "b") == (
        "https://github.com/autumngarage/touchstone/pull/99",
        99,
    )


def test_make_pr_timeout_without_reconciliation_raises_gh_error(
    monkeypatch: pytest.MonkeyPatch,
):
    from alchemist.runner import _GhError, _make_pr

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(["gh", "pr", "create"], timeout=60)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("alchemist.runner._find_pr_for_head", lambda repo, head: None)

    with pytest.raises(_GhError, match="timeout, no PR found on reconciliation"):
        _make_pr("autumngarage/touchstone", "main", "alchemist/issue-7", "t", "b")


def test_make_pr_already_exists_reconciles_existing_pr(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _make_pr

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr=(
                'a pull request for branch "alchemist/issue-7" into branch "main" '
                "already exists"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "alchemist.runner._find_pr_for_head",
        lambda repo, head: ("https://github.com/autumngarage/touchstone/pull/99", 99),
    )

    assert _make_pr("autumngarage/touchstone", "main", "alchemist/issue-7", "t", "b") == (
        "https://github.com/autumngarage/touchstone/pull/99",
        99,
    )


def test_make_pr_already_exists_uses_url_in_gh_error_output(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _make_pr

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr=(
                'a pull request for branch "alchemist/issue-7" into branch "main" '
                "already exists:\n"
                "https://github.com/autumngarage/touchstone/pull/174"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "alchemist.runner._find_pr_for_head",
        lambda repo, head: (_ for _ in ()).throw(AssertionError("should not reconcile")),
    )

    assert _make_pr("autumngarage/touchstone", "main", "alchemist/issue-7", "t", "b") == (
        "https://github.com/autumngarage/touchstone/pull/174",
        174,
    )


def test_make_pr_already_exists_without_reconciliation_raises_gh_error(
    monkeypatch: pytest.MonkeyPatch,
):
    from alchemist.runner import _GhError, _make_pr

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=1,
            stdout="",
            stderr=(
                'a pull request for branch "alchemist/issue-7" into branch "main" '
                "already exists"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("alchemist.runner._find_pr_for_head", lambda repo, head: None)

    with pytest.raises(_GhError, match="already exists"):
        _make_pr("autumngarage/touchstone", "main", "alchemist/issue-7", "t", "b")


def test_pr_state_for_head_returns_dict_on_success(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _pr_state_for_head

    def fake_run(cmd, *args, **kwargs):
        assert "url,number,state,mergedAt" in cmd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=(
                '[{"url":"https://github.com/autumngarage/touchstone/pull/99",'
                '"number":99,"state":"MERGED","mergedAt":"2026-05-07T11:16:50Z"}]'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _pr_state_for_head("autumngarage/touchstone", "alchemist/issue-7") == {
        "url": "https://github.com/autumngarage/touchstone/pull/99",
        "number": 99,
        "state": "MERGED",
        "mergedAt": "2026-05-07T11:16:50Z",
    }


def test_pr_state_for_head_returns_none_on_empty_or_failure(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _pr_state_for_head

    responses = [
        subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
    ]

    def fake_run(cmd, *args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _pr_state_for_head("autumngarage/touchstone", "alchemist/issue-7") is None
    assert _pr_state_for_head("autumngarage/touchstone", "alchemist/issue-7") is None


def test_set_label_retries_once_on_gh_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _GhError, _set_label

    calls = {"count": 0}

    def fake_set_label_once(repo, issue_number, transition, config):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _GhError("boom")

    monkeypatch.setattr("alchemist.runner._set_label_once", fake_set_label_once)
    monkeypatch.setattr("alchemist.runner.time.sleep", lambda _sec: None)

    _set_label(
        "autumngarage/touchstone",
        7,
        ("alchemist-test-working", "alchemist-test-shipped"),
        _config(tmp_path, dry_run=False),
    )

    assert calls["count"] == 2


def test_set_label_propagates_gh_error_after_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _GhError, _set_label

    calls = {"count": 0}

    def fake_set_label_once(repo, issue_number, transition, config):
        calls["count"] += 1
        raise _GhError("boom")

    monkeypatch.setattr("alchemist.runner._set_label_once", fake_set_label_once)
    monkeypatch.setattr("alchemist.runner.time.sleep", lambda _sec: None)

    with pytest.raises(_GhError, match="boom"):
        _set_label(
            "autumngarage/touchstone",
            7,
            ("alchemist-test-working", "alchemist-test-shipped"),
            _config(tmp_path, dry_run=False),
        )

    assert calls["count"] == 2


def test_push_timeout_reconciles_when_remote_matches_local(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _push_branch, _SanitizedSubprocessTimeoutError

    calls = {"count": 0}
    local_sha = "a" * 40

    def fake_run_git_auth(*args, **kwargs):
        calls["count"] += 1
        raise _SanitizedSubprocessTimeoutError("timed out")

    monkeypatch.setattr("alchemist.runner._git_auth_prefix", lambda token: ["git"])
    monkeypatch.setattr("alchemist.runner._refresh_remote_branch_ref", lambda *a, **k: None)
    monkeypatch.setattr("alchemist.runner._remote_branch_oid", lambda *a, **k: None)
    monkeypatch.setattr("alchemist.runner._run_git_auth", fake_run_git_auth)
    monkeypatch.setattr("alchemist.runner._local_head_sha", lambda *a, **k: local_sha)
    monkeypatch.setattr("alchemist.runner._remote_branch_sha", lambda *a, **k: local_sha)

    _push_branch(Path("."), "alchemist/issue-7", "autumngarage/touchstone", "ghp_fake")

    assert calls["count"] == 1


def test_push_timeout_retries_when_remote_differs(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _push_branch, _SanitizedSubprocessTimeoutError

    calls = {"count": 0}

    def fake_run_git_auth(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _SanitizedSubprocessTimeoutError("timed out")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("alchemist.runner._git_auth_prefix", lambda token: ["git"])
    monkeypatch.setattr("alchemist.runner._refresh_remote_branch_ref", lambda *a, **k: None)
    monkeypatch.setattr("alchemist.runner._remote_branch_oid", lambda *a, **k: None)
    monkeypatch.setattr("alchemist.runner._run_git_auth", fake_run_git_auth)
    monkeypatch.setattr("alchemist.runner._local_head_sha", lambda *a, **k: "a" * 40)
    monkeypatch.setattr("alchemist.runner._remote_branch_sha", lambda *a, **k: "b" * 40)

    _push_branch(Path("."), "alchemist/issue-7", "autumngarage/touchstone", "ghp_fake")

    assert calls["count"] == 2


def test_push_timeout_retry_failure_reraises_original_timeout(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import (
        _push_branch,
        _SanitizedSubprocessError,
        _SanitizedSubprocessTimeoutError,
    )

    calls = {"count": 0}
    first_error = _SanitizedSubprocessTimeoutError("first timeout")

    def fake_run_git_auth(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise first_error
        raise _SanitizedSubprocessError("second failure")

    monkeypatch.setattr("alchemist.runner._git_auth_prefix", lambda token: ["git"])
    monkeypatch.setattr("alchemist.runner._refresh_remote_branch_ref", lambda *a, **k: None)
    monkeypatch.setattr("alchemist.runner._remote_branch_oid", lambda *a, **k: None)
    monkeypatch.setattr("alchemist.runner._run_git_auth", fake_run_git_auth)
    monkeypatch.setattr("alchemist.runner._local_head_sha", lambda *a, **k: "a" * 40)
    monkeypatch.setattr("alchemist.runner._remote_branch_sha", lambda *a, **k: None)

    with pytest.raises(_SanitizedSubprocessTimeoutError) as exc_info:
        _push_branch(Path("."), "alchemist/issue-7", "autumngarage/touchstone", "ghp_fake")

    assert calls["count"] == 2
    assert exc_info.value is first_error


def test_pr_create_failure_reports_working_branch_visibility(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch, pr_create_failure=True)
    config = _config(tmp_path, dry_run=False)

    results = run_tick(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].error.startswith("pr-create:")
    assert results[0].branch is not None
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    error_body = next(body for body in bodies if "pr-create:" in body)
    assert f"Working branch: `{results[0].branch}`" in error_body


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
        "alchemist-test-working",
        "alchemist-test-blocked",
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


def test_live_run_assigns_and_comments_on_pickup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Live run posts a pickup comment + assigns the issue to the configured assignee."""
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)
    run_tick(config)
    assert any(
        "--add-assignee" in cmd for cmd in captured["assignee_changes"]
    ), "expected an --add-assignee call"
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    pickup_body = next(body for body in bodies if "picked this up" in body)
    assert "Worker: autumn-alchemist[bot]" in pickup_body
    assert re.search(r"- Started: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", pickup_body)


def test_app_auth_skips_assignee_but_still_comments_on_pickup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured = _stub_all_external(monkeypatch)
    config = replace(
        _config(tmp_path, dry_run=False),
        app_id="123",
        app_installation_id="456",
        app_private_key="fake-app-private-key",
    )

    run_tick(config)

    assert captured["assignee_changes"] == []
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("picked this up" in b for b in bodies), (
        f"expected a pickup comment; got {bodies}"
    )


def test_live_run_comments_on_ship(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """After successful merge, alchemist posts the shipped comment shape."""
    captured = _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)
    run_tick(config)
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("✅ alchemist shipped — see" in b and "/pull/9001" in b for b in bodies), (
        f"expected shipped comment with emoji + PR url; got {bodies}"
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
    """If `gh issue edit --add-assignee` fails, the run still continues."""
    from alchemist import runner as _runner_mod

    real_set_assignee = _runner_mod._set_assignee

    def failing_set_assignee(repo, issue_number, action, assignee, config):
        if action == "add":
            raise _runner_mod._GhError("not in org")
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
    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    assert any("picked this up" in b for b in bodies)


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
# Target dependency preparation (alchemist#89)                                #
# --------------------------------------------------------------------------- #


def test_prepare_target_repo_runs_setup_deps_only_without_orchestrator_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _prepare_target_repo

    (tmp_path / "setup.sh").write_text("#!/usr/bin/env bash\n")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_orchestrator")
    monkeypatch.setenv("GH_TOKEN", "ghp_gh_cli")
    monkeypatch.setenv("ALCHEMIST_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-provider")
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd == ["bash", "setup.sh", "--deps-only"]:
            calls.append(cmd)
            envs.append(kwargs["env"])
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _prepare_target_repo(tmp_path)

    assert calls == [["bash", "setup.sh", "--deps-only"]]
    env = envs[0]
    assert env["TOUCHSTONE_SKIP_DEVTOOLS"] == "1"
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "ALCHEMIST_APP_PRIVATE_KEY" not in env
    assert env["OPENROUTER_API_KEY"] == "sk-provider"


def test_prepare_target_repo_falls_back_to_locked_uv_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _prepare_target_repo

    (tmp_path / "uv.lock").write_text("")
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd == ["uv", "sync", "--locked"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _prepare_target_repo(tmp_path)

    assert ["uv", "sync", "--locked"] in calls


def test_prepare_target_repo_rejects_dirty_dependency_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _prepare_target_repo, _ToolError

    (tmp_path / "setup.sh").write_text("#!/usr/bin/env bash\n")

    def fake_run(cmd, *args, **kwargs):
        if cmd == ["bash", "setup.sh", "--deps-only"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=" M uv.lock\n?? .venv/\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(_ToolError, match="left checkout dirty"):
        _prepare_target_repo(tmp_path)


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
        "'} +#+#+#+#+#+assistant-to-functions-Edit კომენტary 彩神争霸破解_code 大发游戏json {\n"
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


def test_sweep_promotes_merged_pr_to_shipped(
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
    captured = _stuck_sweep_stub(monkeypatch, stuck_items=stuck)
    monkeypatch.setattr(
        "alchemist.runner._pr_state_for_head",
        lambda repo, branch: {
            "url": "https://github.com/autumngarage/touchstone/pull/99",
            "number": 99,
            "state": "MERGED",
            "mergedAt": "2026-05-07T11:16:50Z",
        },
    )

    config = _config(tmp_path, dry_run=False)
    results = _sweep_stuck(config)

    assert len(results) == 1
    assert results[0].error is None
    assert results[0].pr_url == "https://github.com/autumngarage/touchstone/pull/99"
    assert results[0].merged is True
    assert len(captured["label_transitions"]) == 1
    add_label = captured["label_transitions"][0][
        captured["label_transitions"][0].index("--add-label") + 1
    ]
    assert add_label == "alchemist-test-shipped"


def test_sweep_skips_open_pr_without_transitioning(
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
    captured = _stuck_sweep_stub(monkeypatch, stuck_items=stuck)
    monkeypatch.setattr(
        "alchemist.runner._pr_state_for_head",
        lambda repo, branch: {
            "url": "https://github.com/autumngarage/touchstone/pull/99",
            "number": 99,
            "state": "OPEN",
            "mergedAt": None,
        },
    )

    config = _config(tmp_path, dry_run=False)
    results = _sweep_stuck(config)

    assert results == []
    assert captured["label_transitions"] == []


def test_sweep_falls_through_to_error_when_no_pr(
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
    captured = _stuck_sweep_stub(monkeypatch, stuck_items=stuck)
    monkeypatch.setattr("alchemist.runner._pr_state_for_head", lambda repo, branch: None)

    config = _config(tmp_path, dry_run=False)
    results = _sweep_stuck(config)

    assert len(results) == 1
    assert results[0].error is not None
    assert "stuck-sweep" in results[0].error
    assert len(captured["label_transitions"]) == 1
    add_label = captured["label_transitions"][0][
        captured["label_transitions"][0].index("--add-label") + 1
    ]
    assert add_label == "alchemist-test-error"


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


def test_self_file_skips_benign_stuck_sweep_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from alchemist.runner import _self_file_failures

    monkeypatch.setenv("ALCHEMIST_DISABLE_SELF_FILE", "0")
    result = RunResult(
        repo="autumngarage/touchstone",
        issue_number=42,
        pr_url=None,
        merged=None,
        error=(
            "stuck-sweep: detected stuck `-working` state "
            "(40 min old); transitioning to error"
        ),
        elapsed_sec=0.0,
        dry_run=False,
    )

    monkeypatch.setattr(
        "alchemist.runner._is_meta_self_issue_result",
        lambda _result: (_ for _ in ()).throw(AssertionError("should not check meta issues")),
    )
    monkeypatch.setattr(
        "alchemist.runner._self_file_failure",
        lambda _result, _config: (_ for _ in ()).throw(AssertionError("should not self-file")),
    )

    _self_file_failures([result], _config(tmp_path, dry_run=False))


def test_self_file_keeps_non_benign_stuck_sweep_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _self_file_failures

    monkeypatch.setenv("ALCHEMIST_DISABLE_SELF_FILE", "0")
    captured: list[RunResult] = []
    result = RunResult(
        repo="autumngarage/touchstone",
        issue_number=42,
        pr_url=None,
        merged=None,
        error=(
            "stuck-sweep: detected stuck `-working` state "
            "(40 min old); transitioning to error; label-transition: label update failed"
        ),
        elapsed_sec=0.0,
        dry_run=False,
    )

    monkeypatch.setattr("alchemist.runner._is_meta_self_issue_result", lambda _result: False)
    monkeypatch.setattr(
        "alchemist.runner._self_file_failure",
        lambda r, _config: captured.append(r),
    )

    _self_file_failures([result], _config(tmp_path, dry_run=False))

    assert captured == [result]


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


# --------------------------------------------------------------------------- #
# Error-comment telemetry: attempt count, tool calls, cumulative cost         #
# --------------------------------------------------------------------------- #


def test_summarize_tool_calls_counts_by_name(tmp_path: Path):
    from alchemist.runner import _format_tool_call_summary, _summarize_tool_calls

    log = tmp_path / "session.ndjson"
    log.write_text(
        '{"event": "tool_call", "data": {"name": "Read"}}\n'
        '{"event": "tool_call", "data": {"name": "Bash"}}\n'
        '{"event": "tool_call", "data": {"name": "Bash"}}\n'
        '{"event": "usage", "data": {"cost_usd": 0.10}}\n'
        '{"event": "tool_call", "data": {"name": "WebFetch"}}\n'
    )
    counts = _summarize_tool_calls(log)
    assert counts == {"Read": 1, "Bash": 2, "WebFetch": 1}
    assert _format_tool_call_summary(counts) == "Read=1 Edit=0 Write=0 Bash=2 WebFetch=1"


def test_summarize_tool_calls_missing_file_returns_empty(tmp_path: Path):
    from alchemist.runner import _summarize_tool_calls
    assert _summarize_tool_calls(tmp_path / "absent.ndjson") == {}


def test_scan_prior_attempts_counts_error_markers_and_sums_cost(
    monkeypatch: pytest.MonkeyPatch,
):
    from alchemist.runner import _scan_prior_attempts

    marker_one = (
        "<!-- alchemist-stats: attempt=1 signature=abc123abc123 "
        "cost_usd=0.7500 cumulative_cost_usd=0.7500 -->"
    )
    marker_two = (
        "<!-- alchemist-stats: attempt=2 signature=abc123abc123 "
        "cost_usd=0.9000 cumulative_cost_usd=1.6500 -->"
    )
    bodies = (
        "🧪 alchemist picked this up.\n\n"
        "- Worker: autumn-alchemist[bot]\n\n\n"
        "⚠️ alchemist hit an error: conductor: exit 1\n\n"
        "Working branch: `alchemist/issue-7-foo`\n\n"
        f"{marker_one}\n\n"
        "🧪 alchemist picked this up.\n\n\n"
        "⚠️ alchemist hit an error (attempt 2): conductor: exit 1\n\n"
        "Working branch: `alchemist/issue-7-foo`\n\n"
        f"{marker_two}"
    )

    def fake_run(cmd, *args, **kwargs):
        assert cmd[:2] == ["gh", "issue"]
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=bodies, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    prior = _scan_prior_attempts("autumngarage/touchstone", 7)
    assert prior.count == 2
    assert prior.cumulative_cost_usd == pytest.approx(1.65)
    assert prior.last_signature == "abc123abc123"


def test_scan_prior_attempts_handles_no_comments(monkeypatch: pytest.MonkeyPatch):
    from alchemist.runner import _scan_prior_attempts

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    prior = _scan_prior_attempts("autumngarage/touchstone", 7)
    assert prior.count == 0
    assert prior.cumulative_cost_usd == 0.0
    assert prior.last_signature is None


def test_scan_prior_attempts_returns_zeros_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from alchemist.runner import _scan_prior_attempts

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    prior = _scan_prior_attempts("autumngarage/touchstone", 7)
    assert prior.count == 0
    assert prior.cumulative_cost_usd == 0.0
    assert prior.last_signature is None
    assert (
        "alchemist: autumngarage/touchstone#7: prior-attempt scan failed: "
        "gh issue view exit 1: boom"
    ) in capsys.readouterr().err


def test_error_comment_includes_branch_and_attempt_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Even on the first attempt the error comment carries the branch and a
    machine-readable stats marker so the next tick can compute cumulative cost."""
    captured = _stub_all_external(monkeypatch, conductor_outcome="error")
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    error_body = next(body for body in bodies if "alchemist hit an error" in body)
    assert "Working branch: `alchemist/issue-7-fix-typo-in-readme-7`" in error_body
    assert "<!-- alchemist-stats:" in error_body
    assert "attempt=1" in error_body
    assert "signature=" in error_body


def test_early_error_comment_does_not_reuse_stale_ndjson(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from alchemist.runner import _ndjson_path_for, _ToolError

    captured = _stub_all_external(monkeypatch)
    stale_ndjson = _ndjson_path_for(tmp_path, "autumngarage/touchstone", 7)
    stale_ndjson.parent.mkdir(parents=True)
    stale_ndjson.write_text(
        '{"event": "tool_call", "data": {"name": "Bash"}}\n'
        '{"event": "usage", "data": {"cost_usd": 999.0}}\n'
    )

    def fail_prepare(_repo_dir: Path) -> None:
        raise _ToolError(".venv/bin/python: No module named pytest")

    monkeypatch.setattr("alchemist.runner._prepare_target_repo", fail_prepare)
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    error_body = next(body for body in bodies if "alchemist hit an error" in body)
    assert not stale_ndjson.exists()
    assert "Tool calls:" not in error_body
    assert "Cost this run:" not in error_body
    assert "cost_usd=999" not in error_body


def test_error_comment_renders_attempt_count_from_prior_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """When prior error comments are present on the issue, the new comment
    shows the next attempt number and flags repeated signatures."""
    from alchemist.runner import (
        _failure_signature,
        _sanitize_error_text,
        _ToolError,
    )

    # The bail message gets sanitized before signing — match that so we know
    # the signature alchemist computes is the one we embedded in prior_body.
    conductor_tail = "transcript tail:\nboom\nsee /var/alchemist/state/transcripts/x.log"
    bail_message = f"conductor: exit 2; {conductor_tail}"
    prior_signature = _failure_signature(_sanitize_error_text(bail_message))
    prior_marker = (
        f"<!-- alchemist-stats: attempt=1 signature={prior_signature} "
        "cost_usd=0.5000 cumulative_cost_usd=0.5000 -->"
    )
    prior_body = (
        "⚠️ alchemist hit an error: conductor: exit 2\n\n"
        "Working branch: `alchemist/issue-7-fix-typo-in-readme-7`\n\n"
        f"{prior_marker}"
    )

    captured = _stub_all_external(monkeypatch)

    def _conductor_returns_error(**_: object) -> None:
        raise _ToolError(f"exit 2; {conductor_tail}")

    monkeypatch.setattr("alchemist.runner._run_conductor", _conductor_returns_error)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if (
            cmd[:3] == ["gh", "issue", "view"]
            and "--json" in cmd
            and "comments" in cmd
        ):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=prior_body, stderr=""
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    bodies = [cmd[cmd.index("--body") + 1] for cmd in captured["activity_comments"]]
    error_body = next(body for body in bodies if "alchemist hit an error" in body)
    assert "(attempt 2)" in error_body
    assert f"Same failure signature as the previous attempt (`{prior_signature}`)" in error_body


def test_tick_summary_logged_to_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _stub_all_external(monkeypatch)
    config = _config(tmp_path, dry_run=False)

    run_tick(config)

    captured = capsys.readouterr()
    assert re.search(
        r"alchemist tick: 1 processed "
        r"\(shipped=1 blocked=0 errored=0 declined=0 fatal=0\) in \d+\.\d+s",
        captured.err,
    )
