"""Command-line interface for the AI news radar."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_news.config import load_settings, load_source_config
from ai_news.models import (
    Analysis,
    Category,
    DailyReport,
    EditorialLane,
    NewsItem,
    RawItem,
    RunRecord,
    RunStatus,
    SourceRun,
)
from ai_news.pipeline.normalize import to_candidate
from ai_news.pipeline.run import (
    AnalysisPipelineError,
    ConfigurationPipelineError,
    PipelineError,
    PublicationThresholdError,
    run_daily,
)
from ai_news.storage import save_report, save_run_record

_CONFIGURATION_ERROR = "configuration_error"
_PIPELINE_ERROR = "pipeline_error"
_PUBLICATION_THRESHOLD = "publication_threshold"
_SITE_BUILD_ERROR = "site_build_error"
_DEMO_DATE = date(2026, 7, 26)
_DEMO_GENERATED_AT = datetime(2026, 7, 26, 8, tzinfo=UTC)
_BUILD_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print(f"{_CONFIGURATION_ERROR}: ArgumentError", file=sys.stderr)
        raise SystemExit(2)


def _parse_iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD")
    return parsed


def _shanghai_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _historical_cutoff(run_date: date) -> datetime:
    end_of_day = datetime.combine(
        run_date,
        datetime.max.time(),
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    return end_of_day.astimezone(UTC)


def _safe_error(category: str, error: BaseException) -> None:
    if (
        category == _PIPELINE_ERROR
        and isinstance(error, AnalysisPipelineError)
        and error.safe_code is not None
    ):
        print(f"{category}: {error.safe_code}", file=sys.stderr)
        return
    print(f"{category}: Error", file=sys.stderr)


def _build_site(
    root: Path,
    output: Path,
    *,
    build_revision: str = "local",
    workflow_run_id: int | None = None,
) -> None:
    from ai_news.site.builder import build_site

    build_site(
        root,
        output,
        build_revision=build_revision,
        workflow_run_id=workflow_run_id,
    )


def _build_provenance() -> tuple[str, int | None]:
    revision = os.environ.get("AI_NEWS_BUILD_SHA", "local")
    if revision != "local" and _BUILD_SHA_PATTERN.fullmatch(revision) is None:
        raise ValueError("invalid build revision")
    run_id_text = os.environ.get("AI_NEWS_BUILD_RUN_ID", "")
    if not run_id_text:
        return revision, None
    if not run_id_text.isascii() or not run_id_text.isdecimal():
        raise ValueError("invalid workflow run id")
    run_id = int(run_id_text)
    if run_id < 1:
        raise ValueError("invalid workflow run id")
    return revision, run_id


def _demo_report() -> DailyReport:
    sources = [
        ("demo-product", "Offline Product"),
        ("demo-business", "Offline Business"),
        ("demo-platform", "Offline Platform"),
        ("demo-ecosystem", "Offline Ecosystem"),
    ]
    lanes = [
        EditorialLane.PRODUCT_APPLICATION,
        EditorialLane.PRODUCT_APPLICATION,
        EditorialLane.PRODUCT_APPLICATION,
        EditorialLane.BUSINESS_MARKET,
        EditorialLane.BUSINESS_MARKET,
        EditorialLane.PLATFORM_POLICY,
        EditorialLane.PLATFORM_POLICY,
    ]
    items: list[NewsItem] = []
    for index, lane in enumerate(lanes):
        source_id, source_name = sources[index % len(sources)]
        category = Category.MODEL
        candidate = to_candidate(
            RawItem(
                source_id=source_id,
                source_name=source_name,
                source_weight=8,
                title=f"Offline AI release {index + 1}",
                url=f"https://example.com/ai-news/demo-{index + 1}",
                published_at=_DEMO_GENERATED_AT - timedelta(hours=index),
                excerpt=f"Deterministic offline source excerpt {index + 1}.",
                category_hint=category,
                is_official_source=True,
            )
        )
        analysis = Analysis(
            title=f"离线演示资讯 {index + 1}",
            category=category,
            editorial_lane=lane,
            summary=f"这是第 {index + 1} 条完全离线且可重复生成的演示资讯摘要内容。",
            importance=10 - index,
            why_it_matters=f"该演示条目用于验证第 {index + 1} 条日报数据与站点构建流程。",
            tags=["离线演示", f"条目-{index + 1}"],
            is_official=True,
            marketing_risk="low",
            tracking_signal=f"演示信号 {index + 1} 保持固定且不访问网络。",
        )
        items.append(NewsItem.from_candidate_analysis(candidate, analysis))

    return DailyReport(
        schema_version="1.3",
        date=_DEMO_DATE,
        generated_at=_DEMO_GENERATED_AT,
        model="offline-demo",
        degraded=False,
        candidate_count=len(items),
        items=items,
        source_runs=[
            SourceRun.succeeded(
                source_id=source_id,
                source_name=source_name,
                item_count=sum(item.source_id == source_id for item in items),
                elapsed_ms=0,
            )
            for source_id, source_name in sources
        ],
    )


def _generate(root: Path, run_date: date | None) -> int:
    today = _shanghai_today()
    effective_date = run_date or today
    cutoff_at = (
        _historical_cutoff(run_date) if run_date is not None and run_date < today else _utc_now()
    )
    try:
        source_config = load_source_config(root / "sources/feeds.yaml")
        settings = load_settings()
    except Exception as error:
        try:
            save_run_record(
                root,
                RunRecord(
                    date=effective_date,
                    cutoff_at=cutoff_at,
                    status=RunStatus.FAILED_BEFORE_PUBLISH,
                    source_runs=[],
                    safe_error_code="configuration_failed",
                ),
            )
        except Exception as persistence_error:
            _safe_error(_PIPELINE_ERROR, persistence_error)
            return 4
        _safe_error(_CONFIGURATION_ERROR, error)
        return 2

    run_arguments: dict[str, object] = {
        "root": root,
        "run_date": effective_date,
        "source_config": source_config,
        "settings": settings,
    }
    if run_date is not None and run_date < today:
        run_arguments["now"] = _historical_cutoff(run_date)

    try:
        asyncio.run(run_daily(**run_arguments))
    except ConfigurationPipelineError as error:
        _safe_error(_CONFIGURATION_ERROR, error)
        return 2
    except PublicationThresholdError:
        print(
            f"{_PUBLICATION_THRESHOLD}: no eligible items; keeping previous report",
            file=sys.stderr,
        )
        return 0
    except PipelineError as error:
        _safe_error(_PIPELINE_ERROR, error)
        return 4
    except Exception as error:
        _safe_error(_PIPELINE_ERROR, error)
        return 4
    return 0


def _check_config() -> int:
    try:
        load_settings()
    except Exception as error:
        _safe_error(_CONFIGURATION_ERROR, error)
        return 2
    return 0


def _build(root: Path, output: Path) -> int:
    try:
        build_revision, workflow_run_id = _build_provenance()
        _build_site(
            root,
            output,
            build_revision=build_revision,
            workflow_run_id=workflow_run_id,
        )
    except Exception as error:
        _safe_error(_SITE_BUILD_ERROR, error)
        return 5
    return 0


def _demo(root: Path, output: Path) -> int:
    try:
        save_report(root, _demo_report())
        _build_site(
            root,
            output,
            build_revision="offline-demo",
            workflow_run_id=None,
        )
    except Exception as error:
        _safe_error(_SITE_BUILD_ERROR, error)
        return 5
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="ai-news")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "check-config",
        help="validate model configuration without a request",
    )

    generate = commands.add_parser("generate", help="generate a daily report")
    generate.add_argument("--date", type=_parse_iso_date)
    generate.add_argument("--root", type=Path, required=True)

    build = commands.add_parser("build", help="build the static site")
    build.add_argument("--root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)

    demo = commands.add_parser("demo", help="generate deterministic offline demo data")
    demo.add_argument("--root", type=Path, required=True)
    demo.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "check-config":
        return _check_config()
    if arguments.command == "generate":
        return _generate(arguments.root, arguments.date)
    if arguments.command == "build":
        return _build(arguments.root, arguments.output)
    if arguments.command == "demo":
        return _demo(arguments.root, arguments.output)
    raise RuntimeError("unreachable")


__all__ = ["main"]
