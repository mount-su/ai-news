from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_news.models import (
    Analysis,
    Category,
    DailyReport,
    EditorialLane,
    RawItem,
    Settings,
    SourceConfig,
    SourceRun,
    SourceSpec,
)
from ai_news.pipeline.run import (
    AnalysisPipelineError,
    CollectionPipelineError,
    PublicationThresholdError,
    ReportValidationError,
    run_daily,
)
from ai_news.storage import save_report

RUN_DATE = date(2026, 7, 26)
NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


def _source(
    number: int,
    *,
    kind: str = "feed",
    enabled: bool = True,
    official: bool = True,
    role: str | None = None,
    hostname: str = "example.com",
) -> SourceSpec:
    values: dict[str, Any] = {
        "id": f"source-{number}",
        "name": f"Source {number}",
        "kind": kind,
        "category": Category.MODEL,
        "weight": 7,
        "official": official,
        "enabled": enabled,
    }
    if role is not None:
        values["role"] = role
    if kind in {"feed", "arxiv"}:
        values["url"] = f"https://{hostname}/{number}.xml"
    elif kind == "official_page":
        values["url"] = "https://www.anthropic.com/news"
        values["adapter"] = "anthropic_news"
    elif kind == "reddit_rss":
        values["communities"] = ["LocalLLaMA", "OpenAI"]
        values["weight"] = 5
        values["official"] = False
        values["role"] = "discovery"
    else:
        values["repo"] = f"owner/repo-{number}"
    return SourceSpec.model_validate(values)


def _raw(
    number: int,
    *,
    source: SourceSpec | None = None,
    published_at: datetime = NOW,
    query: str = "",
    discovery_verified: bool = False,
) -> RawItem:
    selected_source = source or _source(0)
    return RawItem(
        source_id=selected_source.id,
        source_name=selected_source.name,
        source_weight=selected_source.weight,
        title=f"Release {hashlib.sha256(str(number).encode()).hexdigest()}",
        url=f"https://example.com/news/{number}{query}",
        published_at=published_at,
        excerpt=f"Factual excerpt {number}",
        category_hint=selected_source.category,
        is_official_source=selected_source.official,
        source_role=selected_source.role,
        discovery_verified=discovery_verified,
    )


def _analysis(
    number: int,
    *,
    importance: int = 8,
    editorial_lane: EditorialLane = EditorialLane.PRODUCT_ENGINEERING,
) -> Analysis:
    return Analysis(
        title=f"分析标题 {number}",
        category=Category.MODEL,
        editorial_lane=editorial_lane,
        summary=f"这是第 {number} 条满足长度要求并且完全基于候选事实的中文摘要内容。",
        importance=importance,
        why_it_matters=f"第 {number} 条进展可能影响产品能力和后续行业采用。",
        tags=["模型", f"item-{number}"],
        is_official=True,
        marketing_risk="low",
        tracking_signal=f"继续关注第 {number} 条信息的官方更新。",
    )


def _updated_raw(raw: RawItem, **updates: Any) -> RawItem:
    raw_data = raw.model_dump()
    raw_data.update(updates)
    return RawItem.model_validate(raw_data)


def _select_with_source_cap(items: list[Any], *, limit: int = 7) -> list[Any]:
    source_counts: dict[str, int] = {}
    selected: list[Any] = []
    for item in items:
        source_id = item.raw.source_id
        if source_counts.get(source_id, 0) >= 2:
            continue
        source_counts[source_id] = source_counts.get(source_id, 0) + 1
        selected.append(item)
        if len(selected) == limit:
            break
    return selected


class _Analyzer:
    def __init__(
        self,
        result_factory: Callable[[list[Any]], Any] | None = None,
    ) -> None:
        self.calls: list[list[Any]] = []
        self._result_factory = result_factory

    async def analyze(self, items: list[Any]) -> Any:
        self.calls.append(items)
        if self._result_factory is not None:
            return self._result_factory(items)
        lanes = [
            EditorialLane.PRODUCT_ENGINEERING,
            EditorialLane.PRODUCT_ENGINEERING,
            EditorialLane.PRODUCT_ENGINEERING,
            EditorialLane.BUSINESS,
            EditorialLane.BUSINESS,
            EditorialLane.BUSINESS,
            EditorialLane.RESEARCH,
        ]
        selected = _select_with_source_cap(items)
        return {
            item.id: _analysis(index, editorial_lane=lane)
            for index, (item, lane) in enumerate(zip(selected, lanes, strict=False))
        }


def _collector(items_by_source: dict[str, list[RawItem]]) -> Callable[..., Any]:
    async def collect(_client: Any, source: SourceSpec) -> list[RawItem]:
        return items_by_source[source.id]

    return collect


def _with_filler_sources(
    primary_source: SourceSpec,
    primary_items: list[RawItem],
    *,
    filler_count: int = 3,
) -> tuple[SourceConfig, Callable[..., Any]]:
    filler_sources = [_source(900 + index) for index in range(filler_count)]
    items_by_source = {
        primary_source.id: primary_items,
        **{
            source.id: [
                _raw(900_000 + source_index * 100 + item_index, source=source)
                for item_index in range(3)
            ]
            for source_index, source in enumerate(filler_sources)
        },
    }
    return (
        SourceConfig(sources=[primary_source, *filler_sources]),
        _collector(items_by_source),
    )


def _run(**kwargs: Any) -> DailyReport:
    defaults = {
        "root": Path("/tmp/not-written"),
        "run_date": RUN_DATE,
        "source_config": SourceConfig(sources=[_source(0)]),
        "now": NOW,
        "model": "test-model",
        "history_loader": lambda _root, _date, _days: set(),
        "report_saver": lambda _root, _report: None,
    }
    defaults.update(kwargs)
    return asyncio.run(run_daily(**defaults))


def _source_diagnostics(report: DailyReport) -> dict[str, dict[str, Any]]:
    return {
        source_run.source_id: source_run.model_dump(
            include={
                "item_count",
                "recent_count",
                "new_count",
                "eligible_count",
                "candidate_count",
                "selected_count",
                "latest_published_at",
            }
        )
        for source_run in report.source_runs
    }


