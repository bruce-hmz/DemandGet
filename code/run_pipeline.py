"""
Web 出海需求发现 Agent - 主入口

用法:
    python run_pipeline.py --dry-run         # 不调 LLM，只验证通路
    python run_pipeline.py                   # 完整跑一轮
    python run_pipeline.py --channel reddit  # 只跑某个通道
    python run_pipeline.py --report          # 只重新生成报告

设计原则: 见 ../SOP.md 第 0 节
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("chuhai")


# ---------- 数据结构 ----------

@dataclass
class RawDoc:
    """从某个渠道抓到的一段原文，待提取"""
    source_channel: str
    source_url: str
    text: str
    author_role_hint: Optional[str] = None
    fetched_at: str = ""


@dataclass
class Signal:
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


# ---------- 数据库 ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_channel TEXT NOT NULL,
  source_url TEXT NOT NULL,
  raw_quote TEXT NOT NULL,
  user_role TEXT,
  pain_category TEXT,
  pain_intensity INTEGER,
  implied_task TEXT,
  ai_solvable TEXT,
  monetization_signal TEXT,
  fetched_at TEXT NOT NULL,
  cluster_id INTEGER,
  UNIQUE(source_url, raw_quote)
);

CREATE INDEX IF NOT EXISTS idx_signals_channel ON signals(source_channel);
CREATE INDEX IF NOT EXISTS idx_signals_fetched ON signals(fetched_at);

CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  ended_at TEXT,
  channel TEXT,
  docs_fetched INTEGER,
  signals_extracted INTEGER,
  llm_tokens INTEGER,
  cost_usd REAL,
  status TEXT,
  notes TEXT
);
"""


