from __future__ import annotations

import os
import subprocess
import sys
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ai_news import cli
from ai_news.models import Category, Settings, SourceConfig, SourceSpec
from ai_news.pipeline.run import AnalysisPipelineError, PublicationThresholdError
from ai_news.storage import load_reports

RUN_DATE = date(2026, 7, 26)


def _source_config() -> SourceConfig:
    return SourceConfig(
        sources=[
            SourceSpec(
                id="test-source",
                name="Test Source",
                kind="feed",
                url="https://example.com/feed.xml",
                category=Category.MODEL,
            )
        ]
    )


def _settings() -> Settings:
    return Settings(
        llm_base_url="https://api.example.com",
        llm_token="llm-secret",
        llm_model="test-model",
    )


def test_generate_dispatches_date_root_config_and_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    run_arguments: list[dict[str, Any]] = []
    sources_directory = tmp_path / "sources"
    sources_directory.mkdir()
    (sources_directory / "feeds.yaml").write_text(
        """
sources:
  - id: actual-layout
    name: Actual Layout
    kind: feed
    url: https://example.com/feed.xml
    category: 大模型
""",
        encoding="utf-8",
    )

    async def fake_run_daily(**kwargs: Any) -> object:
        run_arguments.append(kwargs)
        return object()

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "run_daily", fake_run_daily)

    result = cli.main(["generate", "--date", RUN_DATE.isoformat(), "--root", str(tmp_path)])

    assert result == 0
    assert len(run_arguments) == 1
    assert run_arguments[0]["root"] == tmp_path
    assert run_arguments[0]["run_date"] == RUN_DATE
    assert run_arguments[0]["settings"] == settings
    assert [source.id for source in run_arguments[0]["source_config"].sources] == ["actual-layout"]


def test_generate_uses_shanghai_today_when_date_is_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dates: list[date] = []
    monkeypatch.setattr(cli, "_shanghai_today", lambda: RUN_DATE)
    monkeypatch.setattr(cli, "load_source_config", lambda _path: _source_config())
    monkeypatch.setattr(cli, "load_settings", _settings)

    async def fake_run_daily(**kwargs: Any) -> object:
        run_dates.append(kwargs["run_date"])
        return object()

    monkeypatch.setattr(cli, "run_daily", fake_run_daily)

    assert cli.main(["generate", "--root", str(tmp_path)]) == 0
    assert run_dates == [RUN_DATE]


def test_invalid_iso_date_is_rejected_by_argparse_without_echoing_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_value = "not-a-date-secret-query"

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["generate", "--date", secret_value, "--root", "/tmp/root"])

    assert exit_info.value.code == 2
    assert capsys.readouterr().err == "configuration_error: ArgumentError\n"


@pytest.mark.parametrize(
    ("error_factory", "expected_code", "expected_category"),
    [
        (
            lambda: ValueError("configuration contains secret-token"),
            2,
            "configuration_error",
        ),
        (
            lambda: PublicationThresholdError("secret threshold details"),
            3,
            "publication_threshold",
        ),
        (
            lambda: AnalysisPipelineError("secret model endpoint?token=value"),
            4,
            "pipeline_error",
        ),
    ],
)
def test_generate_maps_safe_exit_categories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_factory: Any,
    expected_code: int,
    expected_category: str,
) -> None:
    error = error_factory()
    if expected_code == 2:
        monkeypatch.setattr(
            cli,
            "load_source_config",
            lambda _path: (_ for _ in ()).throw(error),
        )
    else:
        monkeypatch.setattr(cli, "load_source_config", lambda _path: _source_config())
        monkeypatch.setattr(cli, "load_settings", _settings)

        async def broken_run_daily(**_kwargs: Any) -> object:
            raise error

        monkeypatch.setattr(cli, "run_daily", broken_run_daily)

    result = cli.main(["generate", "--date", RUN_DATE.isoformat(), "--root", str(tmp_path)])
    stderr = capsys.readouterr().err

    assert result == expected_code
    assert stderr == f"{expected_category}: {type(error).__name__}\n"
    assert "secret" not in stderr
    assert "token=value" not in stderr


def test_build_dispatches_paths_without_loading_llm_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "dist"
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: pytest.fail("build must not load LLM settings"),
    )
    monkeypatch.setattr(
        cli,
        "_build_site",
        lambda root, selected_output: calls.append((root, selected_output)),
    )

    result = cli.main(["build", "--root", str(tmp_path), "--output", str(output)])

    assert result == 0
    assert calls == [(tmp_path, output)]


def test_lazy_build_adapter_imports_task8_builder_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "dist"
    calls: list[tuple[Path, Path]] = []
    site_package = types.ModuleType("ai_news.site")
    site_package.__path__ = []
    builder_module = types.ModuleType("ai_news.site.builder")
    builder_module.build_site = lambda root, selected_output: calls.append((root, selected_output))
    monkeypatch.setitem(sys.modules, "ai_news.site", site_package)
    monkeypatch.setitem(sys.modules, "ai_news.site.builder", builder_module)

    cli._build_site(tmp_path, output)

    assert calls == [(tmp_path, output)]


def test_build_failure_returns_five_with_safe_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken_builder(_root: Path, _output: Path) -> None:
        raise RuntimeError("https://secret.example/build?token=value")

    monkeypatch.setattr(cli, "_build_site", broken_builder)

    result = cli.main(["build", "--root", str(tmp_path), "--output", str(tmp_path / "dist")])
    stderr = capsys.readouterr().err

    assert result == 5
    assert stderr == "site_build_error: RuntimeError\n"
    assert "secret" not in stderr
    assert "token=value" not in stderr


def test_demo_is_offline_deterministic_and_builds_from_a_real_saved_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "dist"
    calls: list[tuple[Path, Path]] = []
    for environment_name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_AUTH_SCHEME",
    ):
        monkeypatch.delenv(environment_name, raising=False)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: pytest.fail("demo must not load LLM settings"),
    )
    monkeypatch.setattr(
        cli,
        "_build_site",
        lambda root, selected_output: calls.append((root, selected_output)),
    )

    first_result = cli.main(["demo", "--root", str(tmp_path), "--output", str(output)])
    first_report = load_reports(tmp_path)[0]
    second_result = cli.main(["demo", "--root", str(tmp_path), "--output", str(output)])
    second_report = load_reports(tmp_path)[0]

    assert first_result == second_result == 0
    assert len(first_report.items) >= 5
    assert first_report == second_report
    assert first_report.model == "offline-demo"
    assert calls == [(tmp_path, output), (tmp_path, output)]
    assert not any("ANTHROPIC" in name for name in os.environ)


def test_python_m_ai_news_dispatches_to_cli() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "ai_news", "--help"],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "generate" in completed.stdout
    assert "build" in completed.stdout
    assert "demo" in completed.stdout
