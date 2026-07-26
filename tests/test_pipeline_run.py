from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ai_news.models import (
    Analysis,
    Category,
    DailyReport,
    RawItem,
    Settings,
    SourceConfig,
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
) -> SourceSpec:
    values: dict[str, Any] = {
        "id": f"source-{number}",
        "name": f"Source {number}",
        "kind": kind,
        "category": Category.MODEL,
        "weight": 7,
        "official": True,
        "enabled": enabled,
    }
    if kind in {"feed", "arxiv"}:
        values["url"] = f"https://example.com/{number}.xml"
    else:
        values["repo"] = f"owner/repo-{number}"
    return SourceSpec.model_validate(values)


def _raw(
    number: int,
    *,
    source: SourceSpec | None = None,
    published_at: datetime = NOW,
    query: str = "",
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
    )


def _analysis(number: int, *, importance: int = 8) -> Analysis:
    return Analysis(
        title=f"分析标题 {number}",
        category=Category.MODEL,
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
        return {item.id: _analysis(index) for index, item in enumerate(items)}


def _collector(items_by_source: dict[str, list[RawItem]]) -> Callable[..., Any]:
    async def collect(_client: Any, source: SourceSpec) -> list[RawItem]:
        return items_by_source[source.id]

    return collect


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


def test_source_failure_does_not_block_healthy_source_and_marks_degraded() -> None:
    failed_source = _source(0)
    healthy_source = _source(1)

    async def collect(_client: Any, source: SourceSpec) -> list[RawItem]:
        if source.id == failed_source.id:
            raise RuntimeError("private-token-and-query-value")
        return [_raw(index, source=source) for index in range(5)]

    report = _run(
        source_config=SourceConfig(sources=[failed_source, healthy_source]),
        collector=collect,
        analyzer=_Analyzer(),
    )

    assert report.degraded is True
    assert [run.source_id for run in report.source_runs] == [
        failed_source.id,
        healthy_source.id,
    ]
    assert report.source_runs[0].success is False
    assert report.source_runs[0].error_type == "RuntimeError"
    assert report.source_runs[0].item_count == 0
    assert report.source_runs[1].success is True
    assert report.source_runs[1].item_count == 5
    assert "private-token" not in report.model_dump_json()


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


def test_live_wiring_uses_one_twenty_second_client_for_all_sources_and_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_news.pipeline import run as run_module

    sources = [
        _source(0, kind="feed"),
        _source(1, kind="arxiv"),
        _source(2, kind="github"),
        _source(3, kind="feed", enabled=False),
    ]
    client = object()
    factory_timeouts: list[int] = []
    seen_clients: list[object] = []
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
        return [_raw(0, source=source), _raw(1, source=source)]

    async def fake_arxiv(received_client: object, source: SourceSpec) -> list[RawItem]:
        seen_clients.append(received_client)
        return [_raw(2, source=source), _raw(3, source=source)]

    async def fake_github(
        received_client: object,
        source: SourceSpec,
        token: str | None = None,
    ) -> list[RawItem]:
        seen_clients.append(received_client)
        assert token == "github-secret"
        return [_raw(4, source=source), _raw(5, source=source)]

    def analyzer_factory(settings: Settings, *, client: object) -> _Analyzer:
        assert settings.llm_model == "live-model"
        seen_clients.append(client)
        return analyzer

    monkeypatch.setattr(run_module, "get_bytes", fake_get_bytes)
    monkeypatch.setattr(run_module, "parse_feed", fake_parse_feed)
    monkeypatch.setattr(run_module, "collect_arxiv", fake_arxiv)
    monkeypatch.setattr(run_module, "collect_github_releases", fake_github)
    monkeypatch.setattr(run_module, "AnthropicAnalyzer", analyzer_factory)
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")

    report = _run(
        source_config=SourceConfig(sources=sources),
        settings=Settings(
            llm_base_url="https://api.example.com",
            llm_token="llm-secret",
            llm_model="live-model",
        ),
        model=None,
        client_factory=client_factory,
        collector=None,
        analyzer=None,
    )

    assert factory_timeouts == [20]
    assert seen_clients == [client, client, client, client]
    assert len(report.source_runs) == 3
    assert len(analyzer.calls) == 1


def test_time_window_includes_both_boundaries_and_excludes_future() -> None:
    timestamps = [
        NOW - timedelta(hours=36),
        NOW,
        NOW - timedelta(hours=36, microseconds=1),
        NOW + timedelta(microseconds=1),
    ]
    analyzer = _Analyzer()
    items = [_raw(index, published_at=timestamp) for index, timestamp in enumerate(timestamps)]

    with pytest.raises(PublicationThresholdError):
        _run(collector=_collector({_source(0).id: items}), analyzer=analyzer)

    assert len(analyzer.calls) == 1
    assert {item.raw.published_at for item in analyzer.calls[0]} == set(timestamps[:2])


def test_history_deduplication_and_pretrim_bound_analysis_to_thirty() -> None:
    source = _source(0)
    raw_items = [
        _raw(index, source=source, published_at=NOW - timedelta(minutes=index % 60))
        for index in range(510)
    ]
    analyzer = _Analyzer()
    history_calls: list[tuple[Path, date, int]] = []
    historical_url = "https://example.com/news/0"

    def history_loader(root: Path, run_date: date, days: int) -> set[str]:
        history_calls.append((root, run_date, days))
        return {historical_url}

    report = _run(
        root=Path("/tmp/history-root"),
        collector=_collector({source.id: raw_items}),
        analyzer=analyzer,
        history_loader=history_loader,
    )

    assert history_calls == [(Path("/tmp/history-root"), RUN_DATE, 30)]
    assert len(analyzer.calls[0]) == 30
    assert all(str(item.canonical_url) != historical_url for item in analyzer.calls[0])
    assert report.candidate_count == 30


def test_fewer_than_five_valid_items_does_not_write_any_files(tmp_path: Path) -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(4)]

    with pytest.raises(PublicationThresholdError):
        asyncio.run(
            run_daily(
                root=tmp_path,
                run_date=RUN_DATE,
                source_config=SourceConfig(sources=[source]),
                collector=_collector({source.id: items}),
                analyzer=_Analyzer(),
                now=NOW,
                model="test-model",
            )
        )

    assert list(tmp_path.rglob("*")) == []


