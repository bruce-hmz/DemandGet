import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional

import requests

from .base import FetchResult

log = logging.getLogger("chuhai.fetchers.reddit")

REDDIT_BASE = "https://www.reddit.com"

REDDIT_HEADERS = {
    "User-Agent": "web-chuhai-agent/0.1 (+demand-discovery research)",
    "Accept": "application/json",
}


def _reddit_get(url: str, params: dict = None, max_retries: int = 3) -> Optional[dict]:
    """规避反爬的 GET：Reddit 免登陆即可 ~60 req/min，但我们被反爬限制在 30 左右"""
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=REDDIT_HEADERS, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503):
                wait = 5 * (attempt + 1)
                log.warning(f"  Reddit {r.status_code}, sleep {wait}s")
                time.sleep(wait)
                continue
            log.warning(f"  Reddit {r.status_code} on {url}")
            return None
        except requests.RequestException as e:
            log.warning(f"  Reddit 网络异常: {e}")
            time.sleep(2)
    return None


def fetch_reddit(cfg: dict, vertical_name: str = "") -> List[FetchResult]:
    """
    从 reddit.com/.json 端点抓取帖子和评论。
    不需 API key、不需登陆、不需 PRAW。
    失败时见 SOP 4.1 备用方案。
    """
    pain_pat = re.compile(
        "|".join(re.escape(k) for k in cfg["pain_keywords"]),
        re.IGNORECASE,
    )

    docs: List[FetchResult] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    time_filter = cfg["reddit"]["time_filter"]
    top_limit = cfg["reddit"]["posts_per_sub_top"]
    hot_limit = cfg["reddit"]["posts_per_sub_hot"]
    min_score = cfg["reddit"]["min_score"]
    min_comments = cfg["reddit"]["min_comments"]
    cmt_per_post = cfg["reddit"]["top_comments_per_post"]

    for v in cfg["verticals"]:
        if vertical_name and v["name"] != vertical_name:
            continue
        for sub_name in v["subreddits"]:
            sub_count = 0
            try:
                # 抓 top + hot 两种流，去重 post ID
                streams = [
                    (f"{REDDIT_BASE}/r/{sub_name}/top.json",
                     {"t": time_filter, "limit": min(top_limit, 100)}),
                    (f"{REDDIT_BASE}/r/{sub_name}/hot.json",
                     {"limit": min(hot_limit, 100)}),
                ]
                seen_ids = set()
                posts_to_process = []

                for url, params in streams:
                    data = _reddit_get(url, params)
                    if not data:
                        continue
                    for child in data.get("data", {}).get("children", []):
                        p = child.get("data", {})
                        pid = p.get("id")
                        if not pid or pid in seen_ids:
                            continue
                        seen_ids.add(pid)
                        if (p.get("score") or 0) < min_score:
                            continue
                        if (p.get("num_comments") or 0) < min_comments:
                            continue
                        posts_to_process.append(p)
                    time.sleep(2)  # 休息一下

                # 对每篇精选帖子先检查标题+正文是否含痛点关键词，有则直接抓
                for p in posts_to_process:
                    body = (p.get("title") or "") + "\n\n" + (p.get("selftext") or "")
                    permalink = p.get("permalink", "")
                    full_url = f"{REDDIT_BASE}{permalink}"

                    if pain_pat.search(body):
                        docs.append(FetchResult(
                            source_channel="reddit",
                            source_url=full_url,
                            text=body[:8000],
                            author_role_hint=v["name"],
                            fetched_at=fetched_at,
                        ))
                        sub_count += 1
                        continue

                    # 若标题没有痛点，再去抓 top 评论
                    cmt_data = _reddit_get(
                        f"{REDDIT_BASE}{permalink}.json",
                        {"limit": cmt_per_post, "depth": 1, "sort": "top"},
                    )
                    time.sleep(2)
                    if not cmt_data or len(cmt_data) < 2:
                        continue
                    try:
                        cmts = cmt_data[1].get("data", {}).get("children", [])
                        cmt_texts = []
                        for c in cmts[:cmt_per_post]:
                            cd = c.get("data", {})
                            if cd.get("body"):
                                cmt_texts.append(cd["body"])
                        cmt_blob = "\n---\n".join(cmt_texts)
                        if pain_pat.search(cmt_blob):
                            docs.append(FetchResult(
                                source_channel="reddit",
                                source_url=full_url,
                                text=(body + "\n\n[COMMENTS]\n" + cmt_blob)[:8000],
                                author_role_hint=v["name"],
                                fetched_at=fetched_at,
                            ))
                            sub_count += 1
                    except (IndexError, KeyError, TypeError):
                        continue

                log.info(f"  r/{sub_name}: 抓到 {sub_count} 条")

            except Exception as e:
                log.warning(f"r/{sub_name} 抓取失败: {e}，继续")

    log.info(f"Reddit 通道共收集 {len(docs)} 条候选原文")
    return docs
