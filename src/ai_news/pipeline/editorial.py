"""Deterministic editorial eligibility rules for the consumer-facing brief."""

from __future__ import annotations

import re

from ai_news.models import Candidate

_TECHNICAL_MARKERS = re.compile(
    r"(?:\barxiv\b|论文|学术|研究论文|科研|benchmark|基准测试|评测集|" 
    r"\bsdk\b|\bapi\b|接口协议|开发框架|framework|repository|仓库|" 
    r"\b(?:v|version)\s*\d+\.\d+\.\d+|小版本|补丁|修复连接器|超时配置|模型训练|" 
    r"alignment behavior|对齐行为)",
    re.IGNORECASE,
)
_CORPORATE_ONLY_MARKERS = re.compile(
    r"(?:融资|估值|募资|轮融资|高管|任命|离职|收购|投资|funding|valuation|executive|"
    r"appointed|resigns|acquisition|investment)",
    re.IGNORECASE,
)
_CONCRETE_IMPACT_MARKERS = re.compile(
    r"(?:产品|功能|套餐|订阅|价格|免费|free|tier|开放|上线|发布|推出|用户|客户|消费者|"
    r"企业|部署|采用|购物|平台|政策|监管|规则|合规|市场|可用|访问|服务)",
    re.IGNORECASE,
)


def is_editorially_eligible(candidate: Candidate) -> bool:
    """Return whether a candidate is suitable for ordinary users or businesses.

    This is intentionally conservative and deterministic. The LLM still decides
    relative importance and wording among candidates that pass this gate.
    """

    raw = candidate.raw
    text = " ".join((raw.title, raw.source_name, raw.excerpt, raw.category_hint.value))
    if _TECHNICAL_MARKERS.search(text):
        return False
    return not (
        _CORPORATE_ONLY_MARKERS.search(text)
        and not _CONCRETE_IMPACT_MARKERS.search(text)
    )


__all__ = ["is_editorially_eligible"]
