from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).parents[1]
CI_PATH = REPOSITORY / ".github/workflows/ci.yml"
DAILY_PATH = REPOSITORY / ".github/workflows/daily-news.yml"
SOURCE_HEALTH_PATH = REPOSITORY / ".github/workflows/source-health.yml"
ACTION_PINS = {
    "actions/checkout": (
        "d23441a48e516b6c34aea4fa41551a30e30af803",
        "v6",
        "https://github.com/actions/checkout",
    ),
    "actions/setup-python": (
        "ece7cb06caefa5fff74198d8649806c4678c61a1",
        "v6",
        "https://github.com/actions/setup-python",
    ),
    "actions/upload-pages-artifact": (
        "7b1f4a764d45c48632c6b24a0339c27f5614fb0b",
        "v4",
        "https://github.com/actions/upload-pages-artifact",
    ),
    "actions/deploy-pages": (
        "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
        "v4",
        "https://github.com/actions/deploy-pages",
    ),
}
ACTIONLINT_IMAGE = (
    "rhysd/actionlint:1.7.7@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9"
)


class WorkflowLoader(yaml.SafeLoader):
    """Safe YAML loader that does not treat the GitHub `on` key as a boolean."""


WorkflowLoader.yaml_implicit_resolvers = {
    key: list(resolvers) for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for resolver_key, resolvers in WorkflowLoader.yaml_implicit_resolvers.items():
    WorkflowLoader.yaml_implicit_resolvers[resolver_key] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _load_workflow(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as workflow_file:
        workflow = yaml.load(workflow_file, Loader=WorkflowLoader)
    assert isinstance(workflow, dict)
    assert "on" in workflow
    assert True not in workflow
    return workflow


def _steps(workflow: dict[str, object], job: str) -> list[dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    selected = jobs[job]
    assert isinstance(selected, dict)
    steps = selected["steps"]
    assert isinstance(steps, list)
    return steps


def _run_commands(steps: list[dict[str, object]]) -> str:
    return "\n".join(str(step.get("run", "")) for step in steps)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def test_all_external_actions_are_commit_pinned_with_auditable_tag_sources() -> None:
    all_usage_pattern = re.compile(r"^\s*uses:\s*\S+.*$", re.MULTILINE)
    usage_pattern = re.compile(
        r"^\s*uses:\s*([^@\s]+)@([0-9a-f]+)\s+#\s+(\S+)\s+source:\s+(\S+)\s*$",
        re.MULTILINE,
    )
    all_usage_lines: list[str] = []
    usages: list[tuple[str, str, str, str]] = []
    for path in (CI_PATH, DAILY_PATH, SOURCE_HEALTH_PATH):
        source = path.read_text(encoding="utf-8")
        all_usage_lines.extend(all_usage_pattern.findall(source))
        usages.extend(usage_pattern.findall(source))

    assert usages
    assert len(usages) == len(all_usage_lines)
    assert {name for name, _sha, _tag, _source in usages} == set(ACTION_PINS)
    for name, sha, tag, source in usages:
        expected_sha, expected_tag, expected_source = ACTION_PINS[name]
        assert re.fullmatch(r"[0-9a-f]{40}", sha)
        assert (sha, tag, source) == (expected_sha, expected_tag, expected_source)


def test_ci_triggers_and_read_only_permissions() -> None:
    workflow = _load_workflow(CI_PATH)

    assert workflow["on"] == {
        "push": {"branches": ["main"]},
        "pull_request": {},
    }
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    for job in jobs.values():
        assert isinstance(job, dict)
        assert job["permissions"] == {"contents": "read"}


def test_ci_runs_all_quality_and_offline_validation_commands() -> None:
    workflow = _load_workflow(CI_PATH)
    steps = _steps(workflow, "quality")
    commands = _run_commands(steps)

    assert steps[0]["uses"] == f"actions/checkout@{ACTION_PINS['actions/checkout'][0]}"
    assert steps[1]["uses"] == (f"actions/setup-python@{ACTION_PINS['actions/setup-python'][0]}")
    assert steps[1]["with"] == {"python-version": "3.12", "cache": "pip"}
    for command in (
        'python -m pip install ".[dev]"',
        "ruff check .",
        "ruff format --check .",
        "node --test tests/site-search.test.cjs",
        "pytest --cov=ai_news",
        "--cov-fail-under=85",
        "python scripts/check_no_secrets.py",
        "python -m ai_news demo --root .demo --output .demo/dist",
        "python scripts/validate_site.py .demo/dist --base-path /ai-news/",
    ):
        assert command in commands
    assert commands.index("node --test tests/site-search.test.cjs") < commands.index(
        "pytest --cov=ai_news"
    )


def test_ci_actionlint_is_pinned_and_only_ignores_the_known_timezone_schema_gap() -> None:
    workflow = _load_workflow(CI_PATH)
    commands = _run_commands(_steps(workflow, "quality"))

    assert re.findall(r"rhysd/actionlint:\S+", commands) == [ACTIONLINT_IMAGE]
    assert ".github/workflows/ci.yml" in commands
    assert ".github/workflows/daily-news.yml" in commands
    assert ".github/workflows/source-health.yml" in commands
    assert "-ignore" in commands
    assert (
        'element of "schedule" section must be mapping and must contain one key "cron"' in commands
    )
    assert ".*timezone.*" not in commands


def test_source_health_workflow_is_read_only_bounded_and_uses_no_secrets() -> None:
    workflow = _load_workflow(SOURCE_HEALTH_PATH)

    assert workflow["on"] == {
        "pull_request": {
            "paths": [
                "sources/feeds.yaml",
                "src/ai_news/collectors/**",
                "src/ai_news/http.py",
                "src/ai_news/models.py",
                "src/ai_news/source_health.py",
                "scripts/check_source_health.py",
                ".github/workflows/source-health.yml",
            ]
        },
        "schedule": [{"cron": "17 7 * * *", "timezone": "Asia/Shanghai"}],
        "workflow_dispatch": {},
    }
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["live-source-health"]
    assert job["timeout-minutes"] == 10
    assert job["permissions"] == {"contents": "read"}

    steps = _steps(workflow, "live-source-health")
    assert steps[0]["uses"] == f"actions/checkout@{ACTION_PINS['actions/checkout'][0]}"
    assert steps[1]["uses"] == f"actions/setup-python@{ACTION_PINS['actions/setup-python'][0]}"
    assert steps[1]["with"] == {"python-version": "3.12", "cache": "pip"}
    commands = _run_commands(steps)
    assert "python -m pip install ." in commands
    assert "python scripts/check_source_health.py" in commands
    assert "secrets." not in SOURCE_HEALTH_PATH.read_text(encoding="utf-8")


def test_daily_triggers_manual_date_and_concurrency_contract() -> None:
    workflow = _load_workflow(DAILY_PATH)
    triggers = workflow["on"]
    assert isinstance(triggers, dict)

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["run-name"] == "Daily AI news / ${{ inputs.date || 'today' }}"
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["schedule"] == [{"cron": "17 8 * * *", "timezone": "Asia/Shanghai"}]
    assert triggers["workflow_dispatch"] == {
        "inputs": {
            "date": {
                "description": "Optional ISO date to regenerate",
                "required": False,
                "type": "string",
            }
        }
    }
    assert workflow["concurrency"] == {
        "group": "ai-news-daily",
        "cancel-in-progress": False,
    }


def test_daily_generate_is_write_scoped_conditional_and_maps_exact_environment() -> None:
    workflow = _load_workflow(DAILY_PATH)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    generate = jobs["generate"]
    assert isinstance(generate, dict)

    assert generate["if"] == (
        "${{ github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' }}"
    )
    assert generate["timeout-minutes"] == 30
    assert generate["permissions"] == {"contents": "write"}
    steps = _steps(workflow, "generate")
    assert steps[0] == {
        "name": "Check out main",
        "uses": f"actions/checkout@{ACTION_PINS['actions/checkout'][0]}",
        "with": {"ref": "main"},
    }
    generate_step = next(step for step in steps if step.get("name") == "Generate daily report")
    assert generate_step["env"] == {
        "LLM_PROTOCOL": "${{ vars.LLM_PROTOCOL }}",
        "LLM_BASE_URL": "${{ vars.LLM_BASE_URL }}",
        "LLM_API_KEY": "${{ secrets.LLM_API_KEY }}",
        "LLM_MODEL": "${{ vars.LLM_MODEL }}",
        "LLM_AUTH_SCHEME": "${{ vars.LLM_AUTH_SCHEME || 'bearer' }}",
        "SITE_BASE_PATH": "/ai-news/",
        "GITHUB_TOKEN": "${{ github.token }}",
        "INPUT_DATE": "${{ inputs.date }}",
    }


def test_daily_generate_captures_exit_then_persists_safe_data_before_propagating() -> None:
    workflow = _load_workflow(DAILY_PATH)
    steps = _steps(workflow, "generate")
    generate_step = next(step for step in steps if step.get("name") == "Generate daily report")
    command = str(generate_step["run"])

    assert generate_step["id"] == "generation"
    assert "python -m ai_news check-config" not in command
    assert "${{ inputs.date }}" not in command
    assert 'run_date="${INPUT_DATE:-}"' in command
    assert 'args+=(--date "$run_date")' in command
    assert 'python -m ai_news generate "${args[@]}"' in command
    assert "set +e" in command
    assert 'generation_exit_code="$?"' in command
    assert "exit_code=$generation_exit_code" in command
    assert "$GITHUB_OUTPUT" in command

    commit_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Commit generated data"
    )
    propagate_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Propagate generation result"
    )
    assert commit_index < propagate_index
    propagate = steps[propagate_index]
    assert propagate["if"] == "${{ always() }}"
    assert propagate["env"] == {"GENERATION_EXIT_CODE": "${{ steps.generation.outputs.exit_code }}"}
    assert 'exit "$GENERATION_EXIT_CODE"' in str(propagate["run"])


def test_daily_generate_never_logs_environment_or_credential_values() -> None:
    workflow = _load_workflow(DAILY_PATH)
    generate_step = next(
        step for step in _steps(workflow, "generate") if step.get("name") == "Generate daily report"
    )
    command = str(generate_step["run"])

    forbidden_patterns = (
        r"(?m)^\s*set\s+-[^#\n]*x",
        r"(?m)^\s*printenv(?:\s|$)",
        r"(?m)^\s*env(?:\s*(?:\||$))",
    )
    assert all(re.search(pattern, command) is None for pattern in forbidden_patterns)
    assert all(name not in command for name in ("LLM_API_KEY", "LLM_BASE_URL", "GITHUB_TOKEN"))


def test_daily_workflow_has_no_legacy_model_configuration_or_coding_endpoint() -> None:
    source = DAILY_PATH.read_text(encoding="utf-8")

    assert "ANTHROPIC_" not in source
    assert "/api/coding" not in source.casefold()


def test_daily_scans_then_commits_only_publication_data_with_default_token() -> None:
    workflow = _load_workflow(DAILY_PATH)
    steps = _steps(workflow, "generate")
    commands = _run_commands(steps)

    generate_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Generate daily report"
    )
    stage_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Stage generated data"
    )
    scan_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Scan for secrets"
    )
    commit_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Commit generated data"
    )
    assert generate_index < stage_index < scan_index < commit_index
    assert "git add -- data content" in commands
    assert "git diff --cached --quiet" in commands
    assert "github-actions[bot]" in commands
    assert "git push origin HEAD:main" in commands
    assert "git add ." not in commands
    assert "git add -A" not in commands


