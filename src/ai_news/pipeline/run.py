"""Orchestrate one daily AI news report."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ai_news.collectors.arxiv import collect_arxiv
from ai_news.collectors.feed import parse_feed
from ai_news.collectors.github import collect_github_releases
from ai_news.collectors.official_pages import collect_official_page
from ai_news.collectors.reddit_rss import collect_reddit_rss
from ai_news.editorial_policy import (
    MAX_ITEMS_PER_SOURCE,
    MAX_REPORT_ITEMS,
    MIN_PUBLISHED_ITEMS,
)
from ai_news.http import get_bytes
from ai_news.llm.base import AnalysisError
from ai_news.llm.factory import create_analyzer
from ai_news.models import (
    Analysis,
    Candidate,
    DailyReport,
    NewsItem,
    RawItem,
    RunRecord,
    RunStatus,
    Settings,
    SourceConfig,
    SourceRole,
    SourceRun,
    SourceSpec,
)
from ai_news.pipeline.dedupe import deduplicate, duplicate_preference_key
from ai_news.pipeline.editorial import is_editorially_eligible
from ai_news.pipeline.normalize import ensure_utc, normalize_title, to_candidate
from ai_news.pipeline.rank import (
    select_balanced_candidates,
    select_diversity_preserving_candidates,
)
from ai_news.storage import (
    load_history_urls,
    save_publication,
    save_run_record,
)

_COLLECTION_CONCURRENCY = 6
_DEDUPLICATION_LIMIT = 500
_DEDUPLICATION_SOURCE_FLOOR = 6
_EDITORIAL_CANDIDATE_LIMIT = 36
_EDITORIAL_SOURCE_LIMIT = 6
_EDITORIAL_UNOFFICIAL_SOURCE_LIMIT = 3
_WINDOW = timedelta(hours=120)
_ANALYSIS_FIELDS = frozenset(Analysis.model_fields)
_EXACT_STATE_LIMIT = _DEDUPLICATION_LIMIT * 6
_EXACT_STATE_TARGET = _DEDUPLICATION_LIMIT * 4


@dataclass(frozen=True)
class _PreparedCandidates:
    selected: tuple[Candidate, ...]
    latest_by_source: Mapping[str, datetime]
    recent_counts: Mapping[str, int]
    new_counts: Mapping[str, int]
    eligible_counts: Mapping[str, int]
    candidate_counts: Mapping[str, int]


@dataclass
class _RunState:
    source_runs: tuple[SourceRun, ...] = ()
    prepared: _PreparedCandidates | None = None
    selected_counts: Mapping[str, int] | None = None

    def source_run_snapshot(self) -> list[SourceRun]:
        if self.prepared is None:
            return list(self.source_runs)
        selected_counts = self.selected_counts
        return [
            source_run.with_diagnostics(
                recent_count=self.prepared.recent_counts.get(source_run.source_id, 0),
                new_count=self.prepared.new_counts.get(source_run.source_id, 0),
                eligible_count=self.prepared.eligible_counts.get(source_run.source_id, 0),
                candidate_count=self.prepared.candidate_counts.get(source_run.source_id, 0),
                selected_count=(
                    selected_counts.get(source_run.source_id, 0)
                    if selected_counts is not None
                    else None
                ),
                latest_published_at=self.prepared.latest_by_source.get(source_run.source_id),
            )
            if source_run.success
            else source_run
            for source_run in self.source_runs
        ]


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

    _SAFE_CODE = re.compile(
        r"^(?:http_[45][0-9]{2}|invalid_endpoint|redirect|response_too_large|"
        r"invalid_response|retry_exhausted|sensitive_output|invalid_input|"
        r"invalid_after_repair|internal_error)$"
    )

    def __init__(self, message: str, *, safe_code: str | None = None) -> None:
        super().__init__(message)
        self.safe_code = (
            safe_code if safe_code is not None and self._SAFE_CODE.fullmatch(safe_code) else None
        )


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
PublicationSaver = Callable[[Path, DailyReport, RunRecord], None]
RunRecordSaver = Callable[[Path, RunRecord], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(start: float, monotonic: Callable[[], float]) -> int:
    return int(max(0.0, monotonic() - start) * 1000)


async def _collect_live(
    client: httpx.AsyncClient,
    source: SourceSpec,
    *,
    reddit_allowed_domains: frozenset[str] = frozenset(),
) -> list[RawItem]:
    if source.kind == "feed":
        if source.url is None:
            raise ValueError("feed source requires a URL")
        payload = await get_bytes(client, str(source.url))
        return parse_feed(payload, source)
    if source.kind == "official_page":
        return await collect_official_page(client, source)
    if source.kind == "reddit_rss":
        return await collect_reddit_rss(
            client,
            source,
            official_domains=reddit_allowed_domains,
        )
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


def _candidate_preference_key(candidate: Candidate, now: datetime) -> tuple[object, ...]:
    del now
    return duplicate_preference_key(candidate)


def _exact_title_key(candidate: Candidate) -> tuple[str | None, str]:
    canonical = str(candidate.canonical_url)
    return (
        urlsplit(canonical).hostname,
        normalize_title(candidate.raw.title).casefold(),
    )


class _ExactCandidateAccumulator:
    def __init__(self, now: datetime) -> None:
        self._now = now
        self._next_group_id = 0
        self._winners: dict[int, Candidate] = {}
        self._canonical_groups: dict[str, int] = {}
        self._title_groups: dict[tuple[str | None, str], int] = {}

    def _state_size(self) -> int:
        return len(self._winners) + len(self._canonical_groups) + len(self._title_groups)

    def _new_group(self, candidate: Candidate) -> None:
        group_id = self._next_group_id
        self._next_group_id += 1
        self._winners[group_id] = candidate
        self._canonical_groups[str(candidate.canonical_url)] = group_id
        self._title_groups[_exact_title_key(candidate)] = group_id

    def _compact(self) -> None:
        retained_candidates = select_diversity_preserving_candidates(
            list(self._winners.values()),
            self._now,
            limit=_DEDUPLICATION_LIMIT,
            source_floor=_DEDUPLICATION_SOURCE_FLOOR,
        )
        retained_ids = {candidate.id for candidate in retained_candidates}
        retained_winners = {
            group_id: winner
            for group_id, winner in self._winners.items()
            if winner.id in retained_ids
        }

        mandatory_canonical = {
            str(winner.canonical_url): group_id for group_id, winner in retained_winners.items()
        }
        mandatory_titles = {
            _exact_title_key(winner): group_id for group_id, winner in retained_winners.items()
        }
        optional_aliases: list[
            tuple[
                tuple[str, ...],
                str | None,
                tuple[str | None, str] | None,
                int,
            ]
        ] = []
        for canonical, group_id in self._canonical_groups.items():
            if group_id in retained_winners and mandatory_canonical.get(canonical) != group_id:
                optional_aliases.append((("canonical", canonical), canonical, None, group_id))
        for title, group_id in self._title_groups.items():
            if group_id in retained_winners and mandatory_titles.get(title) != group_id:
                optional_aliases.append(
                    (
                        ("title", title[0] or "", title[1]),
                        None,
                        title,
                        group_id,
                    )
                )
        optional_aliases.sort(key=lambda alias: alias[0])

        self._winners = retained_winners
        self._canonical_groups = mandatory_canonical
        self._title_groups = mandatory_titles
        remaining_budget = _EXACT_STATE_TARGET - self._state_size()
        for _, canonical, title, group_id in optional_aliases[:remaining_budget]:
            if canonical is not None:
                self._canonical_groups[canonical] = group_id
            elif title is not None:
                self._title_groups[title] = group_id

    def _merge_groups(self, group_ids: set[int], candidate: Candidate) -> int:
        target_group = min(group_ids)
        winner = min(
            [candidate, *(self._winners[group_id] for group_id in group_ids)],
            key=lambda item: _candidate_preference_key(item, self._now),
        )
        self._winners[target_group] = winner
        if len(group_ids) > 1:
            for group_id in group_ids:
                if group_id != target_group:
                    del self._winners[group_id]
            for canonical, group_id in list(self._canonical_groups.items()):
                if group_id in group_ids:
                    self._canonical_groups[canonical] = target_group
            for title, group_id in list(self._title_groups.items()):
                if group_id in group_ids:
                    self._title_groups[title] = target_group
        return target_group

    def add(self, candidate: Candidate) -> None:
        if self._state_size() > _EXACT_STATE_LIMIT - 3:
            self._compact()

        canonical = str(candidate.canonical_url)
        title = _exact_title_key(candidate)
        group_ids = {
            group_id
            for group_id in (
                self._canonical_groups.get(canonical),
                self._title_groups.get(title),
            )
            if group_id is not None
        }
        if group_ids:
            group_id = self._merge_groups(group_ids, candidate)
            self._canonical_groups[canonical] = group_id
            self._title_groups[title] = group_id
        else:
            self._new_group(candidate)

    def selected(self) -> list[Candidate]:
        if len(self._winners) > _DEDUPLICATION_LIMIT or self._state_size() > _EXACT_STATE_TARGET:
            self._compact()
        return select_diversity_preserving_candidates(
            list(self._winners.values()),
            self._now,
            limit=_DEDUPLICATION_LIMIT,
            source_floor=_DEDUPLICATION_SOURCE_FLOOR,
        )


def _prepare_candidates(
    candidates: list[Candidate],
    historical_urls: set[str],
    now: datetime,
) -> list[Candidate]:
    deduplicated = [
        candidate
        for candidate in deduplicate(candidates, historical_urls)
        if is_editorially_eligible(candidate)
    ]
    return select_balanced_candidates(
        deduplicated,
        now,
        limit=_EDITORIAL_CANDIDATE_LIMIT,
        per_source=_EDITORIAL_SOURCE_LIMIT,
        unofficial_per_source=_EDITORIAL_UNOFFICIAL_SOURCE_LIMIT,
    )


def _prepare_collected_candidates(
    collection_results: Iterable[tuple[SourceRun, list[RawItem]]],
    historical_urls: set[str],
    now: datetime,
) -> _PreparedCandidates:
    window_start = now - _WINDOW
    historical = {str(url) for url in historical_urls}
    latest_by_source: dict[str, datetime] = {}
    recent_counts: Counter[str] = Counter()
    new_counts: Counter[str] = Counter()
    eligible_counts: Counter[str] = Counter()
    accumulator = _ExactCandidateAccumulator(now)
    for source_run, raw_items in collection_results:
        if not source_run.success:
            continue
        for raw_item in raw_items:
            try:
                published_at = ensure_utc(raw_item.published_at)
            except Exception:
                continue
            latest_by_source[source_run.source_id] = max(
                latest_by_source.get(source_run.source_id, published_at),
                published_at,
            )
            try:
                candidate = to_candidate(raw_item)
            except Exception:
                continue
            if not window_start <= published_at <= now:
                continue
            recent_counts[source_run.source_id] += 1
            if str(candidate.canonical_url) in historical:
                continue
            new_counts[source_run.source_id] += 1
            if is_editorially_eligible(candidate):
                eligible_counts[source_run.source_id] += 1
            accumulator.add(candidate)

    selected = _prepare_candidates(accumulator.selected(), historical, now)
    return _PreparedCandidates(
        selected=tuple(selected),
        latest_by_source=MappingProxyType(dict(latest_by_source)),
        recent_counts=MappingProxyType(dict(recent_counts)),
        new_counts=MappingProxyType(dict(new_counts)),
        eligible_counts=MappingProxyType(dict(eligible_counts)),
        candidate_counts=MappingProxyType(
            dict(Counter(candidate.raw.source_id for candidate in selected))
        ),
    )


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
            if isinstance(raw_analysis, Analysis):
                analysis_data = raw_analysis.model_dump()
            elif isinstance(raw_analysis, Mapping):
                analysis_data = dict(raw_analysis)
            else:
                continue
            if set(analysis_data) != _ANALYSIS_FIELDS:
                continue
            try:
                validated[candidate_id] = Analysis.model_validate(
                    analysis_data,
                    strict=True,
                )
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
        if candidate.raw.source_role != SourceRole.PRIMARY and analysis.marketing_risk == "high":
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
    )


def _validate_editorial_distribution(
    items: list[NewsItem],
    selected: list[Candidate],
) -> None:
    if len(items) < MIN_PUBLISHED_ITEMS:
        raise PublicationThresholdError("no valid editorial items")
    if len(items) > MAX_REPORT_ITEMS:
        raise ReportValidationError("editorial distribution invalid")

    candidate_by_id = {candidate.id: candidate for candidate in selected}
    try:
        source_counts = Counter(candidate_by_id[item.id].raw.source_id for item in items)
    except KeyError:
        raise ReportValidationError("editorial distribution invalid") from None
    if any(count > MAX_ITEMS_PER_SOURCE for count in source_counts.values()):
        raise ReportValidationError("editorial distribution invalid")


def _safe_run_error_code(error: PipelineError) -> str:
    if isinstance(error, PublicationThresholdError):
        return "no_eligible_candidates"
    if isinstance(error, ConfigurationPipelineError):
        return "configuration_failed"
    if isinstance(error, CollectionPipelineError):
        return "no_healthy_sources" if str(error) == "no healthy sources" else "collection_failed"
    if isinstance(error, DataPipelineError):
        return "candidate_preparation_failed"
    if isinstance(error, AnalysisPipelineError):
        return "analysis_failed"
    if isinstance(error, ReportValidationError):
        return "report_validation_failed"
    if isinstance(error, PersistencePipelineError):
        return "persistence_failed"
    return "collection_failed"


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
    state: _RunState,
) -> DailyReport:
    if collector is None:
        reddit_allowed_domains = frozenset(
            hostname
            for source in source_config.sources
            if source.enabled
            and source.role in {SourceRole.PRIMARY, SourceRole.TRUSTED_MEDIA}
            and source.url is not None
            and source.url.scheme == "https"
            if (hostname := urlsplit(str(source.url)).hostname) is not None
        )

        async def selected_collector(
            selected_client: httpx.AsyncClient,
            selected_source: SourceSpec,
        ) -> list[RawItem]:
            return await _collect_live(
                selected_client,
                selected_source,
                reddit_allowed_domains=reddit_allowed_domains,
            )

    else:
        selected_collector = collector
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
    state.source_runs = tuple(source_runs)
    if not any(source_run.success for source_run in source_runs):
        raise CollectionPipelineError("no healthy sources")

    try:
        historical_urls = history_loader(root, run_date, 30)
        prepared = _prepare_collected_candidates(collection_results, historical_urls, now)
        state.prepared = prepared
    except Exception:
        raise DataPipelineError("candidate preparation failed") from None

    selected = list(prepared.selected)
    if not selected:
        state.selected_counts = MappingProxyType({})
        raise PublicationThresholdError("no eligible candidates")

    selected_analyzer = analyzer
    if selected_analyzer is None:
        if settings is None:
            raise ConfigurationPipelineError("settings are required")
        initialization_failed = False
        try:
            selected_analyzer = create_analyzer(settings, client=client)
        except Exception:
            initialization_failed = True
        if initialization_failed:
            raise AnalysisPipelineError("analyzer initialization failed") from None

    try:
        raw_analyses = await selected_analyzer.analyze(selected)
    except PipelineError:
        raise
    except AnalysisError as error:
        raise AnalysisPipelineError(
            "analysis failed",
            safe_code=error.safe_code,
        ) from None
    except Exception:
        raise AnalysisPipelineError("analysis failed") from None
    analyses = _validated_analyses(raw_analyses, selected)
    items = _build_items(selected, analyses)
    candidate_by_id = {candidate.id: candidate for candidate in selected}
    selected_counts = Counter(candidate_by_id[item.id].raw.source_id for item in items)
    state.selected_counts = MappingProxyType(dict(selected_counts))
    _validate_editorial_distribution(items, selected)
    source_runs = state.source_run_snapshot()

    try:
        report = DailyReport(
            schema_version="1.3",
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
    publication_saver: PublicationSaver = save_publication,
    run_record_saver: RunRecordSaver = save_run_record,
) -> DailyReport:
    """Collect, analyze, and atomically persist a report with its run outcome."""
    state = _RunState()
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
                state=state,
            )
    except PipelineError as error:
        pipeline_error = error
    except Exception:
        pipeline_error = CollectionPipelineError("client initialization failed")
    else:
        pipeline_error = None

    if pipeline_error is not None:
        safe_error_code = _safe_run_error_code(pipeline_error)
        status = (
            RunStatus.NO_ELIGIBLE_CONTENT
            if isinstance(pipeline_error, PublicationThresholdError)
            else RunStatus.FAILED_BEFORE_PUBLISH
        )
        record = RunRecord(
            date=run_date,
            cutoff_at=normalized_now,
            status=status,
            source_runs=state.source_run_snapshot(),
            safe_error_code=safe_error_code,
        )
        try:
            run_record_saver(Path(root), record)
        except Exception:
            raise PersistencePipelineError("run record persistence failed") from None
        raise pipeline_error

    published_record = RunRecord(
        date=run_date,
        cutoff_at=normalized_now,
        status=RunStatus.PUBLISHED,
        source_runs=report.source_runs,
        safe_error_code="none",
    )

    try:
        publication_saver(Path(root), report, published_record)
    except Exception:
        failed_record = RunRecord(
            date=run_date,
            cutoff_at=normalized_now,
            status=RunStatus.FAILED_BEFORE_PUBLISH,
            source_runs=state.source_run_snapshot(),
            safe_error_code="persistence_failed",
        )
        try:
            run_record_saver(Path(root), failed_record)
        except Exception:
            raise PersistencePipelineError("run record persistence failed") from None
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