def test_source_failure_does_not_block_healthy_source_and_marks_degraded() -> None:
    failed_source = _source(0)
    healthy_sources = [_source(index) for index in range(1, 4)]

    async def collect(_client: Any, source: SourceSpec) -> list[RawItem]:
        if source.id == failed_source.id:
            raise RuntimeError("private-token-and-query-value")
        source_number = int(source.id.rsplit("-", 1)[1])
        return [_raw(source_number * 100 + index, source=source) for index in range(3)]

    report = _run(
        source_config=SourceConfig(sources=[failed_source, *healthy_sources]),
        collector=collect,
        analyzer=_Analyzer(),
    )

    assert report.degraded is True
    assert [run.source_id for run in report.source_runs] == [
        failed_source.id,
        *(source.id for source in healthy_sources),
    ]
    assert report.source_runs[0].success is False
    assert report.source_runs[0].error_type == "RuntimeError"
    assert report.source_runs[0].item_count is None
    assert report.source_runs[1].success is True
    assert all(run.item_count == 3 for run in report.source_runs[1:])
    assert "private-token" not in report.model_dump_json()


def test_source_diagnostics_record_each_pipeline_stage_and_are_order_independent() -> None:
    first_source = _source(0)
    second_source = _source(1)
    historical_url = "https://example.com/diagnostics/historical"
    exact_url = "https://example.com/diagnostics/exact"
    first_latest = NOW + timedelta(hours=1)
    second_latest = NOW - timedelta(minutes=1)
    first_items = [
        _updated_raw(
            _raw(1_000, source=first_source),
            url="https://example.com/diagnostics/outside",
            published_at=NOW - timedelta(hours=121),
        ),
        _updated_raw(
            _raw(1_001, source=first_source),
            url="https://example.com/diagnostics/future",
            published_at=first_latest,
        ),
        _updated_raw(
            _raw(1_002, source=first_source),
            url=historical_url,
            published_at=NOW - timedelta(hours=2),
        ),
        _updated_raw(
            _raw(1_003, source=first_source),
            url="https://example.com/diagnostics/rejected",
            title="Generic tutorial template for teams",
            published_at=NOW - timedelta(hours=3),
        ),
        _updated_raw(
            _raw(1_004, source=first_source),
            url=exact_url,
            title="Shared exact enterprise assistant",
            source_weight=10,
            published_at=NOW - timedelta(minutes=4),
        ),
        _updated_raw(
            _raw(1_005, source=first_source),
            url="https://example.com/diagnostics/fuzzy-first",
            title="Launch Aurora Enterprise AI Assistant for Teams",
            source_weight=10,
            published_at=NOW - timedelta(minutes=5),
        ),
        _updated_raw(
            _raw(1_006, source=first_source),
            url="https://example.com/diagnostics/first-selected",
            title="Product availability expands to teams",
            published_at=NOW - timedelta(minutes=6),
        ),
    ]
    second_items = [
        _updated_raw(
            _raw(2_000, source=second_source),
            url=exact_url,
            title="Exact duplicate from the second source",
            source_weight=1,
            published_at=second_latest,
        ),
        _updated_raw(
            _raw(2_001, source=second_source),
            url="https://example.com/diagnostics/fuzzy-second",
            title="Launch Aurora Enterprise AI Assistant for Team",
            source_weight=1,
            published_at=NOW - timedelta(minutes=7),
        ),
        _updated_raw(
            _raw(2_002, source=second_source),
            url="https://example.com/diagnostics/second-selected-a",
            title="New assistant access for customers",
            published_at=NOW - timedelta(minutes=8),
        ),
        _updated_raw(
            _raw(2_003, source=second_source),
            url="https://example.com/diagnostics/second-selected-b",
            title="New assistant service for businesses",
            published_at=NOW - timedelta(minutes=9),
        ),
    ]

    def analyzed_items(candidates: list[Any]) -> dict[str, Analysis]:
        return {
            candidate.id: _analysis(index)
            for index, candidate in enumerate(candidates)
            if candidate.raw.title != "Launch Aurora Enterprise AI Assistant for Teams"
        }

    def run_with_order(
        sources: list[SourceSpec],
        items_by_source: dict[str, list[RawItem]],
        delays: dict[str, float],
    ) -> tuple[DailyReport, list[str]]:
        completion_order: list[str] = []

        async def collect(_client: Any, source: SourceSpec) -> list[RawItem]:
            await asyncio.sleep(delays[source.id])
            completion_order.append(source.id)
            return items_by_source[source.id]

        report = _run(
            source_config=SourceConfig(sources=sources),
            collector=collect,
            analyzer=_Analyzer(analyzed_items),
            history_loader=lambda _root, _date, _days: {historical_url},
            monotonic=lambda: 100.0,
        )
        return report, completion_order

    first_report, first_completion = run_with_order(
        [first_source, second_source],
        {first_source.id: first_items, second_source.id: second_items},
        {first_source.id: 0.01, second_source.id: 0.0},
    )
    second_report, second_completion = run_with_order(
        [second_source, first_source],
        {
            first_source.id: list(reversed(first_items)),
            second_source.id: list(reversed(second_items)),
        },
        {first_source.id: 0.0, second_source.id: 0.01},
    )

    expected = {
        first_source.id: {
            "item_count": 7,
            "recent_count": 5,
            "new_count": 4,
            "eligible_count": 3,
            "candidate_count": 3,
            "selected_count": 2,
            "latest_published_at": first_latest,
        },
        second_source.id: {
            "item_count": 4,
            "recent_count": 4,
            "new_count": 4,
            "eligible_count": 4,
            "candidate_count": 2,
            "selected_count": 2,
            "latest_published_at": second_latest,
        },
    }
    assert first_completion == [second_source.id, first_source.id]
    assert second_completion == [first_source.id, second_source.id]
    assert _source_diagnostics(first_report) == expected
    assert _source_diagnostics(second_report) == expected
    assert first_report.candidate_count == second_report.candidate_count == 5
    assert len(first_report.items) == len(second_report.items) == 4


def test_source_diagnostics_leave_failed_collection_unknown() -> None:
    failed_source = _source(0)
    healthy_source = _source(1)

    async def collect(_client: Any, source: SourceSpec) -> list[RawItem]:
        if source.id == failed_source.id:
            raise RuntimeError("private failure details")
        return [_raw(3_000 + index, source=source) for index in range(3)]

    report = _run(
        source_config=SourceConfig(sources=[failed_source, healthy_source]),
        collector=collect,
        analyzer=_Analyzer(),
    )

    assert _source_diagnostics(report)[failed_source.id] == {
        "item_count": None,
        "recent_count": None,
        "new_count": None,
        "eligible_count": None,
        "candidate_count": None,
        "selected_count": None,
        "latest_published_at": None,
    }


def test_source_diagnostics_record_successful_empty_collection_as_zero() -> None:
    empty_source = _source(0)
    healthy_source = _source(1)
    items_by_source = {
        empty_source.id: [],
        healthy_source.id: [_raw(4_000 + index, source=healthy_source) for index in range(3)],
    }

    report = _run(
        source_config=SourceConfig(sources=[empty_source, healthy_source]),
        collector=_collector(items_by_source),
        analyzer=_Analyzer(),
    )

    assert _source_diagnostics(report)[empty_source.id] == {
        "item_count": 0,
        "recent_count": 0,
        "new_count": 0,
        "eligible_count": 0,
        "candidate_count": 0,
        "selected_count": 0,
        "latest_published_at": None,
    }