def db_connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def insert_signal(conn: sqlite3.Connection, s: Signal) -> bool:
    try:
        conn.execute(
            """INSERT OR IGNORE INTO signals
            (source_channel, source_url, raw_quote, user_role, pain_category,
             pain_intensity, implied_task, ai_solvable, monetization_signal, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s.source_channel, s.source_url, s.raw_quote, s.user_role,
             s.pain_category, s.pain_intensity, s.implied_task,
             s.ai_solvable, s.monetization_signal, s.fetched_at),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# ---------- 通道 A: Reddit (公开 JSON 端点，无需 API key) ----------

REDDIT_BASE = "https://www.reddit.com"
REDDIT_HEADERS = {
    # 必须设 UA，否则直接 429。任意标识自己即可。
    "User-Agent": "web-chuhai-agent/0.1 (+demand-discovery research)",
    "Accept": "application/json",
}


def _reddit_get(url: str, params: dict = None, max_retries: int = 3) -> Optional[dict]:
    """带退避的 GET。Reddit 无登录限速约 60 req/min，我们保守到 30。"""
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
            log.warning(f"  Reddit 请求异常: {e}")
            time.sleep(2)
    return None


def fetch_reddit(cfg: dict) -> List[RawDoc]:
    """
    用 reddit.com/.json 公开端点抓帖子和评论。
    不需要 API key、不需要登录、不需要 PRAW。
    失败时走 SOP 第 4.1 节回退树。
    """
    pain_pat = re.compile(
        "|".join(re.escape(k) for k in cfg["pain_keywords"]),
        re.IGNORECASE,
    )

    docs: List[RawDoc] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    time_filter = cfg["reddit"]["time_filter"]
    top_limit = cfg["reddit"]["posts_per_sub_top"]
    hot_limit = cfg["reddit"]["posts_per_sub_hot"]
    min_score = cfg["reddit"]["min_score"]
    min_comments = cfg["reddit"]["min_comments"]
    cmt_per_post = cfg["reddit"]["top_comments_per_post"]

    for v in cfg["verticals"]:
        for sub_name in v["subreddits"]:
            sub_count = 0
            try:
                # 主路径：top + hot 两个流，按 ID 去重
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
                    time.sleep(2)  # 礼貌限速

                # 对每个候选帖：先看标题+正文是否命中关键词，否则去抓评论
                for p in posts_to_process:
                    body = (p.get("title") or "") + "\n\n" + (p.get("selftext") or "")
                    permalink = p.get("permalink", "")
                    full_url = f"{REDDIT_BASE}{permalink}"

                    if pain_pat.search(body):
                        docs.append(RawDoc(
                            source_channel="reddit",
                            source_url=full_url,
                            text=body[:8000],
                            author_role_hint=v["name"],
                            fetched_at=fetched_at,
                        ))
                        sub_count += 1
                        continue

                    # 标题正文没命中，看看 top 评论里有没有
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
                            docs.append(RawDoc(
                                source_channel="reddit",
                                source_url=full_url,
                                text=(body + "\n\n[COMMENTS]\n" + cmt_blob)[:8000],
                                author_role_hint=v["name"],
                                fetched_at=fetched_at,
                            ))
                            sub_count += 1
                    except (IndexError, KeyError, TypeError):
                        continue

                log.info(f"  r/{sub_name}: 命中 {sub_count} 条")

            except Exception as e:
                log.warning(f"r/{sub_name} 抓取失败: {e} — 跳过")
                continue

    log.info(f"Reddit 通道共收集 {len(docs)} 条候选原文")
    return docs


# ---------- 通道 B: HackerNews (Algolia API, 完全免费无需 key) ----------

HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_ITEM = "https://hn.algolia.com/api/v1/items"

# 默认搜索词模板：垂直关键词 + 抱怨/愿望关键词
HN_QUERY_TEMPLATES = [
    'wish there was',
    'recommend tool',
    'what do you use for',
    'is there a tool',
    'tired of',
    'spending hours',
]


def fetch_hn(cfg: dict) -> List[RawDoc]:
    """
    HN Algolia API 完全免费、无需 key、无明显限速。
    我们用"垂直名 + 抱怨模板"做交叉搜索。
    """
    pain_pat = re.compile(
        "|".join(re.escape(k) for k in cfg["pain_keywords"]),
        re.IGNORECASE,
    )
    docs: List[RawDoc] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    hn_cfg = cfg.get("hn", {})
    hits_per_q = hn_cfg.get("hits_per_query", 30)
    days_back = hn_cfg.get("days_back", 180)
    min_points = hn_cfg.get("min_points", 3)
    fetch_comments = hn_cfg.get("fetch_comments", True)

    # 时间戳过滤（HN 用 unix timestamp）
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
                    r = requests.get(HN_SEARCH, params={
                        "query": query,
                        "tags": "story",
                        "hitsPerPage": hits_per_q,
                        "numericFilters": f"created_at_i>{cutoff},points>={min_points}",
                    }, timeout=15)
                    if r.status_code != 200:
                        log.warning(f"  HN {r.status_code} for query '{query}'")
                        continue
                    data = r.json()
                    for hit in data.get("hits", []):
                        title = hit.get("title") or ""
                        text = hit.get("story_text") or ""  # Ask HN 类有 story_text
                        body = f"{title}\n\n{text}"
                        if not pain_pat.search(body) and not pain_pat.search(query):
                            # 标题正文没命中但是 query 命中了（说明 LLM 仍可能找到信号），保留但截短
                            if not text:
                                continue
                        full_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"

                        # 可选：拉前几条 top comment
                        if fetch_comments and hit.get("num_comments", 0) >= 3:
                            try:
                                item_r = requests.get(
                                    f"{HN_ITEM}/{hit['objectID']}",
                                    timeout=15,
                                )
                                if item_r.status_code == 200:
                                    item = item_r.json()
                                    cmt_texts = []
                                    for c in (item.get("children") or [])[:8]:
                                        if c.get("text"):
                                            # 去 HTML 标签简版
                                            t = re.sub(r"<[^>]+>", "", c["text"])
                                            cmt_texts.append(t)
                                    if cmt_texts:
                                        body += "\n\n[COMMENTS]\n" + "\n---\n".join(cmt_texts)
                            except requests.RequestException:
                                pass

                        docs.append(RawDoc(
                            source_channel="hn",
                            source_url=full_url,
                            text=body[:8000],
                            author_role_hint=v["name"],
                            fetched_at=fetched_at,
                        ))
                        vert_count += 1
                    time.sleep(0.5)
                except requests.RequestException as e:
                    log.warning(f"  HN 请求异常: {e}")
                    continue

        log.info(f"  vertical={v['name']}: HN 命中 {vert_count} 条")

    log.info(f"HN 通道共收集 {len(docs)} 条候选原文")
    return docs


# ---------- 通道 C: Stack Exchange (免费 300 req/day，专业 Q&A) ----------

SE_BASE = "https://api.stackexchange.com/2.3"

# 常用 SE 站点：按垂直在 config.yaml 里指定
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


def fetch_stackexchange(cfg: dict) -> List[RawDoc]:
    """
    Stack Exchange API：每个 vertical 可配置 se_sites（如 workplace、law、photo）
    免费 300 req/day 无需 key。注册 app 拿 key 可升到 10k/day。
    """
    pain_pat = re.compile(
        "|".join(re.escape(k) for k in cfg["pain_keywords"]),
        re.IGNORECASE,
    )
    docs: List[RawDoc] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    se_cfg = cfg.get("stackexchange", {})
    pagesize = se_cfg.get("pagesize", 20)
    days_back = se_cfg.get("days_back", 365)
    min_score = se_cfg.get("min_score", 2)
    api_key = os.getenv("STACKEX_API_KEY", "")  # 可选，不填用默认配额

    cutoff = int(time.time()) - days_back * 86400

    for v in cfg["verticals"]:
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
                            # 必须命中关键词 OR 已经是 high-score 问题
                            combined = title + "\n\n" + body
                            if not pain_pat.search(combined) and (q.get("score") or 0) < 10:
                                continue
                            qid = q.get("question_id")
                            link = q.get("link") or f"https://{site}.stackexchange.com/q/{qid}"

                            docs.append(RawDoc(
                                source_channel="stackex",
                                source_url=link,
                                text=combined[:8000],
                                author_role_hint=v["name"],
                                fetched_at=fetched_at,
                            ))
                            vert_count += 1

                        # 监控配额
                        remaining = data.get("quota_remaining")
                        if remaining is not None and remaining < 20:
                            log.warning(f"  SE quota 即将耗尽 ({remaining}/天)")
                            break
                        time.sleep(0.3)
                    except requests.RequestException as e:
                        log.warning(f"  SE 请求异常: {e}")
                        continue

        log.info(f"  vertical={v['name']}: Stack Exchange 命中 {vert_count} 条")

    log.info(f"Stack Exchange 通道共收集 {len(docs)} 条候选原文")
    return docs


# ---------- 通道 D: DuckDuckGo Search (反查 Reddit/Quora, 完全免费无需 key) ----------

# DDG 反查模板：site:xxx + 抱怨关键词 + 垂直
DDG_SITE_TEMPLATES = [
    ('site:reddit.com', 'I wish there was'),
    ('site:reddit.com', 'why is there no'),
    ('site:reddit.com', 'tired of'),
    ('site:reddit.com', 'would pay for'),
    ('site:reddit.com', 'frustrated with'),
    ('site:quora.com', 'is there a tool'),
    ('site:quora.com', 'how do I automate'),
    ('site:indiehackers.com', 'looking for tool'),
]


def fetch_ddg(cfg: dict) -> List[RawDoc]:
    """
    DuckDuckGo 反查：通过 `ddgs` 库免费拿 Reddit/Quora 等被反爬站点的搜索结果 snippet。
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

    docs: List[RawDoc] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    ddg_cfg = cfg.get("ddg", {})
    results_per_query = ddg_cfg.get("results_per_query", 15)
    max_queries_per_vertical = ddg_cfg.get("max_queries_per_vertical", 12)
    region = ddg_cfg.get("region", "wt-wt")

    for v in cfg["verticals"]:
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

                        docs.append(RawDoc(
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

        log.info(f"  vertical={v['name']}: DDG 命中 {vert_count} 条 (用了 {queries_done} 次查询)")

    log.info(f"DuckDuckGo 通道共收集 {len(docs)} 条候选原文")
    return docs


# ---------- (已废弃) 通道 D-old: Brave Search ----------
# Brave Search 免费层需要信用卡激活，对中国用户不友好。已用 DDG 替代。
# 如果你已经有 Brave key 且想用，把下面的函数体启用即可：

def fetch_bravesearch(cfg: dict) -> List[RawDoc]:
    """已废弃: 用 fetch_ddg 替代。保留代码以备已有 Brave key 用户启用。"""
    log.warning("fetch_bravesearch 已废弃，请改用 fetch_ddg")
    return []


# ---------- LLM 抽取 ----------

EXTRACT_PROMPT = """你将收到一段网络原文（Reddit 帖、评论、HN 讨论、G2 差评等）。

任务：识别其中是否包含"用户抱怨"或"愿望表达"。如果有，输出 JSON；如果没有，输出 {"signals": []}。

【判定标准】抱怨 = 用户花时间/钱/精力做了某件他认为不应该这么麻烦的事，或在表达"我希望有 X 但没找到"。
不算抱怨 = 单纯吐槽天气/政治/价格、对工具的赞美、bug 报告（除非是缺失功能）。

【monetization_signal 判定 - 重要】
"yes" 触发的语义有 3 个层次，命中任一即标 yes：
  1. 明确付费表达: "I'd pay", "I pay X for", "$X/mo", "would pay good money", "happy to pay"
  2. 软性付费意愿: "I would use this", "looking for something like this", "would consider paying",
     "would buy", "wish there was a paid option", "happy to subscribe", "would sign up"
  3. 间接付费信号: 用户已经在用人工/外包/订阅替代方案 (说明问题真实存在且愿意花钱)，例如:
     "I currently pay a VA $500/mo to do this", "we hired someone to handle this",
     "we use [paid competitor] but it sucks"
"no" 仅当：纯吐槽 / 仅评论功能缺失 / 完全没提解决方案或使用意愿。

【输出 schema】
{
  "signals": [
    {
      "raw_quote": "原文片段，30-200 词，必须是原话不能改写",
      "user_role": "推测用户身份，如 'solo lawyer' / 'etsy seller' / unknown",
      "pain_category": "workflow_friction | manual_repetition | missing_tool | bad_existing_tool | communication_overhead | other",
      "pain_intensity": 1-5,
      "implied_task": "用一句话描述用户想完成的具体任务",
      "ai_solvable": "yes | partial | no",
      "monetization_signal": "yes | no  (按上面 3 层标准；命中任一即 yes)"
    }
  ]
}

【硬约束】
- raw_quote 必须能在原文中精确找到（允许少量空白差异），不能改写、不能翻译
- 一段原文如有多个抱怨，分开输出多条
- 不确定就标 unknown，绝不编造
- 直接输出 JSON，不要任何解释文字

【原文开始】
{text}
【原文结束】

输出 JSON:"""


def _make_llm_client(cfg: dict):
    """
    根据 config.llm.provider 选择 LLM 后端。
    返回 (provider_type, client, provider_config)
    - provider_type ∈ {"openai_compat", "anthropic"}
    - SenseNova / GLM / DeepSeek 都走 openai_compat（OpenAI SDK + 自定义 base_url）
    - Anthropic 走原生 SDK
    """
    provider = cfg["llm"]["provider"]
    p_cfg = cfg["llm"]["providers"].get(provider)
    if not p_cfg:
        raise RuntimeError(f"未配置 provider: {provider}（合法值: {list(cfg['llm']['providers'].keys())}）")

    api_key = os.getenv(p_cfg["api_key_env"], "").strip()
    if not api_key:
        raise RuntimeError(f"环境变量 {p_cfg['api_key_env']} 未设置")

    if provider == "anthropic":
        try:
            from anthropic import Anthropic
        except ImportError:
            raise RuntimeError("anthropic SDK 未装：pip install anthropic")
        return ("anthropic", Anthropic(api_key=api_key), p_cfg)
    else:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai SDK 未装：pip install openai")
        return ("openai_compat",
                OpenAI(api_key=api_key, base_url=p_cfg["base_url"]),
                p_cfg)


def _llm_call(provider_type: str, client, p_cfg: dict,
              prompt: str, max_tokens: int,
              max_retries: int = 5):
    """
    单次调用 + 指数退避（针对 SenseNova/GLM 严格 RPM 限制）。
    返回 (text_response, input_tokens, output_tokens)。
    遇到 429 / rate limit 时按 1s, 2s, 4s, 8s, 16s 退避。
    """
    last_err = None
    for attempt in range(max_retries):
        try:
            if provider_type == "anthropic":
                resp = client.messages.create(
                    model=p_cfg["model"],
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return (
                    resp.content[0].text.strip(),
                    resp.usage.input_tokens,
                    resp.usage.output_tokens,
                )
            else:
                # OpenAI 兼容：SenseNova / GLM / DeepSeek / 通义等
                resp = client.chat.completions.create(
                    model=p_cfg["model"],
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                    stream=False,
                )
                content = resp.choices[0].message.content or ""
                usage = resp.usage
                in_tok = (usage.prompt_tokens if usage else 0) or 0
                out_tok = (usage.completion_tokens if usage else 0) or 0
                return (content.strip(), in_tok, out_tok)
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_rate_limit = (
                "429" in err_str or
                "rate" in err_str or
                "rpm" in err_str or
                "quota" in err_str or
                "too many" in err_str
            )
            if not is_rate_limit:
                # 非限流错误直接抛
                raise
            if attempt == max_retries - 1:
                break
            # 指数退避: 1s, 2s, 4s, 8s, 16s + 抖动
            import random
            wait = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait)
    raise last_err


def extract_signals(docs: List[RawDoc], cfg: dict, dry_run: bool = False,
                    limit: int = 0, concurrency: int = 4) -> List[Signal]:
    """从原文抽取信号。支持 SenseNova / GLM / Anthropic 三家 provider。

    Args:
        limit: 0=不限；>0 时只处理前 N 条
        concurrency: LLM 并发数（reasoning 模型用并发能大幅加速）
    """
    if dry_run:
        log.info(f"[dry-run] 跳过 LLM 调用，假定 {len(docs)} 条原文每条产 0.3 个 signal")
        return []

    try:
        provider_type, client, p_cfg = _make_llm_client(cfg)
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

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    cost_lock = threading.Lock()
    total_cost = [0.0]  # 用列表绕过闭包不可变限制
    stop_flag = [False]

    def process_one(idx_doc):
        i, doc = idx_doc
        if stop_flag[0]:
            return []
        prompt = EXTRACT_PROMPT.replace("{text}", doc.text)
        try:
            content, in_tok, out_tok = _llm_call(
                provider_type, client, p_cfg, prompt, max_tokens,
            )
            cost = in_tok * in_rate + out_tok * out_rate
            with cost_lock:
                total_cost[0] += cost
                if total_cost[0] >= budget:
                    stop_flag[0] = True

            # 提取 JSON
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
                out_signals.append(Signal(
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

    signals: List[Signal] = []
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


# ---------- 周报生成 ----------

def _cluster_signals_tfidf(tasks: List[str], distance_threshold: float = 0.95):
    """
    TF-IDF + AgglomerativeClustering（备选方案，对短句语义较弱）
    返回 labels 列表
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.cluster import AgglomerativeClustering
    import numpy as np

    if len(tasks) < 2:
        return [0] * len(tasks)

    tasks_clean = [t.strip() or "empty" for t in tasks]

    # 中英混合短句：用 char ngram 让中文也有特征，不用 stop_words 避免零向量
    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 4),
        max_features=2000,
        lowercase=True,
        min_df=1,
    )
    X = vectorizer.fit_transform(tasks_clean)

    # 过滤零向量行：把它们各自当一个独立 cluster
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

    # 零向量行：每个独立成 cluster
    next_label = (max(labels) + 1) if any(l >= 0 for l in labels) else 0
    for i in zero_idx:
        labels[i] = next_label
        next_label += 1

    return labels


CLUSTER_PROMPT = """下面是 N 个用户痛点的简短描述，每条带编号。请把语义相关的合并成 cluster，并给每个 cluster 一个简短主题（10 词内 英文）。

判定规则：
- 同一主题/产品方向的合并（如 "AI agent 治理"、"PDF 工具"、"账单/计费"）
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


def _cluster_signals_llm(tasks: List[str], cfg: dict) -> List[int]:
    """
    用 LLM 一次性聚类 N 个 implied_task。
    成本 ~¥0.02 / 100 tasks，比 TF-IDF 语义更准。
    返回 labels 列表（每个 task 对应一个 cluster_id）。
    """
    if len(tasks) < 2:
        return [0] * len(tasks)

    try:
        provider_type, client, p_cfg = _make_llm_client(cfg)
    except RuntimeError as e:
        log.warning(f"LLM 聚类失败 ({e})，回退到 TF-IDF")
        return _cluster_signals_tfidf(tasks)

    # 构造 numbered list
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))
    prompt = CLUSTER_PROMPT.replace("{tasks_list}", numbered)

    # 一次大调用：max_tokens 需要给充足
    max_tokens = max(4000, len(tasks) * 80)

    try:
        content, in_tok, out_tok = _llm_call(
            provider_type, client, p_cfg, prompt, max_tokens,
        )
    except Exception as e:
        log.warning(f"LLM 聚类调用失败 ({e})，回退到 TF-IDF")
        return _cluster_signals_tfidf(tasks)

    # 解析 JSON
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        log.warning("LLM 聚类返回非 JSON，回退到 TF-IDF")
        return _cluster_signals_tfidf(tasks)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        cleaned = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return _cluster_signals_tfidf(tasks)
        try:
            data = json.loads(m.group(0))
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

    # 没分配到 cluster 的（LLM 漏了）单独成簇
    next_id = max(labels) + 1 if labels else 0
    for i, lab in enumerate(labels):
        if lab == -1:
            labels[i] = next_id
            summaries.append(f"unclustered_{i}")
            next_id += 1

    log.info(f"LLM 聚类: {len(tasks)} 条 → {len(set(labels))} cluster")
    return labels


def _ddg_search_count(query: str, max_results: int = 5) -> Tuple[int, List[dict]]:
    """用 DDG 搜索，返回 (估算结果数, 前N条结果列表)。"""
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
        # DDG 不直接返回总数，用结果数粗估：有结果 ≈ 有市场
        count = len(results)
        return count, results
    except Exception as e:
        log.debug(f"DDG 搜索失败 '{query[:40]}': {e}")
        return 0, []


def scan_competitors(cluster_summary: str, top_quotes: List[str]) -> dict:
    """自动竞品扫描：用 DDG 搜 "[痛点] tool/app/SaaS"，返回竞品信息。

    Returns:
        {
            "competitor_count": int,    # 估算竞品数量 (0-5+)
            "competitors": [str],       # 竞品名称列表
            "maturity": str,            # "none" / "early" / "growing" / "saturated"
            "search_query": str,        # 实际搜索词
        }
    """
    import time

    # 从 cluster summary 和 top quotes 提取核心关键词
    # 取 summary 的前 60 字符作为搜索基础
    base = cluster_summary[:60].strip()

    # 搜两轮：一轮找工具，一轮找替代品
    queries = [
        f'"{base}" tool OR app OR SaaS',
        f'"{base}" alternative OR competitor',
    ]

    all_results = []
    for q in queries:
        _, results = _ddg_search_count(q, max_results=5)
        all_results.extend(results)
        time.sleep(1)  # 礼貌限速

    # 从结果里提取竞品名（标题里含 tool/app/SaaS/alternative 的）
    competitor_names = []
    tool_keywords = {'tool', 'app', 'saas', 'software', 'platform', 'alternative',
                     'service', 'solution', 'app', 'extension', 'plugin'}
    for hit in all_results:
        title = (hit.get("title") or "").lower()
        body = (hit.get("body") or "").lower()
        combined = title + " " + body
        # 如果标题/摘要里有工具类关键词，算一个竞品
        if any(kw in combined for kw in tool_keywords):
            name = hit.get("title", "").split(" - ")[0].split(" | ")[0].strip()
            if name and len(name) < 60:
                competitor_names.append(name)

    # 去重
    competitor_names = list(dict.fromkeys(competitor_names))[:10]
    count = len(competitor_names)

    # 判断成熟度
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
            "search_volume": str,    # "high" / "medium" / "low"
            "evidence": str,         # 估算依据
        }
    """
    import time

    base = cluster_summary[:60].strip()

    # 用 DDG 搜索结果数作为市场关注度代理指标
    queries = [
        f'"{base}"',  # 精确匹配，看有多少人讨论这个话题
    ]

    result_count, results = _ddg_search_count(queries[0], max_results=10)
    time.sleep(1)

    # 从搜索结果里提取信号
    # 结果多 = 市场关注度高
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
        volume = "none"
        tam = 1

    # 有专业角色（lawyer, developer 等）加分，说明是专业市场
    pro_roles = {'lawyer', 'attorney', 'developer', 'engineer', 'doctor',
                 'accountant', 'designer', 'marketer', 'founder', 'ceo'}
    role_set = set(r.lower() for r in user_roles)
    if role_set & pro_roles:
        tam = min(10, tam + 2)  # 专业市场付费意愿更高

    evidence = f"DDG 搜索返回 {result_count} 条结果，用户角色: {', '.join(user_roles[:3])}"

    return {
        "tam_score": tam,
        "search_volume": volume,
        "evidence": evidence,
    }


def _verdict(score: float, competitor_count: int, tam_score: int,
             pay_ratio: float, avg_pain: float) -> str:
    """综合判断：GO / MAYBE / SKIP。

    逻辑：
    - GO: 高分 + 竞品少 + TAM 大 + 有付费信号
    - MAYBE: 有潜力但某个维度有短板
    - SKIP: 竞品饱和 或 TAM 太小 或 无付费意愿
    """
    # 一票否决
    if competitor_count >= 5:
        return "SKIP"  # 竞品饱和
    if tam_score <= 2:
        return "SKIP"  # 市场太小
    if pay_ratio == 0 and avg_pain < 4.0:
        return "SKIP"  # 无付费 + 痛感不够

    # GO 条件
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
    import math
    gap = 5 - min(5, competitor_count)
    return (
        2.0 * math.log(cnt + 1) * 2          # frequency
        + 2.0 * avg_pain                      # intensity
        + 4.0 * pay_ratio * 10                # pay signal (高权重)
        + 1.5 * ai_ratio * 5                  # ai fit
        + 2.0 * gap                           # gap
    )


_cfg_for_clustering: Optional[dict] = None  # gen_weekly_report 透传给 _cluster_signals_llm


def gen_weekly_report(conn: sqlite3.Connection, report_dir: str,
                       cluster_threshold: float = 0.95,
                       cfg_for_clustering: Optional[dict] = None):
    """v0.3 LLM 聚类版周报: LLM 一次性聚类 + 多维打分

    cluster_threshold: 仅 TF-IDF 回退路径用
    cfg_for_clustering: 传入主 cfg 让 _cluster_signals_llm 能访问 LLM provider
    """
    global _cfg_for_clustering
    _cfg_for_clustering = cfg_for_clustering
    Path(report_dir).mkdir(parents=True, exist_ok=True)

    cursor = conn.execute("""
        SELECT id, implied_task, pain_intensity, monetization_signal,
               ai_solvable, source_url, source_channel, user_role, raw_quote
        FROM signals
        WHERE implied_task IS NOT NULL AND implied_task != ''
        ORDER BY id
    """)
    rows = list(cursor.fetchall())

    today = datetime.now().strftime("%Y-%m-%d")
    report_path = Path(report_dir) / f"weekly-{today}.md"

    if len(rows) < 3:
        # 数据太少，直接列出
        lines = [f"# Web 出海周报 {today}\n",
                 f"> 仅 {len(rows)} 条信号，跳过聚类直接列出\n"]
        for r in rows:
            lines.append(f"- [{r[6]}] {r[1]} | pain={r[2]} pay={r[3]} ai={r[4]}")
            lines.append(f"  {r[5]}")
        report_path.write_text("\n".join(lines), encoding="utf-8")
        log.info(f"报告已写入: {report_path}")
        return

    # 聚类（默认用 LLM 一次性聚类，更语义化；可在 config 里换回 TF-IDF）
    cluster_method = (cluster_threshold > 0  # 兼容旧 signature
                      and "llm")  # 默认 llm
    tasks = [r[1] for r in rows]
    # 信号数过多时 LLM prompt 会超长且容易截断，直接走 TF-IDF 更稳
    LLM_CLUSTER_MAX = 200
    try:
        if len(tasks) > LLM_CLUSTER_MAX:
            log.info(f"信号数 {len(tasks)} > {LLM_CLUSTER_MAX}，直接使用 TF-IDF 聚类")
            labels = _cluster_signals_tfidf(tasks, distance_threshold=cluster_threshold)
        else:
            # 用 LLM 聚类（语义最强）
            labels = _cluster_signals_llm(tasks, _cfg_for_clustering or {})
    except Exception as e:
        log.warning(f"LLM 聚类失败 ({e})，回退到 TF-IDF")
        try:
            labels = _cluster_signals_tfidf(tasks, distance_threshold=cluster_threshold)
        except Exception as e2:
            log.warning(f"TF-IDF 也失败 ({e2})，回退到朴素分组")
            labels = [hash(t.lower()) for t in tasks]

    # 按 cluster 聚合
    from collections import defaultdict
    clusters = defaultdict(list)
    for row, label in zip(rows, labels):
        clusters[label].append(row)

    # 计算每个 cluster 的统计 + 初步分数
    cluster_summaries = []
    for label, sigs in clusters.items():
        cnt = len(sigs)
        avg_pain = sum(s[2] for s in sigs) / cnt
        pay_count = sum(1 for s in sigs if s[3] == "yes")
        ai_yes = sum(1 for s in sigs if s[4] == "yes")
        ai_partial = sum(1 for s in sigs if s[4] == "partial")
        pay_ratio = pay_count / cnt
        ai_ratio = (ai_yes + 0.5 * ai_partial) / cnt
        score = _score_cluster(cnt, avg_pain, pay_ratio, ai_ratio)

        # cluster summary: 用最短的 task 描述（通常最泛化）
        summary_task = min((s[1] for s in sigs), key=lambda t: len(t or ""))

        # 用户身份 top 3
        roles = defaultdict(int)
        for s in sigs:
            roles[s[7] or "unknown"] += 1
        top_roles = sorted(roles.items(), key=lambda x: -x[1])[:3]

        # 来源去重
        urls = list({s[5] for s in sigs})[:5]
        channels = list({s[6] for s in sigs})

        # 示例原文（取 pain_intensity 最高的）
        best_sig = max(sigs, key=lambda s: s[2])
        sample_quote = (best_sig[8] or "")[:200]

        cluster_summaries.append({
            "cnt": cnt, "avg_pain": avg_pain, "pay_count": pay_count, "pay_ratio": pay_ratio,
            "ai_yes": ai_yes, "ai_partial": ai_partial, "summary": summary_task,
            "roles": top_roles, "urls": urls, "channels": channels,
            "sample_quote": sample_quote, "score": score,
            # 占位，后面填
            "competitor_count": 0, "competitors": [], "maturity": "unknown",
            "tam_score": 0, "tam_volume": "unknown", "tam_evidence": "",
            "verdict": "MAYBE",
        })

    cluster_summaries.sort(key=lambda c: -c["score"])

    # ---------- 自动竞品扫描 + TAM 估算（只扫 top 20，控制 DDG 查询量）----------
    SCAN_TOP_N = 20
    top_clusters = cluster_summaries[:SCAN_TOP_N]
    log.info(f"开始竞品扫描 + TAM 估算 (top {len(top_clusters)} clusters)...")

    for i, c in enumerate(top_clusters):
        # 提取该 cluster 的 top quotes 用于搜索
        top_quotes = [s[8][:100] for s in list(clusters.values())[i][:3] if s[8]]

        try:
            comp = scan_competitors(c["summary"], top_quotes)
            c["competitor_count"] = comp["competitor_count"]
            c["competitors"] = comp["competitors"]
            c["maturity"] = comp["maturity"]
        except Exception as e:
            log.warning(f"竞品扫描失败 cluster#{i}: {e}")

        try:
            role_names = [r[0] for r in c["roles"]]
            tam = estimate_tam(c["summary"], role_names)
            c["tam_score"] = tam["tam_score"]
            c["tam_volume"] = tam["search_volume"]
            c["tam_evidence"] = tam["evidence"]
        except Exception as e:
            log.warning(f"TAM 估算失败 cluster#{i}: {e}")

        # 重新打分（加入真实竞品数据）
        c["score"] = _score_cluster(
            c["cnt"], c["avg_pain"], c["pay_ratio"],
            (c["ai_yes"] + 0.5 * c["ai_partial"]) / max(1, c["cnt"]),
            c["competitor_count"],
        )

        # 综合判断
        c["verdict"] = _verdict(
            c["score"], c["competitor_count"], c["tam_score"],
            c["pay_ratio"], c["avg_pain"],
        )

        if (i + 1) % 5 == 0:
            log.info(f"  扫描进度 {i+1}/{len(top_clusters)}")

    # 重新排序
    cluster_summaries.sort(key=lambda c: -c["score"])
    top_n = min(20, len(cluster_summaries))

    # 统计 verdict 分布
    verdict_counts = defaultdict(int)
    for c in top_clusters:
        verdict_counts[c["verdict"]] += 1

    # 写报告
    lines = [
        f"# Web 出海周报 {today}",
        "",
        f"> v0.4 自动验证版（聚类 + 竞品扫描 + TAM 估算 + GO/MAYBE/SKIP）",
        f"> 总信号: {len(rows)} | 聚类数: {len(clusters)} | 展示 Top {top_n}",
        f"> 结论分布: GO={verdict_counts.get('GO',0)} MAYBE={verdict_counts.get('MAYBE',0)} SKIP={verdict_counts.get('SKIP',0)}",
        "",
    ]

    for i, c in enumerate(cluster_summaries[:top_n], 1):
        verdict_icon = {"GO": "🟢", "MAYBE": "🟡", "SKIP": "🔴"}.get(c["verdict"], "⚪")
        lines.append(f"## #{i} {verdict_icon} [{c['verdict']}] {c['summary']}")
        lines.append("")
        lines.append(f"- **综合分: {c['score']:.1f}** | 聚类信号数: **{c['cnt']}** | "
                     f"平均痛感: **{c['avg_pain']:.1f}/5**")
        lines.append(f"- 付费信号: **{c['pay_count']}/{c['cnt']}** ({c['pay_ratio']*100:.0f}%) "
                     f"| AI 可解: yes={c['ai_yes']} partial={c['ai_partial']}")
        lines.append(f"- 竞品: **{c['competitor_count']} 个** ({c['maturity']})"
                     + (f" — {', '.join(c['competitors'][:3])}" if c["competitors"] else ""))
        lines.append(f"- TAM: **{c['tam_score']}/10** ({c['tam_volume']}) — {c['tam_evidence']}")
        lines.append(f"- 来源通道: {', '.join(c['channels'])}")
        roles_str = ", ".join(f"{r}({n})" for r, n in c["roles"])
        lines.append(f"- 用户身份 Top3: {roles_str}")
        if c["sample_quote"]:
            lines.append(f"- 示例原文: > {c['sample_quote']}")
        lines.append("- 原文样本:")
        for u in c["urls"]:
            lines.append(f"  - {u}")
        lines.append("")

    # 尾部统计
    lines.append("---")
    lines.append("## 统计")
    lines.append(f"- 入库信号总数: {len(rows)}")
    lines.append(f"- 聚类后唯一议题数: {len(clusters)}")
    pay_yes_total = sum(1 for r in rows if r[3] == "yes")
    lines.append(f"- 付费信号 yes 占比: {pay_yes_total}/{len(rows)} ({pay_yes_total/len(rows)*100:.0f}%)")
    ch_counts = defaultdict(int)
    for r in rows:
        ch_counts[r[6]] += 1
    lines.append(f"- 通道分布: {dict(ch_counts)}")
    lines.append(f"- 结论: GO={verdict_counts.get('GO',0)} | MAYBE={verdict_counts.get('MAYBE',0)} | SKIP={verdict_counts.get('SKIP',0)}")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"报告已写入: {report_path}")


# ---------- 主流程 ----------

RESCORE_PROMPT = """根据下面这段引文，判定其中是否包含付费意愿信号。只输出一个 JSON 对象。

【monetization_signal 判定 - 3 个层次，命中任一即 yes】
  1. 明确付费: "I'd pay", "I pay X for", "$X/mo", "would pay good money", "happy to pay"
  2. 软性付费: "I would use this", "looking for something like this", "would consider paying",
     "would buy", "wish there was a paid option", "happy to subscribe", "would sign up"
  3. 间接付费: 用户已在用人工/外包/订阅替代方案 (说明问题真实且愿意花钱)，例如:
     "I currently pay a VA $500/mo to do this", "we hired someone to handle this"
"no" 仅当: 纯吐槽 / 仅评论功能缺失 / 完全没提解决方案或使用意愿。

输出格式 (严格 JSON, 不要解释):
{"monetization_signal": "yes" | "no", "reason": "命中哪条标准的一句话说明"}

引文:
{quote}

JSON:"""


def rescore_monetization(conn: sqlite3.Connection, cfg: dict, concurrency: int = 8):
    """用最新 prompt 重新判定 DB 里所有信号的 monetization_signal。仅 raw_quote 不需要重抓原文。"""
    try:
        provider_type, client, p_cfg = _make_llm_client(cfg)
    except RuntimeError as e:
        log.error(str(e))
        return

    rows = list(conn.execute("SELECT id, raw_quote, monetization_signal FROM signals"))
    log.info(f"对 {len(rows)} 条现有信号重新判定 monetization_signal (provider={cfg['llm']['provider']}, concurrency={concurrency})")

    in_rate = p_cfg.get("price_in", 0.5) / 1_000_000
    out_rate = p_cfg.get("price_out", 1.0) / 1_000_000
    currency = p_cfg.get("currency", "CNY")
    max_tokens = cfg["llm"]["max_tokens_per_call"]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    cost_lock = threading.Lock()
    total_cost = [0.0]
    flipped = [0]   # yes <- no 变化数

    def process(row):
        rid, quote, old_signal = row
        if not quote:
            return rid, old_signal
        prompt = RESCORE_PROMPT.replace("{quote}", quote[:1500])
        try:
            content, in_tok, out_tok = _llm_call(provider_type, client, p_cfg, prompt, max_tokens)
            with cost_lock:
                total_cost[0] += in_tok * in_rate + out_tok * out_rate
            m = re.search(r"\{.*?\}", content, re.DOTALL)
            if not m:
                return rid, old_signal
            data = json.loads(m.group(0))
            new = data.get("monetization_signal", old_signal)
            if new not in ("yes", "no"):
                new = old_signal
            return rid, new
        except Exception as e:
            log.warning(f"  rescore[{rid}] 失败: {type(e).__name__}: {str(e)[:80]}")
            return rid, old_signal

    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(process, r): r for r in rows}
        for fut in as_completed(futures):
            rid, new_signal = fut.result()
            old = futures[fut][2]
            if new_signal != old:
                conn.execute("UPDATE signals SET monetization_signal=? WHERE id=?", (new_signal, rid))
                if old == "no" and new_signal == "yes":
                    flipped[0] += 1
            done += 1
            if done % 10 == 0:
                log.info(f"  rescore 进度 {done}/{len(rows)} | 累计 {total_cost[0]:.3f} {currency} | no→yes 翻转: {flipped[0]}")
    conn.commit()
    log.info(f"rescore 完成: {flipped[0]} 条 no→yes 翻转, 成本 {total_cost[0]:.3f} {currency}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="不调 LLM，只验证抓取")
    parser.add_argument("--channel", choices=["reddit", "hn", "stackex", "ddg", "brave"],
                        help="只跑指定通道（默认跑 hn + stackex + ddg；reddit 和 brave 已废弃）")
    parser.add_argument("--report", action="store_true", help="只重新生成报告")
    parser.add_argument("--rescore-monetization", action="store_true",
                        help="对现有 DB 里所有信号重新判定 monetization_signal（用最新 prompt）。约 ¥0.003/条")
    parser.add_argument("--limit", type=int, default=0,
                        help="LLM 抽取最多处理 N 条原文 (0=不限). 用于快速验证, reasoning 模型推荐 30-50.")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="LLM 并发数。SenseNova/GLM 一般可设 4-8 加速 (默认 4)")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.error(f"配置文件不存在: {cfg_path}（先 cp config.example.yaml config.yaml）")
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    db_path = (ROOT / cfg["output"]["db_path"]).resolve()
    conn = db_connect(str(db_path))
    log.info(f"DB: {db_path}")

    if args.report:
        gen_weekly_report(conn, str((ROOT / cfg["output"]["report_dir"]).resolve()), cluster_threshold=cfg.get("report", {}).get("cluster_threshold", 0.95), cfg_for_clustering=cfg)
        return

    if args.rescore_monetization:
        rescore_monetization(conn, cfg, concurrency=args.concurrency)
        gen_weekly_report(conn, str((ROOT / cfg["output"]["report_dir"]).resolve()), cluster_threshold=cfg.get("report", {}).get("cluster_threshold", 0.95), cfg_for_clustering=cfg)
        return

    started_at = datetime.now(timezone.utc).isoformat()
    all_docs: List[RawDoc] = []

    # Module 1: 信号采集（默认 hn + stackex + brave；reddit 直抓已被 reddit.com 全面 403，需手动加 --channel reddit 启用）
    if args.channel == "reddit":
        log.warning("Reddit 直抓在 2024+ 起被 reddit.com 反爬全面拦截。仅在你已有 OAuth 凭证时才启用。")
        log.info("=== 通道 A: Reddit (公开 JSON 端点) ===")
        all_docs.extend(fetch_reddit(cfg))

    if not args.channel or args.channel == "hn":
        log.info("=== 通道 B: HackerNews ===")
        all_docs.extend(fetch_hn(cfg))

    if not args.channel or args.channel == "stackex":
        log.info("=== 通道 C: Stack Exchange ===")
        all_docs.extend(fetch_stackexchange(cfg))

    if not args.channel or args.channel == "ddg":
        log.info("=== 通道 D: DuckDuckGo (反查 Reddit/Quora，免费无需 key) ===")
        all_docs.extend(fetch_ddg(cfg))

    if args.channel == "brave":
        log.warning("Brave Search 通道已废弃（免费层需信用卡）。请用 --channel ddg。")
        all_docs.extend(fetch_bravesearch(cfg))

    # TODO: 其他通道 (按 SOP 第 13 节优先级)
    # if not args.channel or args.channel == "g2":
    #     all_docs.extend(fetch_g2(cfg))
    # if not args.channel or args.channel == "upwork":
    #     all_docs.extend(fetch_upwork(cfg))

    log.info(f"=== 共采集 {len(all_docs)} 条原文 ===")

    # Module 2: 抽取
    signals = extract_signals(all_docs, cfg, dry_run=args.dry_run,
                              limit=args.limit, concurrency=args.concurrency)

    # 入库
    new_count = 0
    for s in signals:
        if insert_signal(conn, s):
            new_count += 1
    log.info(f"=== 入库 {new_count} 条新信号（{len(signals) - new_count} 条重复）===")

    # 日志
    conn.execute(
        """INSERT INTO run_log (started_at, ended_at, channel, docs_fetched,
                                signals_extracted, status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (started_at, datetime.now(timezone.utc).isoformat(),
         args.channel or "all", len(all_docs), new_count,
         "ok" if not args.dry_run else "dry-run", ""),
    )
    conn.commit()

    # 生成报告
    if not args.dry_run:
        gen_weekly_report(conn, str((ROOT / cfg["output"]["report_dir"]).resolve()), cluster_threshold=cfg.get("report", {}).get("cluster_threshold", 0.95), cfg_for_clustering=cfg)


if __name__ == "__main__":
    main()
