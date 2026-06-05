"""Signal extraction utilities."""

from dataclasses import dataclass
from typing import List, Optional, Tuple, Any
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.services.extractors.llm_client import make_client, call_llm

log = logging.getLogger(__name__)


@dataclass
class ExtractedSignal:
    """LLM 提取出的单条抱怨信号"""
    source_channel: str
    source_url: str
    raw_quote: str
    user_role: str
    pain_category: str
    pain_intensity: int
    implied_task: str
    ai_solvable: str
    monetization_signal: str
    fetched_at: str


EXTRACT_PROMPT = """你将收到一段网络原文（Reddit 帖、评论、HN 讨论、G2 差评等）。

任务：识别其中是否包含"用户抱怨 / 愿望表达"。如果有，输出 JSON；如果没有，输出 {"signals": []}。

【判定标准】抱怨 = 用户花时间/精力做了某件他认为不应该这么麻烦的事，或在表达"我希望有 X 但没找到"。
不算抱怨 = 单纯吐槽天气/政治/价格、对工具的赞美、bug 报告（除非是缺失功能）。

【monetization_signal 判定 - 重要】
"yes" 触发的语义有 3 个层次，命中任一即标 yes：
  1. 明确付费表达: "I'd pay", "I pay X for", "$X/mo", "would pay good money", "happy to pay"
  2. 软性付费意愿: "I would use this", "looking for something like this", "would consider paying",
     "would buy", "wish there was a paid option", "happy to subscribe", "would sign up"
  3. 间接付费信号: 用户已经在用人工/外包/订阅替代方案 (说明问题真实存在且愿意花钱)，例如
     "I currently pay a VA $500/mo to do this", "we hired someone to handle this",
     "we use [paid competitor] but it sucks"
"no" 仅当：纯吐槽 / 仅评论功能缺失 / 完全没提解决方案或使用意愿。

【输出 schema】
{
  "signals": [
    {
      "raw_quote": "原文片段 10-200 词，必须是原话不能改写",
      "user_role": "推测用户身份，如 'solo lawyer' / 'etsy seller' / unknown",
      "pain_category": "workflow_friction | manual_repetition | missing_tool | bad_existing_tool | communication_overhead | other",
      "pain_intensity": 1-5,
      "implied_task": "用一句话描述用户想完成的具体任务",
      "ai_solvable": "yes | partial | no",
      "monetization_signal": "yes | no  (按上述 3 层标准；命中任一即 yes)"
    }
  ]
}

【硬约束】
- raw_quote 必须能在原文中精确找到（允许少量空白差异），不能改写、不能翻译
- 一段原文如有多个抱怨，分开输出多条
- 不确定就填 unknown，绝不编造
- 直接输出 JSON，不要任何解释文本

【原文开始】
{text}
【原文结束】

输出 JSON:"""


def extract_signals(docs: list, cfg: dict, dry_run: bool = False,
                    limit: int = 0, concurrency: int = 4) -> List[ExtractedSignal]:
    """从原文抽取信号。支持 SenseNova / GLM / Anthropic 三家 provider。

    Args:
        limit: 0=不限制，>0 时只处理前 N 条
        concurrency: LLM 并发数
    """
    if dry_run:
        log.info(f"[dry-run] 跳过 LLM 调用，假设 {len(docs)} 条原文每条产 0.3 条 signal")
        return []

    try:
        provider_type, client, p_cfg = make_client(cfg)
    except RuntimeError as e:
        log.error(str(e))
        return []

    log.info(f"LLM provider: {cfg['llm']['provider']} | model: {p_cfg['model']} | concurrency: {concurrency}")

    if limit > 0 and len(docs) > limit:
        log.info(f"--limit={limit}: 从 {len(docs)} 条中只处理前 {limit} 条")
        docs = docs[:limit]

    budget = cfg["llm"].get("batch_budget", 30.0)
    currency = p_cfg.get("currency", "CNY")
    in_rate = p_cfg.get("price_in", 0.5) / 1_000_000
    out_rate = p_cfg.get("price_out", 1.0) / 1_000_000
    max_tokens = cfg["llm"]["max_tokens_per_call"]

    cost_lock = threading.Lock()
    total_cost = [0.0]
    stop_flag = [False]

    def process_one(idx_doc):
        i, doc = idx_doc
        if stop_flag[0]:
            return []
        prompt = EXTRACT_PROMPT.replace("{text}", doc.text)
        try:
            content, in_tok, out_tok = call_llm(
                provider_type, client, p_cfg, prompt, max_tokens,
            )
            cost = in_tok * in_rate + out_tok * out_rate
            with cost_lock:
                total_cost[0] += cost
                if total_cost[0] >= budget:
                    stop_flag[0] = True

            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if not json_match:
                return []
            try:
                data = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                cleaned = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
                m = re.search(r"\{.*\}", cleaned, re.DOTALL)
                if not m:
                    return []
                data = json.loads(m.group(0))

            out_signals = []
            for s in data.get("signals", []):
                quote = (s.get("raw_quote") or "").strip()
                if not quote or quote[:30].lower() not in doc.text.lower():
                    continue
                out_signals.append(ExtractedSignal(
                    source_channel=doc.source_channel,
                    source_url=doc.source_url,
                    raw_quote=quote,
                    user_role=s.get("user_role", "unknown"),
                    pain_category=s.get("pain_category", "other"),
                    pain_intensity=int(s.get("pain_intensity", 1) or 1),
                    implied_task=s.get("implied_task", ""),
                    ai_solvable=s.get("ai_solvable", "no"),
                    monetization_signal=s.get("monetization_signal", "no"),
                    fetched_at=doc.fetched_at,
                ))
            return out_signals
        except Exception as e:
            log.warning(f"  [doc {i+1}] 抽取失败: {type(e).__name__}: {str(e)[:80]}")
            return []

    signals: List[ExtractedSignal] = []
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(process_one, (i, d)): i for i, d in enumerate(docs)}
        for fut in as_completed(futures):
            done += 1
            try:
                signals.extend(fut.result())
            except Exception as e:
                log.warning(f"  worker 异常: {e}")
            if done % 10 == 0:
                log.info(f"  抽取进度 {done}/{len(docs)} | 累计成本 {total_cost[0]:.3f} {currency} | 信号 {len(signals)}")
            if stop_flag[0]:
                log.warning(f"达到预算上限 {budget} {currency}，停止抽取")
                break

    log.info(f"抽取完成: {len(signals)} 条信号，成本 {total_cost[0]:.3f} {currency}")
    return signals
