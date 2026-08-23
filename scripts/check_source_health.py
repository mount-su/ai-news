from __future__ import annotations

import asyncio
from pathlib import Path

from ai_news.config import load_source_config
from ai_news.source_health import check_source_health, format_health_result


def main() -> int:
    config = load_source_config(Path("sources/feeds.yaml"))
    results = asyncio.run(check_source_health(config))
    for result in results:
        print(format_health_result(result))
    return 0 if results and all(result.healthy for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
