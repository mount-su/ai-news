import json
from collections import UserDict
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import SecretStr, ValidationError

from ai_news.config import load_settings, load_source_config
from ai_news.models import Analysis, Category, RawItem, Settings, SourceSpec


def _set_valid_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROTOCOL", "openai-chat")
    monkeypatch.setenv("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "ark-standard-model")
    monkeypatch.setenv("LLM_AUTH_SCHEME", "bearer")
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

    assert settings.llm_protocol == "openai-chat"
    assert settings.llm_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert settings.llm_token.get_secret_value() == "test-secret"
    assert settings.llm_model == "ark-standard-model"
    assert settings.llm_auth_scheme == "bearer"
    assert settings.site_base_path == "/ai-news/"


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "sensitive_value"),
    [
        ("llm_protocol", b"openai-chat", None),
        ("llm_protocol", 101, None),
        (
            "llm_base_url",
            b"https://secret-host.example/api",
            "secret-host.example",
        ),
        ("llm_base_url", 102, "102"),
        ("llm_token", b"bytes-token-secret", "bytes-token-secret"),
        ("llm_token", 103, "103"),
        ("llm_model", b"bytes-model-secret", "bytes-model-secret"),
        ("llm_model", 104, "104"),
        ("llm_auth_scheme", b"bearer", None),
        ("llm_auth_scheme", 105, None),
        ("site_base_path", b"/ai-news/", None),
        ("site_base_path", 106, None),
    ],
)
def test_settings_rejects_non_string_values_without_leaking_input(
    field_name: str,
    invalid_value: object,
    sensitive_value: str | None,
) -> None:
    values = {
        "llm_protocol": "openai-chat",
        "llm_base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "llm_token": "test-secret",
        "llm_model": "ark-standard-model",
        "llm_auth_scheme": "bearer",
        "site_base_path": "/ai-news/",
    }
    values[field_name] = invalid_value

    with pytest.raises(ValueError) as exc_info:
        Settings.model_validate(values)

    error = str(exc_info.value)
    if sensitive_value is not None:
        assert sensitive_value not in error
    assert "test-secret" not in error


@pytest.mark.parametrize(
    "invalid_token",
    [
        b"structured-bytes-secret-7e91",
        ["structured-list-secret-7e91"],
        {"nested": {"token": "structured-dict-secret-7e91"}},
    ],
)
def test_settings_non_string_token_never_leaks_from_validation_error_surfaces(
    invalid_token: object,
) -> None:
    sensitive_value = (
        "structured-"
        + {
            bytes: "bytes",
            list: "list",
            dict: "dict",
        }[type(invalid_token)]
        + "-secret-7e91"
    )

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            llm_protocol="openai-chat",
            llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
            llm_token=invalid_token,
            llm_model="ark-standard-model",
        )

    error = exc_info.value
    rendered_errors = (
        str(error),
        repr(error),
        json.dumps(error.errors(), default=str),
        error.json(),
    )
    assert all(sensitive_value not in rendered for rendered in rendered_errors)
    assert any(item["loc"] == ("llm_token",) for item in error.errors())


