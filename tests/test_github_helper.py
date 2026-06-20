from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dani import github_helper


class FakeGitHubCLI:
    def __init__(self) -> None:
        self.ensure_pull_requests: list[dict[str, str]] = []

    def ensure_pull_request(
        self, repo_full_name: str, *, head: str, base: str, title: str, body: str
    ) -> dict[str, str]:
        payload = {
            "repo_full_name": repo_full_name,
            "head": head,
            "base": base,
            "title": title,
            "body": body,
        }
        self.ensure_pull_requests.append(payload)
        return payload


def test_ensure_pr_uses_existing_github_pull_request_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_github = FakeGitHubCLI()
    body_file = tmp_path / "pr-body.md"
    body_file.write_text("Implements #7\n\n<!-- dani:stage=implementation;job=abc;issue=7 -->", encoding="utf-8")
    monkeypatch.setattr(github_helper, "GitHubCLI", lambda: fake_github)

    result = CliRunner().invoke(
        github_helper.app,
        [
            "ensure-pr",
            "--repo",
            "acme/demo",
            "--head",
            "feature/#7",
            "--base",
            "dev",
            "--title",
            "Feature/#7",
            "--body-file",
            str(body_file),
        ],
    )

    assert result.exit_code == 0
    assert fake_github.ensure_pull_requests == [
        {
            "repo_full_name": "acme/demo",
            "head": "feature/#7",
            "base": "dev",
            "title": "Feature/#7",
            "body": body_file.read_text(encoding="utf-8"),
        }
    ]
    assert json.loads(result.stdout)["head"] == "feature/#7"
