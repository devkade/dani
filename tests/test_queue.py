import threading
import time

from dani.models import JobRecord
from dani.queue import RepoQueueManager


def test_queue_is_serial_per_repo_and_parallel_across_repos() -> None:
    records: list[tuple[str, str, float]] = []
    lock = threading.Lock()
    started = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            records.append((job.repo_full_name, "start", time.perf_counter()))
            if len([entry for entry in records if entry[1] == "start"]) >= 2:
                started.set()
        if job.repo_full_name == "acme/repo-a" and job.issue_number == 1:
            time.sleep(0.2)
        elif job.repo_full_name == "acme/repo-b":
            started.wait(timeout=1)
        with lock:
            records.append((job.repo_full_name, "end", time.perf_counter()))

    manager = RepoQueueManager(handler, repo_concurrency=1)
    manager.submit(JobRecord(repo_full_name="acme/repo-a", stage="issue_request", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo-a", stage="implementation", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/repo-b", stage="issue_request", issue_number=9))
    manager.join_all()

    repo_a_starts = [ts for repo, phase, ts in records if repo == "acme/repo-a" and phase == "start"]
    repo_a_ends = [ts for repo, phase, ts in records if repo == "acme/repo-a" and phase == "end"]
    repo_b_start = next(ts for repo, phase, ts in records if repo == "acme/repo-b" and phase == "start")

    assert repo_a_starts[1] >= repo_a_ends[0]
    assert repo_b_start < repo_a_ends[0]


def test_queue_snapshot_includes_role_metadata() -> None:
    release = threading.Event()
    started = threading.Event()

    def handler(job: JobRecord) -> None:
        started.set()
        assert release.wait(timeout=2)

    manager = RepoQueueManager(handler, repo_concurrency=1)
    manager.submit(JobRecord(repo_full_name="acme/demo", stage="issue_request", role="reviewer", issue_number=1))
    assert started.wait(timeout=2)

    snapshot = manager.snapshot()
    running = snapshot["repos"]["acme/demo"]["running"][0]
    assert running["role"] == "reviewer"

    release.set()
    manager.join_all()


def test_repo_queue_concurrency_one_runs_jobs_fifo_across_issues() -> None:
    handled: list[tuple[str, int | None]] = []

    def handler(job: JobRecord) -> None:
        handled.append((job.stage, job.issue_number))

    manager = RepoQueueManager(handler, repo_concurrency=1)
    manager.submit(JobRecord(repo_full_name="acme/demo", stage="review_round", issue_number=1))
    manager.submit(JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=2))

    manager.join_all()

    assert handled == [("review_round", 1), ("implementation", 2)]


def test_same_repo_different_line_ids_run_concurrently_in_isolated_worktrees(tmp_path) -> None:
    started: list[str] = []
    worktrees: list[str] = []
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            started.append(str(job.metadata["line_id"]))
            worktrees.append(str(job.metadata["worktree_path"]))
            if len(started) == 2:
                both_started.set()
        assert release.wait(timeout=2)

    wt_1 = str(tmp_path / "wt-1")
    wt_2 = str(tmp_path / "wt-2")

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(
        JobRecord(
            repo_full_name="acme/demo",
            stage="implementation",
            issue_number=1,
            metadata={"line_id": "issue-1", "worktree_path": wt_1, "branch_name": "feature/#1"},
        )
    )
    manager.submit(
        JobRecord(
            repo_full_name="acme/demo",
            stage="implementation",
            issue_number=2,
            metadata={"line_id": "issue-2", "worktree_path": wt_2, "branch_name": "feature/#2"},
        )
    )

    assert both_started.wait(timeout=1)
    snapshot = manager.snapshot()
    release.set()
    manager.join_all()

    assert set(started) == {"issue-1", "issue-2"}
    assert set(worktrees) == {wt_1, wt_2}
    repo_snapshot = snapshot["repos"]["acme/demo"]
    assert repo_snapshot["active_worker_count"] == 2
    assert {job["lock_key"] for job in repo_snapshot["running"]} == {"line:issue-1", "line:issue-2"}
    assert {job["worktree_path"] for job in repo_snapshot["running"]} == {wt_1, wt_2}


def test_same_repo_same_line_id_runs_sequentially() -> None:
    events: list[tuple[str, str]] = []
    lock = threading.Lock()
    first_started = threading.Event()
    second_started_before_release = threading.Event()
    release_first = threading.Event()

    def handler(job: JobRecord) -> None:
        line_id = str(job.metadata["line_id"])
        with lock:
            events.append((job.id, "start"))
            if len([event for event in events if event[1] == "start"]) == 1:
                first_started.set()
            else:
                second_started_before_release.set()
        if job.id == "first":
            assert release_first.wait(timeout=2)
        with lock:
            events.append((job.id, "end"))
        assert line_id == "issue-1"

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(
        JobRecord(
            id="first", repo_full_name="acme/demo", stage="review_round", pr_number=10, metadata={"line_id": "issue-1"}
        )
    )
    manager.submit(
        JobRecord(
            id="second",
            repo_full_name="acme/demo",
            stage="implementation",
            pr_number=10,
            metadata={"line_id": "issue-1"},
        )
    )

    assert first_started.wait(timeout=1)
    assert not second_started_before_release.wait(timeout=0.15)
    release_first.set()
    manager.join_all()

    assert events == [("first", "start"), ("first", "end"), ("second", "start"), ("second", "end")]


