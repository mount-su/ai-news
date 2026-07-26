from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_news.config import load_settings, load_source_config
from ai_news.models import Analysis, Category, RawItem


def test_load_source_config_rejects_unsupported_kind(tmp_path: Path) -> None:
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text(
        """
sources:
  - id: unsupported-source
    name: Unsupported Source
    kind: webpage
    url: https://example.com/news
    category: 大模型
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_source_config(config_path)


def test_load_settings_reads_expected_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-secret")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "test-model")
    monkeypatch.setenv("ANTHROPIC_AUTH_SCHEME", "bearer")
    monkeypatch.setenv("SITE_BASE_PATH", "/ai-news/")

    settings = load_settings()

    assert settings.llm_base_url == "https://api.example.com"
    assert settings.llm_token.get_secret_value() == "test-secret"
    assert settings.llm_model == "test-model"
    assert settings.llm_auth_scheme == "bearer"
    assert settings.site_base_path == "/ai-news/"


def test_load_source_config_rejects_empty_sources(tmp_path: Path) -> None:
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text("sources: []\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_source_config(config_path)


@pytest.mark.parametrize(
    ("kind", "locator"),
    [
        ("feed", ""),
        ("arxiv", ""),
        ("github", "url: https://example.com/news"),
    ],
)
def test_load_source_config_requires_kind_specific_locator(
    tmp_path: Path,
    kind: str,
    locator: str,
) -> None:
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text(
        f"""
sources:
  - id: missing-locator
    name: Missing Locator
    kind: {kind}
    {locator}
    category: 大模型
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_source_config(config_path)


@pytest.mark.parametrize("base_path", ["ai-news/", "/ai-news"])
def test_load_settings_rejects_base_path_without_boundary_slashes(
    monkeypatch: pytest.MonkeyPatch,
    base_path: str,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-secret")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "test-model")
    monkeypatch.setenv("SITE_BASE_PATH", base_path)

    with pytest.raises(ValueError):
        load_settings()


def test_load_settings_rejects_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-secret")
    monkeypatch.delenv("ANTHROPIC_DEFAULT_OPUS_MODEL", raising=False)

    with pytest.raises(ValueError):
        load_settings()


def test_repository_source_manifest_contains_only_verified_sources() -> None:
    config = load_source_config(Path(__file__).parents[1] / "sources" / "feeds.yaml")
    actual = {
        (
            source.kind,
            str(source.url) if source.url is not None else None,
            source.repo,
            source.weight,
            source.category.value,
            source.official,
        )
        for source in config.sources
    }
    expected = {
        ("feed", "https://openai.com/news/rss.xml", None, 10, "大模型", True),
        ("feed", "https://blog.google/technology/ai/rss/", None, 9, "大模型", True),
        ("feed", "https://deepmind.google/blog/rss.xml", None, 9, "论文研究", True),
        ("feed", "https://huggingface.co/blog/feed.xml", None, 8, "开源项目", True),
        (
            "arxiv",
            "https://export.arxiv.org/api/query?search_query=cat%3Acs.AI%20OR%20cat%3Acs.CL"
            "%20OR%20cat%3Acs.LG&sortBy=submittedDate&sortOrder=descending&start=0&max_results=40",
            None,
            7,
            "论文研究",
            True,
        ),
        ("github", None, "openai/codex", 10, "Coding Agent", True),
        ("github", None, "anthropics/claude-code", 10, "Coding Agent", True),
        ("github", None, "google-gemini/gemini-cli", 9, "Coding Agent", True),
        ("github", None, "langchain-ai/langchain", 8, "Agent", True),
        ("github", None, "crewAIInc/crewAI", 8, "Agent", True),
        ("github", None, "run-llama/llama_index", 7, "Agent", True),
        ("github", None, "huggingface/transformers", 8, "开源项目", True),
        ("github", None, "vllm-project/vllm", 8, "开源项目", True),
        ("github", None, "ollama/ollama", 8, "AI 工具", True),
        ("github", None, "cline/cline", 8, "Coding Agent", True),
    }

    assert len(config.sources) == len(expected)
    assert actual == expected


def test_public_item_models_accept_valid_pipeline_data() -> None:
    raw_item = RawItem(
        source_id="openai",
        source_name="OpenAI",
        source_weight=10,
        title="A useful model release",
        url="https://example.com/release",
        published_at=datetime(2026, 7, 26, tzinfo=UTC),
        category_hint=Category.MODEL,
        is_official_source=True,
    )
    analysis = Analysis(
        title="A useful model release",
        category=Category.MODEL,
        summary="A sufficiently detailed summary of the release.",
        importance=9,
        why_it_matters="It changes how teams build reliable AI products.",
        tags=["models", "release"],
        is_official=True,
        marketing_risk="low",
        tracking_signal="Watch adoption in production.",
    )

    assert raw_item.excerpt == ""
    assert analysis.importance == 9


@pytest.mark.parametrize("source_weight", [0, 11])
def test_raw_item_rejects_out_of_range_source_weight(source_weight: int) -> None:
    with pytest.raises(ValueError):
        RawItem(
            source_id="openai",
            source_name="OpenAI",
            source_weight=source_weight,
            title="A useful model release",
            url="https://example.com/release",
            published_at=datetime(2026, 7, 26, tzinfo=UTC),
            category_hint=Category.MODEL,
            is_official_source=True,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("summary", "s" * 19),
        ("summary", "s" * 1201),
        ("importance", 0),
        ("importance", 11),
        ("why_it_matters", "w" * 9),
        ("why_it_matters", "w" * 801),
        ("tags", []),
        ("tags", ["tag"] * 7),
        ("tracking_signal", "t" * 4),
        ("tracking_signal", "t" * 501),
    ],
)
def test_analysis_rejects_out_of_range_fields(field_name: str, invalid_value: object) -> None:
    values = {
        "title": "A useful model release",
        "category": Category.MODEL,
        "summary": "A sufficiently detailed summary of the release.",
        "importance": 9,
        "why_it_matters": "It changes how teams build reliable AI products.",
        "tags": ["models", "release"],
        "is_official": True,
        "marketing_risk": "low",
        "tracking_signal": "Watch adoption in production.",
    }
    values[field_name] = invalid_value

    with pytest.raises(ValueError):
        Analysis.model_validate(values)