def test_daily_commit_rebases_and_retries_push_without_force() -> None:
    workflow = _load_workflow(DAILY_PATH)
    steps = _steps(workflow, "generate")
    command = str(
        next(step for step in steps if step.get("name") == "Commit generated data")["run"]
    )
    loop = command[command.index("for attempt") :]

    assert "for attempt in 1 2 3" in loop
    assert loop.index("git fetch origin main") < loop.index("git rebase origin/main")
    assert loop.index("git rebase origin/main") < loop.index("git push origin HEAD:main")
    assert "if git push origin HEAD:main" in loop
    assert "git rebase --abort" in loop
    assert "exit 1" in loop
    assert re.search(r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f(?:\s|$))", command) is None


def test_daily_commit_script_publishes_after_remote_advance_and_one_rejected_push(
    tmp_path: Path,
) -> None:
    workflow = _load_workflow(DAILY_PATH)
    commit_script = str(
        next(
            step
            for step in _steps(workflow, "generate")
            if step.get("name") == "Commit generated data"
        )["run"]
    )
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    worker = tmp_path / "worker"
    racer = tmp_path / "racer"
    published = tmp_path / "published"

    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.name", "Seed")
    _git(seed, "config", "user.email", "seed@example.invalid")
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", str(remote), str(worker)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(racer)], check=True, capture_output=True)

    _git(racer, "config", "user.name", "Racer")
    _git(racer, "config", "user.email", "racer@example.invalid")
    (racer / "remote-update.txt").write_text("remote advanced\n", encoding="utf-8")
    _git(racer, "add", "remote-update.txt")
    _git(racer, "commit", "-m", "advance remote")
    _git(racer, "push", "origin", "main")

    (worker / "data").mkdir()
    (worker / "content").mkdir()
    (worker / "data/report.json").write_text("{}\n", encoding="utf-8")
    (worker / "content/report.md").write_text("# report\n", encoding="utf-8")
    (worker / "local-only.txt").write_text("must remain untracked\n", encoding="utf-8")
    _git(worker, "add", "--", "data", "content")

    rejection_marker = remote / "rejected-once"
    hook = remote / "hooks/pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f"marker={shlex.quote(str(rejection_marker))}\n"
        'if [ ! -f "$marker" ]; then\n'
        '  : > "$marker"\n'
        "  exit 1\n"
        "fi\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    completed = subprocess.run(
        ["bash", "-c", commit_script],
        cwd=worker,
        check=True,
        capture_output=True,
        text=True,
    )

    assert rejection_marker.is_file()
    assert "Push attempt 1 of 3 failed; retrying." in completed.stderr
    subprocess.run(["git", "clone", str(remote), str(published)], check=True, capture_output=True)
    assert (published / "remote-update.txt").read_text(encoding="utf-8") == "remote advanced\n"
    assert (published / "data/report.json").read_text(encoding="utf-8") == "{}\n"
    assert (published / "content/report.md").read_text(encoding="utf-8") == "# report\n"
    assert not (published / "local-only.txt").exists()


