from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

RUNTIME_OMX = "omx"
RUNTIME_OMO = "omo"
RUNTIME_GJC = "gjc"
DEFAULT_AGENT_TIMEOUT_SECONDS = 3600.0


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def infer_runtime_from_session_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    if session_id.startswith("gjc-") or "/.gjc/agent/sessions/" in session_id:
        return RUNTIME_GJC
    if session_id.startswith("ses_"):
        return RUNTIME_OMO
    return RUNTIME_OMX


@dataclass(slots=True)
class RepoConfig:
    full_name: str
    local_path: str
    main_branch: str = "main"
    dev_branch: str = "dev"
    enabled: bool = True
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class JobRecord:
    repo_full_name: str
    stage: str
    role: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    review_round: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "queued"
    session_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SessionRecord:
    repo_full_name: str
    stage: str
    runtime_handle: str
    prompt_path: str
    script_path: str
    worktree_path: str
    job_id: str
    role: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    review_round: int | None = None
    omx_session_id: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    preferred_runtime: str | None = None
    effective_runtime: str | None = None
    native_session_runtime: str | None = None
    fallback_reason: str | None = None
    bridge_source_runtime: str | None = None
    bridge_source_session_id: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "launched"
    ended_at: str | None = None
    termination_reason: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkLineRecord:
    repo_full_name: str
    line_id: str
    issue_id: str = ""
    pr_id: str = ""
    branch_name: str = ""
    worktree_path: str = ""
    repo_path: str = ""
    status: str = "initialized"
    agent_run_ids: list[str] = field(default_factory=list)
    review_state: str = ""
    auto_merge_state: str = ""
    cleanup_state: str = "preserved"
    cleanup_error: str = ""
    retryable: bool = True
    error: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def effective_session_runtime(session: SessionRecord) -> str | None:
    if session.effective_runtime:
        return session.effective_runtime
    if session.native_session_runtime:
        return session.native_session_runtime
    return infer_runtime_from_session_id(session.omx_session_id)


@dataclass(slots=True)
class NormalizedEvent:
    kind: str
    repo_full_name: str
    action: str
    number: int
    actor_login: str
    payload: dict[str, Any]
    body: str | None = None
    title: str | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    delivery_id: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    is_pull_request: bool = False
    issue_state: str | None = None
    pr_state: str | None = None
    pr_merged: bool | None = None
    actor_type: str | None = None


DEFAULT_MAX_ISSUE_FOLLOWUPS = 3
ISSUE_READY_LAUNCH_MANUAL = "manual"
ISSUE_READY_LAUNCH_AUTO = "auto"
ISSUE_READY_LAUNCH_MODES = frozenset({ISSUE_READY_LAUNCH_MANUAL, ISSUE_READY_LAUNCH_AUTO})


@dataclass(slots=True)
class DaniConfig:
    data_dir: Path
    webhook_secret: str
    host: str = "127.0.0.1"
    port: int = 8787
    review_rounds: int = 3
    agent_runtime: str = "omx"
    role_bindings: dict[str, Any] = field(default_factory=dict)
    agent_timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS
    bot_login: str | None = None
    max_issue_followups: int = DEFAULT_MAX_ISSUE_FOLLOWUPS
    repo_concurrency: int = 1
    issue_ready_launch: str = ISSUE_READY_LAUNCH_MANUAL

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.json"

    @property
    def jobs_path(self) -> Path:
        return self.data_dir / "jobs.json"

    @property
    def sessions_path(self) -> Path:
        return self.data_dir / "sessions.json"

    @property
    def events_path(self) -> Path:
        return self.data_dir / "events.jsonl"

    @property
    def processed_events_path(self) -> Path:
        return self.data_dir / "processed-events.json"

    @property
    def terminal_targets_path(self) -> Path:
        return self.data_dir / "terminal-targets.json"

    @property
    def work_lines_path(self) -> Path:
        return self.data_dir / "work-lines.json"

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "runs"
