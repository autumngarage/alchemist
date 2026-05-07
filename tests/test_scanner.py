"""Tests for the scanner — covers gh JSON parsing and error paths."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from alchemist.scanner import DispatchIssue, ScanError, _parse_issues, scan


def test_parse_empty_array_returns_empty_list():
    assert _parse_issues("[]") == []


def test_parse_handles_missing_optional_fields():
    items = [
        {
            "number": 7,
            "title": "Fix typo in README",
            "body": "There's a typo on line 12.",
            "url": "https://github.com/autumngarage/touchstone/issues/7",
            "repository": {"nameWithOwner": "autumngarage/touchstone"},
            "updatedAt": "2026-05-06T18:00:00Z",
            "labels": [{"name": "alchemist-test"}, {"name": "good first issue"}],
        }
    ]
    parsed = _parse_issues(json.dumps(items))
    assert len(parsed) == 1
    issue = parsed[0]
    assert isinstance(issue, DispatchIssue)
    assert issue.number == 7
    assert issue.repository == "autumngarage/touchstone"
    assert "alchemist-test" in issue.labels


def test_parse_invalid_json_raises_scan_error():
    with pytest.raises(ScanError):
        _parse_issues("not-json")


def test_scan_invokes_gh_with_correct_args(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    issues = scan(org="autumngarage", label="alchemist-test")
    assert issues == []
    assert "search" in captured["cmd"]
    assert "issues" in captured["cmd"]
    assert "--owner" in captured["cmd"]
    assert "autumngarage" in captured["cmd"]
    assert "--label" in captured["cmd"]
    assert "alchemist-test" in captured["cmd"]


def test_scan_raises_on_gh_nonzero_exit(monkeypatch: pytest.MonkeyPatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=4, stdout="", stderr="rate limited"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ScanError, match="rate limited"):
        scan(org="autumngarage", label="alchemist-test")


def test_scan_raises_on_missing_gh_binary(monkeypatch: pytest.MonkeyPatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ScanError, match="not found"):
        scan(org="autumngarage", label="alchemist-test")