def test_daily_build_reads_fresh_main_after_generate_success_or_skip() -> None:
    workflow = _load_workflow(DAILY_PATH)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build"]
    assert isinstance(build, dict)

    assert build["needs"] == "generate"
    assert build["if"] == (
        "${{ always() && (needs.generate.result == 'success' || "
        "needs.generate.result == 'skipped') }}"
    )
    assert build["permissions"] == {"contents": "read"}
    steps = _steps(workflow, "build")
    assert steps[0] == {
        "name": "Check out current main",
        "uses": f"actions/checkout@{ACTION_PINS['actions/checkout'][0]}",
        "with": {"ref": "main"},
    }
    commands = _run_commands(steps)
    assert "python -m ai_news build --root . --output dist" in commands
    assert "python scripts/validate_site.py dist --base-path /ai-news/" in commands
    provenance_step = next(step for step in steps if step.get("name") == "Record build provenance")
    provenance_command = str(provenance_step["run"])
    assert 'build_sha="$(git rev-parse HEAD)"' in provenance_command
    assert "AI_NEWS_BUILD_SHA" in provenance_command
    assert "AI_NEWS_BUILD_RUN_ID" in provenance_command
    assert "$GITHUB_ENV" in provenance_command
    assert "$GITHUB_STEP_SUMMARY" in provenance_command
    assert "Build SHA: %s" in provenance_command
    upload = next(
        step
        for step in steps
        if step.get("uses")
        == f"actions/upload-pages-artifact@{ACTION_PINS['actions/upload-pages-artifact'][0]}"
    )
    assert upload["with"] == {"path": "dist"}


def test_daily_deploy_has_minimal_pages_permissions_and_environment() -> None:
    workflow = _load_workflow(DAILY_PATH)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    deploy = jobs["deploy"]
    assert isinstance(deploy, dict)

    assert deploy["needs"] == "build"
    assert deploy["if"] == "${{ always() && needs.build.result == 'success' }}"
    assert deploy["permissions"] == {
        "pages": "write",
        "id-token": "write",
    }
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    assert deploy["steps"] == [
        {
            "name": "Deploy to GitHub Pages",
            "id": "deployment",
            "uses": f"actions/deploy-pages@{ACTION_PINS['actions/deploy-pages'][0]}",
        }
    ]
