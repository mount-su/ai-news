from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_news.collectors.official_pages import minimax, zhipu
from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
)
from ai_news.models import Category, SourceSpec

FIXTURES = Path(__file__).parent / "fixtures" / "official_pages"
Parser = Callable[[bytes, SourceSpec], list[OfficialEntry]]


def _source(source_id: str, name: str, url: str, adapter: str) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "id": source_id,
            "name": name,
            "kind": "official_page",
            "url": url,
            "adapter": adapter,
            "category": Category.MODEL,
            "weight": 9,
            "official": True,
        }
    )


ZHIPU = _source(
    "zhipu-glm",
    "智谱 GLM",
    "https://docs.bigmodel.cn/cn/update/new-releases.md",
    "zhipu_releases",
)
MINIMAX = _source(
    "minimax",
    "MiniMax",
    "https://platform.minimaxi.com/docs/release-notes/models.md",
    "minimax_releases",
)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_zhipu_parses_update_block_and_ignores_non_release_text() -> None:
    entries = zhipu.parse(fixture("zhipu.md"), ZHIPU)

    assert [(entry.title, entry.url) for entry in entries] == [
        (
            "GLM-5.3 新一代旗舰模型上线",
            "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
        )
    ]
    assert entries[0].published_at == datetime(2026, 8, 18, 16, tzinfo=UTC)
    assert entries[0].excerpt == "GLM-5.3 GLM-5.3 面向企业和开发者提供新的模型与产品能力。"


def test_minimax_requires_full_day_heading_and_model_card() -> None:
    entries = minimax.parse(fixture("minimax.md"), MINIMAX)

    assert [entry.title for entry in entries] == ["MiniMax H3"]
    assert entries[0].published_at == datetime(2026, 7, 30, 16, tzinfo=UTC)
    assert entries[0].url == "https://www.minimaxi.com/blog/minimax-h3"
    assert entries[0].excerpt == (
        "MiniMax H3 正式发布，为用户和 AI 产品提供新的模型能力。"  # noqa: RUF001
    )


@pytest.mark.parametrize(
    ("adapter", "source"),
    [(zhipu.parse, ZHIPU), (minimax.parse, MINIMAX)],
)
def test_markdown_adapter_raises_when_reviewed_markers_disappear(
    adapter: Parser,
    source: SourceSpec,
) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"# ordinary markdown", source)


@pytest.mark.parametrize(
    ("adapter", "source"),
    [(zhipu.parse, ZHIPU), (minimax.parse, MINIMAX)],
)
def test_markdown_adapter_raises_on_challenge_response(
    adapter: Parser,
    source: SourceSpec,
) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"Access denied: verify you are human", source)


def test_markdown_adapter_rejects_source_for_another_parser() -> None:
    with pytest.raises(ValueError, match="zhipu_releases source"):
        zhipu.parse(fixture("zhipu.md"), MINIMAX)
