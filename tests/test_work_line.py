from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dani.models import JobRecord, RepoConfig
from dani.omx_runner import OmxRunner
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
    subprocess.run(  # noqa: S603
        ["git", "init", "--bare", str(origin)],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603
        ["git", "clone", str(origin), str(repo_path)],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
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


def test_git_work_line_manager_creates_branch_and_worktree_for_new_line(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path)
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    manager = GitWorkLineManager(tmp_path / "runs")
    job = JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=11)

    context = manager.prepare(repo, job)

    assert context.line_id == "issue-11"
    assert context.branch_name == "feature/#11"
    assert context.worktree_path == tmp_path / "runs" / "worktrees" / "acme-demo" / "issue-11"
    assert _git(repo_path, "show-ref", "--verify", "refs/heads/feature/#11").returncode == 0
    assert (
        _git(repo_path, "rev-parse", "feature/#11").stdout.strip()
        == _git(repo_path, "rev-parse", "origin/dev").stdout.strip()
    )
    assert context.worktree_path.is_dir()
    assert _git(context.worktree_path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    assert _git(context.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature/#11"


def test_git_work_line_manager_isolates_issue_and_pr_lines_from_same_repo(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path)
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    manager = GitWorkLineManager(tmp_path / "runs")

    issue_context = manager.prepare(
        repo,
        JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=11),
    )
    pr_context = manager.prepare(
        repo,
        JobRecord(
            repo_full_name=repo.full_name,
            stage="implementation",
            pr_number=101,
            metadata={"issue_id": "21"},
        ),
    )

    assert issue_context.repo_path == pr_context.repo_path == repo_path
    assert issue_context.issue_id == "11"
    assert issue_context.pr_id == ""
    assert issue_context.line_id == "issue-11"
    assert issue_context.branch_name == "feature/#11"
    assert pr_context.issue_id == "21"
    assert pr_context.pr_id == "101"
    assert pr_context.line_id == "pr-101"
    assert pr_context.branch_name == "dani/pr-101"
    assert issue_context.branch_name != pr_context.branch_name
    assert issue_context.worktree_path != pr_context.worktree_path

    _line_command(
        issue_context.worktree_path,
        "printf 'base\nissue 11 change\n' > app.txt && "
        "printf 'issue 11 only\n' > issue.txt && "
        "git add app.txt issue.txt && "
        "git commit -m 'issue 11 change'",
    )
    _line_command(
        pr_context.worktree_path,
        "printf 'base\npr 101 change\n' > app.txt && "
        "printf 'pr 101 only\n' > pr.txt && "
        "git add app.txt pr.txt && "
        "git commit -m 'pr 101 change'",
    )

    assert (issue_context.worktree_path / "app.txt").read_text(encoding="utf-8") == "base\nissue 11 change\n"
    assert (pr_context.worktree_path / "app.txt").read_text(encoding="utf-8") == "base\npr 101 change\n"
    assert (repo_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert (issue_context.worktree_path / "issue.txt").read_text(encoding="utf-8") == "issue 11 only\n"
    assert not (issue_context.worktree_path / "pr.txt").exists()
    assert (pr_context.worktree_path / "pr.txt").read_text(encoding="utf-8") == "pr 101 only\n"
    assert not (pr_context.worktree_path / "issue.txt").exists()
    assert _git(issue_context.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature/#11"
    assert _git(pr_context.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "dani/pr-101"


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
        "printf 'base\nissue 11 change\n' > app.txt && git add app.txt && git commit -m 'issue 11 change'",
    )

    assert (first.worktree_path / "app.txt").read_text(encoding="utf-8") == "base\nissue 11 change\n"
    assert (sibling.worktree_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert _git(first.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature/#11"
    assert _git(sibling.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature/#12"
    assert _git(sibling.worktree_path, "status", "--short").stdout == ""


def test_parallel_work_lines_preserve_isolated_worktrees_when_one_merge_conflicts(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path)
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    manager = GitWorkLineManager(tmp_path / "runs")
    first = manager.prepare(
        repo,
        JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=11),
    )
    second = manager.prepare(
        repo,
        JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=12),
    )

    _line_command(
        first.worktree_path,
        "printf 'line from issue 11\n' > app.txt && git add app.txt && git commit -m 'issue 11 app change'",
    )
    _line_command(
        second.worktree_path,
        "printf 'line from issue 12\n' > app.txt && git add app.txt && git commit -m 'issue 12 app change'",
    )

    _git(repo_path, "checkout", "dev")
    _git(repo_path, "merge", "--no-ff", "--no-edit", first.branch_name)
    conflicted_merge = _git(repo_path, "merge", "--no-ff", "--no-edit", second.branch_name, check=False)

    assert conflicted_merge.returncode != 0
    assert "CONFLICT" in conflicted_merge.stdout or "CONFLICT" in conflicted_merge.stderr
    assert _git(repo_path, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() == ["app.txt"]
    assert first.worktree_path.is_dir()
    assert second.worktree_path.is_dir()
    assert _git(first.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == first.branch_name
    assert _git(second.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == second.branch_name
    assert _git(first.worktree_path, "status", "--short").stdout == ""
    assert _git(second.worktree_path, "status", "--short").stdout == ""
    assert (first.worktree_path / "app.txt").read_text(encoding="utf-8") == "line from issue 11\n"
    assert (second.worktree_path / "app.txt").read_text(encoding="utf-8") == "line from issue 12\n"


def test_agent_runner_launches_inside_generated_worktree(tmp_path: Path, monkeypatch) -> None:
    repo_path = _init_repo(tmp_path)
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    manager = GitWorkLineManager(tmp_path / "runs")
    job = JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=11)
    context = manager.prepare(repo, job)
    job.metadata.update(context.metadata())

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_omx = bin_dir / "omx"
    fake_omx.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "pwd > agent-cwd.txt\n"
        "git branch --show-current > agent-branch.txt\n"
        "printf 'agent touched worktree\\n' >> app.txt\n",
        encoding="utf-8",
    )
    fake_omx.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runner = OmxRunner(run_dir=tmp_path / "agent-runs")

    session = runner.launch(context.worktree_path, job, "Implement issue 11.")
    try:
        runner.wait(session.runtime_handle, timeout_seconds=5)
    finally:
        runner.close_session(session.runtime_handle)

    assert session.worktree_path == str(context.worktree_path)
    assert (context.worktree_path / "agent-cwd.txt").read_text(encoding="utf-8").strip() == str(context.worktree_path)
    assert (context.worktree_path / "agent-branch.txt").read_text(encoding="utf-8").strip() == "feature/#11"
    assert (context.worktree_path / "app.txt").read_text(encoding="utf-8") == "base\nagent touched worktree\n"
    assert not (repo_path / "agent-cwd.txt").exists()
    assert (repo_path / "app.txt").read_text(encoding="utf-8") == "base\n"


def test_agent_runner_executes_two_work_lines_concurrently_without_shared_state(tmp_path: Path, monkeypatch) -> None:
    repo_path = _init_repo(tmp_path)
    repo = RepoConfig(full_name="acme/demo", local_path=str(repo_path))
    manager = GitWorkLineManager(tmp_path / "runs")
    jobs = [
        JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=11),
        JobRecord(repo_full_name=repo.full_name, stage="implementation", issue_number=12),
    ]
    contexts = [manager.prepare(repo, job) for job in jobs]
    for job, context in zip(jobs, contexts, strict=True):
        job.metadata.update(context.metadata())

    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_omx = bin_dir / "omx"
    fake_omx.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "line=${3#LINE=}\n"
        'export DANI_TEST_LINE_ENV="$line"\n'
        "pwd > agent-cwd.txt\n"
        "git branch --show-current > agent-branch.txt\n"
        "printf '%s\\n' \"$DANI_TEST_LINE_ENV\" > agent-env.txt\n"
        "mkdir agent.lock\n"
        "printf '%s\\n' \"$line\" > agent.lock/owner.txt\n"
        f"touch {sync_dir}/$line.ready\n"
        f"while [ ! -f {sync_dir}/issue-11.ready ] || [ ! -f {sync_dir}/issue-12.ready ]; do sleep 0.05; done\n"
        "printf 'agent %s\\n' \"$line\" >> app.txt\n"
        "git add app.txt agent-cwd.txt agent-branch.txt agent-env.txt agent.lock/owner.txt\n"
        'git commit -m "agent $line"\n',
        encoding="utf-8",
    )
    fake_omx.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runner = OmxRunner(run_dir=tmp_path / "agent-runs")

    def launch_and_wait(job: JobRecord) -> None:
        context = next(item for item in contexts if item.line_id == job.metadata["line_id"])
        session = runner.launch(context.worktree_path, job, f"LINE={context.line_id}")
        try:
            runner.wait(session.runtime_handle, timeout_seconds=10)
        finally:
            runner.close_session(session.runtime_handle)

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        list(executor.map(launch_and_wait, jobs))

    by_line = {context.line_id: context for context in contexts}
    assert by_line["issue-11"].branch_name == "feature/#11"
    assert by_line["issue-12"].branch_name == "feature/#12"
    assert by_line["issue-11"].worktree_path != by_line["issue-12"].worktree_path
    for line_id, context in by_line.items():
        assert (context.worktree_path / "agent-cwd.txt").read_text(encoding="utf-8").strip() == str(
            context.worktree_path
        )
        assert (context.worktree_path / "agent-branch.txt").read_text(encoding="utf-8").strip() == context.branch_name
        assert (context.worktree_path / "agent-env.txt").read_text(encoding="utf-8").strip() == line_id
        assert (context.worktree_path / "agent.lock" / "owner.txt").read_text(encoding="utf-8").strip() == line_id
        assert (context.worktree_path / "app.txt").read_text(encoding="utf-8") == f"base\nagent {line_id}\n"

    assert not (repo_path / "agent.lock").exists()
    assert (repo_path / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert _git(repo_path, "show", "feature/#11:agent-env.txt").stdout.strip() == "issue-11"
    assert _git(repo_path, "show", "feature/#12:agent-env.txt").stdout.strip() == "issue-12"
