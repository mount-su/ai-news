"""Orchestrate one daily AI news report."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import ValidationError

from ai_news.collectors.arxiv import collect_arxiv
from ai_news.collectors.feed import parse_feed
from ai_news.collectors.github import collect_github_releases
from ai_news.http import get_bytes
from ai_news.llm.anthropic import AnthropicAnalyzer
from ai_news.models import (
    Analysis,
    Candidate,
    DailyReport,
    NewsItem,
    RawItem,
    Settings,
    SourceConfig,
    SourceRun,
    SourceSpec,
)
from ai_news.pipeline.dedupe import deduplicate
from ai_news.pipeline.normalize import ensure_utc, to_candidate
from ai_news.pipeline.rank import select_candidates
from ai_news.storage import load_history_urls, save_report

_COLLECTION_CONCURRENCY = 6
_DEDUPLICATION_LIMIT = 500
_ANALYSIS_LIMIT = 30
_PUBLICATION_LIMIT = 20
_PUBLICATION_THRESHOLD = 5
_WINDOW = timedelta(hours=36)


class PipelineError(RuntimeError):
    """Safe base error for daily pipeline failures."""


class ConfigurationPipelineError(PipelineError):
    """Safe pipeline configuration failure."""


class CollectionPipelineError(PipelineError):
    """Safe collection-stage failure."""


class DataPipelineError(PipelineError):
    """Safe history or candidate-processing failure."""


class AnalysisPipelineError(PipelineError):
    """Safe analysis-stage failure."""


class ReportValidationError(PipelineError):
    """Safe report validation failure."""


class PersistencePipelineError(PipelineError):
    """Safe report persistence failure."""


class PublicationThresholdError(PipelineError):
    """The report did not contain enough valid items to publish."""


class Collector(Protocol):
    async def __call__(
        self,
        client: httpx.AsyncClient,
        source: SourceSpec,
    ) -> list[RawItem]: ...


class Analyzer(Protocol):
    async def analyze(self, items: list[Candidate]) -> dict[str, Analysis]: ...


class ClientFactory(Protocol):
    def __call__(
        self,
        *,
        timeout: int,
    ) -> AbstractAsyncContextManager[httpx.AsyncClient]: ...


HistoryLoader = Callable[[Path, date, int], set[str]]
ReportSaver = Callable[[Path, DailyReport], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(start: float, monotonic: Callable[[], float]) -> int:
    return int(max(0.0, monotonic() - start) * 1000)


async def _collect_live(
    client: httpx.AsyncClient,
    source: SourceSpec,
) -> list[RawItem]:
    if source.kind == "feed":
        if source.url is None:
            raise ValueError("feed source requires a URL")
        payload = await get_bytes(client, str(source.url))
        return parse_feed(payload, source)
    if source.kind == "arxiv":
        return await collect_arxiv(client, source)
    if source.kind == "github":
        return await collect_github_releases(
            client,
            source,
            token=os.environ.get("GITHUB_TOKEN"),
        )
    raise ValueError("unsupported source kind")


async def _collect_one(
    *,
    client: httpx.AsyncClient,
    source: SourceSpec,
    collector: Collector,
    semaphore: asyncio.Semaphore,
    monotonic: Callable[[], float],
) -> tuple[SourceRun, list[RawItem]]:
    async with semaphore:
        start = monotonic()
        try:
            raw_items = await collector(client, source)
            if not isinstance(raw_items, list):
                raise TypeError("collector result must be a list")
            source_run = SourceRun.succeeded(
                source_id=source.id,
                source_name=source.name,
                item_count=len(raw_items),
                elapsed_ms=_elapsed_ms(start, monotonic),
            )
        except Exception as error:
            source_run = SourceRun.failed(
                source_id=source.id,
                source_name=source.name,
                elapsed_ms=_elapsed_ms(start, monotonic),
                error=error,
            )
            return source_run, []
    return source_run, raw_items


def _normalize_in_window(
    collection_results: Iterable[tuple[SourceRun, list[RawItem]]],
    now: datetime,
    historical_urls: set[str],
) -> list[Candidate]:
    window_start = now - _WINDOW
    historical = {str(url) for url in historical_urls}
    candidates: list[Candidate] = []
    for source_run, raw_items in collection_results:
        if not source_run.success:
            continue
        for raw_item in raw_items:
            try:
                candidate = to_candidate(raw_item)
                published_at = ensure_utc(candidate.raw.published_at)
            except Exception:
                continue
            if (
                window_start <= published_at <= now
                and str(candidate.canonical_url) not in historical
            ):
                candidates.append(candidate)
                if len(candidates) == _DEDUPLICATION_LIMIT * 2:
                    candidates = select_candidates(
                        candidates,
                        now,
                        limit=_DEDUPLICATION_LIMIT,
                    )
    if len(candidates) > _DEDUPLICATION_LIMIT:
        return select_candidates(candidates, now, limit=_DEDUPLICATION_LIMIT)
    return candidates


def _prepare_candidates(
    candidates: list[Candidate],
    historical_urls: set[str],
    now: datetime,
) -> list[Candidate]:
    deduplicated = deduplicate(candidates, historical_urls)
    return select_candidates(deduplicated, now, limit=_ANALYSIS_LIMIT)


def _validated_analyses(
    raw_results: object,
    selected: list[Candidate],
) -> dict[str, Analysis]:
    items_method = getattr(raw_results, "items", None)
    if not callable(items_method):
        raise AnalysisPipelineError("invalid analysis result")

    expected_ids = {candidate.id for candidate in selected}
    seen_ids: set[str] = set()
    validated: dict[str, Analysis] = {}
    try:
        result_items = items_method()
        for candidate_id, raw_analysis in result_items:
            if not isinstance(candidate_id, str):
                raise AnalysisPipelineError("invalid analysis id")
            if candidate_id in seen_ids:
                raise AnalysisPipelineError("duplicate analysis id")
            seen_ids.add(candidate_id)
            if candidate_id not in expected_ids:
                raise AnalysisPipelineError("unknown analysis id")
            try:
                validated[candidate_id] = Analysis.model_validate(raw_analysis)
            except (TypeError, ValueError, ValidationError):
                continue
    except AnalysisPipelineError:
        raise
    except Exception:
        raise AnalysisPipelineError("invalid analysis result") from None
    return validated


def _build_items(
    selected: list[Candidate],
    analyses: dict[str, Analysis],
) -> list[NewsItem]:
    items: list[NewsItem] = []
    for candidate in selected:
        analysis = analyses.get(candidate.id)
        if analysis is None:
            continue
        try:
            items.append(NewsItem.from_candidate_analysis(candidate, analysis))
        except (TypeError, ValueError, ValidationError):
            continue
    return sorted(
        items,
        key=lambda item: (
            -item.importance,
            -ensure_utc(item.published_at).timestamp(),
            item.id,
        ),
    )[:_PUBLICATION_LIMIT]


async def _run_with_client(
    *,
    client: httpx.AsyncClient,
    root: Path,
    run_date: date,
    source_config: SourceConfig,
    settings: Settings | None,
    collector: Collector | None,
    analyzer: Analyzer | None,
    now: datetime,
    model: str,
    monotonic: Callable[[], float],
    history_loader: HistoryLoader,
) -> DailyReport:
    selected_collector = collector or _collect_live
    enabled_sources = [source for source in source_config.sources if source.enabled]
    semaphore = asyncio.Semaphore(_COLLECTION_CONCURRENCY)
    collection_results = await asyncio.gather(
        *(
            _collect_one(
                client=client,
                source=source,
                collector=selected_collector,
                semaphore=semaphore,
                monotonic=monotonic,
            )
            for source in enabled_sources
        )
    )
    source_runs = [source_run for source_run, _ in collection_results]
    if not any(source_run.success for source_run in source_runs):
        raise CollectionPipelineError("no healthy sources")

    try:
        historical_urls = history_loader(root, run_date, 30)
        candidates = _normalize_in_window(collection_results, now, historical_urls)
        selected = _prepare_candidates(candidates, historical_urls, now)
    except Exception:
        raise DataPipelineError("candidate preparation failed") from None

    if not selected:
        raise PublicationThresholdError("fewer than five valid items")

    selected_analyzer = analyzer
    if selected_analyzer is None:
        if settings is None:
            raise ConfigurationPipelineError("settings are required")
        try:
            selected_analyzer = AnthropicAnalyzer(settings, client=client)
        except Exception:
            raise AnalysisPipelineError("analyzer initialization failed") from None

    try:
        raw_analyses = await selected_analyzer.analyze(selected)
    except PipelineError:
        raise
    except Exception:
        raise AnalysisPipelineError("analysis failed") from None
    analyses = _validated_analyses(raw_analyses, selected)
    items = _build_items(selected, analyses)
    if len(items) < _PUBLICATION_THRESHOLD:
        raise PublicationThresholdError("fewer than five valid items")

    try:
        report = DailyReport(
            date=run_date,
            generated_at=now,
            model=model,
            degraded=any(not source_run.success for source_run in source_runs),
            candidate_count=len(selected),
            items=items,
            source_runs=source_runs,
        )
    except Exception:
        raise ReportValidationError("report validation failed") from None

    return report


async def run_daily(
    *,
    root: Path,
    run_date: date,
    source_config: SourceConfig,
    settings: Settings | None = None,
    collector: Collector | None = None,
    analyzer: Analyzer | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] = _utc_now,
    model: str | None = None,
    client_factory: ClientFactory | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    history_loader: HistoryLoader = load_history_urls,
    report_saver: ReportSaver = save_report,
) -> DailyReport:
    """Collect, analyze, validate, and atomically persist one daily report."""
    try:
        normalized_now = ensure_utc(now if now is not None else clock())
        validated_config = SourceConfig.model_validate(source_config)
    except Exception:
        raise ConfigurationPipelineError("invalid pipeline configuration") from None

    selected_model = (
        model if model is not None else (settings.llm_model if settings is not None else None)
    )
    if selected_model is None:
        raise ConfigurationPipelineError("model is required")

    factory = client_factory or httpx.AsyncClient
    try:
        async with factory(timeout=20) as client:
            report = await _run_with_client(
                client=client,
                root=Path(root),
                run_date=run_date,
                source_config=validated_config,
                settings=settings,
                collector=collector,
                analyzer=analyzer,
                now=normalized_now,
                model=selected_model,
                monotonic=monotonic,
                history_loader=history_loader,
            )
    except PipelineError:
        raise
    except Exception:
        raise CollectionPipelineError("client initialization failed") from None

    try:
        report_saver(Path(root), report)
    except Exception:
        raise PersistencePipelineError("report persistence failed") from None
    return report


__all__ = [
    "AnalysisPipelineError",
    "Analyzer",
    "ClientFactory",
    "CollectionPipelineError",
    "Collector",
    "ConfigurationPipelineError",
    "DataPipelineError",
    "PersistencePipelineError",
    "PipelineError",
    "PublicationThresholdError",
    "ReportValidationError",
    "run_daily",
]
