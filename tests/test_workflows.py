from __future__ import annotations

import re
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).parents[1]
CI_PATH = REPOSITORY / ".github/workflows/ci.yml"
DAILY_PATH = REPOSITORY / ".github/workflows/daily-news.yml"


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

    assert steps[0]["uses"] == "actions/checkout@v6"
    assert steps[1]["uses"] == "actions/setup-python@v6"
    assert steps[1]["with"] == {"python-version": "3.12", "cache": "pip"}
    for command in (
        'python -m pip install ".[dev]"',
        "ruff check .",
        "ruff format --check .",
        "pytest --cov=ai_news",
        "--cov-fail-under=85",
        "python scripts/check_no_secrets.py",
        "python -m ai_news demo --root .demo --output .demo/dist",
        "python scripts/validate_site.py .demo/dist --base-path /ai-news/",
    ):
        assert command in commands


def test_ci_actionlint_is_pinned_and_only_ignores_the_known_timezone_schema_gap() -> None:
    workflow = _load_workflow(CI_PATH)
    commands = _run_commands(_steps(workflow, "quality"))

    assert "rhysd/actionlint:1.7.7" in commands
    assert ".github/workflows/ci.yml" in commands
    assert ".github/workflows/daily-news.yml" in commands
    assert "-ignore" in commands
    assert (
        'element of "schedule" section must be mapping and must contain one key "cron"' in commands
    )
    assert ".*timezone.*" not in commands


def test_daily_triggers_manual_date_and_concurrency_contract() -> None:
    workflow = _load_workflow(DAILY_PATH)
    triggers = workflow["on"]
    assert isinstance(triggers, dict)

    assert workflow["permissions"] == {"contents": "read"}
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
    assert generate["permissions"] == {"contents": "write"}
    steps = _steps(workflow, "generate")
    assert steps[0] == {
        "name": "Check out main",
        "uses": "actions/checkout@v6",
        "with": {"ref": "main"},
    }
    generate_step = next(step for step in steps if step.get("name") == "Generate daily report")
    assert generate_step["env"] == {
        "ANTHROPIC_BASE_URL": "${{ vars.ANTHROPIC_BASE_URL }}",
        "ANTHROPIC_AUTH_TOKEN": "${{ secrets.ANTHROPIC_AUTH_TOKEN }}",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "${{ vars.ANTHROPIC_DEFAULT_OPUS_MODEL }}",
        "ANTHROPIC_AUTH_SCHEME": "${{ vars.ANTHROPIC_AUTH_SCHEME || 'bearer' }}",
        "SITE_BASE_PATH": "/ai-news/",
        "GITHUB_TOKEN": "${{ github.token }}",
        "INPUT_DATE": "${{ inputs.date }}",
    }


def test_daily_generate_handles_optional_date_without_expression_interpolation() -> None:
    workflow = _load_workflow(DAILY_PATH)
    steps = _steps(workflow, "generate")
    generate_step = next(step for step in steps if step.get("name") == "Generate daily report")
    command = str(generate_step["run"])

    assert "${{ inputs.date }}" not in command
    assert 'run_date="${INPUT_DATE:-}"' in command
    assert 'args+=(--date "$run_date")' in command
    assert 'python -m ai_news generate "${args[@]}"' in command


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
        "uses": "actions/checkout@v6",
        "with": {"ref": "main"},
    }
    commands = _run_commands(steps)
    assert "python -m ai_news build --root . --output dist" in commands
    assert "python scripts/validate_site.py dist --base-path /ai-news/" in commands
    upload = next(step for step in steps if step.get("uses") == "actions/upload-pages-artifact@v4")
    assert upload["with"] == {"path": "dist"}


def test_daily_deploy_has_minimal_pages_permissions_and_environment() -> None:
    workflow = _load_workflow(DAILY_PATH)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    deploy = jobs["deploy"]
    assert isinstance(deploy, dict)

    assert deploy["needs"] == "build"
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
            "uses": "actions/deploy-pages@v4",
        }
    ]
