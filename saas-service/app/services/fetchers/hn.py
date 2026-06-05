import re
import time
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests

# Constants from the original run_pipeline.py
HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}"
HN_QUERY_TEMPLATES = [
    "pain",
    "frustrated",
    "annoyed",
    "hate",
    "worst",
    "suck",
    "terrible",
    "awful",
    "horrible",
    "difficult",
    "hard",
    "complex",
    "confusing",
    "struggle",
    "issue",
    "problem",
    "limitation",
    "drawback",
    "complaint",
]


def fetch_hn(cfg: dict) -> List[Dict[str, Any]]:
    """
    Fetch from HN Algolia API.
    Returns a list of dicts with keys: channel, url, content, user_role, pain_category, pain_intensity, implied_task, ai_solvable, monetization_signal
    """
    pain_pat = re.compile(
        "|".join(re.escape(k) for k in cfg["pain_keywords"]),
        re.IGNORECASE,
    )
    docs: List[Dict[str, Any]] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    hn_cfg = cfg.get("hn", {})
    hits_per_q = hn_cfg.get("hits_per_query", 30)
    days_back = hn_cfg.get("days_back", 180)
    min_points = hn_cfg.get("min_points", 3)
    fetch_comments = hn_cfg.get("fetch_comments", True)

    # 时间戳过滤（HN 使用 unix timestamp）
    cutoff = int(time.time()) - days_back * 86400

    for v in cfg["verticals"]:
        # 垂直关键词：name + description 拆词 + 用户额外提供
        seeds = [v["name"].replace("_", " ")]
        seeds.extend(v.get("keywords_extra", []))

        vert_count = 0
        for seed in seeds:
            for tmpl in HN_QUERY_TEMPLATES:
                query = f"{seed} {tmpl}"
                try:
                    r = requests.get(
                        HN_SEARCH,
                        params={
                            "query": query,
                            "tags": "story",
                            "hitsPerPage": hits_per_q,
                            "numericFilters": f"created_at_i>{cutoff},points>={min_points}",
                        },
                        timeout=15,
                    )
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    for hit in data.get("hits", []):
                        title = hit.get("title") or ""
                        text = hit.get("story_text") or ""  # Ask HN 类有 story_text
                        body = f"{title}\n\n{text}"
                        if not pain_pat.search(body) and not pain_pat.search(query):
                            # 标题正文没命中但查询命中了（说明 LLM 仍可能找到信号），保留但截短
                            if not text:
                                continue
                        full_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"

                        # 可选：拉前几条 top comment
                        if fetch_comments and hit.get("num_comments", 0) >= 3:
                            try:
                                item_r = requests.get(
                                    f"{HN_ITEM}/{hit['objectID']}", timeout=15
                                )
                                if item_r.status_code == 200:
                                    item = item_r.json()
                                    comments = item.get("kids", [])[:3]
                                    comment_texts = []
                                    for cid in comments:
                                        c_r = requests.get(
                                            f"{HN_ITEM}/{cid}", timeout=10
                                        )
                                        if c_r.status_code == 200:
                                            c = c_r.json()
                                            comment_texts.append(
                                                c.get("text", "")[:200]
                                            )
                                    if comment_texts:
                                        body += "\n\n评论: " + " ".join(comment_texts)
                            except Exception:
                                pass

                        doc = {
                            "channel": "hn",
                            "url": full_url,
                            "content": body[:1000],  # limit content length
                            "user_role": None,
                            "pain_category": None,
                            "pain_intensity": 0,
                            "implied_task": None,
                            "ai_solvable": True,
                            "monetization_signal": False,
                        }
                        docs.append(doc)
                        vert_count += 1
                        if vert_count >= hn_cfg.get("max_per_vertical", 50):
                            break
                except Exception:
                    continue
            if vert_count >= hn_cfg.get("max_per_vertical", 50):
                break

    return docs
