"""Scoring and ranking utilities for signal processing."""

import logging
import math
import time
from typing import List, Tuple, Dict, Any

log = logging.getLogger(__name__)


def _ddg_search_count(query: str, max_results: int = 5) -> Tuple[int, List[dict]]:
    """搜 DDG 搜索，返回 (估算结果数, 前N条结果列表)"""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return 0, []

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results, safesearch="off"))
        count = len(results)
        return count, results
    except Exception as e:
        log.debug(f"DDG 搜索失败 '{query[:40]}': {e}")
        return 0, []


def scan_competitors(cluster_summary: str, top_quotes: List[str]) -> dict:
    """自动竞品扫描：用 DDG 搜 "[痛点] tool/app/SaaS"，返回竞品信息。

    Returns:
        {
            "competitor_count": int,
            "competitors": [str],
            "maturity": str,  # "none" / "early" / "growing" / "saturated"
            "search_query": str,
        }
    """
    base = cluster_summary[:60].strip()

    queries = [
        f'"{base}" tool OR app OR SaaS',
        f'"{base}" alternative OR competitor',
    ]

    all_results = []
    for q in queries:
        _, results = _ddg_search_count(q, max_results=5)
        all_results.extend(results)
        time.sleep(1)

    competitor_names = []
    tool_keywords = {'tool', 'app', 'saas', 'software', 'platform', 'alternative',
                     'service', 'solution', 'extension', 'plugin'}
    for hit in all_results:
        title = (hit.get("title") or "").lower()
        body = (hit.get("body") or "").lower()
        combined = title + " " + body
        if any(kw in combined for kw in tool_keywords):
            name = hit.get("title", "").split(" - ")[0].split(" | ")[0].strip()
            if name and len(name) < 60:
                competitor_names.append(name)

    competitor_names = list(dict.fromkeys(competitor_names))[:10]
    count = len(competitor_names)

    if count == 0:
        maturity = "none"
    elif count <= 2:
        maturity = "early"
    elif count <= 5:
        maturity = "growing"
    else:
        maturity = "saturated"

    return {
        "competitor_count": count,
        "competitors": competitor_names,
        "maturity": maturity,
        "search_query": queries[0],
    }


def estimate_tam(cluster_summary: str, user_roles: List[str]) -> dict:
    """TAM 估算：用 DDG 搜索结果数 + 用户角色推算市场规模。

    Returns:
        {
            "tam_score": int,        # 1-10 分 (10=巨大市场)
            "search_volume": str,    # "high" / "medium" / "low" / "unvalidated"
            "evidence": str,
        }
    """
    base = cluster_summary[:60].strip()

    queries = [
        f'"{base}"',
        base,
        " ".join(base.split()[:5]),
        f'site:reddit.com {base}',
    ]

    result_count = 0
    used_query = queries[0]
    for q in queries:
        result_count, _ = _ddg_search_count(q, max_results=10)
        if result_count > 0:
            used_query = q
            break
        time.sleep(1)

    if result_count >= 8:
        volume = "high"
        tam = 8
    elif result_count >= 4:
        volume = "medium"
        tam = 5
    elif result_count >= 1:
        volume = "low"
        tam = 3
    else:
        volume = "unvalidated"
        tam = 1

    pro_roles = {'lawyer', 'attorney', 'developer', 'engineer', 'doctor',
                 'accountant', 'designer', 'marketer', 'founder', 'ceo'}
    role_set = set(r.lower() for r in user_roles)
    if role_set & pro_roles:
        tam = min(10, tam + 2)

    evidence = f"DDG 搜索返回 {result_count} 条结果，用户角色: {', '.join(user_roles[:3])}"

    return {
        "tam_score": tam,
        "search_volume": volume,
        "evidence": evidence,
    }


def _verdict(score: float, competitor_count: int, tam_score: int,
             pay_ratio: float, avg_pain: float) -> str:
    """综合判断：GO / MAYBE / SKIP

    - GO: 高分 + 竞品少 + TAM 高 + 有付费信号
    - MAYBE: 有潜力但某个维度有短板
    - SKIP: 竞品饱和 或 TAM 太小 或 无付费意愿
    """
    if competitor_count >= 5:
        return "SKIP"
    if tam_score <= 2:
        return "SKIP"
    if pay_ratio == 0 and avg_pain < 4.0:
        return "SKIP"

    go_signals = 0
    if score >= 40:
        go_signals += 1
    if competitor_count <= 2:
        go_signals += 1
    if tam_score >= 5:
        go_signals += 1
    if pay_ratio >= 0.15:
        go_signals += 1
    if avg_pain >= 3.5:
        go_signals += 1

    if go_signals >= 4:
        return "GO"
    elif go_signals >= 2:
        return "MAYBE"
    else:
        return "SKIP"


def _score_cluster(cnt: int, avg_pain: float, pay_ratio: float,
                   ai_ratio: float, competitor_count: int = 0) -> float:
    """SOP §7 多维打分公式"""
    gap = 5 - min(5, competitor_count)
    return (
        2.0 * math.log(cnt + 1) * 2          # frequency
        + 2.0 * avg_pain                      # intensity
        + 4.0 * pay_ratio * 10                # pay signal (高权重)
        + 1.5 * ai_ratio * 5                  # ai fit
        + 2.0 * gap                           # gap
    )


def score_cluster(cluster: dict) -> dict:
    """对单个 cluster 进行打分"""
    sig_count = cluster.get("signal_count", len(cluster.get("members", [])))
    avg_pain = float(cluster.get("avg_pain_intensity", 0) or 0)
    pay_ratio = float(cluster.get("pay_signal_ratio", 0) or 0)
    ai_ratio = float(cluster.get("ai_fit_ratio", 0) or 0)
    competitor_count = int(cluster.get("competitor_count", 0) or 0)

    score = _score_cluster(sig_count, avg_pain, pay_ratio, ai_ratio, competitor_count)
    cluster["score"] = round(score, 4)
    return cluster


def verdict(score: float, competitor_count: int, tam_score: int,
            pay_ratio: float, avg_pain: float) -> str:
    """综合判断：GO / MAYBE / SKIP"""
    return _verdict(score, competitor_count, tam_score, pay_ratio, avg_pain)


def rank_clusters(clusters: list[dict]) -> list[dict]:
    """接收 cluster dict 列表，对每个补充 score、verdict，按 score 降序返回。"""
    ranked = []
    for c in clusters:
        if isinstance(c, dict):
            summary = c.get("summary", "")
            members = c.get("members", [])
            signal_count = c.get("signal_count", len(members))
        else:
            summary = getattr(c, "summary", "")
            members = getattr(c, "members", [])
            signal_count = getattr(c, "signal_count", len(members))

        ranked.append({
            "summary": summary,
            "members": members,
            "signal_count": signal_count,
            "score": float(c.get("score", 0) if isinstance(c, dict) else getattr(c, "score", 0) or 0),
            "verdict": c.get("verdict", "MAYBE") if isinstance(c, dict) else getattr(c, "verdict", "MAYBE"),
        })

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked
