import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import dani.cli as cli_module
from dani.cli import app
from dani.models import JobRecord


class FakeBootstrapService:
    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.calls: list[tuple[str, str | None]] = []

    def bootstrap_repo(self, repo_full_name: str) -> int:
        self.calls.append(("bootstrap_repo", repo_full_name))
        return self.count

    def wait_for_idle(self) -> None:
        self.calls.append(("wait_for_idle", None))


class FakeRestartService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int] | tuple[str, None]] = []

    def restart_issue(self, repo_full_name: str, issue_number: int) -> JobRecord:
        self.calls.append(("restart_issue", repo_full_name, issue_number))
        return JobRecord(
            repo_full_name=repo_full_name, stage="issue_readiness_review", issue_number=issue_number, id="job-123"
        )

    def wait_for_idle(self) -> None:
        self.calls.append(("wait_for_idle", None))


def test_register_repo_and_show_state(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"

    register_result = runner.invoke(app, ["register-repo", "acme/demo", str(tmp_path), "--data-dir", str(data_dir)])
    assert register_result.exit_code == 0

    state_result = runner.invoke(app, ["show-state", "--data-dir", str(data_dir)])
    assert state_result.exit_code == 0
    payload = json.loads(state_result.stdout)
    assert payload["registry"]["repos"][0]["full_name"] == "acme/demo"


def test_bootstrap_waits_for_idle_before_exiting(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    fake_service = FakeBootstrapService(count=2)
    monkeypatch.setattr(cli_module, "build_service", lambda data_dir: fake_service)

    result = runner.invoke(app, ["bootstrap", "acme/demo", "--data-dir", str(data_dir)])

    assert result.exit_code == 0
    assert fake_service.calls == [("bootstrap_repo", "acme/demo"), ("wait_for_idle", None)]
    assert result.stdout.strip() == "processed 2 issues"


def test_restart_issue_invokes_service_waits_for_idle_and_prints_job(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    fake_service = FakeRestartService()
    monkeypatch.setattr(cli_module, "build_service", lambda data_dir: fake_service)

    result = runner.invoke(app, ["restart-issue", "acme/demo", "41", "--data-dir", str(data_dir)])

    assert result.exit_code == 0
    assert fake_service.calls == [("restart_issue", "acme/demo", 41), ("wait_for_idle", None)]
    payload = json.loads(result.stdout)
    assert payload["id"] == "job-123"
    assert payload["stage"] == "issue_readiness_review"
    assert payload["issue_number"] == 41


def test_build_config_reads_agent_timeout_from_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"agent_timeout_seconds": 7200}), encoding="utf-8")
    monkeypatch.delenv("DANI_AGENT_TIMEOUT_SECONDS", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.agent_timeout_seconds == 7200


def test_build_config_agent_timeout_env_overrides_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"agent_timeout_seconds": 7200}), encoding="utf-8")
    monkeypatch.setenv("DANI_AGENT_TIMEOUT_SECONDS", "5400")

    config = cli_module.build_config(data_dir)

    assert config.agent_timeout_seconds == 5400


def test_build_config_defaults_for_bot_login_and_max_issue_followups(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    monkeypatch.delenv("DANI_BOT_LOGIN", raising=False)
    monkeypatch.delenv("DANI_MAX_ISSUE_FOLLOWUPS", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.bot_login is None
    assert config.max_issue_followups == 3
    assert config.issue_ready_launch == "manual"


def test_build_config_reads_bot_login_from_env(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    monkeypatch.setenv("DANI_BOT_LOGIN", "danibot[bot]")

    config = cli_module.build_config(data_dir)

    assert config.bot_login == "danibot[bot]"


def test_build_config_reads_bot_login_from_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"bot_login": "dani-machine"}), encoding="utf-8")
    monkeypatch.delenv("DANI_BOT_LOGIN", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.bot_login == "dani-machine"


def test_build_config_bot_login_env_overrides_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"bot_login": "from-file"}), encoding="utf-8")
    monkeypatch.setenv("DANI_BOT_LOGIN", "from-env")

    config = cli_module.build_config(data_dir)

    assert config.bot_login == "from-env"