def test_prepared_candidate_diagnostics_are_deeply_immutable() -> None:
    from ai_news.pipeline import run as run_module

    source = _source(0)
    source_run = SourceRun.succeeded(
        source_id=source.id,
        source_name=source.name,
        item_count=1,
        elapsed_ms=0,
    )

    prepared = run_module._prepare_collected_candidates(
        [(source_run, [_raw(4_100, source=source)])],
        set(),
        NOW,
    )

    assert isinstance(prepared.selected, tuple)
    with pytest.raises(TypeError):
        prepared.latest_by_source[source.id] = NOW
    with pytest.raises(TypeError):
        prepared.recent_counts[source.id] = 0
    with pytest.raises(TypeError):
        prepared.new_counts[source.id] = 0
    with pytest.raises(TypeError):
        prepared.eligible_counts[source.id] = 0
    with pytest.raises(TypeError):
        prepared.candidate_counts[source.id] = 0


def test_source_collection_concurrency_never_exceeds_six() -> None:
    sources = [_source(index) for index in range(12)]
    active = 0
    peak = 0

    async def collect(_client: Any, source: SourceSpec) -> list[RawItem]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [_raw(int(source.id.rsplit("-", 1)[1]), source=source)]

    _run(
        source_config=SourceConfig(sources=sources),
        collector=collect,
        analyzer=_Analyzer(),
    )

    assert peak == 6


def test_pipeline_publishes_available_quality_items_below_seven() -> None:
    primary_source = _source(0)
    source_config, collector = _with_filler_sources(
        primary_source,
        [_raw(index, source=primary_source) for index in range(4)],
        filler_count=2,
    )

    class FourItemAnalyzer(_Analyzer):
        async def analyze(self, items: list[Any]) -> Any:
            self.calls.append(items)
            return {item.id: _analysis(index) for index, item in enumerate(items[:4])}

    report = _run(
        source_config=source_config,
        collector=collector,
        analyzer=FourItemAnalyzer(),
    )

    assert len(report.items) == 4


def test_live_wiring_uses_one_twenty_second_client_for_all_sources_and_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_news.pipeline import run as run_module

    sources = [
        _source(
            0,
            kind="feed",
            official=False,
            role="discovery",
            hostname="discovery.example",
        ),
        _source(
            1,
            kind="arxiv",
            official=False,
            role="trusted_media",
            hostname="media.example",
        ),
        _source(2, kind="github"),
        _source(3, kind="official_page"),
        _source(4, kind="reddit_rss"),
        _source(5, kind="feed", enabled=False, hostname="disabled.example"),
    ]
    client = object()
    factory_timeouts: list[int] = []
    seen_clients: list[object] = []
    seen_settings: list[Settings] = []
    reddit_domain_allowlists: list[set[str] | frozenset[str]] = []
    analyzer = _Analyzer()

    class _ClientContext:
        async def __aenter__(self) -> object:
            return client

        async def __aexit__(self, *_args: object) -> None:
            return None

    def client_factory(*, timeout: int) -> _ClientContext:
        factory_timeouts.append(timeout)
        return _ClientContext()

    async def fake_get_bytes(received_client: object, _url: str) -> bytes:
        seen_clients.append(received_client)
        return b"feed"

    def fake_parse_feed(_payload: bytes, source: SourceSpec) -> list[RawItem]:
        return [_raw(index, source=source) for index in range(3)]

    async def fake_arxiv(received_client: object, source: SourceSpec) -> list[RawItem]:
        seen_clients.append(received_client)
        return [_raw(100 + index, source=source) for index in range(3)]

    async def fake_github(
        received_client: object,
        source: SourceSpec,
        token: str | None = None,
    ) -> list[RawItem]:
        seen_clients.append(received_client)
        assert token == "github-secret"
        return [_raw(200 + index, source=source) for index in range(3)]

    async def fake_official_page(
        received_client: object,
        source: SourceSpec,
    ) -> list[RawItem]:
        seen_clients.append(received_client)
        return [_raw(300 + index, source=source) for index in range(3)]

    async def fake_reddit(
        received_client: object,
        source: SourceSpec,
        *,
        official_domains: set[str] | frozenset[str],
    ) -> list[RawItem]:
        seen_clients.append(received_client)
        reddit_domain_allowlists.append(official_domains)
        return [_raw(400 + index, source=source) for index in range(3)]

    def analyzer_factory(settings: Settings, *, client: object) -> _Analyzer:
        seen_settings.append(settings)
        seen_clients.append(client)
        return analyzer

    monkeypatch.setattr(run_module, "get_bytes", fake_get_bytes)
    monkeypatch.setattr(run_module, "parse_feed", fake_parse_feed)
    monkeypatch.setattr(run_module, "collect_arxiv", fake_arxiv)
    monkeypatch.setattr(run_module, "collect_github_releases", fake_github)
    monkeypatch.setattr(run_module, "collect_official_page", fake_official_page)
    monkeypatch.setattr(run_module, "collect_reddit_rss", fake_reddit)
    monkeypatch.setattr(run_module, "create_analyzer", analyzer_factory)
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")

    settings = Settings(
        llm_protocol="openai-chat",
        llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
        llm_token="llm-secret",
        llm_model="live-model",
    )
    report = _run(
        source_config=SourceConfig(sources=sources),
        settings=settings,
        model=None,
        client_factory=client_factory,
        collector=None,
        analyzer=None,
    )

    assert factory_timeouts == [20]
    assert seen_settings == [settings]
    assert seen_settings[0].llm_protocol == "openai-chat"
    assert seen_settings[0].llm_model == "live-model"
    assert seen_clients == [client, client, client, client, client, client]
    assert reddit_domain_allowlists == [{"media.example", "www.anthropic.com"}]
    assert len(report.source_runs) == 5
    assert len(analyzer.calls) == 1


