from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from dani.cli import app
from dani.queue_inspection import build_queue_report, inspect_job, render_status_text


def _write_state(
    data_dir: Path, *, jobs: list[dict], sessions: list[dict] | None = None, work_lines: list[dict] | None = None
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "registry.json").write_text(
        json.dumps({
            "repos": [
                {
                    "full_name": "acme/demo",
                    "local_path": "/repo",
                    "main_branch": "main",
                    "dev_branch": "dev",
                    "enabled": True,
                }
            ]
        }),
        encoding="utf-8",
    )
    (data_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    (data_dir / "sessions.json").write_text(json.dumps({"sessions": sessions or []}), encoding="utf-8")
    (data_dir / "work-lines.json").write_text(json.dumps({"work_lines": work_lines or []}), encoding="utf-8")
    (data_dir / "processed-events.json").write_text(json.dumps({"keys": ["sig-a", "sig-a"]}), encoding="utf-8")
    (data_dir / "terminal-targets.json").write_text(
        json.dumps({"prs": [{"repo": "acme/demo", "pr": 7, "merged": False}], "issues": []}),
        encoding="utf-8",
    )
    (data_dir / "config.json").write_text(
        json.dumps({
            "role_bindings": {"worker": {"runtime": "gjc"}, "reviewer": {"runtime": "hermes", "profile": "warden"}}
        }),
        encoding="utf-8",
    )
    (data_dir / "events.jsonl").write_text(json.dumps({"kind": "issue_comment", "number": 3}) + "\n", encoding="utf-8")


def _job(job_id: str, stage: str, role: str, status: str = "queued", **extra) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "id": job_id,
        "repo_full_name": "acme/demo",
        "stage": stage,
        "role": role,
        "issue_number": extra.pop("issue_number", 3),
        "pr_number": extra.pop("pr_number", None),
        "review_round": extra.pop("review_round", None),
        "metadata": extra.pop("metadata", {}),
        "status": status,
        "session_id": extra.pop("session_id", None),
        "created_at": extra.pop("created_at", now),
        "updated_at": extra.pop("updated_at", now),
    }
    payload.update(extra)
    return payload


def test_queue_status_text_summarizes_health_and_lanes(tmp_path: Path) -> None:
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    jobs = [
        _job(
            "job-1",
            "implementation",
            "worker",
            pr_number=4,
            created_at=created_at,
            metadata={"route_reason": "review_round_changes_requested", "role_binding": {"runtime": "gjc"}},
        ),
        _job("job-2", "review_round", "reviewer", status="completed", pr_number=4),
    ]
    _write_state(tmp_path, jobs=jobs)

    report = build_queue_report(tmp_path)
    text = render_status_text(report)

    assert report["queue"]["queued"] == 1
    assert report["queue"]["failed_last_24h"] == 0
    assert "Dani queue health" in text
    assert "registered repos: 1" in text
    assert "stage: implementation" in text
    assert "role: worker" in text
    assert "runtime: gjc" in text
    assert "review_round_changes_requested" in text


def test_latest_active_lanes_show_newest_jobs_first(tmp_path: Path) -> None:
    base_time = datetime(2026, 6, 20, tzinfo=timezone.utc)
    jobs = [
        _job(
            f"job-{index}",
            "implementation",
            "worker",
            created_at=(base_time + timedelta(minutes=index)).isoformat(),
        )
        for index in range(12)
    ]
    _write_state(tmp_path, jobs=jobs)

    report = build_queue_report(tmp_path)

    assert [item["id"] for item in report["latest_active_lanes"]] == [
        "job-11",
        "job-10",
        "job-9",
        "job-8",
        "job-7",
        "job-6",
        "job-5",
        "job-4",
        "job-3",
        "job-2",
    ]


def test_storage_errors_make_queue_health_fail(tmp_path: Path) -> None:
    _write_state(tmp_path, jobs=[])
    (tmp_path / "jobs.json").write_text("{broken", encoding="utf-8")

    report = build_queue_report(tmp_path)

    assert report["health"] == "fail"
    assert report["storage_errors"]["jobs"]
    assert any(item["code"] == "storage_error" and item["file"] == "jobs" for item in report["failures"])