def test_five_valid_items_build_and_persist_report(tmp_path: Path) -> None:
    source = _source(0)
    report = asyncio.run(
        run_daily(
            root=tmp_path,
            run_date=RUN_DATE,
            source_config=SourceConfig(sources=[source]),
            collector=_collector({source.id: [_raw(index, source=source) for index in range(5)]}),
            analyzer=_Analyzer(),
            now=NOW,
            model="test-model",
        )
    )

    report_path = tmp_path / "data/2026/07/2026-07-26.json"
    markdown_path = tmp_path / "content/2026-07-26.md"
    index_path = tmp_path / "data/index.json"
    assert DailyReport.model_validate_json(report_path.read_bytes()) == report
    assert markdown_path.is_file()
    assert index_path.is_file()


def test_final_items_are_stably_sorted_and_limited_to_twenty() -> None:
    source = _source(0)
    items = [
        _raw(index, source=source, published_at=NOW - timedelta(minutes=index % 3))
        for index in range(25)
    ]

    def analyses(candidates: list[Any]) -> dict[str, Analysis]:
        return {
            candidate.id: _analysis(
                index,
                importance=(index % 4) + 7,
            )
            for index, candidate in enumerate(candidates)
        }

    report = _run(
        collector=_collector({source.id: items}),
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

    assert len(report.items) == 20
    assert report.items == expected
    assert report.items[:3] == expected[:3]


@pytest.mark.parametrize("invalid_kind", ["missing", "invalid"])
def test_missing_or_invalid_analysis_is_not_fabricated(
    invalid_kind: str,
) -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(5)]

    def result(candidates: list[Any]) -> dict[str, Any]:
        values = {candidate.id: _analysis(index) for index, candidate in enumerate(candidates)}
        if invalid_kind == "missing":
            values.pop(candidates[-1].id)
        else:
            values[candidates[-1].id] = {"title": "invalid"}
        return values

    with pytest.raises(PublicationThresholdError):
        _run(
            collector=_collector({source.id: items}),
            analyzer=_Analyzer(result),
        )


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
    items = [_raw(index, source=source) for index in range(5)]
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
            collector=_collector({source.id: items}),
            analyzer=_Analyzer(result),
            report_saver=lambda _root, report: saves.append(report),
        )

    assert saves == []


