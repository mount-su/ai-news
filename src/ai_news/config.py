from __future__ import annotations

import os
from pathlib import Path

import yaml

from ai_news.models import Settings, SourceConfig


def load_source_config(path: Path) -> SourceConfig:
    with path.open(encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)

    return SourceConfig.model_validate(data)


def load_settings() -> Settings:
    environment_names = {
        "llm_base_url": "ANTHROPIC_BASE_URL",
        "llm_token": "ANTHROPIC_AUTH_TOKEN",
        "llm_model": "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "llm_auth_scheme": "ANTHROPIC_AUTH_SCHEME",
        "site_base_path": "SITE_BASE_PATH",
    }
    values = {
        field_name: os.environ[environment_name]
        for field_name, environment_name in environment_names.items()
        if environment_name in os.environ
    }
    return Settings.model_validate(values)