def test_live_official_structure_failure_isolated_from_healthy_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_news.collectors.official_pages.base import OfficialPageStructureError
    from ai_news.pipeline import run as run_module

    official_source = _source(0, kind="official_page")
    feed_source = _source(1, kind="feed")

    async def fake_official_page(
        _client: object,
        _source: SourceSpec,
    ) -> list[RawItem]:
        raise OfficialPageStructureError("reviewed structure missing")

    async def fake_get_bytes(_client: object, _url: str) -> bytes:
        return b"feed"

    def fake_parse_feed(_payload: bytes, source: SourceSpec) -> list[RawItem]:
        return [_raw(100 + index, source=source) for index in range(3)]

    monkeypatch.setattr(run_module, "collect_official_page", fake_official_page)
    monkeypatch.setattr(run_module, "get_bytes", fake_get_bytes)
    monkeypatch.setattr(run_module, "parse_feed", fake_parse_feed)

    report = _run(
        source_config=SourceConfig(sources=[official_source, feed_source]),
        collector=None,
        analyzer=_Analyzer(),
    )
    runs_by_id = {run.source_id: run for run in report.source_runs}

    assert runs_by_id[official_source.id].success is False
    assert runs_by_id[official_source.id].error_type == "OfficialPageStructureError"
    assert runs_by_id[feed_source.id].success is True
    assert report.degraded is True


def test_analyzer_factory_failure_does_not_expose_or_retain_sensitive_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_news.pipeline import run as run_module

    secret = "unique-live-standard-api-key"
    request = httpx.Request(
        "POST",
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        headers={"Authorization": f"Bearer {secret}"},
    )

    class _UnsafeFactoryError(RuntimeError):
        def __init__(self) -> None:
            super().__init__(f"factory rejected {secret}")
            self.request = request
            self.headers = request.headers

    def broken_factory(_settings: Settings, *, client: object) -> _Analyzer:
        del client
        raise _UnsafeFactoryError

    monkeypatch.setattr(run_module, "create_analyzer", broken_factory)
    source = _source(0)
    source_config, collector = _with_filler_sources(
        source,
        [_raw(index, source=source) for index in range(3)],
    )

    with pytest.raises(AnalysisPipelineError) as exc_info:
        _run(
            source_config=source_config,
            collector=collector,
            settings=Settings(
                llm_protocol="openai-chat",
                llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
                llm_token=secret,
                llm_model="live-model",
            ),
            model=None,
            analyzer=None,
        )

    error = exc_info.value
    assert str(error) == "analyzer initialization failed"
    assert repr(error) == "AnalysisPipelineError('analyzer initialization failed')"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in f"{error!s} {error!r}"
    for value in vars(error).values():
        assert not isinstance(value, httpx.Request | httpx.Response | httpx.Headers)
        assert secret not in f"{value!s} {value!r}"


def test_time_window_includes_one_hundred_twenty_hour_boundaries_and_excludes_future() -> None:
    timestamps = [
        NOW - timedelta(hours=120),
        NOW,
        NOW - timedelta(hours=120, microseconds=1),
        NOW + timedelta(microseconds=1),
    ]
    analyzer = _Analyzer()
    items = [_raw(index, published_at=timestamp) for index, timestamp in enumerate(timestamps)]
    source = _source(0)
    source_config, collector = _with_filler_sources(source, items)

    _run(
        source_config=source_config,
        collector=collector,
        analyzer=analyzer,
    )

    assert len(analyzer.calls) == 1
    assert {
        item.raw.published_at for item in analyzer.calls[0] if item.raw.source_id == source.id
    } == set(timestamps[:2])


def test_history_deduplication_and_pretrim_build_balanced_thirty_six_item_pool() -> None:
    sources = [_source(source_index) for source_index in range(6)]
    raw_items_by_source = {
        source.id: [
            _raw(
                source_index * 1_000 + index,
                source=source,
                published_at=NOW - timedelta(minutes=index % 60),
            )
            for index in range(85)
        ]
        for source_index, source in enumerate(sources)
    }
    analyzer = _Analyzer()
    history_calls: list[tuple[Path, date, int]] = []
    historical_url = "https://example.com/news/0"

    def history_loader(root: Path, run_date: date, days: int) -> set[str]:
        history_calls.append((root, run_date, days))
        return {historical_url}

    report = _run(
        root=Path("/tmp/history-root"),
        source_config=SourceConfig(sources=sources),
        collector=_collector(raw_items_by_source),
        analyzer=analyzer,
        history_loader=history_loader,
    )

    assert history_calls == [(Path("/tmp/history-root"), RUN_DATE, 30)]
    source_counts: dict[str, int] = {}
    for item in analyzer.calls[0]:
        source_counts[item.raw.source_id] = source_counts.get(item.raw.source_id, 0) + 1
    assert len(analyzer.calls[0]) == 36
    assert max(source_counts.values()) == 6
    assert all(str(item.canonical_url) != historical_url for item in analyzer.calls[0])
    assert report.candidate_count == 36


def test_eight_valid_candidates_publish_at_most_seven_items(
    tmp_path: Path,
) -> None:
    sources = [_source(index) for index in range(3)]
    items_by_source = {
        sources[0].id: [_raw(index, source=sources[0]) for index in range(3)],
        sources[1].id: [_raw(100 + index, source=sources[1]) for index in range(3)],
        sources[2].id: [_raw(200 + index, source=sources[2]) for index in range(2)],
    }
    analyzer = _Analyzer()

    report = asyncio.run(
        run_daily(
            root=tmp_path,
            run_date=RUN_DATE,
            source_config=SourceConfig(sources=sources),
            collector=_collector(items_by_source),
            analyzer=analyzer,
            now=NOW,
            model="test-model",
        )
    )

    assert len(report.items) == 6
    assert len(analyzer.calls) == 1


def test_seven_valid_items_build_and_persist_schema_1_3_report(tmp_path: Path) -> None:
    sources = [_source(index) for index in range(4)]
    items_by_source = {
        source.id: [_raw(source_index * 100 + index, source=source) for index in range(2)]
        for source_index, source in enumerate(sources)
    }
    report = asyncio.run(
        run_daily(
            root=tmp_path,
            run_date=RUN_DATE,
            source_config=SourceConfig(sources=sources),
            collector=_collector(items_by_source),
            analyzer=_Analyzer(),
            now=NOW,
            model="test-model",
        )
    )

    report_path = tmp_path / "data/2026/07/2026-07-26.json"
    markdown_path = tmp_path / "content/2026-07-26.md"
    index_path = tmp_path / "data/index.json"
    assert DailyReport.model_validate_json(report_path.read_bytes()) == report
    assert report.schema_version == "1.3"
    assert len(report.items) == 7
    assert all(item.source_id is not None for item in report.items)
    assert all(item.source_role is not None for item in report.items)
    assert all(item.discovery_verified is not None for item in report.items)
    assert markdown_path.is_file()
    assert index_path.is_file()


