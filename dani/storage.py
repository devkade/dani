from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from dani.models import (
    DaniConfig,
    JobRecord,
    RepoConfig,
    SessionRecord,
    WorkLineRecord,
    effective_session_runtime,
    utc_now,
)


class JsonStorage:
    def __init__(self, config: DaniConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.run_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_json_file(self.config.registry_path, {"repos": []})
        self._ensure_json_file(self.config.jobs_path, {"jobs": []})
        self._ensure_json_file(self.config.sessions_path, {"sessions": []})
        self._ensure_json_file(self.config.processed_events_path, {"keys": []})
        self._ensure_json_file(self.config.terminal_targets_path, {"prs": [], "issues": []})
        self._ensure_json_file(self.config.work_lines_path, {"work_lines": []})
        if not self.config.events_path.exists():
            self.config.events_path.write_text("", encoding="utf-8")

    def _ensure_json_file(self, path: Path, default: dict[str, Any]) -> None:
        if path.exists():
            return
        path.write_text(json.dumps(default, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    def register_repo(self, repo: RepoConfig) -> None:
        with self._lock:
            payload = self._read_json(self.config.registry_path)
            repos = [item for item in payload["repos"] if item["full_name"] != repo.full_name]
            repos.append(repo.to_dict())
            payload["repos"] = sorted(repos, key=lambda item: item["full_name"])
            self._write_json(self.config.registry_path, payload)

    def get_repo(self, full_name: str) -> RepoConfig | None:
        with self._lock:
            payload = self._read_json(self.config.registry_path)
            for item in payload["repos"]:
                if item["full_name"] == full_name:
                    return RepoConfig(**item)
        return None

    def list_repos(self) -> list[RepoConfig]:
        with self._lock:
            payload = self._read_json(self.config.registry_path)
            return [RepoConfig(**item) for item in payload["repos"]]

    def create_job(self, job: JobRecord) -> JobRecord:
        with self._lock:
            payload = self._read_json(self.config.jobs_path)
            payload["jobs"].append(job.to_dict())
            self._write_json(self.config.jobs_path, payload)
        return job

    def update_job(self, job_id: str, **changes: Any) -> JobRecord:
        with self._lock:
            payload = self._read_json(self.config.jobs_path)
            for item in payload["jobs"]:
                if item["id"] != job_id:
                    continue
                item.update(changes)
                item["updated_at"] = utc_now()
                self._write_json(self.config.jobs_path, payload)
                return JobRecord(**item)
        msg = f"Unknown job id: {job_id}"
        raise KeyError(msg)

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            payload = self._read_json(self.config.jobs_path)
            for item in payload["jobs"]:
                if item["id"] == job_id:
                    return JobRecord(**item)
        return None

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            payload = self._read_json(self.config.jobs_path)
            return [JobRecord(**item) for item in payload["jobs"]]

    def find_jobs(
        self,
        *,
        repo_full_name: str | None = None,
        stage: str | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
    ) -> list[JobRecord]:
        jobs = self.list_jobs()
        filtered: list[JobRecord] = []
        for job in jobs:
            if repo_full_name is not None and job.repo_full_name != repo_full_name:
                continue
            if stage is not None and job.stage != stage:
                continue
            if issue_number is not None and job.issue_number != issue_number:
                continue
            if pr_number is not None and job.pr_number != pr_number:
                continue
            filtered.append(job)
        return filtered

    def create_session(self, session: SessionRecord) -> SessionRecord:
        with self._lock:
            payload = self._read_json(self.config.sessions_path)
            payload["sessions"].append(session.to_dict())
            self._write_json(self.config.sessions_path, payload)
        return session

    def update_session(self, session_id: str, **changes: Any) -> SessionRecord:
        with self._lock:
            payload = self._read_json(self.config.sessions_path)
            for item in payload["sessions"]:
                if item["id"] != session_id:
                    continue
                item.update(changes)
                item["updated_at"] = utc_now()
                self._write_json(self.config.sessions_path, payload)
                return SessionRecord(**item)
        msg = f"Unknown session id: {session_id}"
        raise KeyError(msg)

    def list_sessions(self) -> list[SessionRecord]:
        with self._lock:
            payload = self._read_json(self.config.sessions_path)
            return [SessionRecord(**item) for item in payload["sessions"]]

    def upsert_work_line(self, work_line: WorkLineRecord) -> WorkLineRecord:
        with self._lock:
            payload = self._read_json(self.config.work_lines_path)
            lines = payload["work_lines"]
            for item in lines:
                if item["repo_full_name"] == work_line.repo_full_name and item["line_id"] == work_line.line_id:
                    created_at = item.get("created_at", work_line.created_at)
                    item.update(work_line.to_dict())
                    item["created_at"] = created_at
                    item["updated_at"] = utc_now()
                    self._write_json(self.config.work_lines_path, payload)
                    return WorkLineRecord(**item)
            lines.append(work_line.to_dict())
            payload["work_lines"] = sorted(lines, key=lambda item: (item["repo_full_name"], item["line_id"]))
            self._write_json(self.config.work_lines_path, payload)
            return work_line

    def update_work_line(
        self,
        repo_full_name: str,
        line_id: str,
        *,
        agent_run_id: str | None = None,
        **changes: Any,
    ) -> WorkLineRecord:
        with self._lock:
            payload = self._read_json(self.config.work_lines_path)
            for item in payload["work_lines"]:
                if item["repo_full_name"] != repo_full_name or item["line_id"] != line_id:
                    continue
                item.update(changes)
                if agent_run_id:
                    agent_run_ids = [str(value) for value in item.get("agent_run_ids", [])]
                    if agent_run_id not in agent_run_ids:
                        agent_run_ids.append(agent_run_id)
                    item["agent_run_ids"] = agent_run_ids
                item["updated_at"] = utc_now()
                self._write_json(self.config.work_lines_path, payload)
                return WorkLineRecord(**item)
        msg = f"Unknown work line: {repo_full_name}#{line_id}"
        raise KeyError(msg)

    def get_work_line(self, repo_full_name: str, line_id: str) -> WorkLineRecord | None:
        with self._lock:
            payload = self._read_json(self.config.work_lines_path)
            for item in payload["work_lines"]:
                if item["repo_full_name"] == repo_full_name and item["line_id"] == line_id:
                    return WorkLineRecord(**item)
        return None

    def list_work_lines(self, *, repo_full_name: str | None = None) -> list[WorkLineRecord]:
        with self._lock:
            payload = self._read_json(self.config.work_lines_path)
            lines = [WorkLineRecord(**item) for item in payload["work_lines"]]
        if repo_full_name is None:
            return lines
        return [line for line in lines if line.repo_full_name == repo_full_name]

    def find_latest_session(
        self,
        *,
        repo_full_name: str,
        stage: str | None = None,
        job_id: str | None = None,
        issue_number: int | None = None,
        pr_number: int | None = None,
        require_omx_session_id: bool = False,
        effective_runtime: str | None = None,
    ) -> SessionRecord | None:
        for session in reversed(self.list_sessions()):
            if session.repo_full_name != repo_full_name:
                continue
            if stage is not None and session.stage != stage:
                continue
            if job_id is not None and session.job_id != job_id:
                continue
            if issue_number is not None and session.issue_number != issue_number:
                continue
            if pr_number is not None and session.pr_number != pr_number:
                continue
            if require_omx_session_id and not session.omx_session_id:
                continue
            if effective_runtime is not None and effective_session_runtime(session) != effective_runtime:
                continue
            return session
        return None

    def append_event(self, event: dict[str, Any]) -> None:
        with self._lock, self.config.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_processed_event(self, key: str) -> bool:
        with self._lock:
            payload = self._read_json(self.config.processed_events_path)
            if key in payload["keys"]:
                return False
            payload["keys"].append(key)
            self._write_json(self.config.processed_events_path, payload)
            return True

    def has_processed_event(self, key: str) -> bool:
        with self._lock:
            payload = self._read_json(self.config.processed_events_path)
            return key in payload["keys"]

    def mark_terminal_pr(self, repo_full_name: str, pr_number: int, *, merged: bool) -> None:
        with self._lock:
            payload = self._read_json(self.config.terminal_targets_path)
            entries = payload.setdefault("prs", [])
            for entry in entries:
                if entry.get("repo") == repo_full_name and entry.get("pr") == pr_number:
                    return
            entries.append({"repo": repo_full_name, "pr": pr_number, "merged": bool(merged)})
            self._write_json(self.config.terminal_targets_path, payload)

    def is_terminal_pr(self, repo_full_name: str, pr_number: int) -> bool:
        with self._lock:
            payload = self._read_json(self.config.terminal_targets_path)
            for entry in payload.get("prs", []):
                if entry.get("repo") == repo_full_name and entry.get("pr") == pr_number:
                    return True
        return False

    def mark_terminal_issue(self, repo_full_name: str, issue_number: int) -> None:
        with self._lock:
            payload = self._read_json(self.config.terminal_targets_path)
            entries = payload.setdefault("issues", [])
            for entry in entries:
                if entry.get("repo") == repo_full_name and entry.get("issue") == issue_number:
                    return
            entries.append({"repo": repo_full_name, "issue": issue_number})
            self._write_json(self.config.terminal_targets_path, payload)

    def is_terminal_issue(self, repo_full_name: str, issue_number: int) -> bool:
        with self._lock:
            payload = self._read_json(self.config.terminal_targets_path)
            for entry in payload.get("issues", []):
                if entry.get("repo") == repo_full_name and entry.get("issue") == issue_number:
                    return True
        return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "registry": self._read_json(self.config.registry_path),
                "jobs": self._read_json(self.config.jobs_path),
                "sessions": self._read_json(self.config.sessions_path),
                "work_lines": self._read_json(self.config.work_lines_path),
                "processed_events": self._read_json(self.config.processed_events_path),
                "terminal_targets": self._read_json(self.config.terminal_targets_path),
                "events_path": str(self.config.events_path),
            }
