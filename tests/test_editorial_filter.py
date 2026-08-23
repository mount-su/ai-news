# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_news.models import Category, RawItem
from ai_news.pipeline.editorial import is_editorially_eligible
from ai_news.pipeline.normalize import to_candidate


def _candidate(title: str, excerpt: str = "", source_id: str = "source-test"):
    return to_candidate(
        RawItem(
            source_id=source_id,
            source_name="Test source",
            source_weight=7,
            title=title,
            url="https://example.com/editorial",
            published_at=datetime(2026, 8, 1, 8, tzinfo=UTC),
            excerpt=excerpt,
            category_hint=Category.MODEL,
            is_official_source=True,
        )
    )


@pytest.mark.parametrize(
    "title",
    [
        "arXiv: A new benchmark for multimodal models",
        "SDK v2.4.0 adds a streaming API",
        "Open-source framework introduces a new agent runtime",
        "Research paper studies alignment behavior",
        "Version 1.2.3 fixes connector timeout",
        "Company raises $100M in latest funding round",
    ],
)
def test_editorial_filter_rejects_technical_academic_and_unimpactful_corporate_news(
    title: str,
) -> None:
    assert is_editorially_eligible(_candidate(title)) is False


@pytest.mark.parametrize(
    "title,excerpt",
    [
        ("ChatGPT launches a new consumer plan", "The plan is available to all users today."),
        ("AI assistant cuts subscription price", "The monthly price changes for customers."),
        ("Major retailer deploys AI shopping assistant", "Millions of shoppers can use it."),
        (
            "Government publishes new AI platform rules",
            "The rules change what providers must disclose.",
        ),
        (
            "Company raises funding to expand free access",
            "The investment will expand the product's free tier.",
        ),
    ],
)
def test_editorial_filter_accepts_concrete_user_or_market_changes(
    title: str,
    excerpt: str,
) -> None:
    assert is_editorially_eligible(_candidate(title, excerpt)) is True


@pytest.mark.parametrize(
    "title",
    [
        "LangChain core 1.5.5 release fixes tool validation",
        "LangChain v0.2.8 changelog adds provider metadata",
        "LangChain patch fixes streaming reasoning content",
    ],
)
def test_editorial_filter_rejects_github_release_details(title: str) -> None:
    assert is_editorially_eligible(_candidate(title, source_id="github-langchain")) is False


@pytest.mark.parametrize(
    "title,excerpt",
    [
        (
            "OpenAI 发布可显著降低企业推理成本的新模型",
            "新模型在公开评测和生产部署中带来明显的成本与性能改善。",
        ),
        (
            "PyTorch 发布重大安全漏洞缓解方案，影响主流 AI 部署",
            "该漏洞影响大量线上部署，修复建议已公开并可立即执行。",
        ),
        (
            "多模态模型在真实客服场景实现大规模落地",
            "企业客户已将该能力用于客服流程，服务覆盖范围显著扩大。",
        ),
        (
            "多模态 benchmark 显示推理性能提升 40%",
            "公开评测显示该提升已在生产部署中得到验证。",
        ),
        (
            "Benchmark shows 40% lower inference cost for production API",
            "The benchmark reports lower latency and cost for teams using the API.",
        ),
        (
            "LangChain major release changes runtime architecture",
            "The major release introduces migration requirements for production agent systems.",
        ),
    ],
)
def test_editorial_filter_keeps_high_impact_technical_news(
    title: str,
    excerpt: str,
) -> None:
    source_id = "github-pytorch" if title.startswith("PyTorch") else "source-test"
    assert is_editorially_eligible(_candidate(title, excerpt, source_id=source_id)) is True


@pytest.mark.parametrize(
    "title,excerpt",
    [
        (
            "LangChain v1.0 introduces breaking agent runtime changes",
            "The release changes production agent deployment paths and migration requirements.",
        ),
        (
            "OpenAI API update reduces enterprise inference latency",
            "The API change improves latency and lowers cost for production deployments.",
        ),
        (
            "Claude Code SDK adds production stability controls",
            "The SDK update targets enterprise rollout reliability and compatibility.",
        ),
        (
            "Transformers release expands model availability for developers",
            "The release changes access to widely used models across production AI applications.",
        ),
    ],
)
def test_editorial_filter_accepts_high_impact_developer_ecosystem_updates(
    title: str,
    excerpt: str,
) -> None:
    assert is_editorially_eligible(_candidate(title, excerpt, source_id="github-langchain")) is True


@pytest.mark.parametrize(
    "title,excerpt",
    [
        (
            "How to build a prompt template in five minutes",
            "A generic tutorial with no new product facts.",
        ),
        (
            "Model benchmark leaderboard update",
            "Scores changed without product availability or cost impact.",
        ),
        ("SDK v2.4.1 fixes connector timeout", "A patch-level changelog item for one connector."),
        (
            "Framework repository adds example notebooks",
            "The update is documentation-only and not a major release.",
        ),
        (
            "LangChain v1.0 tutorial for migration",
            "A prompt-template tutorial with no new product facts.",
        ),
    ],
)
def test_editorial_filter_rejects_low_value_technical_content(
    title: str,
    excerpt: str,
) -> None:
    assert (
        is_editorially_eligible(_candidate(title, excerpt, source_id="github-langchain")) is False
    )