def test_unknown_analysis_id_fails_the_whole_run_without_saving() -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(5)]
    saves: list[DailyReport] = []

    def result(candidates: list[Any]) -> dict[str, Analysis]:
        values = {candidate.id: _analysis(index) for index, candidate in enumerate(candidates)}
        values["0123456789abcdef"] = _analysis(99)
        return values

    with pytest.raises(AnalysisPipelineError):
        _run(
            collector=_collector({source.id: items}),
            analyzer=_Analyzer(result),
            report_saver=lambda _root, report: saves.append(report),
        )

    assert saves == []


def test_duplicate_analysis_id_semantics_are_rejected() -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(5)]

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
            collector=_collector({source.id: items}),
            analyzer=_Analyzer(result),
        )


def test_report_validation_failure_happens_before_save() -> None:
    source = _source(0)
    items = [_raw(index, source=source) for index in range(5)]
    saves: list[DailyReport] = []

    with pytest.raises(ReportValidationError):
        _run(
            collector=_collector({source.id: items}),
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

    with pytest.raises(AnalysisPipelineError) as analysis_error:
        _run(
            collector=_collector({source.id: [_raw(index, source=source) for index in range(5)]}),
            analyzer=_BrokenAnalyzer(),
        )
    assert str(analysis_error.value) == "analysis failed"


def test_fixed_clocks_make_the_report_deterministic() -> None:
    sources = [_source(0), _source(1)]
    items = {
        source.id: [_raw(index + source_index * 5, source=source) for index in range(5)]
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

    report = _run(
        collector=_collector({source.id: [*duplicate_items, *unique_items]}),
        analyzer=_Analyzer(),
    )

    assert report.candidate_count == 6
    assert len(report.items) == 6
    assert sum(str(item.canonical_url) == duplicate_url for item in report.items) == 1


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

    report = _run(
        collector=_collector({source.id: [*noise_items, *healthy_items]}),
        analyzer=_Analyzer(),
    )

    assert report.candidate_count == 6
    assert len(report.items) == 6


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
        dedupe_sizes.append(len(candidates))
        return real_deduplicate(candidates, historical_urls)

    monkeypatch.setattr(run_module, "deduplicate", observed_deduplicate)

    report = _run(
        collector=_collector({source.id: [*bridge_items, *healthy_items]}),
        analyzer=_Analyzer(),
    )

    assert dedupe_sizes == [6]
    assert report.candidate_count == 6
    assert len(report.items) == 6


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
        dedupe_sizes.append(len(candidates))
        return real_deduplicate(candidates, historical_urls)

    monkeypatch.setattr(run_module, "deduplicate", bounded_deduplicate)
    first_analyzer = _Analyzer()
    second_analyzer = _Analyzer()

    first_report = _run(
        collector=_collector({source.id: all_items}),
        analyzer=first_analyzer,
        monotonic=lambda: 100.0,
    )
    second_report = _run(
        collector=_collector({source.id: list(reversed(all_items))}),
        analyzer=second_analyzer,
        monotonic=lambda: 100.0,
    )

    assert dedupe_sizes == [6, 6]
    assert first_report.candidate_count == second_report.candidate_count == 6
    assert [item.id for item in first_analyzer.calls[0]] == [
        item.id for item in second_analyzer.calls[0]
    ]


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
        dedupe_sizes.append(len(candidates))
        return real_deduplicate(candidates, historical_urls)

    monkeypatch.setattr(run_module, "deduplicate", bounded_deduplicate)
    first_analyzer = _Analyzer()
    second_analyzer = _Analyzer()

    _run(
        collector=_collector({source.id: raw_items}),
        analyzer=first_analyzer,
        monotonic=lambda: 100.0,
    )
    _run(
        collector=_collector({source.id: list(reversed(raw_items))}),
        analyzer=second_analyzer,
        monotonic=lambda: 100.0,
    )

    assert dedupe_sizes == [500, 500]
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

    with pytest.raises(CollectionPipelineError) as exit_error:
        _run(
            root=tmp_path,
            collector=_collector({source.id: [_raw(index, source=source) for index in range(5)]}),
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

    report = _run(
        root=tmp_path,
        collector=_collector({source.id: [_raw(index, source=source) for index in range(5)]}),
        analyzer=_Analyzer(),
        client_factory=lambda *, timeout: _SuccessfulContext(),
        report_saver=saver,
    )

    assert events == ["enter", "exit", "save"]
    assert saves == [report]
