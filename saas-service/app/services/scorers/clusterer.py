"""Clustering utilities for signal processing."""

from typing import List, Dict, Any, Tuple
import json
import logging
import re

from app.services.extractors.llm_client import make_client, call_llm

log = logging.getLogger(__name__)


CLUSTER_PROMPT = """下面是 N 个用户痛点的简短描述，每条带编号。请把语义相关的合并成 cluster，并给每个 cluster 一个简短主题（10 词内 英文）。

判定规则：
- 同一主题/产品方向的合并（如 "AI agent 治理" / "PDF 工具" / "账单/计费"）
- 同一用户场景的合并（如都是 lawyer 的、都是 etsy seller 的）
- 不确定时倾向于分开（cluster 太多比错合并好）

【输出 schema, 严格 JSON】
{
  "clusters": [
    {"summary": "短主题英文", "members": [1, 3, 7]},
    {"summary": "...", "members": [2]},
    ...
  ]
}
- members 是输入编号列表（从 1 开始），每个编号只能出现在一个 cluster
- 单条也算一个 cluster（members 只有 1 个元素）

【输入痛点列表】
{tasks_list}

输出 JSON:"""


CLUSTER_SUMMARY_PROMPT = """下面是 {n} 个用户痛点 cluster，每个有编号、top 3 个 implied_task。
请为每个 cluster 生成一个 10 词内的英文主题摘要，精确概括核心痛点。

【输出 schema, 严格 JSON】
{{"summaries": ["cluster 0 的摘要", "cluster 1 的摘要", ...]}}

【输入 clusters】
{clusters_text}

输出 JSON:"""


def _cluster_signals_tfidf(tasks: List[str], distance_threshold: float = 0.95) -> List[int]:
    """TF-IDF + AgglomerativeClustering（备选方案，对短句语义较弱）
    返回 labels 列表
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.cluster import AgglomerativeClustering
    import numpy as np

    if len(tasks) < 2:
        return [0] * len(tasks)

    tasks_clean = [t.strip() or "empty" for t in tasks]

    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 5),
        max_features=3000,
        lowercase=True,
        min_df=1,
        sublinear_tf=True,
    )
    X = vectorizer.fit_transform(tasks_clean)

    arr = X.toarray()
    row_sums = arr.sum(axis=1)
    nonzero_idx = np.where(row_sums > 0)[0]
    zero_idx = np.where(row_sums == 0)[0]

    labels = [-1] * len(tasks)

    if len(nonzero_idx) >= 2:
        clustering = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=distance_threshold,
            metric="cosine",
            linkage="average",
        )
        sub_labels = clustering.fit_predict(arr[nonzero_idx])
        for j, i in enumerate(nonzero_idx):
            labels[i] = int(sub_labels[j])
    elif len(nonzero_idx) == 1:
        labels[nonzero_idx[0]] = 0

    next_label = (max(labels) + 1) if any(l >= 0 for l in labels) else 0
    for i in zero_idx:
        labels[i] = next_label
        next_label += 1

    return labels


def _cluster_signals_llm(tasks: List[str], cfg: dict) -> List[int]:
    """用 LLM 一次性聚类 N 个 implied_task。
    成本 ~¥0.02 / 100 tasks，比 TF-IDF 语义更准。
    返回 labels 列表（每个 task 对应一个 cluster_id）。
    """
    if len(tasks) < 2:
        return [0] * len(tasks)

    try:
        provider_type, client, p_cfg = make_client(cfg)
    except RuntimeError as e:
        log.warning(f"LLM 聚类失败 ({e})，回退到 TF-IDF")
        return _cluster_signals_tfidf(tasks)

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))
    prompt = CLUSTER_PROMPT.replace("{tasks_list}", numbered)

    max_tokens = max(4000, len(tasks) * 80)

    try:
        content, in_tok, out_tok = call_llm(
            provider_type, client, p_cfg, prompt, max_tokens,
        )
    except Exception as e:
        log.warning(f"LLM 聚类调用失败 ({e})，回退到 TF-IDF")
        return _cluster_signals_tfidf(tasks)

    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        log.warning("LLM 聚类返回非 JSON，回退到 TF-IDF")
        return _cluster_signals_tfidf(tasks)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        cleaned = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
        m2 = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m2:
            return _cluster_signals_tfidf(tasks)
        try:
            data = json.loads(m2.group(0))
        except json.JSONDecodeError:
            return _cluster_signals_tfidf(tasks)

    clusters = data.get("clusters", [])
    labels = [-1] * len(tasks)
    summaries: List[str] = []
    for cid, c in enumerate(clusters):
        summaries.append(c.get("summary", f"cluster_{cid}"))
        for member in c.get("members", []):
            try:
                idx = int(member) - 1
                if 0 <= idx < len(tasks):
                    labels[idx] = cid
            except (ValueError, TypeError):
                continue

    next_id = max(labels) + 1 if labels else 0
    for i, lab in enumerate(labels):
        if lab == -1:
            labels[i] = next_id
            summaries.append(f"unclustered_{i}")
            next_id += 1

    log.info(f"LLM 聚类: {len(tasks)} 条 → {len(set(labels))} cluster")
    return labels


def cluster_signals_tfidf(signals: List[dict]) -> List[dict]:
    """TF-IDF 聚信号列表，返回 cluster dict 列表。"""
    if not signals:
        return []
    tasks = [sig.get("implied_task", "") or sig.get("raw_quote", "")[:50] for sig in signals]
    labels = _cluster_signals_tfidf(tasks)
    return _build_cluster_dicts(signals, labels)


def cluster_signals_llm(signals: List[dict], cfg: dict = None) -> List[dict]:
    """LLM 聚信号列表，返回 cluster dict 列表。
    每个 cluster 包含：summary, members, signal_count, avg_pain_intensity, pay_signal_ratio, ai_fit_ratio
    """
    if not signals:
        return []

    if cfg is None:
        cfg = {"llm": {"provider": "openai", "providers": {}}}

    tasks = [sig.get("implied_task", "") or sig.get("raw_quote", "")[:50] for sig in signals]

    try:
        labels = _cluster_signals_llm(tasks, cfg)
    except Exception as e:
        log.warning(f"LLM 聚类失败 ({e})，回退到 TF-IDF")
        labels = _cluster_signals_tfidf(tasks)

    return _build_cluster_dicts(signals, labels)


def _build_cluster_dicts(signals: List[dict], labels: List[int]) -> List[dict]:
    """按 labels 分组构建 cluster dict 列表。"""
    clusters_dict: Dict[int, list] = {}
    for i, label in enumerate(labels):
        clusters_dict.setdefault(label, []).append(signals[i])

    clusters = []
    for label, members in clusters_dict.items():
        pain_intensities = [m.get("pain_intensity", 0) or 0 for m in members]
        pay_signals = [m for m in members if m.get("monetization_signal") == "yes"]
        ai_solvable = [m for m in members if m.get("ai_solvable") in ("yes", "partial")]

        avg_pain = sum(pain_intensities) / len(pain_intensities) if pain_intensities else 0
        pay_ratio = len(pay_signals) / len(members) if members else 0
        ai_ratio = len(ai_solvable) / len(members) if members else 0

        summary = members[0].get("pain_category") or members[0].get("implied_task", "Unknown")[:50]

        clusters.append({
            "summary": summary,
            "members": [m.get("raw_quote", "")[:100] for m in members],
            "signal_count": len(members),
            "avg_pain_intensity": round(avg_pain, 2),
            "pay_signal_ratio": round(pay_ratio, 4),
            "ai_fit_ratio": round(ai_ratio, 4),
        })

    return clusters
