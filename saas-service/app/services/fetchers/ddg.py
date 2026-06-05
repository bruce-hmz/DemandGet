import logging
import re
import time
from datetime import datetime, timezone
from typing import List

from .base import FetchResult

log = logging.getLogger("chuhai.fetchers.ddg")

DDG_SITE_TEMPLATES = [
    ('site:reddit.com', 'I wish there was'),
    ('site:reddit.com', 'why is there no'),
    ('site:reddit.com', 'tired of'),
    ('site:reddit.com', 'would pay for'),
    ('site:reddit.com', 'frustrated with'),
    ('site:quora.com', 'is there a tool'),
    ('site:quora.com', 'how do I automate'),
    ('site:indiehackers.com', 'looking for tool'),
    ('site:producthunt.com', 'I wish'),
    ('site:producthunt.com', 'needs'),
    ('site:trustpilot.com', 'terrible'),
    ('site:trustpilot.com', 'waste of money'),
    ('site:g2.com', 'disappointed'),
    ('site:g2.com', 'switched from'),
    ('site:twitter.com', 'frustrated with'),
    ('site:twitter.com', 'why is there no'),
]


def fetch_ddg(cfg: dict, vertical_name: str = "") -> List[FetchResult]:
    """
    DuckDuckGo 反查：通过 ddgs 库免费拿 Reddit/Quora 等被反爬站点的搜索结果 snippet。
    无需 API key，无需信用卡，无需注册。是 Brave Search 的免费替代品。
    限速约 ~50 query/min，我们加 1s sleep 保守跑。
    """
    try:
        from ddgs import DDGS  # 新版库名（duckduckgo_search 已改名）
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # 兼容老版
        except ImportError:
            log.error("ddgs 未装：pip install ddgs")
            return []

    docs: List[FetchResult] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    ddg_cfg = cfg.get("ddg", {})
    results_per_query = ddg_cfg.get("results_per_query", 15)
    max_queries_per_vertical = ddg_cfg.get("max_queries_per_vertical", 12)
    region = ddg_cfg.get("region", "wt-wt")

    for v in cfg["verticals"]:
        if vertical_name and v["name"] != vertical_name:
            continue
        seeds = [v["name"].replace("_", " ")] + v.get("keywords_extra", [])[:8]
        vert_count = 0
        queries_done = 0

        for site_op, pain_q in DDG_SITE_TEMPLATES:
            if queries_done >= max_queries_per_vertical:
                break
            for seed in seeds:
                if queries_done >= max_queries_per_vertical:
                    break
                query = f'{site_op} "{pain_q}" {seed}'
                try:
                    with DDGS() as ddgs:
                        results = list(ddgs.text(
                            query,
                            max_results=results_per_query,
                            region=region,
                            safesearch="off",
                        ))
                    queries_done += 1

                    for hit in results:
                        title = hit.get("title") or ""
                        body = hit.get("body") or ""
                        url = hit.get("href") or hit.get("url") or ""
                        if not url or not body:
                            continue
                        if len(body) < 50:  # 过短的 snippet 没价值
                            continue

                        # 推断 source_channel: reddit / quora / indiehackers
                        site_tag = site_op.split(":")[-1].split(".")[0]

                        docs.append(FetchResult(
                            source_channel=f"ddg:{site_tag}",
                            source_url=url,
                            text=f"{title}\n\n{body}"[:8000],
                            author_role_hint=v["name"],
                            fetched_at=fetched_at,
                        ))
                        vert_count += 1
                    time.sleep(1)  # 礼貌限速，避免触发 DDG 反爬
                except Exception as e:
                    log.warning(f"  DDG 查询 '{query[:50]}' 失败: {e}")
                    time.sleep(3)
                    continue

        log.info(f"  vertical={v['name']}: DDG 命中 {vert_count} 条（用了 {queries_done} 次查询）")

    log.info(f"DuckDuckGo 通道共收集 {len(docs)} 条候选原文")
    return docs
