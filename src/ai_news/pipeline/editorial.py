"""Deterministic editorial eligibility rules for the consumer-facing brief."""

from __future__ import annotations

import re

from ai_news.models import Candidate, SourceRole

_ACADEMIC_MARKERS = re.compile(
    r"(?:\barxiv\b|\bpaper\b|\bpreprint\b|\bstudy\b|"
    r"\brandomi[sz]ed\s+controlled\b|\bresearch\s+evaluation\b|"
    r"\balignment\s+behavio(?:u)?r\b|论文|学术|研究论文|科研|模型原理|模型训练|对齐行为)",
    re.IGNORECASE,
)
_ACADEMIC_IMPACT_EXCEPTIONS = re.compile(
    r"(?:\b(?:is|are|now)\s+available\b|\blaunch(?:es|ed|ing)?\b|"
    r"\brelease[sd]?\s+(?:to|for)\b|\broll(?:s|ed|ing)?\s+out\b|"
    r"\bproduction\s+(?:deployment|service|use|availability)\b|"
    r"\bdeployed\s+in\s+production\b|\bpricing\b|\bprice\s+(?:cut|change|drops?)\b|"
    r"\blower\s+[^.。]{0,40}(?:cost|latency)\b|\bpolicy\s+(?:change|rule|update)\b|"
    r"\bregulat(?:ion|ory)\s+(?:change|rule|update)\b|"
    r"\bsecurity\s+(?:issue|update|vulnerability|advisory)\b|"
    r"现已可用|正式上线|正式发布|投入生产|生产部署|价格调整|降价|成本降低|"
    r"政策变化|监管规则|安全漏洞)",
    re.IGNORECASE,
)
_SPONSORED_MARKERS = re.compile(
    r"(?:\bpresented\s+by\b|\bpartner\s+content\b|\bsponsored\b|赞助内容|商业推广)",
    re.IGNORECASE,
)
_BENCHMARK_MARKERS = re.compile(
    r"(?:benchmark|基准测试|评测集|leaderboard|排行榜)",
    re.IGNORECASE,
)
_ALWAYS_LOW_VALUE_TECHNICAL_MARKERS = re.compile(
    r"(?:泛教程|generic tutorial|documentation-only|no new product facts|"
    r"how to [^.。]{0,80}template|template[- ]tutorial|tutorial [^.。]{0,80}template|"
    r"example notebooks?|notebook examples?)",
    re.IGNORECASE,
)
_LOW_VALUE_TECHNICAL_MARKERS = re.compile(
    r"(?:小版本|补丁|修复连接器|超时配置|"
    r"\b(?:v|version)\s*\d+\.\d+\.\d+|\b\d+\.\d+\.\d+\b)",
    re.IGNORECASE,
)
_DEVELOPER_ECOSYSTEM_MARKERS = re.compile(
    r"(?:\bsdk\b|\bapi\b|接口协议|开发框架|framework|repository|仓库|代码工具|"
    r"agent runtime|runtime|开源项目|developer|开发者)",
    re.IGNORECASE,
)
_CORPORATE_ONLY_MARKERS = re.compile(
    r"(?:融资|估值|募资|轮融资|高管|任命|离职|收购|投资|funding|valuation|executive|"
    r"appointed|resigns|acquisition|investment)",
    re.IGNORECASE,
)
_CONCRETE_IMPACT_MARKERS = re.compile(
    r"(?:产品|功能|套餐|订阅|价格|免费|free|tier|开放|上线|发布|推出|用户|客户|消费者|"
    r"企业|部署|采用|购物|平台|政策|监管|规则|合规|市场|可用|访问|服务|开发者|"
    r"production|enterprise|availability|available|access|cost|latency|compatibility|"
    r"migration|support|supports|configured)",
    re.IGNORECASE,
)
_GITHUB_RELEASE_MARKERS = re.compile(
    r"(?:\brelease\b|\bchangelog\b|patch|修复|版本|\bversion\b|\bv\d+(?:\.\d+){1,3}\b|"
    r"\b\d+\.\d+\.\d+\b)",
    re.IGNORECASE,
)
_HIGH_IMPACT_TECHNICAL_MARKERS = re.compile(
    r"(?:安全漏洞|漏洞|security|vulnerability|重大故障|中断|outage|大规模落地|大规模采用|"
    r"生产部署|成本降低|成本|性能提升|performance|latency|adoption|breaking change|"
    r"重大版本|架构变化|migration|compatibility|enterprise|production|availability|"
    r"third-party gateway|gateway provider|open models?|可用范围|访问门槛|主流产品|"
    r"稳定性|stability|reliability|major release)",
    re.IGNORECASE,
)
_BENCHMARK_IMPACT_MARKERS = re.compile(
    r"(?:生产部署|大规模采用|production deployment|production deployments|"
    r"deployed in production|enterprise adoption|lower [^.。]{0,40}cost|"
    r"cost [^.。]{0,40}reduc|lower [^.。]{0,40}latency|性能提升|"
    r"performance improvement|latency)",
    re.IGNORECASE,
)


def is_editorially_eligible(candidate: Candidate) -> bool:
    """Return whether a candidate is suitable for ordinary users or businesses.

    This is intentionally conservative and deterministic. The LLM still decides
    relative importance and wording among candidates that pass this gate.
    """

    raw = candidate.raw
    if raw.source_role == SourceRole.DISCOVERY and not raw.discovery_verified:
        return False

    text = " ".join((raw.title, raw.excerpt))
    if _SPONSORED_MARKERS.search(text):
        return False
    if _ACADEMIC_MARKERS.search(text) and not _ACADEMIC_IMPACT_EXCEPTIONS.search(text):
        return False
    if _ALWAYS_LOW_VALUE_TECHNICAL_MARKERS.search(text):
        return False
    if _BENCHMARK_MARKERS.search(text) and not _BENCHMARK_IMPACT_MARKERS.search(text):
        return False
    if _LOW_VALUE_TECHNICAL_MARKERS.search(text) and not _HIGH_IMPACT_TECHNICAL_MARKERS.search(
        text
    ):
        return False
    if _DEVELOPER_ECOSYSTEM_MARKERS.search(text) and not (
        _HIGH_IMPACT_TECHNICAL_MARKERS.search(text) and _CONCRETE_IMPACT_MARKERS.search(text)
    ):
        return False
    if (
        raw.source_id.startswith("github-")
        and _GITHUB_RELEASE_MARKERS.search(text)
        and not _HIGH_IMPACT_TECHNICAL_MARKERS.search(text)
    ):
        return False
    return not (_CORPORATE_ONLY_MARKERS.search(text) and not _CONCRETE_IMPACT_MARKERS.search(text))


__all__ = ["is_editorially_eligible"]
