from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dani.models import JobRecord, RepoConfig
from dani.work_line import GitWorkLineManager


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(path), *args],  # noqa: S607
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _line_command(path: Path, command: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", command],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo_path = tmp_path / "repo"
    subprocess.run(  # noqa: S603,S607
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True, text=True
    )
    subprocess.run(  # noqa: S603,S607
        ["git", "clone", str(origin), str(repo_path)], check=True, capture_output=True, text=True
    )

    (repo_path / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo_path, "add", "app.txt")
    _git(repo_path, "commit", "-m", "initial")
    _git(repo_path, "branch", "-M", "main")
    _git(repo_path, "push", "-u", "origin", "main")
    _git(repo_path, "checkout", "-b", "dev")
    _git(repo_path, "push", "-u", "origin", "dev")
    _git(repo_path, "checkout", "main")
    return repo_path


def test_git_work_line_manager_allocates_unique_branches_and_worktrees_concurrently(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path)
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    manager = GitWorkLineManager(tmp_path / "runs")
    jobs = [
        JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=issue_number)
        for issue_number in range(11, 19)
    ]

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        contexts = list(executor.map(lambda job: manager.prepare(repo, job), jobs))

    branch_names = {context.branch_name for context in contexts}
    worktree_paths = {context.worktree_path for context in contexts}
    assert len(branch_names) == len(jobs)
    assert len(worktree_paths) == len(jobs)

    for context in contexts:
        assert context.branch_name == f"feature/#{context.issue_id}"
        assert context.worktree_path.name == context.line_id
        assert context.worktree_path.is_dir()
        assert _git(context.worktree_path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def test_line_command_file_changes_stay_in_assigned_worktree(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path)
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    manager = GitWorkLineManager(tmp_path / "runs")

    first = manager.prepare(
        repo,
        JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=11),
    )
    sibling = manager.prepare(
        repo,
        JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=12),
    )

    _line_command(
        first.worktree_path,
        "printf 'base\nissue 11 change\n' > app.txt && "
        "git add app.txt && "
        "git commit -m 'issue 11 change'",
    )

    assert (first.worktree_path / "app.txt").read_text(encoding="utf-8") == "base\nissue 11 change\n"
    assert (sibling.worktree_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert _git(first.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature/#11"
    assert _git(sibling.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature/#12"
    assert _git(sibling.worktree_path, "status", "--short").stdout == ""