@pytest.mark.parametrize(
    ("mapped_values", "sensitive_value"),
    [
        (
            UserDict(
                {
                    "llm_protocol": "openai-chat",
                    "llm_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "llm_token": b"userdict-mapped-secret-7e91",
                    "llm_model": "ark-standard-model",
                }
            ),
            "userdict-mapped-secret-7e91",
        ),
        (
            MappingProxyType(
                {
                    "llm_protocol": "openai-chat",
                    "llm_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "llm_token": ["proxy-mapped-secret-7e91"],
                    "llm_model": "ark-standard-model",
                }
            ),
            "proxy-mapped-secret-7e91",
        ),
    ],
)
def test_settings_mapped_non_string_token_never_leaks_from_error_surfaces(
    mapped_values: object,
    sensitive_value: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate(mapped_values)

    error = exc_info.value
    rendered_errors = (
        str(error),
        repr(error),
        json.dumps(error.errors(), default=str),
        error.json(),
    )
    assert all(sensitive_value not in rendered for rendered in rendered_errors)
    assert any(item["loc"] == ("llm_token",) for item in error.errors())


def test_settings_strips_existing_secret_str_token() -> None:
    settings = Settings(
        llm_protocol="anthropic",
        llm_base_url="https://api.anthropic.com",
        llm_token=SecretStr("  existing-secret  "),
        llm_model="claude-test",
    )

    assert settings.llm_token.get_secret_value() == "existing-secret"
    assert "existing-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("environment_name", "invalid_value"),
    [
        ("LLM_PROTOCOL", ""),
        ("LLM_PROTOCOL", "   "),
        ("LLM_BASE_URL", ""),
        ("LLM_BASE_URL", "   "),
        ("LLM_API_KEY", ""),
        ("LLM_API_KEY", "   "),
        ("LLM_MODEL", ""),
        ("LLM_MODEL", "   "),
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
    monkeypatch.setenv("LLM_PROTOCOL", "  anthropic  ")
    monkeypatch.setenv("LLM_BASE_URL", "  https://API.EXAMPLE.COM/custom/v1  ")
    monkeypatch.setenv("LLM_API_KEY", "  test-secret  ")
    monkeypatch.setenv("LLM_MODEL", "  test-model  ")

    settings = load_settings()

    assert settings.llm_protocol == "anthropic"
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
    monkeypatch.setenv("LLM_BASE_URL", base_url)

    with pytest.raises(ValueError) as exc_info:
        load_settings()

    assert "test-secret" not in str(exc_info.value)


@pytest.mark.parametrize("protocol", ["openai", "anthropic-messages", "OPENAI-CHAT"])
def test_load_settings_rejects_unknown_protocol(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.setenv("LLM_PROTOCOL", protocol)

    with pytest.raises(ValueError) as exc_info:
        load_settings()

    assert "test-secret" not in str(exc_info.value)


def test_openai_chat_rejects_x_api_key_auth_without_echoing_key() -> None:
    with pytest.raises(ValueError) as exc_info:
        Settings(
            llm_protocol="openai-chat",
            llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
            llm_token="standard-api-secret",
            llm_model="ark-standard-model",
            llm_auth_scheme="x-api-key",
        )

    assert "standard-api-secret" not in str(exc_info.value)


def test_protocol_auth_validation_redacts_token_from_structured_errors() -> None:
    unique_secret = "structured-error-secret-7e91b2"

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            llm_protocol="openai-chat",
            llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
            llm_token=unique_secret,
            llm_model="ark-standard-model",
            llm_auth_scheme="x-api-key",
        )

    error = exc_info.value
    rendered_errors = (
        str(error),
        repr(error),
        json.dumps(error.errors(), default=str),
        error.json(),
    )
    assert all(unique_secret not in rendered for rendered in rendered_errors)


def test_anthropic_accepts_x_api_key_auth() -> None:
    settings = Settings(
        llm_protocol="anthropic",
        llm_base_url="https://api.anthropic.com",
        llm_token="anthropic-secret",
        llm_model="claude-test",
        llm_auth_scheme="x-api-key",
    )

    assert settings.llm_auth_scheme == "x-api-key"


def test_load_settings_rejects_coding_plan_url_without_echoing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.setenv(
        "LLM_BASE_URL",
        "https://ark.cn-beijing.volces.com/api/coding",
    )
    monkeypatch.setenv("LLM_API_KEY", "standard-api-secret")

    with pytest.raises(ValueError) as exc_info:
        load_settings()

    error = str(exc_info.value)
    assert "Ark Coding Plan endpoints are not allowed" in error
    assert "standard-api-secret" not in error


def test_load_settings_does_not_fall_back_to_legacy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for environment_name in (
        "LLM_PROTOCOL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_AUTH_SCHEME",
    ):
        monkeypatch.delenv(environment_name, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "legacy-secret")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "legacy-model")
    monkeypatch.setenv("ANTHROPIC_AUTH_SCHEME", "x-api-key")

    with pytest.raises(ValueError) as exc_info:
        load_settings()

    assert "legacy-secret" not in str(exc_info.value)


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
        "../repo",
        "./repo",
        "owner/..",
        "owner/.",
        "owner--name/repo",
        "-owner/repo",
        "owner-/repo",
        "owner.name/repo",
        f"{'o' * 40}/repo",
        f"owner/{'r' * 101}",
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
        repo="  owner-name/repo.name_1-2  ",
        category=Category.OPEN_SOURCE,
    )

    assert source.repo == "owner-name/repo.name_1-2"


@pytest.mark.parametrize("base_path", ["ai-news/", "/ai-news"])
def test_load_settings_rejects_base_path_without_boundary_slashes(
    monkeypatch: pytest.MonkeyPatch,
    base_path: str,
) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.setenv("SITE_BASE_PATH", base_path)

    with pytest.raises(ValueError):
        load_settings()


@pytest.mark.parametrize(
    "environment_name",
    ["LLM_PROTOCOL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"],
)
def test_load_settings_rejects_missing_required_value(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.delenv(environment_name, raising=False)

    with pytest.raises(ValueError) as exc_info:
        load_settings()

    assert "test-secret" not in str(exc_info.value)


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
