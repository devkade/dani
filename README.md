# dani

Simple GitHub webhook -> agent automation loop. Supports four pluggable agent
runtimes: **Oh-My-Codex (`omx`, default)**, **Oh-My-OpenAgents (`omo`,
opt-in)**, **Gajae Code (`gjc`, opt-in)**, and **Hermes (`hermes`, opt-in)**.

## What v1 includes
- Typer CLI
- FastAPI webhook server
- Registered repos only
- Repo-serial / cross-repo parallel job handling
- Pluggable agent runtime: non-interactive `omx exec` / `omx exec resume` (default),
  HTTP-backed Oh-My-OpenAgents (`opencode serve`), non-interactive GJC (`gjc -p`),
  or non-interactive Hermes (`hermes chat -q`)
- Separate prompt templates in `dani/prompts.py`
- Workflows for:
  - issue request report
  - `/approve` implementation
  - 3 review rounds for agent-authored PRs
  - PR-comment-triggered merge conflict resolution via `/resolve-conflict`,
    `/resolve merge conflict`, `/solve merge conflict`, or `solve merge conflict`
  - external contributor account-age eligibility checks
  - event-driven, duplicate-delivery-safe re-review for external contributor PRs
  - final verdict + auto-merge on APPROVE

## Environment
Required local tools (depending on the selected runtime):
- `git`
- `omx` — required when `DANI_AGENT_RUNTIME=omx` (default)
- `opencode` — required when `DANI_AGENT_RUNTIME=omo`
- `gjc` — required when `DANI_AGENT_RUNTIME=gjc`
- `hermes` — required when `DANI_AGENT_RUNTIME=hermes`

Required environment variables:
- `DANI_WEBHOOK_SECRET`
- `DANI_GITHUB_TOKEN` (preferred) or `GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_PAT`

Optional environment variables:
- `DANI_AGENT_RUNTIME` — selects the agent backend. Accepted values:
  - `omx` / `oh-my-codex` / `codex` (default)
  - `omo` / `oh-my-openagents` / `oh-my-openagent` / `opencode`
  - `gjc` / `gajae-code`
  - `hermes`
- `DANI_AGENT_TIMEOUT_SECONDS` — overrides the per-job agent wait timeout in seconds.
- `DANI_ISSUE_READY_LAUNCH` — selects what happens after a reviewer marks an issue ready:
  - `manual` (default) waits for maintainer `/approve`.
  - `auto` launches the worker immediately from the reviewer-ready signature.

Optional config file (`~/.dani/config.json` by default, or `<data-dir>/config.json`):

```json
{
  "agent_runtime": "omx",
  "agent_timeout_seconds": 3600,
  "issue_ready_launch": "manual",
  "role_bindings": {
    "reviewer": {"runtime": "hermes", "profile": "reviewer-profile"}
  }
}
```

`agent_timeout_seconds` defaults to `3600`; `issue_ready_launch` defaults to
`manual`. The corresponding environment variables take precedence over the
config file when set.

`role_bindings.<role>.runtime` overrides the global `agent_runtime` for that role.
`role_bindings.<role>.profile` is a Hermes-only option: Dani passes it to
`hermes -p <profile>` when that role's effective runtime is `hermes`, and ignores it
for `omx`, `omo`, and `gjc` bindings. Reviewer stages (`issue_readiness_review`,
`review_round`, `check_review`, and `final_verdict`) can be bound to a Hermes
profile without changing worker or planner runtime bindings.

When `DANI_AGENT_RUNTIME=omo` is selected, dani automatically prefixes every
opencode prompt with the `ultrawork` keyword so oh-my-openagents' ultrawork
loop mode is always active, and runtime-specific prompt substitutions swap
codex-only shell commands (`$ralph`, `$code-review`) for their opencode
equivalents (ultrawork mode and the Momus-Plan-Critic subagent respectively).

By default, `omo` drives opencode through its long-lived **HTTP server**
(`opencode serve`) instead of one-shot `opencode run` subprocesses. Each
registered repository gets its own cached `opencode serve` process, lazily
spawned on first launch and reused for every subsequent session in that
repo. The HTTP backend keeps the agent loop alive inside the server even
when subagents (oh-my-openagents `task` calls) come and go, which avoids
the parent-process termination problem the subprocess backend hit when
subagents finished.

