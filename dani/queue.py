from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dani.models import JobRecord

JobHandler = Callable[[JobRecord], Any]

_REPO_WIDE_LOCK_KEY = "repo"
_REPO_WIDE_STAGES = frozenset({"dev_sync", "final_verdict_merge"})
_ISOLATION_REQUIRED_STAGES = frozenset({"merge_conflict_resolution"})
_WORK_LINE_STAGES = frozenset({"implementation", "issue_followup", "review_round"})


@dataclass(slots=True)
class _QueuedJob:
    job: JobRecord
    lock_key: str


@dataclass(slots=True)
class _RunningJob:
    job: JobRecord
    lock_key: str
    worker_name: str


@dataclass(slots=True)
class _RepoScheduler:
    repo_full_name: str
    handler: JobHandler
    max_workers: int
    pending: deque[_QueuedJob] = field(default_factory=deque)
    running: dict[str, _RunningJob] = field(default_factory=dict)
    active_lock_keys: set[str] = field(default_factory=set)
    repo_wide_active: bool = False
    exclusive_waiting: int = 0
    unfinished_count: int = 0
    condition: threading.Condition = field(default_factory=lambda: threading.Condition(threading.RLock()))
    workers: list[threading.Thread] = field(default_factory=list)

    def start(self) -> None:
        for index in range(self.max_workers):
            thread = threading.Thread(
                target=self._worker,
                daemon=True,
                name=f"dani-worker-{self.repo_full_name.replace('/', '-')}-{index + 1}",
            )
            self.workers.append(thread)
            thread.start()

    def submit(self, job: JobRecord) -> None:
        lock_key = job_lock_key(job)
        with self.condition:
            self.unfinished_count += 1
            self.pending.append(_QueuedJob(job=job, lock_key=lock_key))
            self.condition.notify_all()

    def join(self) -> None:
        with self.condition:
            while self.unfinished_count > 0:
                self.condition.wait()

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return {
                "max_workers": self.max_workers,
                "active_worker_count": len(self.running),
                "queued_count": len(self.pending),
                "repo_wide_active": self.repo_wide_active,
                "exclusive_waiting": self.exclusive_waiting,
                "active_lock_keys": sorted(self.active_lock_keys),
                "running": [
                    self._job_snapshot(item.job, item.lock_key, worker_name=item.worker_name)
                    for item in self.running.values()
                ],
                "queued": [self._job_snapshot(item.job, item.lock_key) for item in self.pending],
            }

    def run_exclusive(self, callback: Callable[[], Any]) -> Any:
        with self.condition:
            self.exclusive_waiting += 1
            try:
                while self.running or self.repo_wide_active:
                    self.condition.wait()
                self.repo_wide_active = True
            finally:
                self.exclusive_waiting -= 1
                self.condition.notify_all()
        try:
            return callback()
        finally:
            with self.condition:
                self.repo_wide_active = False
                self.condition.notify_all()

    def _worker(self) -> None:
        while True:
            queued = self._take_next_dispatchable()
            try:
                try:
                    self.handler(queued.job)
                except Exception:
                    queued.job.status = "failed"
            finally:
                self._finish(queued)

    def _take_next_dispatchable(self) -> _QueuedJob:
        with self.condition:
            while True:
                for index, queued in enumerate(self.pending):
                    if self._is_dispatchable(queued):
                        self.pending.remove(queued)
                        self.running[queued.job.id] = _RunningJob(
                            job=queued.job,
                            lock_key=queued.lock_key,
                            worker_name=threading.current_thread().name,
                        )
                        if queued.lock_key == _REPO_WIDE_LOCK_KEY:
                            self.repo_wide_active = True
                        else:
                            self.active_lock_keys.add(queued.lock_key)
                        return queued
                    if queued.lock_key == _REPO_WIDE_LOCK_KEY:
                        break
                    if index == 0 and self.repo_wide_active:
                        break
                self.condition.wait()

    def _is_dispatchable(self, queued: _QueuedJob) -> bool:
        if self.exclusive_waiting > 0:
            return False
        if self.repo_wide_active:
            return False
        if queued.lock_key == _REPO_WIDE_LOCK_KEY:
            return not self.running
        return queued.lock_key not in self.active_lock_keys

    def _finish(self, queued: _QueuedJob) -> None:
        with self.condition:
            self.running.pop(queued.job.id, None)
            if queued.lock_key == _REPO_WIDE_LOCK_KEY:
                self.repo_wide_active = False
            else:
                self.active_lock_keys.discard(queued.lock_key)
            self.unfinished_count -= 1
            self.condition.notify_all()

    @staticmethod
    def _job_snapshot(job: JobRecord, lock_key: str, *, worker_name: str | None = None) -> dict[str, Any]:
        snapshot = {
            "id": job.id,
            "stage": job.stage,
            "role": job.role,
            "status": job.status,
            "issue_number": job.issue_number,
            "pr_number": job.pr_number,
            "lock_key": lock_key,
            "line_id": job.metadata.get("line_id"),
            "worktree_path": job.metadata.get("worktree_path"),
            "branch_name": job.metadata.get("branch_name"),
        }
        for key in ("issue_readiness_state", "launch_gate_state", "pr_review_state"):
            if key in job.metadata:
                snapshot[key] = job.metadata.get(key)
        if worker_name is not None:
            snapshot["worker_name"] = worker_name
        return snapshot


