from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from dani.agent_runner import build_agent_runner, normalize_runtime
from dani.gjc_runner import GjcRunner
from dani.models import RUNTIME_GJC, RUNTIME_OMX, JobRecord, infer_runtime_from_session_id


def test_runtime_factory_accepts_gjc(tmp_path: Path) -> None:
    assert normalize_runtime("gjc") == "gjc"
    assert normalize_runtime("gajae-code") == "gjc"
    assert isinstance(build_agent_runner("gjc", tmp_path / "runs"), GjcRunner)
    configured = build_agent_runner("gjc", tmp_path / "configured-runs", gjc_bin="/opt/gjc")
    assert isinstance(configured, GjcRunner)
    assert configured.gjc_bin == "/opt/gjc"


def test_runtime_inference_accepts_only_gjc_ids_and_gjc_session_paths() -> None:
    assert infer_runtime_from_session_id("gjc-session-123") == RUNTIME_GJC
    assert infer_runtime_from_session_id("/Users/me/.gjc/agent/sessions/session.jsonl") == RUNTIME_GJC
    assert infer_runtime_from_session_id("not-gjc/session.jsonl") == RUNTIME_OMX


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
            raise subprocess.TimeoutExpired("gjc", 1)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_build_script_uses_gjc_print_mode(tmp_path: Path) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs")

    script = runner._build_script(repo_path=tmp_path / "repo", prompt_path=tmp_path / "prompt.txt")

    assert "exec gjc -p" in script
    assert "$(cat" in script


def test_build_script_uses_configured_gjc_binary(tmp_path: Path) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs", gjc_bin="/Users/devkade/.bun/bin/gjc")

    script = runner._build_script(repo_path=tmp_path / "repo", prompt_path=tmp_path / "prompt.txt")

    assert "export PATH=/Users/devkade/.bun/bin:$PATH" in script
    assert "exec /Users/devkade/.bun/bin/gjc -p" in script


def test_build_script_resolves_configured_gjc_binary_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gjc = bin_dir / "custom-gjc"
    fake_gjc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gjc.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    runner = GjcRunner(run_dir=tmp_path / "runs", gjc_bin="custom-gjc")

    script = runner._build_script(repo_path=tmp_path / "repo", prompt_path=tmp_path / "prompt.txt")

    assert f"export PATH={bin_dir}:$PATH" in script
    assert f"exec {fake_gjc} -p" in script


def test_build_resume_script_uses_configured_gjc_binary(tmp_path: Path) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs", gjc_bin="/Users/devkade/.bun/bin/gjc")

    script = runner._build_resume_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        gjc_session_id="gjc-session-123",
    )

    assert "export PATH=/Users/devkade/.bun/bin:$PATH" in script
    assert "exec /Users/devkade/.bun/bin/gjc --resume gjc-session-123 -p" in script


def test_build_resume_script_resolves_configured_gjc_binary_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gjc = bin_dir / "custom-gjc"
    fake_gjc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gjc.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    runner = GjcRunner(run_dir=tmp_path / "runs", gjc_bin="custom-gjc")

    script = runner._build_resume_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        gjc_session_id="gjc-session-123",
    )

    assert f"export PATH={bin_dir}:$PATH" in script
    assert f"exec {fake_gjc} --resume gjc-session-123 -p" in script


def test_launch_fails_fast_when_configured_gjc_binary_is_not_executable(tmp_path: Path) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs", gjc_bin=str(tmp_path / "missing-gjc"))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=7)

    with pytest.raises(RuntimeError, match="configured gjc binary is not executable"):
        runner.launch(repo_path, job, "Implement issue 7.")


def test_launch_falls_back_to_bare_gjc_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gjc = bin_dir / "gjc"
    fake_gjc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gjc.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    runner = GjcRunner(run_dir=tmp_path / "runs")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.setattr("dani.gjc_runner.subprocess.Popen", lambda *args, **kwargs: _Process(returncode=0))
    monkeypatch.setattr("dani.gjc_runner.os.getpgid", lambda pid: 9876)
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=7)

    session = runner.launch(repo_path, job, "Implement issue 7.")

    assert "exec gjc -p" in Path(session.script_path).read_text(encoding="utf-8")


