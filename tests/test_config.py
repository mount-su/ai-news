from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_news.config import load_settings, load_source_config
from ai_news.models import Analysis, Category, RawItem, SourceSpec


def _set_valid_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-secret")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "test-model")
    monkeypatch.setenv("ANTHROPIC_AUTH_SCHEME", "bearer")
    monkeypatch.setenv("SITE_BASE_PATH", "/ai-news/")


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
    _set_valid_settings_environment(monkeypatch)

    settings = load_settings()

    assert settings.llm_base_url == "https://api.example.com"
    assert settings.llm_token.get_secret_value() == "test-secret"
    assert settings.llm_model == "test-model"
    assert settings.llm_auth_scheme == "bearer"
    assert settings.site_base_path == "/ai-news/"


@pytest.mark.parametrize(
    ("environment_name", "invalid_value"),
    [
        ("ANTHROPIC_BASE_URL", ""),
        ("ANTHROPIC_BASE_URL", "   "),
        ("ANTHROPIC_AUTH_TOKEN", ""),
        ("ANTHROPIC_AUTH_TOKEN", "   "),
        ("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
        ("ANTHROPIC_DEFAULT_OPUS_MODEL", "   "),
    ],
)
def test_load_settings_rejects_blank_required_values(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    invalid_value: str,
) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.setenv(environment_name, invalid_value)

    with pytest.raises(ValueError) as exc_info:
        load_settings()

    assert "test-secret" not in str(exc_info.value)


def test_load_settings_strips_required_values_and_preserves_custom_base_url_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "  https://api.example.com/custom/v1  ")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "  test-secret  ")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "  test-model  ")

    settings = load_settings()

    assert settings.llm_base_url == "https://api.example.com/custom/v1"
    assert settings.llm_token.get_secret_value() == "test-secret"
    assert settings.llm_model == "test-model"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com",
        "not-a-url",
        "https:///missing-host",
    ],
)
def test_load_settings_requires_valid_https_base_url(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)

    with pytest.raises(ValueError) as exc_info:
        load_settings()

    assert "test-secret" not in str(exc_info.value)


def test_load_source_config_rejects_empty_sources(tmp_path: Path) -> None:
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text("sources: []\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_source_config(config_path)


def test_load_source_config_rejects_duplicate_source_ids(tmp_path: Path) -> None:
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text(
        """
sources:
  - id: duplicate-source
    name: First Source
    kind: feed
    url: https://example.com/first.xml
    category: 大模型
  - id: duplicate-source
    name: Second Source
    kind: feed
    url: https://example.com/second.xml
    category: 大模型
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_source_config(config_path)


@pytest.mark.parametrize(
    "yaml_text",
    [
        """
sources:
  - id: typo-source
    name: Typo Source
    kind: feed
    url: https://example.com/feed.xml
    category: 大模型
    enable: true
""",
        """
sources:
  - id: typo-source
    name: Typo Source
    kind: feed
    url: https://example.com/feed.xml
    category: 大模型
offical: true
""",
    ],
)
def test_load_source_config_rejects_unknown_fields(tmp_path: Path, yaml_text: str) -> None:
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

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


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "   ",
        "owner",
        "owner/repo/extra",
        "/repo",
        "owner/",
        "owner/re po",
        "owner/@repo",
    ],
)
def test_github_source_rejects_invalid_repo(repo: str) -> None:
    with pytest.raises(ValueError):
        SourceSpec(
            id="github-invalid",
            name="Invalid GitHub Repo",
            kind="github",
            repo=repo,
            category=Category.OPEN_SOURCE,
        )


def test_github_source_strips_valid_repo() -> None:
    source = SourceSpec(
        id="github-valid",
        name="Valid GitHub Repo",
        kind="github",
        repo="  owner.name/repo_name-1  ",
        category=Category.OPEN_SOURCE,
    )

    assert source.repo == "owner.name/repo_name-1"


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
            source.id,
            source.name,
            source.kind,
            str(source.url) if source.url is not None else None,
            source.repo,
            source.weight,
            source.category.value,
            source.official,
            source.enabled,
        )
        for source in config.sources
    }
    expected = {
        (
            "openai",
            "OpenAI",
            "feed",
            "https://openai.com/news/rss.xml",
            None,
            10,
            "大模型",
            True,
            True,
        ),
        (
            "google-ai",
            "Google AI",
            "feed",
            "https://blog.google/technology/ai/rss/",
            None,
            9,
            "大模型",
            True,
            True,
        ),
        (
            "deepmind",
            "DeepMind",
            "feed",
            "https://deepmind.google/blog/rss.xml",
            None,
            9,
            "论文研究",
            True,
            True,
        ),
        (
            "hugging-face",
            "Hugging Face",
            "feed",
            "https://huggingface.co/blog/feed.xml",
            None,
            8,
            "开源项目",
            True,
            True,
        ),
        (
            "arxiv-ai",
            "arXiv AI",
            "arxiv",
            "https://export.arxiv.org/api/query?search_query=cat%3Acs.AI%20OR%20cat%3Acs.CL"
            "%20OR%20cat%3Acs.LG&sortBy=submittedDate&sortOrder=descending&start=0&max_results=40",
            None,
            7,
            "论文研究",
            True,
            True,
        ),
        (
            "github-codex",
            "OpenAI Codex",
            "github",
            None,
            "openai/codex",
            10,
            "Coding Agent",
            True,
            True,
        ),
        (
            "github-claude-code",
            "Claude Code",
            "github",
            None,
            "anthropics/claude-code",
            10,
            "Coding Agent",
            True,
            True,
        ),
        (
            "github-gemini-cli",
            "Gemini CLI",
            "github",
            None,
            "google-gemini/gemini-cli",
            9,
            "Coding Agent",
            True,
            True,
        ),
        (
            "github-langchain",
            "LangChain",
            "github",
            None,
            "langchain-ai/langchain",
            8,
            "Agent",
            True,
            True,
        ),
        (
            "github-crewai",
            "CrewAI",
            "github",
            None,
            "crewAIInc/crewAI",
            8,
            "Agent",
            True,
            True,
        ),
        (
            "github-llama-index",
            "LlamaIndex",
            "github",
            None,
            "run-llama/llama_index",
            7,
            "Agent",
            True,
            True,
        ),
        (
            "github-transformers",
            "Transformers",
            "github",
            None,
            "huggingface/transformers",
            8,
            "开源项目",
            True,
            True,
        ),
        (
            "github-vllm",
            "vLLM",
            "github",
            None,
            "vllm-project/vllm",
            8,
            "开源项目",
            True,
            True,
        ),
        (
            "github-ollama",
            "Ollama",
            "github",
            None,
            "ollama/ollama",
            8,
            "AI 工具",
            True,
            True,
        ),
        (
            "github-cline",
            "Cline",
            "github",
            None,
            "cline/cline",
            8,
            "Coding Agent",
            True,
            True,
        ),
    }
    source_ids = [source.id for source in config.sources]

    assert len(config.sources) == len(expected)
    assert len(source_ids) == len(set(source_ids))
    assert all(source.enabled for source in config.sources)
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
