"""Command-line interface for the AI news radar."""

from __future__ import annotations

import argparse
import asyncio
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
    NewsItem,
    RawItem,
    SourceRun,
)
from ai_news.pipeline.normalize import to_candidate
from ai_news.pipeline.run import (
    ConfigurationPipelineError,
    PipelineError,
    PublicationThresholdError,
    run_daily,
)
from ai_news.storage import save_report

_SAFE_EXCEPTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_DEMO_DATE = date(2026, 7, 26)
_DEMO_GENERATED_AT = datetime(2026, 7, 26, 8, tzinfo=UTC)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        print("configuration_error: ArgumentError", file=sys.stderr)
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


def _safe_error(category: str, error: BaseException) -> None:
    error_name = type(error).__name__
    if _SAFE_EXCEPTION_NAME.fullmatch(error_name) is None:
        error_name = "Error"
    print(f"{category}: {error_name}", file=sys.stderr)


def _build_site(root: Path, output: Path) -> None:
    from ai_news.site.build import build_site

    build_site(root, output)


def _demo_report() -> DailyReport:
    source_id = "demo-source"
    source_name = "Offline Demo"
    items: list[NewsItem] = []
    for index in range(6):
        candidate = to_candidate(
            RawItem(
                source_id=source_id,
                source_name=source_name,
                source_weight=8,
                title=f"Offline AI release {index + 1}",
                url=f"https://example.com/ai-news/demo-{index + 1}",
                published_at=_DEMO_GENERATED_AT - timedelta(hours=index),
                excerpt=f"Deterministic offline source excerpt {index + 1}.",
                category_hint=Category.MODEL,
                is_official_source=True,
            )
        )
        analysis = Analysis(
            title=f"离线演示资讯 {index + 1}",
            category=Category.MODEL,
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
                item_count=len(items),
                elapsed_ms=0,
            )
        ],
    )


def _generate(root: Path, run_date: date | None) -> int:
    try:
        source_config = load_source_config(root / "sources/sources.yml")
        settings = load_settings()
    except Exception as error:
        _safe_error("configuration_error", error)
        return 2

    try:
        asyncio.run(
            run_daily(
                root=root,
                run_date=run_date or _shanghai_today(),
                source_config=source_config,
                settings=settings,
            )
        )
    except ConfigurationPipelineError as error:
        _safe_error("configuration_error", error)
        return 2
    except PublicationThresholdError as error:
        _safe_error("publication_threshold", error)
        return 3
    except PipelineError as error:
        _safe_error("pipeline_error", error)
        return 4
    except Exception as error:
        _safe_error("pipeline_error", error)
        return 4
    return 0


def _build(root: Path, output: Path) -> int:
    try:
        _build_site(root, output)
    except Exception as error:
        _safe_error("site_build_error", error)
        return 5
    return 0


def _demo(root: Path, output: Path) -> int:
    try:
        save_report(root, _demo_report())
        _build_site(root, output)
    except Exception as error:
        _safe_error("site_build_error", error)
        return 5
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="ai-news")
    commands = parser.add_subparsers(dest="command", required=True)

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
    if arguments.command == "generate":
        return _generate(arguments.root, arguments.date)
    if arguments.command == "build":
        return _build(arguments.root, arguments.output)
    if arguments.command == "demo":
        return _demo(arguments.root, arguments.output)
    raise RuntimeError("unreachable")


__all__ = ["main"]
