from __future__ import annotations

import os
import subprocess
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_news import cli
from ai_news.models import Category, RunStatus, Settings, SourceConfig, SourceSpec
from ai_news.pipeline.run import AnalysisPipelineError, PublicationThresholdError
from ai_news.storage import load_reports, load_run_records

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
        llm_protocol="anthropic",
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
    monkeypatch.setattr(cli, "_shanghai_today", lambda: date(2026, 8, 23))

    result = cli.main(["generate", "--date", RUN_DATE.isoformat(), "--root", str(tmp_path)])

    assert result == 0
    assert len(run_arguments) == 1
    assert run_arguments[0]["root"] == tmp_path
    assert run_arguments[0]["run_date"] == RUN_DATE
    assert run_arguments[0]["settings"] == settings
    assert run_arguments[0]["now"] == datetime(
        2026,
        7,
        26,
        15,
        59,
        59,
        999999,
        tzinfo=UTC,
    )
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


def test_generate_does_not_override_collection_clock_for_current_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_arguments: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "_shanghai_today", lambda: RUN_DATE)
    monkeypatch.setattr(cli, "load_source_config", lambda _path: _source_config())
    monkeypatch.setattr(cli, "load_settings", _settings)

    async def fake_run_daily(**kwargs: Any) -> object:
        run_arguments.append(kwargs)
        return object()

    monkeypatch.setattr(cli, "run_daily", fake_run_daily)

    assert cli.main(["generate", "--date", RUN_DATE.isoformat(), "--root", str(tmp_path)]) == 0
    assert "now" not in run_arguments[0]


def test_generate_configuration_failure_persists_safe_historical_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_shanghai_today", lambda: date(2026, 8, 30))
    monkeypatch.setattr(
        cli,
        "load_source_config",
        lambda _path: (_ for _ in ()).throw(ValueError("secret source config")),
    )

    result = cli.main(["generate", "--date", RUN_DATE.isoformat(), "--root", str(tmp_path)])

    assert result == 2
    assert capsys.readouterr().err == "configuration_error: Error\n"
    records = load_run_records(tmp_path)
    assert len(records) == 1
    assert records[0].date == RUN_DATE
    assert records[0].cutoff_at == datetime(
        2026,
        7,
        26,
        15,
        59,
        59,
        999999,
        tzinfo=UTC,
    )
    assert records[0].status is RunStatus.FAILED_BEFORE_PUBLISH
    assert records[0].safe_error_code == "configuration_failed"
    assert records[0].source_runs == []


def test_generate_configuration_record_failure_returns_safe_pipeline_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_shanghai_today", lambda: RUN_DATE)
    monkeypatch.setattr(cli, "_utc_now", lambda: datetime(2026, 7, 26, 12, tzinfo=UTC))
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: (_ for _ in ()).throw(ValueError("secret settings")),
    )
    monkeypatch.setattr(
        cli,
        "save_run_record",
        lambda *_args: (_ for _ in ()).throw(OSError("secret storage")),
    )

    result = cli.main(["generate", "--root", str(tmp_path)])

    assert result == 4
    assert capsys.readouterr().err == "pipeline_error: Error\n"


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
            0,
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
    expected_stderr = (
        "publication_threshold: no eligible items; keeping previous report\n"
        if expected_category == "publication_threshold"
        else f"{expected_category}: Error\n"
    )
    assert stderr == expected_stderr
    assert "secret" not in stderr
    assert "token=value" not in stderr


def test_generate_prints_allowlisted_analysis_diagnostic_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_source_config", lambda _path: _source_config())
    monkeypatch.setattr(cli, "load_settings", _settings)

    async def broken_run_daily(**_kwargs: Any) -> object:
        raise AnalysisPipelineError("analysis failed", safe_code="http_401")

    monkeypatch.setattr(cli, "run_daily", broken_run_daily)

    result = cli.main(["generate", "--date", RUN_DATE.isoformat(), "--root", str(tmp_path)])

    assert result == 4
    assert capsys.readouterr().err == "pipeline_error: http_401\n"


def test_check_config_loads_only_settings_and_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = object()
    calls: list[str] = []

    def load_settings() -> object:
        calls.append("load_settings")
        return settings

    monkeypatch.setattr(cli, "load_settings", load_settings)
    monkeypatch.setattr(
        cli,
        "load_source_config",
        lambda *_args, **_kwargs: pytest.fail("check-config must not load sources"),
    )
    monkeypatch.setattr(
        cli,
        "run_daily",
        lambda *_args, **_kwargs: pytest.fail("check-config must not make requests"),
    )
    monkeypatch.setattr(
        cli,
        "_build_site",
        lambda *_args, **_kwargs: pytest.fail("check-config must not build or write"),
    )
    monkeypatch.setattr(
        cli,
        "save_report",
        lambda *_args, **_kwargs: pytest.fail("check-config must not write reports"),
    )

    assert cli.main(["check-config"]) == 0
    assert calls == ["load_settings"]
    assert capsys.readouterr() == ("", "")


def test_check_config_failure_is_safe_and_does_not_retain_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "live-standard-api-key"
    secret_class_name = "UniqueSecretClassKey_7Z9"
    secret_error = type(secret_class_name, (ValueError,), {})

    def broken_load_settings() -> object:
        raise secret_error(f"invalid token {secret}")

    monkeypatch.setattr(cli, "load_settings", broken_load_settings)

    result = cli.main(["check-config"])
    captured = capsys.readouterr()

    assert result == 2
    assert captured.out == ""
    assert captured.err == "configuration_error: Error\n"
    assert secret not in captured.err
    assert secret_class_name not in captured.err
    assert all(not isinstance(value, BaseException) for value in vars(cli).values())


def test_check_config_ignores_model_and_source_files_and_needs_no_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources/feeds.yaml").write_text("not: valid: yaml", encoding="utf-8")
    (tmp_path / "models.yaml").write_text("not: valid: yaml", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: object())

    assert cli.main(["check-config"]) == 0


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
    assert stderr == "site_build_error: Error\n"
    assert "secret" not in stderr
    assert "token=value" not in stderr


def test_demo_is_offline_deterministic_and_builds_from_a_real_saved_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "dist"
    calls: list[tuple[Path, Path]] = []
    for environment_name in (
        "LLM_PROTOCOL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_AUTH_SCHEME",
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
    assert len(first_report.items) == 7
    assert first_report.schema_version == "1.3"
    assert first_report == second_report
    assert first_report.model == "offline-demo"
    assert calls == [(tmp_path, output), (tmp_path, output)]
    assert not any(name.startswith("LLM_") for name in os.environ)


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
    assert "check-config" in completed.stdout
    assert "validate model configuration without a request" in completed.stdout