def test_news_item_provenance_comes_from_raw_source_not_model_claims() -> None:
    official = _source(0, official=True, role="primary")
    media = _source(1, official=False, role="trusted_media")
    fillers = [_source(2), _source(3)]
    items_by_source = {
        official.id: [_raw(10, source=official)],
        media.id: [_raw(11, source=media)],
        **{
            source.id: [_raw(20 + index, source=source) for index in range(2)]
            for index, source in enumerate(fillers)
        },
    }

    def analyses(candidates: list[Any]) -> dict[str, Analysis]:
        selected = {
            candidate.raw.source_id: candidate
            for candidate in candidates
            if candidate.raw.source_id in {official.id, media.id}
        }
        return {
            selected[official.id].id: _analysis(0).model_copy(update={"is_official": False}),
            selected[media.id].id: _analysis(1).model_copy(update={"is_official": True}),
        }

    report = _run(
        source_config=SourceConfig(sources=[official, media, *fillers]),
        collector=_collector(items_by_source),
        analyzer=_Analyzer(analyses),
    )
    items_by_source_id = {item.source_id: item for item in report.items}

    assert items_by_source_id[official.id].is_official is True
    assert items_by_source_id[official.id].source_role.value == "primary"
    assert items_by_source_id[official.id].discovery_verified is False
    assert items_by_source_id[media.id].is_official is False
    assert items_by_source_id[media.id].source_role.value == "trusted_media"
    assert items_by_source_id[media.id].discovery_verified is False


def test_non_primary_high_marketing_risk_is_removed_but_primary_is_retained() -> None:
    primary = _source(0, official=True, role="primary")
    media = _source(1, official=False, role="trusted_media")
    filler = _source(2)
    items_by_source = {
        primary.id: [_raw(30, source=primary)],
        media.id: [_raw(31, source=media)],
        filler.id: [_raw(32, source=filler)],
    }

    def analyses(candidates: list[Any]) -> dict[str, Analysis]:
        return {
            candidate.id: _analysis(index).model_copy(
                update={
                    "marketing_risk": (
                        "high" if candidate.raw.source_id in {primary.id, media.id} else "low"
                    )
                }
            )
            for index, candidate in enumerate(candidates)
        }

    report = _run(
        source_config=SourceConfig(sources=[primary, media, filler]),
        collector=_collector(items_by_source),
        analyzer=_Analyzer(analyses),
    )

    assert {item.source_id for item in report.items} == {primary.id, filler.id}
    retained_primary = next(item for item in report.items if item.source_id == primary.id)
    assert retained_primary.marketing_risk == "high"


def test_empty_model_selection_publishes_no_report() -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(3)]
    saves: list[DailyReport] = []

    with pytest.raises(PublicationThresholdError, match="no valid editorial items"):
        _run(
            source_config=SourceConfig(sources=[source]),
            collector=_collector({source.id: items}),
            analyzer=_Analyzer(lambda _candidates: {}),
            report_saver=lambda _root, report: saves.append(report),
        )

    assert saves == []


def test_final_seven_items_are_stably_sorted() -> None:
    sources = [_source(source_index) for source_index in range(4)]
    items_by_source = {
        source.id: [
            _raw(
                source_index * 100 + index,
                source=source,
                published_at=NOW - timedelta(minutes=index % 3),
            )
            for index in range(4)
        ]
        for source_index, source in enumerate(sources)
    }

    def analyses(candidates: list[Any]) -> dict[str, Analysis]:
        lanes = [
            EditorialLane.PRODUCT_ENGINEERING,
            EditorialLane.PRODUCT_ENGINEERING,
            EditorialLane.PRODUCT_ENGINEERING,
            EditorialLane.BUSINESS,
            EditorialLane.BUSINESS,
            EditorialLane.BUSINESS,
            EditorialLane.RESEARCH,
        ]
        selected = _select_with_source_cap(candidates)
        return {
            candidate.id: _analysis(
                index,
                importance=(index % 4) + 7,
                editorial_lane=lane,
            )
            for index, (candidate, lane) in enumerate(zip(selected, lanes, strict=True))
        }

    report = _run(
        source_config=SourceConfig(sources=sources),
        collector=_collector(items_by_source),
        analyzer=_Analyzer(analyses),
    )
    expected = sorted(
        report.items,
        key=lambda item: (
            -item.importance,
            -item.published_at.timestamp(),
            item.id,
        ),
    )

    assert len(report.items) == 7
    assert report.items == expected


def test_pipeline_rejects_more_than_seven_analyzed_items() -> None:
    sources = [_source(index) for index in range(3)]
    items_by_source = {
        source.id: [_raw(source_index * 100 + index, source=source) for index in range(3)]
        for source_index, source in enumerate(sources)
    }

    def eight_analyses(candidates: list[Any]) -> dict[str, Analysis]:
        return {candidate.id: _analysis(index) for index, candidate in enumerate(candidates[:8])}

    with pytest.raises(ReportValidationError, match="distribution"):
        _run(
            source_config=SourceConfig(sources=sources),
            collector=_collector(items_by_source),
            analyzer=_Analyzer(eight_analyses),
        )


def test_pipeline_allows_unbalanced_editorial_lanes() -> None:
    sources = [_source(index) for index in range(4)]
    items_by_source = {
        source.id: [_raw(source_index * 100 + index, source=source) for index in range(3)]
        for source_index, source in enumerate(sources)
    }
    lanes = [
        EditorialLane.PRODUCT_ENGINEERING,
        EditorialLane.PRODUCT_ENGINEERING,
        EditorialLane.PRODUCT_ENGINEERING,
        EditorialLane.BUSINESS,
        EditorialLane.BUSINESS,
        EditorialLane.RESEARCH,
        EditorialLane.RESEARCH,
    ]

    def invalid_lanes(candidates: list[Any]) -> dict[str, Analysis]:
        selected = _select_with_source_cap(candidates)
        return {
            candidate.id: _analysis(index, editorial_lane=lane)
            for index, (candidate, lane) in enumerate(zip(selected, lanes, strict=True))
        }

    report = _run(
        source_config=SourceConfig(sources=sources),
        collector=_collector(items_by_source),
        analyzer=_Analyzer(invalid_lanes),
    )
    assert len(report.items) == 7


def test_pipeline_rejects_more_than_two_final_items_from_one_source() -> None:
    sources = [_source(index) for index in range(3)]
    source_sizes = (3, 2, 2)
    items_by_source = {
        source.id: [
            _raw(source_index * 100 + index, source=source)
            for index in range(source_sizes[source_index])
        ]
        for source_index, source in enumerate(sources)
    }

    def invalid_distribution(candidates: list[Any]) -> dict[str, Analysis]:
        return {candidate.id: _analysis(index) for index, candidate in enumerate(candidates[:7])}

    with pytest.raises(ReportValidationError, match="distribution"):
        _run(
            source_config=SourceConfig(sources=sources),
            collector=_collector(items_by_source),
            analyzer=_Analyzer(invalid_distribution),
        )