def test_queue_doctor_json_flags_role_routing_and_pr_anomalies(tmp_path: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    jobs = [
        _job("bad-role", "implementation", "reviewer"),
        _job(
            "bad-route",
            "implementation",
            "worker",
            pr_number=6,
            metadata={
                "source_event": {
                    "kind": "issue_comment",
                    "signature_stage": "review_round",
                    "review_verdict": "no_blockers_found",
                },
                "route_decision": {
                    "from": "review_round",
                    "to": "implementation",
                    "because": "review found no blockers",
                },
            },
        ),
        _job("closed-pr", "implementation", "worker", pr_number=7),
        _job("stale", "issue_followup", "planner", created_at=old),
    ]
    _write_state(tmp_path, jobs=jobs)

    result = CliRunner().invoke(
        app, ["queue", "doctor", "--json", "--data-dir", str(tmp_path), "--stuck-age-seconds", "3600"]
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    codes = {item["code"] for item in payload["failures"] + payload["warnings"]}
    assert "role_binding_mismatch" in codes
    assert "review_no_blockers_routed_to_implementation" in codes
    assert "active_worker_targets_terminal_pr" in codes
    assert "active_job_stuck" in codes
    assert "duplicate_processed_event" in codes


def test_queue_doctor_json_flags_closed_and_merged_worker_jobs(tmp_path: Path) -> None:
    jobs = [
        _job("closed-metadata", "implementation", "worker", metadata={"pr_state": "closed"}),
        _job("merged-metadata", "implementation", "worker", metadata={"pr_merged": True}),
        _job("closed-source-event", "implementation", "worker", metadata={"source_event": {"pr_state": "closed"}}),
        _job("merged-source-event", "implementation", "worker", metadata={"source_event": {"pr_merged": True}}),
    ]
    _write_state(tmp_path, jobs=jobs)

    result = CliRunner().invoke(app, ["queue", "doctor", "--json", "--data-dir", str(tmp_path)])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    findings = [item for item in payload["failures"] if item["code"] == "active_worker_targets_terminal_pr"]
    assert {item["job_id"] for item in findings} == {
        "closed-metadata",
        "merged-metadata",
        "closed-source-event",
        "merged-source-event",
    }


def test_inspect_job_renders_route_session_and_work_line(tmp_path: Path) -> None:
    jobs = [
        _job(
            "job-1",
            "implementation",
            "worker",
            pr_number=4,
            session_id="session-1",
            metadata={
                "line_id": "issue-3",
                "route_reason": "review_round_changes_requested",
                "source_event": {
                    "kind": "issue_comment",
                    "signature_stage": "review_round",
                    "signature_job": "review-job",
                },
                "route_decision": {
                    "from": "review_round",
                    "to": "implementation",
                    "because": "review requested changes",
                },
                "role_binding": {"runtime": "gjc", "profile": None},
            },
        )
    ]
    sessions = [
        {
            "id": "session-1",
            "repo_full_name": "acme/demo",
            "stage": "implementation",
            "runtime_handle": "gjc-run",
            "prompt_path": "/runs/job-1/prompt.md",
            "script_path": "/runs/job-1/run.sh",
            "worktree_path": "/worktrees/job-1",
            "job_id": "job-1",
            "role": "worker",
            "issue_number": 3,
            "pr_number": 4,
            "review_round": 1,
            "omx_session_id": "gjc-session",
            "stdout_path": "/runs/job-1/stdout.log",
            "stderr_path": "/runs/job-1/stderr.log",
            "preferred_runtime": "gjc",
            "effective_runtime": "gjc",
            "native_session_runtime": "gjc",
            "fallback_reason": None,
            "bridge_source_runtime": None,
            "bridge_source_session_id": None,
            "hermes_profile": None,
            "status": "launched",
            "ended_at": None,
            "termination_reason": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    work_lines = [
        {
            "repo_full_name": "acme/demo",
            "line_id": "issue-3",
            "status": "review_changes_requested",
            "issue_id": "3",
            "pr_id": "4",
        }
    ]
    _write_state(tmp_path, jobs=jobs, sessions=sessions, work_lines=work_lines)

    detail = inspect_job(tmp_path, "job-1")

    assert detail["job"]["route_reason"] == "review_round_changes_requested"
    assert detail["job"]["runtime"] == "gjc"
    assert detail["source_event"]["signature_job"] == "review-job"
    assert detail["route_decision"]["to"] == "implementation"
    assert detail["session"]["prompt_path"] == "/runs/job-1/prompt.md"
    assert detail["work_line"]["status"] == "review_changes_requested"
