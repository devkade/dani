from dani.models import NormalizedEvent
from dani.routing import ROLE_PLANNER, ROLE_REVIEWER, ROLE_WORKER, parse_role_bindings, route_event


def test_route_issue_opened_to_planner_issue_request() -> None:
    event = NormalizedEvent(
        kind="issue_opened",
        repo_full_name="acme/demo",
        action="opened",
        number=4,
        actor_login="contributor",
        payload={},
        delivery_id="d1",
    )

    decision = route_event(event)

    assert decision is not None
    assert decision.role == ROLE_PLANNER
    assert decision.stage == "issue_request"
    assert decision.reason == "issue_opened"
    assert decision.target_metadata == {
        "event_kind": "issue_opened",
        "event_action": "opened",
        "target_number": 4,
        "actor_login": "contributor",
        "delivery_id": "d1",
        "issue_number": 4,
    }


def test_route_non_approve_issue_comment_to_planner_followup() -> None:
    event = NormalizedEvent(
        kind="issue_comment",
        repo_full_name="acme/demo",
        action="created",
        number=4,
        actor_login="contributor",
        payload={},
        body="Can you clarify the plan?",
    )

    decision = route_event(event, is_approve_comment=False)

    assert decision is not None
    assert decision.role == ROLE_PLANNER
    assert decision.stage == "issue_followup"
    assert decision.reason == "issue_comment_non_approve"
    assert decision.target_metadata["issue_number"] == 4


def test_route_approve_issue_comment_to_worker_implementation() -> None:
    event = NormalizedEvent(
        kind="issue_comment",
        repo_full_name="acme/demo",
        action="created",
        number=4,
        actor_login="maintainer",
        payload={},
        body="/approve",
    )

    decision = route_event(event, is_approve_comment=True)

    assert decision is not None
    assert decision.role == ROLE_WORKER
    assert decision.stage == "implementation"
    assert decision.reason == "issue_comment_approve"


def test_route_pr_lifecycle_to_reviewer_review_round() -> None:
    event = NormalizedEvent(
        kind="pull_request_opened",
        repo_full_name="acme/demo",
        action="synchronize",
        number=9,
        actor_login="contributor",
        payload={},
        base_branch="dev",
        head_branch="feature/9",
        commit_sha="abc123",
        is_pull_request=True,
    )

    decision = route_event(event)

    assert decision is not None
    assert decision.role == ROLE_REVIEWER
    assert decision.stage == "review_round"
    assert decision.reason == "pull_request_synchronize"
    assert decision.target_metadata["pr_number"] == 9
    assert decision.target_metadata["base_branch"] == "dev"
    assert decision.target_metadata["head_branch"] == "feature/9"
    assert decision.target_metadata["commit_sha"] == "abc123"


def test_parse_role_bindings_default_to_global_agent_runtime() -> None:
    bindings = parse_role_bindings({}, agent_runtime="omo")

    assert bindings[ROLE_WORKER].runtime == "omo"
    assert bindings[ROLE_REVIEWER].runtime == "omo"
    assert bindings[ROLE_PLANNER].runtime == "omo"
    assert "merge_pull_request" in bindings[ROLE_WORKER].forbidden_actions
    assert "push_commits" in bindings[ROLE_REVIEWER].forbidden_actions
    assert "push_commits" in bindings[ROLE_PLANNER].forbidden_actions


def test_parse_role_bindings_overrides_single_role_policy() -> None:
    bindings = parse_role_bindings(
        {
            "reviewer": {
                "runtime": "omx",
                "display_name": "Review Bot",
                "forbidden_actions": ["push_commits"],
                "prompt_policy": "Read-only reviewer.",
            }
        },
        agent_runtime="omo",
    )

    assert bindings[ROLE_WORKER].runtime == "omo"
    assert bindings[ROLE_REVIEWER].runtime == "omx"
    assert bindings[ROLE_REVIEWER].display_name == "Review Bot"
    assert bindings[ROLE_REVIEWER].forbidden_actions == ["push_commits"]
    assert bindings[ROLE_REVIEWER].prompt_policy == "Read-only reviewer."


def test_route_check_status_to_reviewer_check_review() -> None:
    event = NormalizedEvent(
        kind="check_status",
        repo_full_name="acme/demo",
        action="completed",
        number=9,
        actor_login="github-actions",
        payload={},
        commit_sha="abc123",
        is_pull_request=True,
    )

    decision = route_event(event)

    assert decision is not None
    assert decision.role == ROLE_REVIEWER
    assert decision.stage == "check_review"
    assert decision.reason == "check_status_completed"
    assert decision.target_metadata["pr_number"] == 9
    assert decision.target_metadata["commit_sha"] == "abc123"