def test_build_resume_script_uses_gjc_resume_and_print_mode(tmp_path: Path) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs")

    script = runner._build_resume_script(
        repo_path=tmp_path / "repo",
        prompt_path=tmp_path / "prompt.txt",
        gjc_session_id="gjc-session-123",
    )

    assert "exec gjc --resume gjc-session-123 -p" in script


def test_can_resume_rejects_omo_and_omx_session_ids(tmp_path: Path) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs")

    assert runner.can_resume("gjc-session-123") is True
    assert runner.can_resume("/Users/me/.gjc/agent/sessions/session.jsonl") is True
    assert runner.can_resume("not-gjc/session.jsonl") is False
    assert runner.can_resume("ses_abc") is False
    assert runner.can_resume("omx-abc") is False
    assert runner.can_resume("") is False


def test_get_session_id_parses_trustworthy_gjc_id(tmp_path: Path) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs")
    session_dir = tmp_path / "runs" / "runtime-123"
    session_dir.mkdir(parents=True)
    (session_dir / "stdout.log").write_text("done\ngjc_session_id=gjc-session-123\n", encoding="utf-8")

    assert runner.get_session_id("runtime-123") == "gjc-session-123"


def test_wait_raises_clear_error_on_nonzero_exit(tmp_path: Path) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs")
    session_dir = tmp_path / "runs" / "runtime-123"
    session_dir.mkdir(parents=True)
    stdout_file = (session_dir / "stdout.log").open("w", encoding="utf-8")
    stderr_file = (session_dir / "stderr.log").open("w", encoding="utf-8")
    process = _Process(returncode=127)
    runner._processes["runtime-123"] = (process, stdout_file, stderr_file)

    with pytest.raises(RuntimeError, match="gjc process failed with exit code 127"):
        runner.wait("runtime-123", timeout_seconds=0.01)

    stdout_file.close()
    stderr_file.close()


def test_close_session_signals_process_group_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = GjcRunner(run_dir=tmp_path / "runs")
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    stdout_file = stdout_path.open("r+", encoding="utf-8")
    stderr_file = stderr_path.open("r+", encoding="utf-8")
    process = _Process(returncode=0)
    sent_signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr("dani.gjc_runner.os.killpg", lambda pgid, sig: sent_signals.append((pgid, sig)))
    runner._processes["runtime-123"] = (process, stdout_file, stderr_file)
    runner._process_groups["runtime-123"] = 4321

    runner.close_session("runtime-123")

    assert sent_signals == [(4321, signal.SIGTERM)]
    assert stdout_file.closed
    assert stderr_file.closed


def test_launch_persists_run_artifacts_with_gjc_runtime_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gjc = tmp_path / "gjc"
    fake_gjc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gjc.chmod(0o755)
    runner = GjcRunner(run_dir=tmp_path / "runs", gjc_bin=str(fake_gjc))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    started: list[dict[str, object]] = []

    class PopenProcess(_Process):
        pass

    def fake_popen(args: list[str], **kwargs: object) -> PopenProcess:
        started.append({"args": args, **kwargs})
        return PopenProcess(returncode=0)

    monkeypatch.setattr("dani.gjc_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr("dani.gjc_runner.os.getpgid", lambda pid: 9876)
    job = JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=7)

    session = runner.launch(repo_path, job, "Implement issue 7.")

    assert session.runtime_handle.startswith("dani-gjc-implementation-")
    assert Path(session.prompt_path).read_text(encoding="utf-8") == "Implement issue 7."
    assert str(fake_gjc) in Path(session.script_path).read_text(encoding="utf-8")
    assert started[0]["cwd"] == str(repo_path)