Optional `omo` runtime environment variables:
- `DANI_OPENCODE_SERVER_URL` — attach every repo to a single external
  opencode server (e.g. `http://127.0.0.1:4096`) instead of spawning local
  ones. Sessions are still scoped per repo via the `directory` query param.
- `DANI_OPENCODE_PERMISSION_RESPONSE` — `once` (default), `always`, or
  `reject`. Controls how dani responds to opencode `permission.updated`
  events automatically.
- `OPENCODE_SERVER_PASSWORD` — forwarded to the spawned server and used
  for HTTP basic auth on every request when set.

## Codex/OMX trust prerequisite
Before dani can reliably launch or resume OMX/Codex sessions for a repository, that repository directory should be trusted by Codex at least once. In practice, run `omx exec 'hello'` or `codex exec 'hello'` once from the target repo and accept the trust prompt before using dani automation there. Otherwise a trust prompt can block session startup or resume.

## Oh-My-OpenAgents (opencode) prerequisite
When running with `DANI_AGENT_RUNTIME=omo`, dani drives opencode through the
`opencode serve` HTTP backend by default: it spawns `opencode serve --port 0
--hostname 127.0.0.1 --print-logs` once per registered repository (with the
repo as cwd), then talks to that server over HTTP for session create, prompt
submission, completion via SSE, and resume. Install `opencode` (the
`oh-my-openagent` plugin is loaded automatically when configured in
`~/.config/opencode/opencode.json`) and make sure the target repository
directory is trusted at least once via `opencode run 'hello'` before pointing
dani at it.

## Gajae Code (GJC) prerequisite
When running with `DANI_AGENT_RUNTIME=gjc`, dani launches GJC in
non-interactive print mode with `gjc -p` and resumes only when a trustworthy
GJC session id or session file path is available. Install `gjc` and trust the
target repository at least once in GJC before using webhook automation there.
Dani maps its GitHub-driven stages onto GJC workflow semantics instead of
handing native `.gjc` workflow state directly to `ralplan` or `ultragoal`:
planning jobs produce ralplan-style pending-approval comments, approved
implementation jobs run ultragoal-style execution from that approved plan, and
PR review jobs run a GJC quality-gate review. Runtime-specific prompt
substitutions still replace Codex-only `$ralph` and `$code-review` tokens.

## Hermes prerequisite
When running with `DANI_AGENT_RUNTIME=hermes`, dani launches Hermes in
non-interactive chat mode with `hermes chat -q`. A role binding can set
`profile` to launch that role with `hermes -p <profile> chat -q`; configure the
profile in Hermes first. Dani does not resume Hermes sessions yet, so follow-up
jobs start fresh unless another runtime handles the lineage.

## CLI
```bash
dani register-repo owner/name /absolute/path/to/repo
dani serve --data-dir .dani
dani bootstrap owner/name
dani show-state
dani doctor
dani status
dani queue doctor --json
dani inspect job <job-id>
```

### Queue Observability

`dani status` and `dani queue status` print read-only queue health for operators:
server/data-dir details, queue counts, stuck active jobs, latest active lanes,
and recent transitions.

`dani queue doctor` runs queue-specific invariant checks over the existing JSON
state. Use `--json` for automation and `--stuck-age-seconds N` to tune the active
job warning threshold. Exit code `0` means healthy, `1` means warnings only, and
`2` means failures such as storage parse errors, role binding drift, duplicate
processed events, or active worker jobs targeting terminal PRs.

`dani inspect job <job-id>` prints one persisted job with its role, runtime,
profile, source event, route reason, route decision, session paths, and work-line
linkage. Add `--json` for machine-readable output.

## dani doctor

`dani doctor` runs read-only health diagnostics against a dani installation.
It is safe to run while `dani serve` is live on the same machine: doctor never
writes to `~/.dani/`, never binds the webhook port, never spawns runners or
competes with the serve process for storage locks, and never echoes any value
of `DANI_WEBHOOK_SECRET`/`DANI_GITHUB_TOKEN`/`GITHUB_TOKEN`/`GH_TOKEN`/
`GITHUB_PAT` or any raw `ps` argv.