def test_build_config_reads_max_issue_followups_from_env(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    monkeypatch.setenv("DANI_MAX_ISSUE_FOLLOWUPS", "5")

    config = cli_module.build_config(data_dir)

    assert config.max_issue_followups == 5


def test_build_config_reads_max_issue_followups_from_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"max_issue_followups": 7}), encoding="utf-8")
    monkeypatch.delenv("DANI_MAX_ISSUE_FOLLOWUPS", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.max_issue_followups == 7


def test_build_config_reads_issue_ready_launch_from_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"issue_ready_launch": "auto"}), encoding="utf-8")
    monkeypatch.delenv("DANI_ISSUE_READY_LAUNCH", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.issue_ready_launch == "auto"


def test_build_config_reads_issue_ready_launch_from_env(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    monkeypatch.setenv("DANI_ISSUE_READY_LAUNCH", "auto-launch")

    config = cli_module.build_config(data_dir)

    assert config.issue_ready_launch == "auto"


def test_build_config_rejects_unknown_issue_ready_launch(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    monkeypatch.setenv("DANI_ISSUE_READY_LAUNCH", "surprise")

    with pytest.raises(cli_module.typer.BadParameter):
        cli_module.build_config(data_dir)


def test_build_config_reads_gjc_bin_from_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"gjc_bin": "/opt/gjc"}), encoding="utf-8")
    monkeypatch.delenv("DANI_GJC_BIN", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.gjc_bin == "/opt/gjc"


def test_build_config_gjc_bin_env_overrides_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"gjc_bin": "/opt/gjc"}), encoding="utf-8")
    monkeypatch.setenv("DANI_GJC_BIN", "/Users/devkade/.bun/bin/gjc")

    config = cli_module.build_config(data_dir)

    assert config.gjc_bin == "/Users/devkade/.bun/bin/gjc"


def test_build_config_reads_nested_gjc_bin(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"gjc": {"bin": "/opt/gjc"}}), encoding="utf-8")
    monkeypatch.delenv("DANI_GJC_BIN", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.gjc_bin == "/opt/gjc"


def test_build_config_reads_repo_concurrency_from_env(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    monkeypatch.setenv("DANI_REPO_CONCURRENCY", "4")

    config = cli_module.build_config(data_dir)

    assert config.repo_concurrency == 4


def test_build_config_reads_repo_concurrency_from_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"repo_concurrency": 3}), encoding="utf-8")
    monkeypatch.delenv("DANI_REPO_CONCURRENCY", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.repo_concurrency == 3


def test_build_config_accepts_max_repo_workers_alias_from_config_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"max_repo_workers": 5}), encoding="utf-8")
    monkeypatch.delenv("DANI_REPO_CONCURRENCY", raising=False)
    monkeypatch.delenv("DANI_MAX_REPO_WORKERS", raising=False)

    config = cli_module.build_config(data_dir)

    assert config.repo_concurrency == 5