@pytest.mark.parametrize("invalid_kind", ["missing", "invalid"])
def test_missing_or_invalid_analysis_is_not_fabricated(
    invalid_kind: str,
) -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(3)]

    def result(candidates: list[Any]) -> dict[str, Any]:
        values = {candidate.id: _analysis(index) for index, candidate in enumerate(candidates)}
        if invalid_kind == "missing":
            values.pop(candidates[-1].id)
        else:
            values[candidates[-1].id] = {"title": "invalid"}
        return values

    report = _run(
        collector=_collector({source.id: items}),
        analyzer=_Analyzer(result),
    )
    assert len(report.items) == 2
    assert report.source_runs[0].candidate_count == 3
    assert report.source_runs[0].selected_count == 2


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "numeric-string",
        "boolean-string",
        "boolean-as-integer",
        "tuple-tags",
        "extra-field",
        "missing-field",
    ],
)
def test_non_strict_analysis_payloads_are_discarded_without_saving(
    invalid_kind: str,
) -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(3)]
    source_config, collector = _with_filler_sources(source, items)
    saves: list[DailyReport] = []

    def result(candidates: list[Any]) -> dict[str, dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for index, candidate in enumerate(candidates):
            payload = _analysis(index).model_dump()
            if invalid_kind == "numeric-string":
                payload["importance"] = "8"
            elif invalid_kind == "boolean-string":
                payload["is_official"] = "false"
            elif invalid_kind == "boolean-as-integer":
                payload["importance"] = True
            elif invalid_kind == "tuple-tags":
                payload["tags"] = tuple(payload["tags"])
            elif invalid_kind == "extra-field":
                payload["unexpected"] = "must be rejected"
            else:
                payload.pop("tracking_signal")
            values[candidate.id] = payload
        return values

    with pytest.raises(PublicationThresholdError):
        _run(
            source_config=source_config,
            collector=collector,
            analyzer=_Analyzer(result),
            report_saver=lambda _root, report: saves.append(report),
        )

    assert saves == []


def test_unknown_analysis_id_fails_the_whole_run_without_saving() -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(3)]
    source_config, collector = _with_filler_sources(source, items)
    saves: list[DailyReport] = []

    def result(candidates: list[Any]) -> dict[str, Analysis]:
        values = {candidate.id: _analysis(index) for index, candidate in enumerate(candidates)}
        values["0123456789abcdef"] = _analysis(99)
        return values

    with pytest.raises(AnalysisPipelineError):
        _run(
            source_config=source_config,
            collector=collector,
            analyzer=_Analyzer(result),
            report_saver=lambda _root, report: saves.append(report),
        )

    assert saves == []


def test_duplicate_analysis_id_semantics_are_rejected() -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(3)]
    source_config, collector = _with_filler_sources(source, items)

    class _DuplicateResult:
        def items(self) -> list[tuple[str, Analysis]]:
            candidate_id = candidates[0].id
            return [
                (candidate.id, _analysis(index)) for index, candidate in enumerate(candidates)
            ] + [(candidate_id, _analysis(99))]

    candidates: list[Any] = []

    def result(selected: list[Any]) -> _DuplicateResult:
        candidates.extend(selected)
        return _DuplicateResult()

    with pytest.raises(AnalysisPipelineError):
        _run(
            source_config=source_config,
            collector=collector,
            analyzer=_Analyzer(result),
        )


def test_report_validation_failure_happens_before_save() -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(3)]
    source_config, collector = _with_filler_sources(source, items)
    saves: list[DailyReport] = []

    with pytest.raises(ReportValidationError):
        _run(
            source_config=source_config,
            collector=collector,
            analyzer=_Analyzer(),
            model=" ",
            report_saver=lambda _root, report: saves.append(report),
        )

    assert saves == []


def test_no_healthy_source_and_analyzer_failure_are_safe_pipeline_errors() -> None:
    source = _source(0)

    async def broken_collector(_client: Any, _source: SourceSpec) -> list[RawItem]:
        raise RuntimeError("secret collector details")

    with pytest.raises(CollectionPipelineError) as collection_error:
        _run(collector=broken_collector, analyzer=_Analyzer())
    assert str(collection_error.value) == "no healthy sources"

    class _BrokenAnalyzer:
        async def analyze(self, _items: list[Any]) -> dict[str, Analysis]:
            raise RuntimeError("secret analyzer details")

    source_config, collector = _with_filler_sources(
        source,
        [_raw(index, source=source) for index in range(3)],
    )
    with pytest.raises(AnalysisPipelineError) as analysis_error:
        _run(
            source_config=source_config,
            collector=collector,
            analyzer=_BrokenAnalyzer(),
        )
    assert str(analysis_error.value) == "analysis failed"


def test_analysis_pipeline_error_accepts_only_safe_diagnostic_codes() -> None:
    error = AnalysisPipelineError("analysis failed", safe_code="http_401")
    unsafe = AnalysisPipelineError(
        "secret model endpoint?token=value",
        safe_code="secret-token-value",
    )

    assert error.safe_code == "http_401"
    assert unsafe.safe_code is None


def test_fixed_clocks_make_the_report_deterministic() -> None:
    sources = [_source(0), _source(1), _source(2)]
    items = {
        source.id: [_raw(index + source_index * 100, source=source) for index in range(3)]
        for source_index, source in enumerate(sources)
    }
    arguments = {
        "source_config": SourceConfig(sources=sources),
        "collector": _collector(items),
        "analyzer": _Analyzer(),
        "monotonic": lambda: 100.0,
    }

    first = _run(**arguments)
    arguments["analyzer"] = _Analyzer()
    second = _run(**arguments)

    assert first == second
    assert all(source_run.elapsed_ms == 0 for source_run in first.source_runs)


def test_duplicate_canonical_urls_are_folded_before_bounded_pretrim() -> None:
    source = _source(0)
    duplicate_url = "https://example.com/news/high-priority-duplicate"
    duplicate_items: list[RawItem] = []
    for index in range(500):
        raw_data = _raw(index, source=source).model_dump()
        raw_data.update(url=duplicate_url, source_weight=10)
        duplicate_items.append(RawItem.model_validate(raw_data))
    unique_items: list[RawItem] = []
    for index in range(5):
        raw_data = _raw(10_000 + index, source=source).model_dump()
        raw_data["source_weight"] = 1
        unique_items.append(RawItem.model_validate(raw_data))

    analyzer = _Analyzer()
    source_config, collector = _with_filler_sources(
        source,
        [*duplicate_items, *unique_items],
    )
    report = _run(
        source_config=source_config,
        collector=collector,
        analyzer=analyzer,
    )

    assert report.candidate_count == 15
    assert len(report.items) == 7
    assert sum(str(item.canonical_url) == duplicate_url for item in analyzer.calls[0]) == 1