```bash
# Run every check; human-readable output.
dani doctor

# Machine-readable JSON for monitoring / CI.
dani doctor --json | jq

# Run a subset of checks.
dani doctor --check stuck_sessions --check disk_usage --json

# Treat warnings as exit 1 (e.g., in CI gates).
dani doctor --strict

# Override default thresholds (strict allow-list of keys).
dani doctor --threshold runs_bytes_warn=10000000000 --threshold stuck_job_age_seconds=7200

# Probe a non-default webhook port (default: DANI_PORT env or 8787).
dani doctor --check server_health --port 8000

# Write the report to a file outside ~/.dani/.
dani doctor --json --output /tmp/dani-report.json
```

### Checks

| Name | Description |
|---|---|
| `config_env` | webhook secret, GitHub token, agent runtime, config.json parse status |
| `binaries` | `git`, `gh`, plus runtime-specific `omx`/`codex`, `opencode`, `gjc`, or `hermes` (skips opencode if `DANI_OPENCODE_SERVER_URL` is set) |
| `storage_files` | parse-ability of `registry.json`/`jobs.json`/`sessions.json`/`processed-events.json`/`terminal-targets.json`; tolerates one transient parse error per file plus an `events.jsonl` last-line append race |
| `registered_repos` | each registered repo's `local_path` is a git working tree and resolves both `main_branch` and `dev_branch` |
| `github_auth` | resolves the GitHub token, verifies it with the minimum `Github.get_rate_limit()` call, reports rate-limit headroom; never echoes the token, only its source env-var name |
| `server_health` | if `127.0.0.1:<port>` is listening, GET `/health` and compare to `{"status":"ok"}`; SKIP (not FAIL) when no local server |
| `stuck_jobs` | warns when active jobs (`queued`/`launched`/`retrying`/`recovering`) are older than `stuck_job_age_seconds` (default = `agent_timeout_seconds`) |
| `stuck_sessions` | FAILs on confirmed A-class drift (session `launched` while linked job is terminal — re-validated with a 100ms re-snapshot and a 60s grace window); WARNs on long-running launches and orphans |
| `disk_usage` | size of each storage file plus a soft-deadlined walk of `runs/`; thresholds for warn/fail per target |
| `backup_files` | accumulated `*.bak.*` files in `data_dir`: count / oldest age / total bytes |
| `process_sprawl` | counts alive `omx exec` / `opencode serve` / `opencode run` / `gjc -p` processes; reports only PID/PPID/etime/classifier — never raw argv |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | overall `ok` (warnings allowed without `--strict`; also returned when every check skipped) |
| `1` | overall `warn` AND `--strict` |
| `2` | overall `fail` (any check failed, or invalid CLI option) |
| `3` | `dani doctor` itself crashed (unhandled exception); sanitized error to stderr, redacted traceback only with `--verbose` |

### JSON schema

`dani doctor --json` emits a stable schema:

```json
{
  "schema_version": 1,
  "started_at": "...", "finished_at": "...",
  "data_dir": "...",
  "host": {"port": 8787, "platform": "darwin", "python_version": "3.13.0", "dani_version": "0.0.1"},
  "overall_status": "ok|warn|fail|skip",
  "summary": {"ok": N, "warn": N, "fail": N, "skip": N},
  "results": [{"name": "...", "status": "...", "summary": "...", "details": {...}, "duration_ms": N, "error": null}]
}
```

If the schema needs to change, `schema_version` is bumped.

## Persistence
State is stored under `~/.dani/` by default:
- `registry.json`
- `jobs.json`
- `sessions.json`
- `events.jsonl`
- `runs/` for generated agent-runtime prompt/script artifacts (omx, omo, gjc, or hermes)


## GitHub surfaces
- OMX sessions should use `gh` for issue comments, PR comments, and PR creation/update.
- `dani/github.py` and `dani/github_helper.py` remain PyGithub-backed internal surfaces for dani runtime logic.
