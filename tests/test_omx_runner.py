from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from dani.errors import RolloutMissingError
from dani.models import JobRecord
from dani.omx_runner import OmxRunner
from dani.signatures import build_signature


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


def test_capture_omx_session_id_matches_exec_signature_and_repo_path(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    sessions_root = tmp_path / "sessions"
    session_day_dir = sessions_root / "2026" / "03" / "19"
    session_day_dir.mkdir(parents=True)
    signature = build_signature(stage="issue_request", job="job-123", issue=7)
    session_file = session_day_dir / "rollout-2026-03-19T11-26-54-session-123.jsonl"
    session_file.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-03-19T02:26:54.703Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-123",
                    "cwd": str(repo_path),
                    "originator": "codex_exec",
                },
            }),
            json.dumps({
                "timestamp": "2026-03-19T02:26:56.936Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Prompt with {signature}"}],
                },
            }),
        ]),
        encoding="utf-8",
    )
    started_at = time.time() - 1
    runner = OmxRunner(run_dir=tmp_path / "runs", sessions_root=sessions_root)

    omx_session_id = runner._capture_omx_session_id(
        repo_path=repo_path,
        prompt=f"Please use this signature: {signature}",
        started_at=started_at,
        poll_interval=0.01,
        timeout_seconds=0.05,
    )

    assert omx_session_id == "session-123"


def test_close_session_terminates_active_process(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")

    process = type(
        "Process",
        (),
        {
            "poll": lambda self: None,
            "terminate": lambda self: None,
            "wait": lambda self, timeout=None: 0,
            "kill": lambda self: None,
        },
    )()
    runner._processes["runtime-123"] = (process, stdout_file, stderr_file)

    runner.close_session("runtime-123")

    assert stdout_file.closed
    assert stderr_file.closed
    assert runner._processes == {}


def test_close_session_skips_missing_process(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    runner.close_session("runtime-123")


def test_build_script_uses_omx_exec(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    script = runner._build_script(repo_path=tmp_path / "repo", prompt_path=tmp_path / "prompt.txt")

    assert "omx exec --dangerously-bypass-approvals-and-sandbox" in script


def test_launch_command_observes_assigned_worktree_as_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree_path = tmp_path / "repo" / ".dani-worktrees" / "issue-77"
    worktree_path.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_omx = bin_dir / "omx"
    fake_omx.write_text("#!/bin/sh\npwd\n", encoding="utf-8")
    fake_omx.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    runner = OmxRunner(run_dir=tmp_path / "runs")
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=77)

    session = runner.launch(worktree_path, job, "Implement issue 77.")
    try:
        runner.wait(session.runtime_handle, timeout_seconds=5)
    finally:
        runner.close_session(session.runtime_handle)

    stdout = Path(session.stdout_path or "").read_text(encoding="utf-8").splitlines()
    assert stdout == [str(worktree_path)]
    assert session.worktree_path == str(worktree_path)


def test_launch_command_checks_out_assigned_branch_before_git_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init")
    (repo_path / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo_path, "add", "app.txt")
    _git(repo_path, "commit", "-m", "initial")
    _git(repo_path, "branch", "-M", "main")
    _git(repo_path, "checkout", "-b", "feature/#77")
    _git(repo_path, "checkout", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_omx = bin_dir / "omx"
    fake_omx.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "git branch --show-current > branch.txt\n"
        "printf 'agent\\n' >> app.txt\n"
        "git add app.txt branch.txt\n"
        "git commit -m agent\n",
        encoding="utf-8",
    )
    fake_omx.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    runner = OmxRunner(run_dir=tmp_path / "runs")
    job = JobRecord(
        repo_full_name="acme/demo",
        stage="implementation",
        issue_number=77,
        metadata={"branch_name": "feature/#77"},
    )

    session = runner.launch(repo_path, job, "Implement issue 77.")
    try:
        runner.wait(session.runtime_handle, timeout_seconds=5)
    finally:
        runner.close_session(session.runtime_handle)

    assert _git(repo_path, "show", "feature/#77:branch.txt").stdout.strip() == "feature/#77"
    assert _git(repo_path, "show", "main:branch.txt", check=False).returncode != 0
    assert _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature/#77"


def test_build_resume_script_uses_omx_exec_resume(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    script = runner._build_resume_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        omx_session_id="session-123",
    )

    assert "omx exec resume session-123 --dangerously-bypass-approvals-and-sandbox" in script


def test_wait_raises_rollout_missing_error_when_resume_stderr_mentions_no_rollout_found(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    runtime_handle = "runtime-123"
    runtime_dir = runner.run_dir / runtime_handle
    runtime_dir.mkdir(parents=True)
    stdout_path = tmp_path / "stdout.log"
    stderr_path = runtime_dir / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text(
        "Error: thread/resume failed: no rollout found for thread id 019d6829",
        encoding="utf-8",
    )
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("a", encoding="utf-8")
    process = type(
        "Process",
        (),
        {
            "poll": lambda self: 1,
            "terminate": lambda self: None,
            "wait": lambda self, timeout=None: 1,
            "kill": lambda self: None,
        },
    )()
    runner._processes[runtime_handle] = (process, stdout_file, stderr_file)

    try:
        with pytest.raises(RolloutMissingError, match="no rollout found"):
            runner.wait(runtime_handle)
    finally:
        stdout_file.close()
        stderr_file.close()


def test_omx_can_resume_uuid_like_session_id(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    assert runner.can_resume("019da16a-565d-7c81-98c9-4b7ff38a3f9b") is True


def test_omx_can_resume_rejects_opencode_prefixed_session_id(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    assert runner.can_resume("ses_25afdf9c7ffekN3dovMQw6meL2") is False


def test_omx_can_resume_rejects_empty_or_none_session_id(tmp_path: Path) -> None:
    runner = OmxRunner(run_dir=tmp_path / "runs")
    assert runner.can_resume("") is False
