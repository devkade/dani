from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ACTIVE_JOB_STATUSES = frozenset({"queued", "launched", "running", "retrying", "recovering"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "superseded"})
EXPECTED_ROLE_BY_STAGE = {
    "issue_readiness_review": "reviewer",
    "issue_followup": "planner",
    "implementation": "worker",
}
DEFAULT_STUCK_AGE_SECONDS = 3600


def build_queue_report(data_dir: Path, *, stuck_age_seconds: int = DEFAULT_STUCK_AGE_SECONDS) -> dict[str, Any]:
    state = _load_state(data_dir)
    now = datetime.now(timezone.utc)
    jobs = _items(state, "jobs", "jobs")
    sessions = _items(state, "sessions", "sessions")
    repos = _items(state, "registry", "repos")
    active_jobs = [job for job in jobs if job.get("status") in ACTIVE_JOB_STATUSES]
    failures, warnings = _find_findings(state, now=now, stuck_age_seconds=stuck_age_seconds)
    failures = _storage_error_findings(state) + failures
    recent_jobs = sorted(jobs, key=_job_sort_time, reverse=True)
    latest_active_jobs = sorted(active_jobs, key=_job_sort_time, reverse=True)
    latest_lanes = [_lane_summary(job, sessions=sessions, now=now) for job in latest_active_jobs[:10]]
    recent_transitions = [_transition_summary(job) for job in recent_jobs[:10]]

    return {
        "schema_version": 1,
        "data_dir": str(data_dir),
        "server": {
            "running": "unknown",
            "data_dir": str(data_dir),
            "registered_repos": len(repos),
        },
        "queue": {
            "queued": _count_status(jobs, {"queued"}),
            "launched_running": _count_status(jobs, {"launched", "running"}),
            "retrying_recovering": _count_status(jobs, {"retrying", "recovering"}),
            "failed_last_24h": _failed_last_24h(jobs, now),
            "stuck_active_jobs": sum(1 for item in warnings + failures if item["code"] == "active_job_stuck"),
            "active_jobs": len(active_jobs),
            "total_jobs": len(jobs),
        },
        "health": "fail" if failures else "warn" if warnings else "ok",
        "warnings": warnings,
        "failures": failures,
        "active_jobs": [_job_summary(job, now=now) for job in active_jobs],
        "latest_active_lanes": latest_lanes,
        "recent_transitions": recent_transitions,
        "storage_errors": state.get("errors", {}),
    }


def inspect_job(data_dir: Path, job_id: str) -> dict[str, Any]:
    state = _load_state(data_dir)
    now = datetime.now(timezone.utc)
    jobs = _items(state, "jobs", "jobs")
    job = next((item for item in jobs if item.get("id") == job_id), None)
    if job is None:
        msg = f"Unknown job id: {job_id}"
        raise KeyError(msg)
    metadata = _dict(job.get("metadata"))
    sessions = [item for item in _items(state, "sessions", "sessions") if item.get("job_id") == job_id]
    session = sessions[-1] if sessions else None
    work_line = _matching_work_line(state, job)
    detail = {
        "schema_version": 1,
        "job": _job_summary(job, now=now),
        "source_event": metadata.get("source_event"),
        "route_reason": metadata.get("route_reason"),
        "route_decision": metadata.get("route_decision"),
        "session": _session_summary(session) if session else None,
        "work_line": work_line,
        "metadata": metadata,
    }
    detail["job"].update({
        "session_id": job.get("session_id"),
        "route_reason": metadata.get("route_reason"),
        "runtime": _job_runtime(job, session),
        "profile": _job_profile(job, session),
    })
    return detail


def render_status_text(report: dict[str, Any]) -> str:
    server = _dict(report.get("server"))
    queue = _dict(report.get("queue"))
    lines = [
        "Dani queue health",
        "",
        "Server:",
        f"- running: {server.get('running', 'unknown')}",
        f"- data_dir: {server.get('data_dir', '')}",
        f"- registered repos: {server.get('registered_repos', 0)}",
        "",
        "Queue:",
        f"- queued: {queue.get('queued', 0)}",
        f"- launched/running: {queue.get('launched_running', 0)}",
        f"- retrying/recovering: {queue.get('retrying_recovering', 0)}",
        f"- failed last 24h: {queue.get('failed_last_24h', 0)}",
        f"- stuck active jobs: {queue.get('stuck_active_jobs', 0)}",
        "",
        "Latest active lanes:",
    ]
    lanes = list(report.get("latest_active_lanes") or [])
    if lanes:
        for lane in lanes:
            lines.extend([
                f"- repo: {lane.get('repo')}",
                f"  issue: {_number(lane.get('issue'))}",
                f"  pr: {_number(lane.get('pr'))}",
                f"  stage: {lane.get('stage')}",
                f"  role: {lane.get('role')}",
                f"  runtime: {lane.get('runtime') or '-'}",
                f"  profile: {lane.get('profile') or '-'}",
                f"  age: {lane.get('age')}",
            ])
    else:
        lines.append("- none")
    lines.extend(["", "Recent transitions:"])
    transitions = list(report.get("recent_transitions") or [])
    if transitions:
        for transition in transitions:
            lines.append(
                f"- {transition.get('source', 'event')} -> {transition.get('stage')} {transition.get('status')}"
                f" / {transition.get('reason', '-')}"
            )
    else:
        lines.append("- none")
    return "\n".join(lines)


