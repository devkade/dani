from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import threading
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from dani.agent_runner import ManagedProcess
from dani.errors import check_rollout_missing_error, check_transient_capacity_error
from dani.models import DEFAULT_AGENT_TIMEOUT_SECONDS, JobRecord, SessionRecord

__all__ = ["HermesRunner"]


class HermesRunner:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self._processes: dict[str, tuple[ManagedProcess, TextIO, TextIO]] = {}
        self._process_groups: dict[str, int] = {}
        self._profiles: dict[str, str | None] = {}
        self._lock = threading.RLock()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def launch(self, repo_path: Path, job: JobRecord, prompt: str) -> SessionRecord:
        process_handle = self._process_handle(job)
        session_dir = self.run_dir / process_handle
        prompt_path, script_path, stdout_path, stderr_path = self._prepare_session_files(session_dir, prompt)
        profile = self._profile_for_job(job)
        script_path.write_text(
            self._build_script(
                repo_path=repo_path,
                prompt_path=prompt_path,
                profile=profile,
                branch_name=self._branch_name_for_job(job),
            ),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        with self._lock:
            self._profiles[process_handle] = profile
        self._start_process(process_handle, script_path, repo_path, stdout_path, stderr_path)
        return self._session_record(job, repo_path, process_handle, prompt_path, script_path, stdout_path, stderr_path, profile)

    def resume(self, repo_path: Path, job: JobRecord, prompt: str, omx_session_id: str) -> SessionRecord:
        del repo_path, job, prompt, omx_session_id
        msg = "hermes runtime does not support resume yet"
        raise RuntimeError(msg)

    def _process_handle(self, job: JobRecord) -> str:
        return f"dani-hermes-{job.stage}-{uuid4().hex[:10]}"

    def _prepare_session_files(self, session_dir: Path, prompt: str) -> tuple[Path, Path, Path, Path]:
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        script_path = session_dir / "run.sh"
        stdout_path = session_dir / "stdout.log"
        stderr_path = session_dir / "stderr.log"
        prompt_path.write_text(prompt, encoding="utf-8")
        return prompt_path, script_path, stdout_path, stderr_path

    def _start_process(
        self, process_handle: str, script_path: Path, repo_path: Path, stdout_path: Path, stderr_path: Path
    ) -> None:
        stdout_file = stdout_path.open("w", encoding="utf-8")
        stderr_file = stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen(  # noqa: S603
            [str(script_path)],
            cwd=str(repo_path),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        with self._lock:
            self._processes[process_handle] = (process, stdout_file, stderr_file)
            self._remember_process_group(process_handle, process)

    def _session_record(
        self,
        job: JobRecord,
        repo_path: Path,
        process_handle: str,
        prompt_path: Path,
        script_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        profile: str | None,
    ) -> SessionRecord:
        return SessionRecord(
            repo_full_name=job.repo_full_name,
            stage=job.stage,
            runtime_handle=process_handle,
            prompt_path=str(prompt_path),
            script_path=str(script_path),
            worktree_path=str(repo_path),
            job_id=job.id,
            role=job.role,
            issue_number=job.issue_number,
            pr_number=job.pr_number,
            review_round=job.review_round,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            hermes_profile=profile,
        )

    def _build_script(
        self, *, repo_path: Path, prompt_path: Path, profile: str | None = None, branch_name: str | None = None
    ) -> str:
        quoted_repo = shlex.quote(str(repo_path))
        quoted_prompt = shlex.quote(str(prompt_path))
        branch_guard = self._build_branch_guard(branch_name)
        profile_args = f"-p {shlex.quote(profile)} " if profile else ""
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"cd {quoted_repo}\n"
            f"{branch_guard}"
            f'exec hermes {profile_args}chat -q "$(cat {quoted_prompt})"\n'
        )

    def _profile_for_job(self, job: JobRecord) -> str | None:
        profile = job.metadata.get("hermes_profile")
        return profile if isinstance(profile, str) and profile else None

    def _branch_name_for_job(self, job: JobRecord) -> str | None:
        branch_name = job.metadata.get("branch_name")
        return branch_name if isinstance(branch_name, str) and branch_name else None

    def _build_branch_guard(self, branch_name: str | None) -> str:
        if not branch_name:
            return ""
        quoted_branch = shlex.quote(branch_name)
        return (
            f"expected_branch={quoted_branch}\n"
            'current_branch="$(git branch --show-current)"\n'
            'if [ "$current_branch" != "$expected_branch" ]; then\n'
            '  git checkout --quiet "$expected_branch"\n'
            '  current_branch="$(git branch --show-current)"\n'
            "fi\n"
            'if [ "$current_branch" != "$expected_branch" ]; then\n'
            '  echo "dani branch context mismatch: expected $expected_branch, got $current_branch" >&2\n'
            "  exit 1\n"
            "fi\n"
        )

    def wait(
        self, runtime_handle: str, *, poll_interval: float = 0.5, timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS
    ) -> None:
        del poll_interval
        with self._lock:
            entry = self._processes.get(runtime_handle)
            profile = self._profiles.get(runtime_handle)
        if entry is None:
            return
        process, _, _ = entry
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self.close_session(runtime_handle)
            msg = f"hermes process did not exit before timeout: {runtime_handle}"
            raise TimeoutError(msg) from exc

        self._check_logs_for_errors(runtime_handle)
        returncode = process.poll()
        if isinstance(returncode, int) and returncode != 0:
            profile_text = f" for profile {profile!r}" if profile else ""
            stderr_path = self.run_dir / runtime_handle / "stderr.log"
            msg = f"hermes process failed with exit code {returncode}{profile_text}; stderr: {stderr_path}"
            raise RuntimeError(msg)

    def _check_logs_for_errors(self, runtime_handle: str) -> None:
        session_dir = self.run_dir / runtime_handle
        text_parts: list[str] = []
        for name in ("stdout.log", "stderr.log"):
            path = session_dir / name
            if path.exists():
                text_parts.append(path.read_text(encoding="utf-8", errors="replace"))
        text = "\n".join(text_parts)
        if not text:
            return
        check_rollout_missing_error(text)
        check_transient_capacity_error(text)

    def close_session(self, runtime_handle: str) -> None:
        with self._lock:
            entry = self._processes.pop(runtime_handle, None)
            process_group_id = self._process_groups.pop(runtime_handle, None)
            self._profiles.pop(runtime_handle, None)
        if entry is None:
            return
        process, stdout_file, stderr_file = entry
        try:
            if process_group_id is not None:
                self._signal_process_group(process_group_id, signal.SIGTERM)
            elif process.poll() is None:
                process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
            if process.poll() is None:
                if process_group_id is not None:
                    self._signal_process_group(process_group_id, signal.SIGKILL)
                else:
                    process.kill()
        finally:
            stdout_file.close()
            stderr_file.close()

    def _remember_process_group(self, process_handle: str, process: ManagedProcess) -> None:
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            self._process_groups[process_handle] = os.getpgid(pid)
        except OSError:
            return

    def _signal_process_group(self, process_group_id: int, sig: signal.Signals) -> None:
        try:
            os.killpg(process_group_id, sig)
        except ProcessLookupError:
            return

    def get_session_id(self, runtime_handle: str) -> str | None:
        del runtime_handle
        return None

    def can_resume(self, session_id: str) -> bool:
        del session_id
        return False