def test_exact_accumulator_prefers_primary_over_higher_weight_media_duplicate() -> None:
    primary = _source(0, official=True, role="primary")
    media = _source(1, official=False, role="trusted_media")
    fillers = [_source(2), _source(3), _source(4)]
    duplicate_url = "https://example.com/news/shared-official-event"
    primary_item = _updated_raw(
        _raw(100, source=primary),
        url=duplicate_url,
        title="Official product launch",
        source_weight=1,
        published_at=NOW - timedelta(hours=2),
    )
    media_item = _updated_raw(
        _raw(101, source=media),
        url=duplicate_url,
        title="Media product launch",
        source_weight=10,
        published_at=NOW,
    )
    items_by_source = {
        primary.id: [primary_item],
        media.id: [media_item],
        **{
            source.id: [_raw(200 + index, source=source) for index in range(3)]
            for index, source in enumerate(fillers)
        },
    }
    analyzer = _Analyzer()

    _run(
        source_config=SourceConfig(sources=[primary, media, *fillers]),
        collector=_collector(items_by_source),
        analyzer=analyzer,
    )

    duplicate = [item for item in analyzer.calls[0] if str(item.canonical_url) == duplicate_url]
    assert len(duplicate) == 1
    assert duplicate[0].raw.source_id == primary.id


def test_same_host_exact_titles_are_folded_before_bounded_pretrim() -> None:
    source = _source(0)
    noise_items = [
        _updated_raw(
            _raw(index, source=source),
            title="  SAME　normalized title  ",
            source_weight=10,
        )
        for index in range(500)
    ]
    healthy_items = [
        _updated_raw(
            _raw(10_000 + index, source=source),
            source_weight=1,
        )
        for index in range(5)
    ]

    analyzer = _Analyzer()
    source_config, collector = _with_filler_sources(
        source,
        [*noise_items, *healthy_items],
    )
    report = _run(
        source_config=source_config,
        collector=collector,
        analyzer=analyzer,
    )

    assert report.candidate_count == 15
    assert len(report.items) == 7
    primary_titles = [
        item.raw.title for item in analyzer.calls[0] if item.raw.source_id == source.id
    ]
    assert len([title for title in primary_titles if "SAME" in title]) == 1


def test_exact_key_bridge_merges_both_existing_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_news.pipeline import run as run_module

    source = _source(0)
    bridge_items = [
        _updated_raw(
            _raw(20_000, source=source),
            url="https://example.com/bridge/first",
            title="Alpha exact group",
            source_weight=10,
        ),
        _updated_raw(
            _raw(20_001, source=source),
            url="https://example.com/bridge/second",
            title="Beta exact group",
            source_weight=9,
        ),
        _updated_raw(
            _raw(20_002, source=source),
            url="https://example.com/bridge/first",
            title="Beta exact group",
            source_weight=8,
        ),
    ]
    healthy_items = [_raw(21_000 + index, source=source) for index in range(5)]
    dedupe_sizes: list[int] = []
    real_deduplicate = run_module.deduplicate

    def observed_deduplicate(
        candidates: list[Any],
        historical_urls: set[str],
    ) -> list[Any]:
        dedupe_sizes.append(sum(candidate.raw.source_id == source.id for candidate in candidates))
        return real_deduplicate(candidates, historical_urls)

    monkeypatch.setattr(run_module, "deduplicate", observed_deduplicate)

    source_config, collector = _with_filler_sources(
        source,
        [*bridge_items, *healthy_items],
    )
    report = _run(
        source_config=source_config,
        collector=collector,
        analyzer=_Analyzer(),
    )

    assert dedupe_sizes == [6]
    assert report.candidate_count == 15
    assert len(report.items) == 7


def test_large_exact_title_alias_state_is_bounded_and_order_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_news.pipeline import run as run_module

    source = _source(0)
    aliases = [
        _updated_raw(
            _raw(30_000 + index, source=source),
            title="One exact title across many aliases",
            source_weight=10,
        )
        for index in range(3_200)
    ]
    healthy_items = [
        _updated_raw(
            _raw(40_000 + index, source=source),
            source_weight=1,
        )
        for index in range(5)
    ]
    all_items = [*aliases, *healthy_items]
    dedupe_sizes: list[int] = []
    real_deduplicate = run_module.deduplicate

    def bounded_deduplicate(candidates: list[Any], historical_urls: set[str]) -> list[Any]:
        dedupe_sizes.append(sum(candidate.raw.source_id == source.id for candidate in candidates))
        return real_deduplicate(candidates, historical_urls)

    monkeypatch.setattr(run_module, "deduplicate", bounded_deduplicate)
    first_analyzer = _Analyzer()
    second_analyzer = _Analyzer()

    first_config, first_collector = _with_filler_sources(source, all_items)
    second_config, second_collector = _with_filler_sources(
        source,
        list(reversed(all_items)),
    )
    first_report = _run(
        source_config=first_config,
        collector=first_collector,
        analyzer=first_analyzer,
        monotonic=lambda: 100.0,
    )
    second_report = _run(
        source_config=second_config,
        collector=second_collector,
        analyzer=second_analyzer,
        monotonic=lambda: 100.0,
    )

    assert dedupe_sizes == [6, 6]
    assert first_report.candidate_count == second_report.candidate_count == 15
    assert [item.id for item in first_analyzer.calls[0]] == [
        item.id for item in second_analyzer.calls[0]
    ]


def test_compaction_preserves_retained_aliases_for_later_bridges() -> None:
    from ai_news.pipeline import run as run_module

    source = _source(0)
    first = run_module.to_candidate(
        _updated_raw(
            _raw(50_000, source=source),
            url="https://example.com/a",
            title="Alpha",
            source_weight=10,
        )
    )
    alias = run_module.to_candidate(
        _updated_raw(
            _raw(50_001, source=source),
            url="https://example.com/b",
            title="Alpha",
            source_weight=9,
        )
    )
    later_bridge = run_module.to_candidate(
        _updated_raw(
            _raw(50_002, source=source),
            url="https://example.com/b",
            title="Different",
            source_weight=9,
        )
    )
    fillers = [
        run_module.to_candidate(
            _updated_raw(
                _raw(51_000 + index, source=source),
                source_weight=1,
            )
        )
        for index in range(1_000)
    ]

    def selected(sequence: list[Any]) -> list[Any]:
        accumulator = run_module._ExactCandidateAccumulator(NOW)
        for candidate in sequence:
            accumulator.add(candidate)
        return accumulator.selected()

    forward = selected([first, alias, *fillers, later_bridge])
    reverse = selected([later_bridge, *reversed(fillers), alias, first])
    bridge_ids = {first.id, alias.id, later_bridge.id}

    assert len(forward) == len(reverse) == 500
    assert {candidate.id for candidate in forward} == {candidate.id for candidate in reverse}
    assert len(bridge_ids & {candidate.id for candidate in forward}) == 1
    assert len(run_module.deduplicate(forward, set())) == 500