def render_inspect_text(detail: dict[str, Any]) -> str:
    job = _dict(detail.get("job"))
    session = _dict(detail.get("session")) if detail.get("session") else None
    work_line = _dict(detail.get("work_line")) if detail.get("work_line") else None
    lines = [
        f"Dani job {job.get('id')}",
        f"- repo: {job.get('repo')}",
        f"- stage: {job.get('stage')}",
        f"- role: {job.get('role')}",
        f"- runtime: {job.get('runtime') or '-'}",
        f"- profile: {job.get('profile') or '-'}",
        f"- status: {job.get('status')}",
        f"- age: {job.get('age')}",
        f"- issue: {_number(job.get('issue'))}",
        f"- pr: {_number(job.get('pr'))}",
        f"- route_reason: {detail.get('route_reason') or '-'}",
        f"- source_event: {json.dumps(detail.get('source_event'), ensure_ascii=False, sort_keys=True)}",
        f"- route_decision: {json.dumps(detail.get('route_decision'), ensure_ascii=False, sort_keys=True)}",
    ]
    if session:
        lines.extend([
            "Session:",
            f"- id: {session.get('id')}",
            f"- prompt_path: {session.get('prompt_path')}",
            f"- script_path: {session.get('script_path')}",
            f"- worktree_path: {session.get('worktree_path')}",
            f"- stdout_path: {session.get('stdout_path')}",
            f"- stderr_path: {session.get('stderr_path')}",
        ])
    if work_line:
        lines.extend(["Work line:", f"- line_id: {work_line.get('line_id')}", f"- status: {work_line.get('status')}"])
    return "\n".join(lines)


