import logging
import os
import re
import time
from typing import List

import requests

from .base import FetchResult

log = logging.getLogger("chuhai.fetchers.stackexchange")

SE_BASE = "https://api.stackexchange.com/2.3"

# 默认 SE 站点：也可以在 config.yaml 里自定义
SE_DEFAULT_TEMPLATES = [
    "how do I automate",
    "is there a tool",
    "wish there was",
    "how to streamline",
    "tired of",
    "best way to",
]


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


def fetch_stackexchange(cfg: dict, vertical_name: str = "") -> List[FetchResult]:
    """
    Stack Exchange API 每个 vertical 可以指定 se_sites，如 workplace、law、photo。
    限 300 req/day 不需 key，注册 app 可加 key 到 10k/day。
    """
    pain_pat = re.compile(
        "|".join(re.escape(k) for k in cfg["pain_keywords"]),
        re.IGNORECASE,
    )
    docs: List[FetchResult] = []
    fetched_at = ""

    se_cfg = cfg.get("stackexchange", {})
    pagesize = se_cfg.get("pagesize", 20)
    days_back = se_cfg.get("days_back", 365)
    min_score = se_cfg.get("min_score", 2)
    api_key = os.getenv("STACKEX_API_KEY", "")  # 可选加大，不影响默认

    cutoff = int(time.time()) - days_back * 86400

    for v in cfg["verticals"]:
        if vertical_name and v["name"] != vertical_name:
            continue
        sites = v.get("se_sites", [])
        if not sites:
            continue

        seeds = [v["name"].replace("_", " ")] + v.get("keywords_extra", [])
        vert_count = 0

        for site in sites:
            for seed in seeds:
                for tmpl in SE_DEFAULT_TEMPLATES:
                    query = f"{seed} {tmpl}"
                    params = {
                        "order": "desc",
                        "sort": "votes",
                        "q": query,
                        "site": site,
                        "pagesize": pagesize,
                        "filter": "withbody",
                        "fromdate": cutoff,
                    }
                    if api_key:
                        params["key"] = api_key

                    try:
                        r = requests.get(f"{SE_BASE}/search/advanced",
                                         params=params, timeout=15)
                        if r.status_code != 200:
                            log.warning(f"  SE {r.status_code} on '{query}' @ {site}")
                            continue
                        data = r.json()
                        for q in data.get("items", []):
                            if (q.get("score") or 0) < min_score:
                                continue
                            title = q.get("title") or ""
                            body = _strip_html(q.get("body") or "")
                            # 包含痛点关键词 OR 已经是 high-score 帖
                            combined = title + "\n\n" + body
                            if not pain_pat.search(combined) and (q.get("score") or 0) < 10:
                                continue
                            qid = q.get("question_id")
                            link = q.get("link") or f"https://{site}.stackexchange.com/q/{qid}"

                            docs.append(FetchResult(
                                source_channel="stackex",
                                source_url=link,
                                text=combined[:8000],
                                author_role_hint=v["name"],
                                fetched_at=fetched_at,
                            ))
                            vert_count += 1

                        # 配额
                        remaining = data.get("quota_remaining")
                        if remaining is not None and remaining < 20:
                            log.warning(f"  SE quota 接近耗尽 ({remaining}/次)")
                            break
                        time.sleep(0.3)
                    except requests.RequestException as e:
                        log.warning(f"  SE 网络异常: {e}")
                        continue

        log.info(f"  vertical={v['name']}: Stack Exchange 命中 {vert_count} 条")

    log.info(f"Stack Exchange 通道共收集 {len(docs)} 条候选原文")
    return docs