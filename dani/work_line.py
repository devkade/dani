from __future__ import annotations

import contextlib
import re
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from dani.models import JobRecord, RepoConfig

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


@dataclass(slots=True)
class WorkLineContext:
    line_id: str
    issue_id: str
    pr_id: str
    branch_name: str
    worktree_path: Path
    repo_path: Path

    def metadata(self) -> dict[str, object]:
        return {
            "line_id": self.line_id,
            "issue_id": self.issue_id,
            "pr_id": self.pr_id,
            "branch_name": self.branch_name,
            "worktree_path": str(self.worktree_path),
            "repo_path": str(self.repo_path),
            "work_line_status": "worktree_ready",
            "agent_run_ids": [],
            "review_state": "",
            "auto_merge_state": "",
            "cleanup_state": "preserved",
            "cleanup_error": "",
            "retryable": True,
        }


class GitWorkLineManager:
    def __init__(self, run_dir: Path) -> None:
        self.worktrees_dir = run_dir / "worktrees"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._allocation_lock = threading.RLock()
        self._allocation_lock_path = self.worktrees_dir / ".allocation.lock"

    def prepare(self, repo: RepoConfig, job: JobRecord) -> WorkLineContext:
        with self._locked_allocation():
            context = self._context_for(repo, job)
            self._ensure_repo(context.repo_path)
            if context.worktree_path.exists():
                self._ensure_existing_worktree(context)
                return context

            base_ref = self._base_ref(context.repo_path, repo.dev_branch)
            if not self._branch_exists(context.repo_path, context.branch_name):
                self._run_git(context.repo_path, "branch", context.branch_name, base_ref)
            self._run_git(context.repo_path, "worktree", "add", str(context.worktree_path), context.branch_name)
            return context

    def planned_metadata(self, repo: RepoConfig, job: JobRecord) -> dict[str, object]:
        """Return deterministic work-line metadata without touching git state."""

        return self._context_for(repo, job).metadata()

    @contextlib.contextmanager
    def _locked_allocation(self) -> Iterator[None]:
        with self._allocation_lock, self._allocation_lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _context_for(self, repo: RepoConfig, job: JobRecord) -> WorkLineContext:
        issue_id = str(job.issue_number or job.metadata.get("issue_id") or "")
        pr_id = str(job.pr_number or job.metadata.get("pr_id") or "")
        line_id = str(job.metadata.get("line_id") or self._line_id(issue_id=issue_id, pr_id=pr_id, job=job))
        branch_name = str(job.metadata.get("branch_name") or job.metadata.get("head_branch") or self._branch_name(job))
        repo_path = Path(repo.local_path)
        worktree_path = Path(
            str(job.metadata.get("worktree_path") or self.worktrees_dir / self._safe_path(repo.full_name) / line_id)
        )
        return WorkLineContext(
            line_id=line_id,
            issue_id=issue_id,
            pr_id=pr_id,
            branch_name=branch_name,
            worktree_path=worktree_path,
            repo_path=repo_path,
        )

    def _line_id(self, *, issue_id: str, pr_id: str, job: JobRecord) -> str:
        if pr_id:
            return f"pr-{pr_id}"
        if issue_id:
            return f"issue-{issue_id}"
        return f"job-{job.id}"

    def _branch_name(self, job: JobRecord) -> str:
        if job.issue_number:
            return f"feature/#{job.issue_number}"
        if job.pr_number:
            return f"dani/pr-{job.pr_number}"
        return f"dani/job-{job.id}"

    def _safe_path(self, value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "repo"

    def _ensure_repo(self, repo_path: Path) -> None:
        result = self._run_git(repo_path, "rev-parse", "--is-inside-work-tree", check=False)
        if result.returncode != 0 or result.stdout.strip() != "true":
            msg = f"not-a-git-repository: {repo_path}"
            raise RuntimeError(msg)

    def _ensure_existing_worktree(self, context: WorkLineContext) -> None:
        result = self._run_git(context.worktree_path, "rev-parse", "--is-inside-work-tree", check=False)
        if result.returncode != 0 or result.stdout.strip() != "true":
            msg = f"worktree-path-exists-but-is-not-git-worktree: {context.worktree_path}"
            raise RuntimeError(msg)

    def _base_ref(self, repo_path: Path, dev_branch: str) -> str:
        for ref in (f"origin/{dev_branch}", dev_branch, "HEAD"):
            result = self._run_git(repo_path, "rev-parse", "--verify", ref, check=False)
            if result.returncode == 0:
                return ref
        msg = f"missing-base-ref: {dev_branch}"
        raise RuntimeError(msg)

    def _branch_exists(self, repo_path: Path, branch_name: str) -> bool:
        result = self._run_git(repo_path, "rev-parse", "--verify", f"refs/heads/{branch_name}", check=False)
        return result.returncode == 0

    def _run_git(self, repo_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_path), *args],  # noqa: S607
            check=check,
            capture_output=True,
            text=True,
        )