def test_issue_backed_implementation_and_line_review_share_canonical_lock() -> None:
    events: list[tuple[str, str]] = []
    lock = threading.Lock()
    implementation_started = threading.Event()
    review_started_before_release = threading.Event()
    release_implementation = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            events.append((job.stage, "start"))
            if job.stage == "implementation":
                implementation_started.set()
            elif job.stage == "review_round":
                review_started_before_release.set()
        if job.stage == "implementation":
            assert release_implementation.wait(timeout=2)
        with lock:
            events.append((job.stage, "end"))

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=12))
    manager.submit(
        JobRecord(
            repo_full_name="acme/demo",
            stage="review_round",
            issue_number=12,
            pr_number=99,
            metadata={"line_id": "issue-12"},
        )
    )

    assert implementation_started.wait(timeout=1)
    assert not review_started_before_release.wait(timeout=0.15)
    snapshot = manager.snapshot()["repos"]["acme/demo"]
    release_implementation.set()
    manager.join_all()

    assert snapshot["running"][0]["lock_key"] == "line:issue-12"
    assert events == [
        ("implementation", "start"),
        ("implementation", "end"),
        ("review_round", "start"),
        ("review_round", "end"),
    ]


def test_repo_wide_locked_stage_blocks_conflicting_line_jobs() -> None:
    events: list[tuple[str, str]] = []
    lock = threading.Lock()
    dev_sync_started = threading.Event()
    implementation_started_before_release = threading.Event()
    release_dev_sync = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            events.append((job.stage, "start"))
            if job.stage == "dev_sync":
                dev_sync_started.set()
            elif job.stage == "implementation":
                implementation_started_before_release.set()
        if job.stage == "dev_sync":
            assert release_dev_sync.wait(timeout=2)
        with lock:
            events.append((job.stage, "end"))

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(JobRecord(repo_full_name="acme/demo", stage="dev_sync", metadata={"main_sha": "abc"}))
    manager.submit(
        JobRecord(repo_full_name="acme/demo", stage="implementation", issue_number=1, metadata={"line_id": "issue-1"})
    )

    assert dev_sync_started.wait(timeout=1)
    assert not implementation_started_before_release.wait(timeout=0.15)
    release_dev_sync.set()
    manager.join_all()

    assert events == [
        ("dev_sync", "start"),
        ("dev_sync", "end"),
        ("implementation", "start"),
        ("implementation", "end"),
    ]


def test_pending_repo_wide_stage_is_a_barrier_for_later_line_jobs() -> None:
    events: list[tuple[str, str]] = []
    lock = threading.Lock()
    line_one_started = threading.Event()
    line_two_started_before_barrier = threading.Event()
    dev_sync_started = threading.Event()
    release_line_one = threading.Event()
    release_dev_sync = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            events.append((job.id, "start"))
            if job.id == "line-1":
                line_one_started.set()
            elif job.id == "line-2":
                line_two_started_before_barrier.set()
            elif job.id == "dev-sync":
                dev_sync_started.set()
        if job.id == "line-1":
            assert release_line_one.wait(timeout=2)
        elif job.id == "dev-sync":
            assert release_dev_sync.wait(timeout=2)
        with lock:
            events.append((job.id, "end"))

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(
        JobRecord(
            id="line-1",
            repo_full_name="acme/demo",
            stage="implementation",
            issue_number=1,
            metadata={"line_id": "issue-1"},
        )
    )
    assert line_one_started.wait(timeout=1)

    manager.submit(JobRecord(id="dev-sync", repo_full_name="acme/demo", stage="dev_sync"))
    manager.submit(
        JobRecord(
            id="line-2",
            repo_full_name="acme/demo",
            stage="implementation",
            issue_number=2,
            metadata={"line_id": "issue-2"},
        )
    )

    assert not line_two_started_before_barrier.wait(timeout=0.15)
    release_line_one.set()
    assert dev_sync_started.wait(timeout=1)
    assert not line_two_started_before_barrier.wait(timeout=0.15)
    snapshot = manager.snapshot()["repos"]["acme/demo"]
    release_dev_sync.set()
    manager.join_all()

    assert snapshot["repo_wide_active"] is True
    assert events == [
        ("line-1", "start"),
        ("line-1", "end"),
        ("dev-sync", "start"),
        ("dev-sync", "end"),
        ("line-2", "start"),
        ("line-2", "end"),
    ]