def test_build_config_rejects_zero_repo_concurrency(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / ".dani"
    data_dir.mkdir()
    monkeypatch.setenv("DANI_REPO_CONCURRENCY", "0")

    with pytest.raises(cli_module.typer.BadParameter):
        cli_module.build_config(data_dir)


def _write_queue_state(data_dir: Path, *, jobs: list[dict]) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "registry.json").write_text(json.dumps({"repos": [{"full_name": "acme/demo"}]}), encoding="utf-8")
    (data_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    (data_dir / "sessions.json").write_text(json.dumps({"sessions": []}), encoding="utf-8")
    (data_dir / "work-lines.json").write_text(json.dumps({"work_lines": []}), encoding="utf-8")
    (data_dir / "processed-events.json").write_text(json.dumps({"keys": []}), encoding="utf-8")
    (data_dir / "terminal-targets.json").write_text(json.dumps({"prs": [], "issues": []}), encoding="utf-8")


def test_status_command_prints_queue_health(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    _write_queue_state(
        data_dir,
        jobs=[
            {
                "id": "job-1",
                "repo_full_name": "acme/demo",
                "stage": "implementation",
                "role": "worker",
                "issue_number": 16,
                "pr_number": None,
                "review_round": None,
                "metadata": {"route_reason": "issue_comment_approve"},
                "status": "queued",
                "session_id": None,
                "created_at": "2026-06-20T00:00:00+00:00",
                "updated_at": "2026-06-20T00:00:00+00:00",
            }
        ],
    )

    result = runner.invoke(app, ["status", "--data-dir", str(data_dir)])

    assert result.exit_code == 0
    assert "Dani queue health" in result.stdout
    assert "- queued: 1" in result.stdout
    assert "issue_comment_approve" in result.stdout


def test_queue_doctor_json_reports_failures(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    _write_queue_state(
        data_dir,
        jobs=[
            {
                "id": "job-1",
                "repo_full_name": "acme/demo",
                "stage": "implementation",
                "role": "reviewer",
                "issue_number": 16,
                "pr_number": None,
                "review_round": None,
                "metadata": {},
                "status": "queued",
                "session_id": None,
                "created_at": "2026-06-20T00:00:00+00:00",
                "updated_at": "2026-06-20T00:00:00+00:00",
            }
        ],
    )

    result = runner.invoke(app, ["queue", "doctor", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["health"] == "fail"
    assert payload["failures"][0]["code"] == "role_binding_mismatch"


def test_queue_doctor_json_reports_warning_exit(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    _write_queue_state(
        data_dir,
        jobs=[
            {
                "id": "job-1",
                "repo_full_name": "acme/demo",
                "stage": "implementation",
                "role": "worker",
                "issue_number": 16,
                "pr_number": None,
                "review_round": None,
                "metadata": {},
                "status": "queued",
                "session_id": None,
                "created_at": "2026-06-20T00:00:00+00:00",
                "updated_at": "2026-06-20T00:00:00+00:00",
            }
        ],
    )

    result = runner.invoke(app, ["queue", "doctor", "--json", "--data-dir", str(data_dir), "--stuck-age-seconds", "0"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["health"] == "warn"
    assert payload["warnings"][0]["code"] == "active_job_stuck"


def test_inspect_job_command_reports_unknown_job(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    _write_queue_state(data_dir, jobs=[])

    result = runner.invoke(app, ["inspect", "job", "missing", "--data-dir", str(data_dir)])

    assert result.exit_code == 1
    assert "Unknown job id: missing" in result.stderr


def test_inspect_job_command_prints_route_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / ".dani"
    _write_queue_state(
        data_dir,
        jobs=[
            {
                "id": "job-1",
                "repo_full_name": "acme/demo",
                "stage": "implementation",
                "role": "worker",
                "issue_number": 16,
                "pr_number": 4,
                "review_round": 1,
                "metadata": {
                    "route_reason": "review_round_changes_requested",
                    "source_event": {"kind": "issue_comment", "signature_stage": "review_round"},
                    "route_decision": {
                        "from": "review_round",
                        "to": "implementation",
                        "because": "review requested changes",
                    },
                },
                "status": "queued",
                "session_id": None,
                "created_at": "2026-06-20T00:00:00+00:00",
                "updated_at": "2026-06-20T00:00:00+00:00",
            }
        ],
    )

    result = runner.invoke(app, ["inspect", "job", "job-1", "--data-dir", str(data_dir)])

    assert result.exit_code == 0
    assert "Dani job job-1" in result.stdout
    assert "route_reason: review_round_changes_requested" in result.stdout
    assert '"to": "implementation"' in result.stdout
