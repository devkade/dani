from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dani.agent_runner import build_agent_runner, normalize_runtime
from dani.hermes_runner import HermesRunner
from dani.models import RUNTIME_HERMES, JobRecord


class _Process:
    def __init__(self, *, returncode: int | None = 0) -> None:
        self.pid = 12345
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise subprocess.TimeoutExpired("hermes", 1)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_runtime_factory_accepts_hermes(tmp_path: Path) -> None:
    assert normalize_runtime("hermes") == RUNTIME_HERMES
    assert isinstance(build_agent_runner("hermes", tmp_path / "runs"), HermesRunner)


def test_build_script_uses_hermes_chat_with_profile(tmp_path: Path) -> None:
    runner = HermesRunner(run_dir=tmp_path / "runs")

    script = runner._build_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        profile="reviewer-profile",
    )

    assert "exec hermes -p reviewer-profile chat -q" in script
    assert "$(cat" in script


def test_can_resume_is_false_until_session_contract_exists(tmp_path: Path) -> None:
    runner = HermesRunner(run_dir=tmp_path / "runs")

    assert runner.can_resume("hermes-session-123") is False
    assert runner.get_session_id("runtime-123") is None


def test_launch_persists_run_artifacts_command_cwd_and_profile_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HermesRunner(run_dir=tmp_path / "runs")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    started: list[dict[str, object]] = []

    class PopenProcess(_Process):
        pass

    def fake_popen(args: list[str], **kwargs: object) -> PopenProcess:
        started.append({"args": args, **kwargs})
        return PopenProcess(returncode=0)

    monkeypatch.setattr("dani.hermes_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr("dani.hermes_runner.os.getpgid", lambda pid: 9876)
    job = JobRecord(
        repo_full_name="acme/demo",
        stage="review_round",
        role="reviewer",
        pr_number=7,
        metadata={"hermes_profile": "reviewer-profile"},
    )

    session = runner.launch(repo_path, job, "Review PR 7.")

    assert session.runtime_handle.startswith("dani-hermes-review_round-")
    assert session.hermes_profile == "reviewer-profile"
    assert session.stdout_path is not None
    assert session.stderr_path is not None
    assert Path(session.prompt_path).read_text(encoding="utf-8") == "Review PR 7."
    script = Path(session.script_path).read_text(encoding="utf-8")
    assert "exec hermes -p reviewer-profile chat -q" in script
    assert started[0]["cwd"] == str(repo_path)
    assert started[0]["stdin"] is subprocess.DEVNULL


def test_wait_nonzero_mentions_profile_and_stderr_path(tmp_path: Path) -> None:
    runner = HermesRunner(run_dir=tmp_path / "runs")
    session_dir = tmp_path / "runs" / "runtime-123"
    session_dir.mkdir(parents=True)
    stdout_file = (session_dir / "stdout.log").open("w", encoding="utf-8")
    stderr_file = (session_dir / "stderr.log").open("w", encoding="utf-8")
    process = _Process(returncode=2)
    runner._processes["runtime-123"] = (process, stdout_file, stderr_file)
    runner._profiles["runtime-123"] = "reviewer-profile"

    with pytest.raises(RuntimeError, match="hermes process failed with exit code 2 for profile 'reviewer-profile'"):
        runner.wait("runtime-123", timeout_seconds=0.01)

    stdout_file.close()
    stderr_file.close()