def _find_findings(
    state: dict[str, Any], *, now: datetime, stuck_age_seconds: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    jobs = _items(state, "jobs", "jobs")
    terminal_prs = _terminal_pr_targets(state)

    for job in jobs:
        failures.extend(_job_failures(job, terminal_prs=terminal_prs))
        warnings.extend(_job_warnings(job, now=now, stuck_age_seconds=stuck_age_seconds))

    failures.extend(_duplicate_processed_event_findings(state))
    warnings.extend(_duplicate_source_findings(jobs))
    warnings.extend(_work_line_mismatches(state))
    return failures, warnings


def _terminal_pr_targets(state: dict[str, Any]) -> set[tuple[Any, Any]]:
    return {
        (entry.get("repo"), entry.get("pr"))
        for entry in _items(state, "terminal_targets", "prs")
        if entry.get("repo") and entry.get("pr") is not None
    }


def _job_failures(job: dict[str, Any], *, terminal_prs: set[tuple[Any, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    stage = str(job.get("stage") or "")
    role = _job_role(job)

    expected_role = EXPECTED_ROLE_BY_STAGE.get(stage)
    if expected_role and role != expected_role:
        failures.append(_finding("role_binding_mismatch", job, f"{stage} expected {expected_role}, got {role}"))

    if _is_review_no_blockers_to_implementation(job):
        failures.append(
            _finding(
                "review_no_blockers_routed_to_implementation",
                job,
                "review_round no_blockers_found routed to implementation",
            )
        )

    if _active_worker_targets_terminal_pr(job, stage=stage, role=role, terminal_prs=terminal_prs):
        failures.append(_finding("active_worker_targets_terminal_pr", job, "worker job targets terminal PR"))

    if _active_worker_targets_closed_or_merged_pr(job, stage=stage, role=role):
        failures.append(_finding("active_worker_targets_terminal_pr", job, "worker job targets closed or merged PR"))

    return failures


def _job_warnings(job: dict[str, Any], *, now: datetime, stuck_age_seconds: int) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if job.get("status") in ACTIVE_JOB_STATUSES:
        age_s = _age_seconds(job, now)
        if age_s is not None and age_s > stuck_age_seconds:
            warnings.append(
                _finding("active_job_stuck", job, f"active job age {int(age_s)}s exceeds {stuck_age_seconds}s")
            )
    if not job.get("id"):
        warnings.append(_finding("job_missing_id", job, "job has no id"))
    return warnings


def _active_worker_targets_terminal_pr(
    job: dict[str, Any],
    *,
    stage: str,
    role: Any,
    terminal_prs: set[tuple[Any, Any]],
) -> bool:
    return (
        job.get("status") in ACTIVE_JOB_STATUSES
        and stage == "implementation"
        and role == "worker"
        and (job.get("repo_full_name"), job.get("pr_number")) in terminal_prs
    )


def _active_worker_targets_closed_or_merged_pr(job: dict[str, Any], *, stage: str, role: Any) -> bool:
    metadata = _dict(job.get("metadata"))
    source_event = _dict(metadata.get("source_event"))
    pr_state = source_event.get("pr_state", metadata.get("pr_state"))
    pr_merged = source_event.get("pr_merged", metadata.get("pr_merged"))
    return (
        job.get("status") in ACTIVE_JOB_STATUSES
        and stage == "implementation"
        and role == "worker"
        and (pr_state == "closed" or pr_merged is True)
    )


def _storage_error_findings(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"code": "storage_error", "message": f"{name}.json could not be read", "file": name, "error": error}
        for name, error in _dict(state.get("errors")).items()
    ]


def _duplicate_processed_event_findings(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "code": "duplicate_processed_event",
            "message": f"processed event {key} appears {count} times",
            "event_key": key,
        }
        for key, count in Counter(_items(state, "processed_events", "keys")).items()
        if key and count > 1
    ]


def _duplicate_source_findings(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"code": "duplicate_source_side_effect", "message": f"source event {source} produced {count} jobs"}
        for source, count in _duplicate_sources(jobs).items()
        if count > 1
    ]


def _load_state(data_dir: Path) -> dict[str, Any]:
    files = {
        "registry": "registry.json",
        "jobs": "jobs.json",
        "sessions": "sessions.json",
        "work_lines": "work-lines.json",
        "processed_events": "processed-events.json",
        "terminal_targets": "terminal-targets.json",
        "config": "config.json",
    }
    state: dict[str, Any] = {"errors": {}}
    for key, filename in files.items():
        path = data_dir / filename
        try:
            state[key] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            state[key] = {}
            state["errors"][key] = str(exc)
    state["events"] = _read_recent_events(data_dir / "events.jsonl")
    return state


def _read_recent_events(path: Path, limit: int = 10) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    except OSError:
        return []
    return events


def _items(state: dict[str, Any], key: str, collection: str) -> list[Any]:
    payload = state.get(key)
    if key == "processed_events" and collection == "keys" and isinstance(payload, dict):
        values = payload.get("keys")
        return list(values) if isinstance(values, list) else []
    if isinstance(payload, dict):
        values = payload.get(collection)
        return list(values) if isinstance(values, list) else []
    return []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _job_role(job: dict[str, Any]) -> Any:
    return job.get("role") or _dict(job.get("metadata")).get("role")


def _count_status(jobs: list[dict[str, Any]], statuses: set[str]) -> int:
    return sum(1 for job in jobs if job.get("status") in statuses)


def _failed_last_24h(jobs: list[dict[str, Any]], now: datetime) -> int:
    cutoff = now - timedelta(hours=24)
    count = 0
    for job in jobs:
        if job.get("status") != "failed":
            continue
        timestamp = _parse_time(job.get("updated_at") or job.get("created_at"))
        if timestamp is not None and timestamp >= cutoff:
            count += 1
    return count


def _job_sort_time(job: dict[str, Any]) -> str:
    return str(job.get("updated_at") or job.get("created_at") or "")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(job: dict[str, Any], now: datetime) -> float | None:
    started = _parse_time(job.get("created_at") or job.get("updated_at"))
    return None if started is None else (now - started).total_seconds()


def _age_text(job: dict[str, Any], now: datetime) -> str:
    seconds = _age_seconds(job, now)
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


def _job_runtime(job: dict[str, Any], session: dict[str, Any] | None = None) -> str | None:
    metadata = _dict(job.get("metadata"))
    binding = _dict(metadata.get("role_binding"))
    if session:
        return (
            session.get("effective_runtime")
            or session.get("native_session_runtime")
            or session.get("preferred_runtime")
            or binding.get("runtime")
        )
    return binding.get("runtime") or metadata.get("effective_runtime") or metadata.get("preferred_runtime")


def _job_profile(job: dict[str, Any], session: dict[str, Any] | None = None) -> str | None:
    metadata = _dict(job.get("metadata"))
    binding = _dict(metadata.get("role_binding"))
    if session and session.get("hermes_profile"):
        return str(session.get("hermes_profile"))
    return metadata.get("hermes_profile") or binding.get("profile")


def _job_summary(job: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    return {
        "id": job.get("id"),
        "repo": job.get("repo_full_name"),
        "stage": job.get("stage"),
        "role": _job_role(job),
        "runtime": _job_runtime(job),
        "profile": _job_profile(job),
        "status": job.get("status"),
        "issue": job.get("issue_number"),
        "pr": job.get("pr_number"),
        "review_round": job.get("review_round"),
        "age": _age_text(job, now),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


def _lane_summary(job: dict[str, Any], *, sessions: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    session = next((item for item in reversed(sessions) if item.get("job_id") == job.get("id")), None)
    summary = _job_summary(job, now=now)
    summary["runtime"] = _job_runtime(job, session)
    summary["profile"] = _job_profile(job, session)
    return summary


def _transition_summary(job: dict[str, Any]) -> dict[str, Any]:
    metadata = _dict(job.get("metadata"))
    source = _dict(metadata.get("source_event"))
    return {
        "job_id": job.get("id"),
        "source": source.get("kind") or metadata.get("route_reason") or "event",
        "stage": job.get("stage"),
        "status": job.get("status"),
        "reason": metadata.get("route_reason") or _dict(metadata.get("route_decision")).get("because"),
    }


def _session_summary(session: dict[str, Any] | None) -> dict[str, Any] | None:
    if session is None:
        return None
    keys = (
        "id",
        "runtime_handle",
        "prompt_path",
        "script_path",
        "worktree_path",
        "stdout_path",
        "stderr_path",
        "preferred_runtime",
        "effective_runtime",
        "native_session_runtime",
        "hermes_profile",
        "status",
        "created_at",
        "updated_at",
        "ended_at",
        "termination_reason",
    )
    return {key: session.get(key) for key in keys}


def _matching_work_line(state: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _dict(job.get("metadata"))
    candidates = _items(state, "work_lines", "work_lines")
    line_id = metadata.get("line_id")
    if line_id:
        match = next(
            (
                item
                for item in candidates
                if item.get("repo_full_name") == job.get("repo_full_name") and item.get("line_id") == line_id
            ),
            None,
        )
        if match:
            return match
    issue = str(job.get("issue_number") or "")
    pr = str(job.get("pr_number") or "")
    return next(
        (
            item
            for item in candidates
            if item.get("repo_full_name") == job.get("repo_full_name")
            and ((issue and str(item.get("issue_id") or "") == issue) or (pr and str(item.get("pr_id") or "") == pr))
        ),
        None,
    )


def _finding(code: str, job: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "job_id": job.get("id"),
        "repo": job.get("repo_full_name"),
        "stage": job.get("stage"),
        "role": _job_role(job),
        "status": job.get("status"),
        "issue": job.get("issue_number"),
        "pr": job.get("pr_number"),
    }


def _is_review_no_blockers_to_implementation(job: dict[str, Any]) -> bool:
    if job.get("stage") != "implementation":
        return False
    metadata = _dict(job.get("metadata"))
    source = _dict(metadata.get("source_event"))
    decision = _dict(metadata.get("route_decision"))
    verdict = str(source.get("review_verdict") or source.get("signature_verdict") or "").casefold()
    return (
        source.get("signature_stage") == "review_round"
        and verdict in {"no_blockers_found", "no_blockers", "clean", "approved"}
        and (decision.get("to") in {None, "implementation"})
    )


def _duplicate_sources(jobs: list[dict[str, Any]]) -> Counter[str]:
    sources: Counter[str] = Counter()
    for job in jobs:
        source = _dict(_dict(job.get("metadata")).get("source_event"))
        key = source.get("delivery_id") or source.get("signature_job") or source.get("event_key")
        if key:
            sources[str(key)] += 1
    return sources


def _work_line_mismatches(state: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    jobs = _items(state, "jobs", "jobs")
    for line in _items(state, "work_lines", "work_lines"):
        repo = line.get("repo_full_name")
        line_id = line.get("line_id")
        related = [
            job
            for job in jobs
            if job.get("repo_full_name") == repo and _dict(job.get("metadata")).get("line_id") == line_id
        ]
        if not related:
            continue
        latest = sorted(related, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))[-1]
        line_status = str(line.get("status") or "")
        job_status = str(latest.get("status") or "")
        if (
            line_status in {"running", "review_running", "implementation_running"}
            and job_status in TERMINAL_JOB_STATUSES
        ):
            warnings.append({
                "code": "work_line_job_status_mismatch",
                "message": f"work line {line_id} is {line_status} but latest job {latest.get('id')} is {job_status}",
                "job_id": latest.get("id"),
                "repo": repo,
                "line_id": line_id,
            })
    return warnings


def _number(value: Any) -> str:
    return f"#{value}" if value not in (None, "") else "-"