def test_single_huge_exact_group_remains_hard_bounded_and_order_independent() -> None:
    from ai_news.pipeline import run as run_module

    source = _source(0)
    aliases = [
        run_module.to_candidate(
            _updated_raw(
                _raw(60_000 + index, source=source),
                title="One huge exact group",
                source_weight=10,
            )
        )
        for index in range(6_200)
    ]

    def accumulate(sequence: list[Any]) -> tuple[int, list[Any]]:
        accumulator = run_module._ExactCandidateAccumulator(NOW)
        maximum_state_size = 0
        for candidate in sequence:
            accumulator.add(candidate)
            maximum_state_size = max(maximum_state_size, accumulator._state_size())
        return maximum_state_size, accumulator.selected()

    forward_maximum, forward = accumulate(aliases)
    reverse_maximum, reverse = accumulate(list(reversed(aliases)))

    assert forward_maximum <= run_module._EXACT_STATE_LIMIT
    assert reverse_maximum <= run_module._EXACT_STATE_LIMIT
    assert len(forward) == len(reverse) == 1
    assert forward[0].id == reverse[0].id


def test_bounded_pretrim_preserves_lower_ranked_source_diversity() -> None:
    from ai_news.pipeline import run as run_module

    dominant_source = _source(0)
    secondary_sources = [_source(index) for index in range(1, 4)]
    dominant = [
        run_module.to_candidate(
            _updated_raw(
                _raw(70_000 + index, source=dominant_source),
                source_weight=10,
            )
        )
        for index in range(501)
    ]
    diverse = [
        run_module.to_candidate(
            _updated_raw(
                _raw(80_000 + source_index * 10 + index, source=source),
                source_weight=1,
            )
        )
        for source_index, source in enumerate(secondary_sources)
        for index in range(3)
    ]

    def accumulate(sequence: list[Any]) -> list[Any]:
        accumulator = run_module._ExactCandidateAccumulator(NOW)
        for candidate in sequence:
            accumulator.add(candidate)
        return accumulator.selected()

    retained = accumulate([*dominant, *diverse])
    reversed_retained = accumulate([*reversed(diverse), *reversed(dominant)])
    selected = run_module._prepare_candidates(retained, set(), NOW)

    assert len(retained) == run_module._DEDUPLICATION_LIMIT
    assert {candidate.id for candidate in retained} == {
        candidate.id for candidate in reversed_retained
    }
    assert {candidate.raw.source_id for candidate in diverse} <= {
        candidate.raw.source_id for candidate in retained
    }
    assert len(selected) >= 9


def test_more_than_1500_unique_items_stay_bounded_and_order_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_news.pipeline import run as run_module

    source = _source(0)
    raw_items = [
        _raw(
            index,
            source=source,
            published_at=NOW - timedelta(seconds=index % 3600),
        )
        for index in range(1_601)
    ]
    dedupe_sizes: list[int] = []
    real_deduplicate = run_module.deduplicate

    def bounded_deduplicate(candidates: list[Any], historical_urls: set[str]) -> list[Any]:
        dedupe_sizes.append(sum(candidate.raw.source_id == source.id for candidate in candidates))
        return real_deduplicate(candidates, historical_urls)

    monkeypatch.setattr(run_module, "deduplicate", bounded_deduplicate)
    first_analyzer = _Analyzer()
    second_analyzer = _Analyzer()

    first_config, first_collector = _with_filler_sources(source, raw_items)
    second_config, second_collector = _with_filler_sources(
        source,
        list(reversed(raw_items)),
    )
    _run(
        source_config=first_config,
        collector=first_collector,
        analyzer=first_analyzer,
        monotonic=lambda: 100.0,
    )
    _run(
        source_config=second_config,
        collector=second_collector,
        analyzer=second_analyzer,
        monotonic=lambda: 100.0,
    )

    assert dedupe_sizes[0] == dedupe_sizes[1]
    assert 0 < dedupe_sizes[0] <= 500
    assert [item.id for item in first_analyzer.calls[0]] == [
        item.id for item in second_analyzer.calls[0]
    ]


def test_client_exit_failure_is_safe_and_prevents_all_persistence(
    tmp_path: Path,
) -> None:
    source = _source(0)
    saves: list[DailyReport] = []

    class _FailingExitContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            raise RuntimeError("client-exit-secret?token=value")

    def saver(root: Path, report: DailyReport) -> None:
        saves.append(report)
        save_report(root, report)

    source_config, collector = _with_filler_sources(
        source,
        [_raw(index, source=source) for index in range(3)],
    )
    with pytest.raises(CollectionPipelineError) as exit_error:
        _run(
            root=tmp_path,
            source_config=source_config,
            collector=collector,
            analyzer=_Analyzer(),
            client_factory=lambda *, timeout: _FailingExitContext(),
            report_saver=saver,
        )

    assert str(exit_error.value) == "client initialization failed"
    assert "secret" not in str(exit_error.value)
    assert saves == []
    assert not (tmp_path / "data/2026/07/2026-07-26.json").exists()
    assert not (tmp_path / "content/2026-07-26.md").exists()
    assert not (tmp_path / "data/index.json").exists()


def test_report_is_saved_once_only_after_normal_client_exit(tmp_path: Path) -> None:
    source = _source(0)
    events: list[str] = []
    saves: list[DailyReport] = []

    class _SuccessfulContext:
        async def __aenter__(self) -> object:
            events.append("enter")
            return object()

        async def __aexit__(self, *_args: object) -> None:
            events.append("exit")

    def saver(_root: Path, report: DailyReport) -> None:
        events.append("save")
        saves.append(report)

    source_config, collector = _with_filler_sources(
        source,
        [_raw(index, source=source) for index in range(3)],
    )
    report = _run(
        root=tmp_path,
        source_config=source_config,
        collector=collector,
        analyzer=_Analyzer(),
        client_factory=lambda *, timeout: _SuccessfulContext(),
        report_saver=saver,
    )

    assert events == ["enter", "exit", "save"]
    assert saves == [report]
