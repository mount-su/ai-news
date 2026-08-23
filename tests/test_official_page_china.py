from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_news.collectors.official_pages import baidu, tencent, volcengine
from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
)
from ai_news.models import Category, SourceSpec

FIXTURES = Path(__file__).parent / "fixtures" / "official_pages"
Parser = Callable[[bytes, SourceSpec], list[OfficialEntry]]


def _source(
    source_id: str,
    name: str,
    url: str,
    adapter: str,
    weight: int,
) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "id": source_id,
            "name": name,
            "kind": "official_page",
            "url": url,
            "adapter": adapter,
            "category": Category.MODEL,
            "weight": weight,
            "official": True,
        }
    )


TENCENT = _source(
    "tencent-hunyuan",
    "腾讯混元",
    "https://www.tencent.com/zh-cn/newsroom/all-news/",
    "tencent_hunyuan",
    8,
)
VOLCENGINE = _source(
    "volcengine-doubao",
    "豆包与火山方舟",
    "https://www.volcengine.com/news",
    "volcengine_doubao",
    9,
)
BAIDU = _source(
    "baidu-ernie",
    "百度文心",
    "https://cloud.baidu.com/news/news",
    "baidu_ernie",
    8,
)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_tencent_keeps_only_hunyuan_article() -> None:
    entries = tencent.parse(fixture("tencent.html"), TENCENT)

    assert [entry.title for entry in entries] == ["腾讯混元发布全新产品能力"]
    assert entries[0].url == "https://www.tencent.com/zh-cn/articles/2200131.html"
    assert entries[0].published_at == datetime(2026, 8, 19, 16, tzinfo=UTC)
    assert entries[0].excerpt == "腾讯混元面向企业和用户推出一项重要的 AI 产品能力。"


def test_volcengine_reads_router_data_and_keeps_doubao_or_ark() -> None:
    entries = volcengine.parse(fixture("volcengine.html"), VOLCENGINE)

    assert [entry.url for entry in entries] == ["https://www.volcengine.com/news/detail/123456"]
    assert entries[0].title == "豆包与火山方舟发布新的产品能力"
    assert entries[0].excerpt == "豆包与火山方舟面向用户发布重要能力。"
    assert entries[0].published_at == datetime(2026, 8, 20, 1, tzinfo=UTC)


def test_volcengine_reads_relevant_banner_entry() -> None:
    payload = b"""<script>window._ROUTER_DATA = {"loaderData":{
      "__ssr_without_user/news/page":{
        "banner":{"link":"/news/detail/banner-1","title":"Doubao product launch",
          "date":"2026-08-20 11:00:00","tag":"Doubao","category":"AI"},
        "listOnlineArticle":{"List":[]}}}};</script>"""

    entries = volcengine.parse(payload, VOLCENGINE)

    assert [entry.url for entry in entries] == ["https://www.volcengine.com/news/detail/banner-1"]
    assert entries[0].title == "Doubao product launch"


def test_baidu_reads_next_data_and_keeps_ernie_or_qianfan() -> None:
    entries = baidu.parse(fixture("baidu.html"), BAIDU)

    assert [entry.url for entry in entries] == ["https://cloud.baidu.com/news/news_98765"]
    assert entries[0].title == "百度文心与千帆发布重要产品更新"
    assert entries[0].excerpt == "文心与千帆平台的重要产品更新。"
    assert entries[0].published_at == datetime(2026, 8, 20, 2, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("adapter", "source"),
    [
        (tencent.parse, TENCENT),
        (volcengine.parse, VOLCENGINE),
        (baidu.parse, BAIDU),
    ],
)
def test_chinese_newsroom_adapter_rejects_missing_reviewed_structure(
    adapter: Parser,
    source: SourceSpec,
) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"<html><body>empty newsroom</body></html>", source)


@pytest.mark.parametrize(
    ("adapter", "source"),
    [
        (tencent.parse, TENCENT),
        (volcengine.parse, VOLCENGINE),
        (baidu.parse, BAIDU),
    ],
)
def test_chinese_newsroom_adapter_rejects_challenge_response(
    adapter: Parser,
    source: SourceSpec,
) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"<html><body>Verify you are human</body></html>", source)


@pytest.mark.parametrize(
    ("adapter", "source", "payload"),
    [
        (volcengine.parse, VOLCENGINE, b"<script>window._ROUTER_DATA = not_json;</script>"),
        (baidu.parse, BAIDU, b'<script id="__NEXT_DATA__">not_json</script>'),
    ],
)
def test_embedded_json_adapter_rejects_malformed_payload(
    adapter: Parser,
    source: SourceSpec,
    payload: bytes,
) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(payload, source)


def test_chinese_newsroom_adapter_rejects_source_for_another_parser() -> None:
    with pytest.raises(ValueError, match="tencent_hunyuan source"):
        tencent.parse(fixture("tencent.html"), BAIDU)
