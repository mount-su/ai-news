from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DESIGN = ROOT / "docs" / "superpowers" / "specs" / "2026-07-26-ai-news-github-pages-design.md"
OLD_IMPLEMENTATION_PLAN = (
    ROOT / "docs" / "superpowers" / "plans" / "2026-07-26-ai-news-github-pages.md"
)

EXPECTED_HEADINGS = [
    "# AI 情报雷达",
    "## 它如何工作",
    "## 页面与数据结构",
    "## 本地快速开始",
    "## 离线演示",
    "## 使用真实模型生成",
    "## GitHub Secrets 与 Variables",
    "## GitHub Actions",
    "## 数据源维护",
    "## 故障与补跑",
    "## 安全与版权",
    "## 项目结构",
]


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_uses_the_approved_top_level_structure() -> None:
    headings = re.findall(r"^#{1,2} .+$", _readme(), flags=re.MULTILINE)

    assert headings == EXPECTED_HEADINGS


def test_readme_documents_local_offline_and_live_commands() -> None:
    readme = _readme()

    required_fragments = [
        "Python 3.12",
        'python -m pip install -e ".[dev]"',
        "python -m ai_news demo",
        "python -m ai_news build",
        "python scripts/validate_site.py",
        "python -m http.server",
        "python -m ai_news generate --root .",
        "python -m ai_news generate --root . --date YYYY-MM-DD",
    ]

    for fragment in required_fragments:
        assert fragment in readme


def test_readme_documents_exact_repository_configuration() -> None:
    readme = _readme()

    for variable in [
        "LLM_PROTOCOL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_AUTH_SCHEME",
    ]:
        assert variable in readme
    assert "https://ark.cn-beijing.volces.com/api/v3" in readme
    assert "gh secret set LLM_API_KEY" in readme
    assert "/api/coding" in readme
    assert "不能用于" in readme
    assert "SITE_BASE_PATH=/ai-news/" in readme
    assert "08:17" in readme
    assert "Asia/Shanghai" in readme
    assert "https://mount-su.github.io/ai-news/" in readme
    assert "预期 Pages 地址" in readme
    assert not re.search(r"^线上地址", readme, flags=re.MULTILINE)


def test_readme_documents_automation_recovery_and_safety_boundaries() -> None:
    readme = _readme()

    required_fragments = [
        "workflow_dispatch",
        "gh workflow run daily-news.yml",
        "push 到 `main`",
        "不调用 LLM",
        "最多 3 次",
        "原子",
        "降级",
        "最后一次成功部署",
        "Coding Plan",
        "无人值守的非编码资讯摘要",
        "标准模型 API 凭证",
        "不保存文章全文",
        "聊天、截图、日志或本地环境中的凭证",
    ]

    for fragment in required_fragments:
        assert fragment in readme


def test_readme_links_the_design_and_implementation_plan() -> None:
    readme = _readme()

    assert "docs/superpowers/specs/2026-07-26-ai-news-github-pages-design.md" in readme
    assert "docs/superpowers/plans/2026-07-27-standard-ark-api.md" in readme
    assert "当前实施计划" in readme
    assert "docs/superpowers/plans/2026-07-26-ai-news-github-pages.md" not in readme


def test_superseded_plan_warns_before_any_executable_content() -> None:
    old_plan = OLD_IMPLEMENTATION_PLAN.read_text(encoding="utf-8")
    canonical_plan = "docs/superpowers/plans/2026-07-27-standard-ark-api.md"

    assert old_plan.startswith("> [!WARNING]\n")
    warning_end = old_plan.index("# AI 情报雷达 Implementation Plan")
    warning = old_plan[:warning_end]
    assert "模型配置步骤已废弃" in warning
    assert "请勿执行" in warning
    assert canonical_plan in warning
    assert "取代" in warning


def test_readme_does_not_contain_screenshot_markup_or_token_assignment() -> None:
    readme = _readme()

    assert "![" not in readme
    assert "当前截图中的凭证" not in readme
    assert not re.search(
        r"LLM_API_KEY\s*=\s*[\"']?[A-Za-z0-9_-]{16,}",
        readme,
    )


def test_readme_describes_the_implemented_degraded_rule() -> None:
    readme = _readme()

    assert "来源失败且最终仍有至少 5 条" in readme
    assert "`degraded=true`" in readme
    assert "该字段由来源运行是否失败决定" in readme
    assert "候选损失时可以降级发布" not in readme


def test_docs_describe_strict_batch_analysis_failure() -> None:
    readme = _readme()
    design = DESIGN.read_text(encoding="utf-8")

    for document in [readme, design]:
        assert "整批严格校验" in document
        assert "只执行一次结构修复" in document
        assert "整次生成失败" in document
        assert "不写入新日报" in document
        assert "舍弃该批" not in document
        assert "个别分析无效会被丢弃" not in document


def test_runtime_docs_do_not_instruct_use_of_legacy_model_variables() -> None:
    readme = _readme()
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "ANTHROPIC_" not in readme
    assert "ANTHROPIC_" not in env_example


def test_env_example_uses_safe_non_secret_placeholders() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "LLM_PROTOCOL=openai-chat" in env_example
    assert "LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3" in env_example
    assert "LLM_API_" + "KEY=replace-with-a-repository-secret" in env_example
    assert "LLM_MODEL=replace-with-a-standard-api-model-id" in env_example
    assert "LLM_AUTH_SCHEME=bearer" in env_example
    assert "SITE_BASE_PATH=/ai-news/" in env_example


def test_readme_documents_standard_ark_operator_boundaries() -> None:
    readme = _readme()

    required_fragments = [
        "`/api/coding` 端点和 Coding Plan 密钥不能用于本项目",
        "标准 Ark API 使用 `/api/v3`",
        "provider 控制台",
        "标准 API 模型 ID",
        "直接写入 GitHub Secret",
        "隐藏终端输入",
        "不能发送到聊天",
        "不能写入文件",
        "至少一份成功生成的真实日报",
        "双协议模型适配与严格响应校验",
    ]

    for fragment in required_fragments:
        assert fragment in readme


def test_original_design_points_to_the_canonical_protocol_supplement() -> None:
    design = DESIGN.read_text(encoding="utf-8")

    assert "## 2026-07-27 模型协议补充" in design
    assert "docs/superpowers/specs/2026-07-27-standard-ark-api-design.md" in design
    assert "以该补充设计为准" in design
