from __future__ import annotations

import json
import socket
from collections import Counter
from pathlib import Path

import pytest

from ai_news.cli import main
from ai_news.models import DailyReport, EditorialLane

REPOSITORY = Path(__file__).parents[1]
DEMO_CONTRACT = REPOSITORY / "tests/fixtures/demo-report-input.json"


def _directory_bytes(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _deny_network(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the offline demo attempted a network connection")


def _run_demo(root: Path, output: Path) -> None:
    assert main(["demo", "--root", str(root), "--output", str(output)]) == 0


def test_demo_cli_is_network_free_complete_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = json.loads(DEMO_CONTRACT.read_text(encoding="utf-8"))
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    for variable in (
        "LLM_PROTOCOL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_AUTH_SCHEME",
    ):
        monkeypatch.delenv(variable, raising=False)

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_output = first_root / "dist"
    second_output = second_root / "dist"

    _run_demo(first_root, first_output)
    _run_demo(second_root, second_output)

    report_path = first_root / "data/2026/07/2026-07-26.json"
    report = DailyReport.model_validate_json(report_path.read_bytes())
    assert report.date.isoformat() == contract["date"]
    assert len(report.items) == contract["item_count"] == 7
    assert report.schema_version == "1.3"
    lane_counts = Counter(item.editorial_lane for item in report.items)
    assert set(lane_counts) == {
        EditorialLane.PRODUCT_APPLICATION,
        EditorialLane.BUSINESS_MARKET,
        EditorialLane.PLATFORM_POLICY,
    }
    assert max(lane_counts.values()) <= 4
    assert max(Counter(item.source for item in report.items).values()) <= 2
    assert [item.title for item in report.items] == contract["expected_titles"]
    assert all(
        item.summary and item.why_it_matters and item.tracking_signal for item in report.items
    )

    markdown = first_root / "content/2026-07-26.md"
    required_files = {
        "index.html",
        "404.html",
        "days/2026-07-26/index.html",
        "categories/model/index.html",
        "categories/agent/index.html",
        "categories/ai-tools/index.html",
        "categories/open-source/index.html",
        "categories/industry-policy/index.html",
        "archive/index.html",
        "search/index.html",
        "sources/index.html",
        "search.json",
        "feed.xml",
        "sitemap.xml",
        "robots.txt",
        "build-info.json",
        "assets/styles.css",
        "assets/app.js",
        "assets/search-core.js",
        "assets/favicon.svg",
    }
    assert markdown.is_file()
    assert markdown.read_text(encoding="utf-8").startswith("# AI 资讯日报")
    assert required_files <= set(_directory_bytes(first_output))

    search_payload = json.loads((first_output / "search.json").read_text(encoding="utf-8"))
    assert len(search_payload) == len(report.items)
    assert all(
        entry["page_url"].startswith("/ai-news/days/2026-07-26/#item-") for entry in search_payload
    )
    assert json.loads((first_output / "build-info.json").read_text(encoding="utf-8")) == {
        "revision": "offline-demo",
        "schema_version": "1.0",
        "workflow_run_id": None,
    }

    assert _directory_bytes(first_root / "data") == _directory_bytes(second_root / "data")
    assert _directory_bytes(first_root / "content") == _directory_bytes(second_root / "content")
    assert _directory_bytes(first_output) == _directory_bytes(second_output)