def test_merge_conflict_resolution_without_worktree_uses_repo_wide_lock() -> None:
    events: list[tuple[str, str]] = []
    lock = threading.Lock()
    first_started = threading.Event()
    second_started_before_release = threading.Event()
    release_first = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            events.append((job.id, "start"))
            if job.id == "first":
                first_started.set()
            elif job.id == "second":
                second_started_before_release.set()
        if job.id == "first":
            assert release_first.wait(timeout=2)
        with lock:
            events.append((job.id, "end"))

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(JobRecord(id="first", repo_full_name="acme/demo", stage="merge_conflict_resolution", pr_number=1))
    manager.submit(JobRecord(id="second", repo_full_name="acme/demo", stage="merge_conflict_resolution", pr_number=2))

    assert first_started.wait(timeout=1)
    assert not second_started_before_release.wait(timeout=0.15)
    release_first.set()
    manager.join_all()

    assert events == [("first", "start"), ("first", "end"), ("second", "start"), ("second", "end")]


def test_shared_checkout_external_pr_reviews_use_repo_wide_lock() -> None:
    events: list[tuple[str, str]] = []
    lock = threading.Lock()
    first_started = threading.Event()
    second_started_before_release = threading.Event()
    release_first = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            events.append((job.id, "start"))
            if job.id == "pr-1":
                first_started.set()
            elif job.id == "pr-2":
                second_started_before_release.set()
        if job.id == "pr-1":
            assert release_first.wait(timeout=2)
        with lock:
            events.append((job.id, "end"))

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(
        JobRecord(
            id="pr-1",
            repo_full_name="acme/demo",
            stage="review_round",
            pr_number=1,
            metadata={"external_contribution": True},
        )
    )
    manager.submit(
        JobRecord(
            id="pr-2",
            repo_full_name="acme/demo",
            stage="review_round",
            pr_number=2,
            metadata={"external_contribution": True},
        )
    )

    assert first_started.wait(timeout=1)
    assert not second_started_before_release.wait(timeout=0.15)
    snapshot = manager.snapshot()["repos"]["acme/demo"]
    release_first.set()
    manager.join_all()

    assert snapshot["running"][0]["lock_key"] == "repo"
    assert events == [("pr-1", "start"), ("pr-1", "end"), ("pr-2", "start"), ("pr-2", "end")]


def test_run_exclusive_waits_for_running_jobs_and_blocks_later_dispatch() -> None:
    events: list[str] = []
    lock = threading.Lock()
    first_started = threading.Event()
    exclusive_started = threading.Event()
    second_started_before_exclusive = threading.Event()
    release_first = threading.Event()
    release_exclusive = threading.Event()

    def record(event: str) -> None:
        with lock:
            events.append(event)

    def handler(job: JobRecord) -> None:
        record(f"{job.id}:start")
        if job.id == "first":
            first_started.set()
            assert release_first.wait(timeout=2)
        elif job.id == "second":
            second_started_before_exclusive.set()
        record(f"{job.id}:end")

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(
        JobRecord(id="first", repo_full_name="acme/demo", stage="implementation", metadata={"line_id": "issue-1"})
    )
    assert first_started.wait(timeout=1)

    exclusive_thread = threading.Thread(
        target=lambda: manager.run_exclusive(
            "acme/demo",
            lambda: (
                record("exclusive:start"),
                exclusive_started.set(),
                release_exclusive.wait(timeout=2),
                record("exclusive:end"),
            ),
        )
    )
    exclusive_thread.start()
    manager.submit(
        JobRecord(id="second", repo_full_name="acme/demo", stage="implementation", metadata={"line_id": "issue-2"})
    )

    assert not exclusive_started.wait(timeout=0.15)
    assert not second_started_before_exclusive.wait(timeout=0.15)
    release_first.set()
    assert exclusive_started.wait(timeout=1)
    assert not second_started_before_exclusive.wait(timeout=0.15)
    release_exclusive.set()
    exclusive_thread.join(timeout=2)
    manager.join_all()

    assert events == ["first:start", "first:end", "exclusive:start", "exclusive:end", "second:start", "second:end"]


def test_merge_conflict_resolution_with_line_id_but_without_worktree_uses_repo_wide_lock() -> None:
    events: list[tuple[str, str]] = []
    lock = threading.Lock()
    first_started = threading.Event()
    second_started_before_release = threading.Event()
    release_first = threading.Event()

    def handler(job: JobRecord) -> None:
        with lock:
            events.append((job.id, "start"))
            if job.id == "first":
                first_started.set()
            elif job.id == "second":
                second_started_before_release.set()
        if job.id == "first":
            assert release_first.wait(timeout=2)
        with lock:
            events.append((job.id, "end"))

    manager = RepoQueueManager(handler, repo_concurrency=2)
    manager.submit(
        JobRecord(
            id="first",
            repo_full_name="acme/demo",
            stage="merge_conflict_resolution",
            pr_number=1,
            metadata={"line_id": "issue-1"},
        )
    )
    manager.submit(
        JobRecord(
            id="second",
            repo_full_name="acme/demo",
            stage="merge_conflict_resolution",
            pr_number=2,
            metadata={"line_id": "issue-2"},
        )
    )

    assert first_started.wait(timeout=1)
    assert not second_started_before_release.wait(timeout=0.15)
    assert manager.snapshot()["repos"]["acme/demo"]["running"][0]["lock_key"] == "repo"
    release_first.set()
    manager.join_all()

    assert events == [("first", "start"), ("first", "end"), ("second", "start"), ("second", "end")]
