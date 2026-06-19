from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from dani.agent_runner import normalize_runtime
from dani.models import NormalizedEvent

ROLE_WORKER = "worker"
ROLE_REVIEWER = "reviewer"
ROLE_PLANNER = "planner"

WORKER_FORBIDDEN_ACTIONS = (
    "merge_pull_request",
    "close_issue_without_instruction",
    "change_release_authority",
)
REVIEWER_FORBIDDEN_ACTIONS = (
    "push_commits",
    "create_or_update_pr_branch",
    "merge_pull_request",
    "close_issue_without_instruction",
)
PLANNER_FORBIDDEN_ACTIONS = REVIEWER_FORBIDDEN_ACTIONS

DEFAULT_ROLE_FOR_STAGE = {
    "issue_readiness_review": ROLE_REVIEWER,
    "issue_request": ROLE_PLANNER,
    "issue_followup": ROLE_PLANNER,
    "issue_request_recovery": ROLE_PLANNER,
    "issue_followup_recovery": ROLE_PLANNER,
    "check_review": ROLE_REVIEWER,
    "review_round": ROLE_REVIEWER,
    "final_verdict": ROLE_REVIEWER,
    "implementation": ROLE_WORKER,
    "merge_conflict_resolution": ROLE_WORKER,
    "dev_sync": ROLE_WORKER,
    "dev_sync_conflict": ROLE_WORKER,
    "final_verdict_merge": ROLE_WORKER,
}

DEFAULT_FORBIDDEN_ACTIONS_BY_ROLE = {
    ROLE_WORKER: WORKER_FORBIDDEN_ACTIONS,
    ROLE_REVIEWER: REVIEWER_FORBIDDEN_ACTIONS,
    ROLE_PLANNER: PLANNER_FORBIDDEN_ACTIONS,
}

PR_LIFECYCLE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review", "review_requested"})


@dataclass(slots=True)
class AgentRoleBinding:
    role: str
    runtime: str = "omx"
    display_name: str | None = None
    profile: str | None = None
    command: str | None = None
    skills: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    prompt_policy: str | None = None

    @classmethod
    def from_dict(cls, role: str, payload: dict[str, Any], *, fallback_runtime: str) -> AgentRoleBinding:
        runtime = str(payload.get("runtime") or fallback_runtime or "omx")
        forbidden = payload.get("forbidden_actions")
        allowed = payload.get("allowed_actions")
        skills = payload.get("skills")
        return cls(
            role=role,
            runtime=normalize_runtime(runtime),
            display_name=_optional_str(payload.get("display_name") or payload.get("agent")),
            profile=_optional_str(payload.get("profile")),
            command=_optional_str(payload.get("command")),
            skills=[str(item) for item in skills] if isinstance(skills, list) else [],
            allowed_actions=[str(item) for item in allowed] if isinstance(allowed, list) else [],
            forbidden_actions=[str(item) for item in forbidden]
            if isinstance(forbidden, list)
            else list(default_forbidden_actions(role)),
            prompt_policy=_optional_str(payload.get("prompt_policy")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "runtime": self.runtime,
            "display_name": self.display_name,
            "profile": self.profile,
            "command": self.command,
            "skills": list(self.skills),
            "allowed_actions": list(self.allowed_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "prompt_policy": self.prompt_policy,
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    role: str
    stage: str
    reason: str
    target_metadata: dict[str, Any] = field(default_factory=dict)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def default_forbidden_actions(role: str) -> tuple[str, ...]:
    return DEFAULT_FORBIDDEN_ACTIONS_BY_ROLE.get(role, ())


def default_role_for_stage(stage: str) -> str:
    return DEFAULT_ROLE_FOR_STAGE.get(stage, ROLE_WORKER)


def default_role_bindings(agent_runtime: str) -> dict[str, AgentRoleBinding]:
    runtime = normalize_runtime(agent_runtime or "omx")
    return {
        ROLE_WORKER: AgentRoleBinding(
            role=ROLE_WORKER,
            runtime=runtime,
            forbidden_actions=list(default_forbidden_actions(ROLE_WORKER)),
        ),
        ROLE_REVIEWER: AgentRoleBinding(
            role=ROLE_REVIEWER,
            runtime=runtime,
            forbidden_actions=list(default_forbidden_actions(ROLE_REVIEWER)),
        ),
        ROLE_PLANNER: AgentRoleBinding(
            role=ROLE_PLANNER,
            runtime=runtime,
            forbidden_actions=list(default_forbidden_actions(ROLE_PLANNER)),
        ),
    }


def parse_role_bindings(raw: object, *, agent_runtime: str) -> dict[str, AgentRoleBinding]:
    bindings = default_role_bindings(agent_runtime)
    if not isinstance(raw, dict):
        return bindings
    for role, payload in raw.items():
        role_name = str(role).strip()
        if not role_name or not isinstance(payload, dict):
            continue
        fallback = bindings.get(role_name, bindings[ROLE_WORKER]).runtime
        bindings[role_name] = AgentRoleBinding.from_dict(
            role_name, cast(dict[str, Any], payload), fallback_runtime=fallback
        )
    return bindings


def route_event(event: NormalizedEvent, *, is_approve_comment: bool = False) -> RouteDecision | None:
    target = _target_metadata(event)
    if event.kind == "issue_opened":
        return RouteDecision(ROLE_PLANNER, "issue_request", "issue_opened", target)
    if event.kind == "issue_comment" and is_approve_comment:
        return RouteDecision(ROLE_WORKER, "implementation", "issue_comment_approve", target)
    if event.kind == "issue_comment":
        return RouteDecision(ROLE_PLANNER, "issue_followup", "issue_comment_non_approve", target)
    if event.kind == "pull_request_opened" and event.action in PR_LIFECYCLE_ACTIONS:
        return RouteDecision(ROLE_REVIEWER, "review_round", f"pull_request_{event.action}", target)
    if event.kind == "check_status":
        return RouteDecision(ROLE_REVIEWER, "check_review", f"check_status_{event.action}", target)
    if event.kind == "pull_request_opened":
        return RouteDecision(ROLE_REVIEWER, "review_round", "pull_request_lifecycle", target)
    return None


def _target_metadata(event: NormalizedEvent) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "event_kind": event.kind,
        "event_action": event.action,
        "target_number": event.number,
        "actor_login": event.actor_login,
    }
    if event.delivery_id:
        metadata["delivery_id"] = event.delivery_id
    if event.kind.startswith("issue_") or (event.kind == "issue_comment" and not event.is_pull_request):
        metadata["issue_number"] = event.number
    if event.is_pull_request or event.kind.startswith("pull_request"):
        metadata["pr_number"] = event.number
    if event.base_branch:
        metadata["base_branch"] = event.base_branch
    if event.head_branch:
        metadata["head_branch"] = event.head_branch
    if event.commit_sha:
        metadata["commit_sha"] = event.commit_sha
    return metadata