def job_lock_key(job: JobRecord) -> str:
    if job.stage in _REPO_WIDE_STAGES or job.metadata.get("repo_wide_lock") is True:
        return _REPO_WIDE_LOCK_KEY
    if job.stage in _ISOLATION_REQUIRED_STAGES and not job.metadata.get("worktree_path"):
        return _REPO_WIDE_LOCK_KEY
    if (
        job.stage in _WORK_LINE_STAGES
        and job.metadata.get("external_contribution") is True
        and not job.metadata.get("worktree_path")
    ):
        return _REPO_WIDE_LOCK_KEY
    line_id = job.metadata.get("line_id")
    if line_id:
        return f"line:{line_id}"
    if job.stage in _WORK_LINE_STAGES:
        if job.pr_number is not None:
            return f"line:pr-{job.pr_number}"
        if job.issue_number is not None:
            return f"line:issue-{job.issue_number}"
    if job.pr_number is not None:
        return f"pr:{job.pr_number}"
    if job.issue_number is not None:
        return f"issue:{job.issue_number}"
    return _REPO_WIDE_LOCK_KEY


class RepoQueueManager:
    def __init__(self, handler: JobHandler, repo_concurrency: int = 1) -> None:
        if repo_concurrency < 1:
            msg = "repo_concurrency must be at least 1"
            raise ValueError(msg)
        self._handler = handler
        self._repo_concurrency = repo_concurrency
        self._schedulers: dict[str, _RepoScheduler] = {}
        self._lock = threading.RLock()

    def submit(self, job: JobRecord) -> None:
        with self._lock:
            scheduler = self._schedulers.get(job.repo_full_name)
            if scheduler is None:
                scheduler = _RepoScheduler(
                    repo_full_name=job.repo_full_name,
                    handler=self._handler,
                    max_workers=self._repo_concurrency,
                )
                self._schedulers[job.repo_full_name] = scheduler
                scheduler.start()
        scheduler.submit(job)

    def join_all(self) -> None:
        with self._lock:
            schedulers = list(self._schedulers.values())
        for scheduler in schedulers:
            scheduler.join()

    def run_exclusive(self, repo_full_name: str, callback: Callable[[], Any]) -> Any:
        with self._lock:
            scheduler = self._schedulers.get(repo_full_name)
            if scheduler is None:
                scheduler = _RepoScheduler(
                    repo_full_name=repo_full_name,
                    handler=self._handler,
                    max_workers=self._repo_concurrency,
                )
                self._schedulers[repo_full_name] = scheduler
                scheduler.start()
        return scheduler.run_exclusive(callback)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            schedulers = dict(self._schedulers)
        return {
            "repo_concurrency": self._repo_concurrency,
            "repos": {repo_full_name: scheduler.snapshot() for repo_full_name, scheduler in schedulers.items()},
        }
