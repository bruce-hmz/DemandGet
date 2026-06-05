"""
Web 鍑烘捣闇€姹傚彂鐜?Agent - 涓诲叆鍙?

鐢ㄦ硶:
    python run_pipeline.py --dry-run         # 涓嶈皟 LLM锛屽彧楠岃瘉閫氳矾
    python run_pipeline.py                   # 瀹屾暣璺戜竴杞?
    python run_pipeline.py --channel reddit  # 鍙窇鏌愪釜閫氶亾
    python run_pipeline.py --report          # 鍙噸鏂扮敓鎴愭姤鍛?

璁捐鍘熷垯: 瑙?../SOP.md 绗?0 鑺?
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


# ---------- 鏁版嵁缁撴瀯 ----------

@dataclass
class RawDoc:
    """浠庢煇涓笭閬撴姄鍒扮殑涓€娈靛師鏂囷紝寰呮彁鍙?""
    source_channel: str
    source_url: str
    text: str
    author_role_hint: Optional[str] = None
    fetched_at: str = ""


@dataclass
class Signal:
    """LLM 鎻愬彇鍑虹殑鍗曟潯鎶辨€ㄤ俊鍙?""
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


# ---------- 鏁版嵁搴?----------

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


# ---------- 閫氶亾 A: Reddit (鍏紑 JSON 绔偣锛屾棤闇€ API key) ----------

REDDIT_BASE = "https://www.reddit.com"
REDDIT_HEADERS = {
    # 蹇呴』璁?UA锛屽惁鍒欑洿鎺?429銆備换鎰忔爣璇嗚嚜宸卞嵆鍙€?
    "User-Agent": "web-chuhai-agent/0.1 (+demand-discovery research)",
    "Accept": "application/json",
}


def _reddit_get(url: str, params: dict = None, max_retries: int = 3) -> Optional[dict]:
    """甯﹂€€閬跨殑 GET銆俁eddit 鏃犵櫥褰曢檺閫熺害 60 req/min锛屾垜浠繚瀹堝埌 30銆?""
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
            log.warning(f"  Reddit 璇锋眰寮傚父: {e}")
            time.sleep(2)
    return None


def fetch_reddit(cfg: dict) -> List[RawDoc]:
    """
    鐢?reddit.com/.json 鍏紑绔偣鎶撳笘瀛愬拰璇勮銆?
    涓嶉渶瑕?API key銆佷笉闇€瑕佺櫥褰曘€佷笉闇€瑕?PRAW銆?
    澶辫触鏃惰蛋 SOP 绗?4.1 鑺傚洖閫€鏍戙€?
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
                # 涓昏矾寰勶細top + hot 涓や釜娴侊紝鎸?ID 鍘婚噸
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
                    time.sleep(2)  # 绀艰矊闄愰€?

                # 瀵规瘡涓€欓€夊笘锛氬厛鐪嬫爣棰?姝ｆ枃鏄惁鍛戒腑鍏抽敭璇嶏紝鍚﹀垯鍘绘姄璇勮
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

                    # 鏍囬姝ｆ枃娌″懡涓紝鐪嬬湅 top 璇勮閲屾湁娌℃湁
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

                log.info(f"  r/{sub_name}: 鍛戒腑 {sub_count} 鏉?)

            except Exception as e:
                log.warning(f"r/{sub_name} 鎶撳彇澶辫触: {e} 鈥?璺宠繃")
                continue

    log.info(f"Reddit 閫氶亾鍏辨敹闆?{len(docs)} 鏉″€欓€夊師鏂?)
    return docs


# ---------- 閫氶亾 B: HackerNews (Algolia API, 瀹屽叏鍏嶈垂鏃犻渶 key) ----------

HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_ITEM = "https://hn.algolia.com/api/v1/items"

# 榛樿鎼滅储璇嶆ā鏉匡細鍨傜洿鍏抽敭璇?+ 鎶辨€?鎰挎湜鍏抽敭璇?
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
    HN Algolia API 瀹屽叏鍏嶈垂銆佹棤闇€ key銆佹棤鏄庢樉闄愰€熴€?
    鎴戜滑鐢?鍨傜洿鍚?+ 鎶辨€ㄦā鏉?鍋氫氦鍙夋悳绱€?
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

    # 鏃堕棿鎴宠繃婊わ紙HN 鐢?unix timestamp锛?
    cutoff = int(time.time()) - days_back * 86400

    for v in cfg["verticals"]:
        # 鍨傜洿鍏抽敭璇嶏細name + description 鎷嗚瘝 + 鐢ㄦ埛棰濆鎻愪緵
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
                        text = hit.get("story_text") or ""  # Ask HN 绫绘湁 story_text
                        body = f"{title}\n\n{text}"
                        if not pain_pat.search(body) and not pain_pat.search(query):
                            # 鏍囬姝ｆ枃娌″懡涓絾鏄?query 鍛戒腑浜嗭紙璇存槑 LLM 浠嶅彲鑳芥壘鍒颁俊鍙凤級锛屼繚鐣欎絾鎴煭
                            if not text:
                                continue
                        full_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"

                        # 鍙€夛細鎷夊墠鍑犳潯 top comment
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
                                            # 鍘?HTML 鏍囩绠€鐗?
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
                    log.warning(f"  HN 璇锋眰寮傚父: {e}")
                    continue

        log.info(f"  vertical={v['name']}: HN 鍛戒腑 {vert_count} 鏉?)

    log.info(f"HN 閫氶亾鍏辨敹闆?{len(docs)} 鏉″€欓€夊師鏂?)
    return docs


# ---------- 閫氶亾 C: Stack Exchange (鍏嶈垂 300 req/day锛屼笓涓?Q&A) ----------

SE_BASE = "https://api.stackexchange.com/2.3"

# 甯哥敤 SE 绔欑偣锛氭寜鍨傜洿鍦?config.yaml 閲屾寚瀹?
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
    Stack Exchange API锛氭瘡涓?vertical 鍙厤缃?se_sites锛堝 workplace銆乴aw銆乸hoto锛?
    鍏嶈垂 300 req/day 鏃犻渶 key銆傛敞鍐?app 鎷?key 鍙崌鍒?10k/day銆?
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
    api_key = os.getenv("STACKEX_API_KEY", "")  # 鍙€夛紝涓嶅～鐢ㄩ粯璁ら厤棰?

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
                            # 蹇呴』鍛戒腑鍏抽敭璇?OR 宸茬粡鏄?high-score 闂
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

                        # 鐩戞帶閰嶉
                        remaining = data.get("quota_remaining")
                        if remaining is not None and remaining < 20:
                            log.warning(f"  SE quota 鍗冲皢鑰楀敖 ({remaining}/澶?")
                            break
                        time.sleep(0.3)
                    except requests.RequestException as e:
                        log.warning(f"  SE 璇锋眰寮傚父: {e}")
                        continue

        log.info(f"  vertical={v['name']}: Stack Exchange 鍛戒腑 {vert_count} 鏉?)

    log.info(f"Stack Exchange 閫氶亾鍏辨敹闆?{len(docs)} 鏉″€欓€夊師鏂?)
    return docs


# ---------- 閫氶亾 D: DuckDuckGo Search (鍙嶆煡 Reddit/Quora, 瀹屽叏鍏嶈垂鏃犻渶 key) ----------

# DDG 鍙嶆煡妯℃澘锛歴ite:xxx + 鎶辨€ㄥ叧閿瘝 + 鍨傜洿
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


def fetch_ddg(cfg: dict) -> List[RawDoc]:
    """
    DuckDuckGo 鍙嶆煡锛氶€氳繃 `ddgs` 搴撳厤璐规嬁 Reddit/Quora 绛夎鍙嶇埇绔欑偣鐨勬悳绱㈢粨鏋?snippet銆?
    鏃犻渶 API key锛屾棤闇€淇＄敤鍗★紝鏃犻渶娉ㄥ唽銆傛槸 Brave Search 鐨勫厤璐规浛浠ｅ搧銆?
    闄愰€熺害 ~50 query/min锛屾垜浠姞 1s sleep 淇濆畧璺戙€?
    """
    try:
        from ddgs import DDGS  # 鏂扮増搴撳悕锛坉uckduckgo_search 宸叉敼鍚嶏級
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # 鍏煎鑰佺増
        except ImportError:
            log.error("ddgs 鏈锛歱ip install ddgs")
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
                        if len(body) < 50:  # 杩囩煭鐨?snippet 娌′环鍊?
                            continue

                        # 鎺ㄦ柇 source_channel: reddit / quora / indiehackers
                        site_tag = site_op.split(":")[-1].split(".")[0]

                        docs.append(RawDoc(
                            source_channel=f"ddg:{site_tag}",
                            source_url=url,
                            text=f"{title}\n\n{body}"[:8000],
                            author_role_hint=v["name"],
                            fetched_at=fetched_at,
                        ))
                        vert_count += 1
                    time.sleep(1)  # 绀艰矊闄愰€燂紝閬垮厤瑙﹀彂 DDG 鍙嶇埇
                except Exception as e:
                    log.warning(f"  DDG 鏌ヨ '{query[:50]}' 澶辫触: {e}")
                    time.sleep(3)
                    continue

        log.info(f"  vertical={v['name']}: DDG 鍛戒腑 {vert_count} 鏉?(鐢ㄤ簡 {queries_done} 娆℃煡璇?")

    log.info(f"DuckDuckGo 閫氶亾鍏辨敹闆?{len(docs)} 鏉″€欓€夊師鏂?)
    return docs


# ---------- (宸插簾寮? 閫氶亾 D-old: Brave Search ----------
# Brave Search 鍏嶈垂灞傞渶瑕佷俊鐢ㄥ崱婵€娲伙紝瀵逛腑鍥界敤鎴蜂笉鍙嬪ソ銆傚凡鐢?DDG 鏇夸唬銆?
# 濡傛灉浣犲凡缁忔湁 Brave key 涓旀兂鐢紝鎶婁笅闈㈢殑鍑芥暟浣撳惎鐢ㄥ嵆鍙細

def fetch_bravesearch(cfg: dict) -> List[RawDoc]:
    """宸插簾寮? 鐢?fetch_ddg 鏇夸唬銆備繚鐣欎唬鐮佷互澶囧凡鏈?Brave key 鐢ㄦ埛鍚敤銆?""
    log.warning("fetch_bravesearch 宸插簾寮冿紝璇锋敼鐢?fetch_ddg")
    return []


# ---------- LLM 鎶藉彇 ----------

EXTRACT_PROMPT = """浣犲皢鏀跺埌涓€娈电綉缁滃師鏂囷紙Reddit 甯栥€佽瘎璁恒€丠N 璁ㄨ銆丟2 宸瘎绛夛級銆?

浠诲姟锛氳瘑鍒叾涓槸鍚﹀寘鍚?鐢ㄦ埛鎶辨€?鎴?鎰挎湜琛ㄨ揪"銆傚鏋滄湁锛岃緭鍑?JSON锛涘鏋滄病鏈夛紝杈撳嚭 {"signals": []}銆?

銆愬垽瀹氭爣鍑嗐€戞姳鎬?= 鐢ㄦ埛鑺辨椂闂?閽?绮惧姏鍋氫簡鏌愪欢浠栬涓轰笉搴旇杩欎箞楹荤儲鐨勪簨锛屾垨鍦ㄨ〃杈?鎴戝笇鏈涙湁 X 浣嗘病鎵惧埌"銆?
涓嶇畻鎶辨€?= 鍗曠函鍚愭Ы澶╂皵/鏀挎不/浠锋牸銆佸宸ュ叿鐨勮禐缇庛€乥ug 鎶ュ憡锛堥櫎闈炴槸缂哄け鍔熻兘锛夈€?

銆恗onetization_signal 鍒ゅ畾 - 閲嶈銆?
"yes" 瑙﹀彂鐨勮涔夋湁 3 涓眰娆★紝鍛戒腑浠讳竴鍗虫爣 yes锛?
  1. 鏄庣‘浠樿垂琛ㄨ揪: "I'd pay", "I pay X for", "$X/mo", "would pay good money", "happy to pay"
  2. 杞€т粯璐规剰鎰? "I would use this", "looking for something like this", "would consider paying",
     "would buy", "wish there was a paid option", "happy to subscribe", "would sign up"
  3. 闂存帴浠樿垂淇″彿: 鐢ㄦ埛宸茬粡鍦ㄧ敤浜哄伐/澶栧寘/璁㈤槄鏇夸唬鏂规 (璇存槑闂鐪熷疄瀛樺湪涓旀効鎰忚姳閽?锛屼緥濡?
     "I currently pay a VA $500/mo to do this", "we hired someone to handle this",
     "we use [paid competitor] but it sucks"
"no" 浠呭綋锛氱函鍚愭Ы / 浠呰瘎璁哄姛鑳界己澶?/ 瀹屽叏娌℃彁瑙ｅ喅鏂规鎴栦娇鐢ㄦ剰鎰裤€?

銆愯緭鍑?schema銆?
{
  "signals": [
    {
      "raw_quote": "鍘熸枃鐗囨锛?0-200 璇嶏紝蹇呴』鏄師璇濅笉鑳芥敼鍐?,
      "user_role": "鎺ㄦ祴鐢ㄦ埛韬唤锛屽 'solo lawyer' / 'etsy seller' / unknown",
      "pain_category": "workflow_friction | manual_repetition | missing_tool | bad_existing_tool | communication_overhead | other",
      "pain_intensity": 1-5,
      "implied_task": "鐢ㄤ竴鍙ヨ瘽鎻忚堪鐢ㄦ埛鎯冲畬鎴愮殑鍏蜂綋浠诲姟",
      "ai_solvable": "yes | partial | no",
      "monetization_signal": "yes | no  (鎸変笂闈?3 灞傛爣鍑嗭紱鍛戒腑浠讳竴鍗?yes)"
    }
  ]
}

銆愮‖绾︽潫銆?
- raw_quote 蹇呴』鑳藉湪鍘熸枃涓簿纭壘鍒帮紙鍏佽灏戦噺绌虹櫧宸紓锛夛紝涓嶈兘鏀瑰啓銆佷笉鑳界炕璇?
- 涓€娈靛師鏂囧鏈夊涓姳鎬紝鍒嗗紑杈撳嚭澶氭潯
- 涓嶇‘瀹氬氨鏍?unknown锛岀粷涓嶇紪閫?
- 鐩存帴杈撳嚭 JSON锛屼笉瑕佷换浣曡В閲婃枃瀛?

銆愬師鏂囧紑濮嬨€?
{text}
銆愬師鏂囩粨鏉熴€?

杈撳嚭 JSON:"""


def _make_llm_client(cfg: dict):
    """
    鏍规嵁 config.llm.provider 閫夋嫨 LLM 鍚庣銆?
    杩斿洖 (provider_type, client, provider_config)
    - provider_type 鈭?{"openai_compat", "anthropic"}
    - SenseNova / GLM / DeepSeek 閮借蛋 openai_compat锛圤penAI SDK + 鑷畾涔?base_url锛?
    - Anthropic 璧板師鐢?SDK
    """
    provider = cfg["llm"]["provider"]
    p_cfg = cfg["llm"]["providers"].get(provider)
    if not p_cfg:
        raise RuntimeError(f"鏈厤缃?provider: {provider}锛堝悎娉曞€? {list(cfg['llm']['providers'].keys())}锛?)

    api_key = os.getenv(p_cfg["api_key_env"], "").strip()
    if not api_key:
        raise RuntimeError(f"鐜鍙橀噺 {p_cfg['api_key_env']} 鏈缃?)

    if provider == "anthropic":
        try:
            from anthropic import Anthropic
        except ImportError:
            raise RuntimeError("anthropic SDK 鏈锛歱ip install anthropic")
        return ("anthropic", Anthropic(api_key=api_key), p_cfg)
    else:
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai SDK 鏈锛歱ip install openai")
        return ("openai_compat",
                OpenAI(api_key=api_key, base_url=p_cfg["base_url"]),
                p_cfg)


def _llm_call(provider_type: str, client, p_cfg: dict,
              prompt: str, max_tokens: int,
              max_retries: int = 5):
    """
    鍗曟璋冪敤 + 鎸囨暟閫€閬匡紙閽堝 SenseNova/GLM 涓ユ牸 RPM 闄愬埗锛夈€?
    杩斿洖 (text_response, input_tokens, output_tokens)銆?
    閬囧埌 429 / rate limit 鏃舵寜 1s, 2s, 4s, 8s, 16s 閫€閬裤€?
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
                # OpenAI 鍏煎锛歋enseNova / GLM / DeepSeek / 閫氫箟绛?
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
                # 闈為檺娴侀敊璇洿鎺ユ姏
                raise
            if attempt == max_retries - 1:
                break
            # 鎸囨暟閫€閬? 1s, 2s, 4s, 8s, 16s + 鎶栧姩
            import random
            wait = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait)
    raise last_err


def extract_signals(docs: List[RawDoc], cfg: dict, dry_run: bool = False,
                    limit: int = 0, concurrency: int = 4) -> List[Signal]:
    """浠庡師鏂囨娊鍙栦俊鍙枫€傛敮鎸?SenseNova / GLM / Anthropic 涓夊 provider銆?

    Args:
        limit: 0=涓嶉檺锛?0 鏃跺彧澶勭悊鍓?N 鏉?
        concurrency: LLM 骞跺彂鏁帮紙reasoning 妯″瀷鐢ㄥ苟鍙戣兘澶у箙鍔犻€燂級
    """
    if dry_run:
        log.info(f"[dry-run] 璺宠繃 LLM 璋冪敤锛屽亣瀹?{len(docs)} 鏉″師鏂囨瘡鏉′骇 0.3 涓?signal")
        return []

    try:
        provider_type, client, p_cfg = _make_llm_client(cfg)
    except RuntimeError as e:
        log.error(str(e))
        return []

    log.info(f"LLM provider: {cfg['llm']['provider']} | model: {p_cfg['model']} | concurrency: {concurrency}")

    if limit > 0 and len(docs) > limit:
        log.info(f"--limit={limit}: 浠?{len(docs)} 鏉′腑鍙鐞嗗墠 {limit} 鏉?)
        docs = docs[:limit]

    budget = cfg["llm"].get("batch_budget", 30.0)
    currency = p_cfg.get("currency", "CNY")
    in_rate = p_cfg.get("price_in", 0.5) / 1_000_000
    out_rate = p_cfg.get("price_out", 1.0) / 1_000_000
    max_tokens = cfg["llm"]["max_tokens_per_call"]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    cost_lock = threading.Lock()
    total_cost = [0.0]  # 鐢ㄥ垪琛ㄧ粫杩囬棴鍖呬笉鍙彉闄愬埗
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

            # 鎻愬彇 JSON
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
            log.warning(f"  [doc {i+1}] 鎶藉彇澶辫触: {type(e).__name__}: {str(e)[:80]}")
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
                log.warning(f"  worker 寮傚父: {e}")
            if done % 10 == 0:
                log.info(f"  鎶藉彇杩涘害 {done}/{len(docs)} | 绱鎴愭湰 {total_cost[0]:.3f} {currency} | 淇″彿 {len(signals)}")
            if stop_flag[0]:
                log.warning(f"杈惧埌棰勭畻涓婇檺 {budget} {currency}锛屽仠姝㈡娊鍙?)
                break

    log.info(f"鎶藉彇瀹屾垚: {len(signals)} 鏉′俊鍙凤紝鎴愭湰 {total_cost[0]:.3f} {currency}")
    return signals


# ---------- 鍛ㄦ姤鐢熸垚 ----------

def _cluster_signals_tfidf(tasks: List[str], distance_threshold: float = 0.95):
    """
    TF-IDF + AgglomerativeClustering锛堝閫夋柟妗堬紝瀵圭煭鍙ヨ涔夎緝寮憋級
    杩斿洖 labels 鍒楄〃
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.cluster import AgglomerativeClustering
    import numpy as np

    if len(tasks) < 2:
        return [0] * len(tasks)

    tasks_clean = [t.strip() or "empty" for t in tasks]

    # 涓嫳娣峰悎鐭彞锛氱敤 char ngram 璁╀腑鏂囦篃鏈夌壒寰侊紝涓嶇敤 stop_words 閬垮厤闆跺悜閲?
    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 5),
        max_features=3000,
        lowercase=True,
        min_df=1,
        sublinear_tf=True,
    )
    X = vectorizer.fit_transform(tasks_clean)

    # 杩囨护闆跺悜閲忚锛氭妸瀹冧滑鍚勮嚜褰撲竴涓嫭绔?cluster
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

    # 闆跺悜閲忚锛氭瘡涓嫭绔嬫垚 cluster
    next_label = (max(labels) + 1) if any(l >= 0 for l in labels) else 0
    for i in zero_idx:
        labels[i] = next_label
        next_label += 1

    return labels


CLUSTER_PROMPT = """涓嬮潰鏄?N 涓敤鎴风棝鐐圭殑绠€鐭弿杩帮紝姣忔潯甯︾紪鍙枫€傝鎶婅涔夌浉鍏崇殑鍚堝苟鎴?cluster锛屽苟缁欐瘡涓?cluster 涓€涓畝鐭富棰橈紙10 璇嶅唴 鑻辨枃锛夈€?

鍒ゅ畾瑙勫垯锛?
- 鍚屼竴涓婚/浜у搧鏂瑰悜鐨勫悎骞讹紙濡?"AI agent 娌荤悊"銆?PDF 宸ュ叿"銆?璐﹀崟/璁¤垂"锛?
- 鍚屼竴鐢ㄦ埛鍦烘櫙鐨勫悎骞讹紙濡傞兘鏄?lawyer 鐨勩€侀兘鏄?etsy seller 鐨勶級
- 涓嶇‘瀹氭椂鍊惧悜浜庡垎寮€锛坈luster 澶姣旈敊鍚堝苟濂斤級

銆愯緭鍑?schema, 涓ユ牸 JSON銆?
{
  "clusters": [
    {"summary": "鐭富棰樿嫳鏂?, "members": [1, 3, 7]},
    {"summary": "...", "members": [2]},
    ...
  ]
}
- members 鏄緭鍏ョ紪鍙峰垪琛紙浠?1 寮€濮嬶級锛屾瘡涓紪鍙峰彧鑳藉嚭鐜板湪涓€涓?cluster
- 鍗曟潯涔熺畻涓€涓?cluster锛坢embers 鍙湁 1 涓厓绱狅級

銆愯緭鍏ョ棝鐐瑰垪琛ㄣ€?
{tasks_list}

杈撳嚭 JSON:"""


def _cluster_signals_llm(tasks: List[str], cfg: dict) -> List[int]:
    """
    鐢?LLM 涓€娆℃€ц仛绫?N 涓?implied_task銆?
    鎴愭湰 ~楼0.02 / 100 tasks锛屾瘮 TF-IDF 璇箟鏇村噯銆?
    杩斿洖 labels 鍒楄〃锛堟瘡涓?task 瀵瑰簲涓€涓?cluster_id锛夈€?
    """
    if len(tasks) < 2:
        return [0] * len(tasks)

    try:
        provider_type, client, p_cfg = _make_llm_client(cfg)
    except RuntimeError as e:
        log.warning(f"LLM 鑱氱被澶辫触 ({e})锛屽洖閫€鍒?TF-IDF")
        return _cluster_signals_tfidf(tasks)

    # 鏋勯€?numbered list
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))
    prompt = CLUSTER_PROMPT.replace("{tasks_list}", numbered)

    # 涓€娆″ぇ璋冪敤锛歮ax_tokens 闇€瑕佺粰鍏呰冻
    max_tokens = max(4000, len(tasks) * 80)

    try:
        content, in_tok, out_tok = _llm_call(
            provider_type, client, p_cfg, prompt, max_tokens,
        )
    except Exception as e:
        log.warning(f"LLM 鑱氱被璋冪敤澶辫触 ({e})锛屽洖閫€鍒?TF-IDF")
        return _cluster_signals_tfidf(tasks)

    # 瑙ｆ瀽 JSON
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        log.warning("LLM 鑱氱被杩斿洖闈?JSON锛屽洖閫€鍒?TF-IDF")
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

    # 娌″垎閰嶅埌 cluster 鐨勶紙LLM 婕忎簡锛夊崟鐙垚绨?
    next_id = max(labels) + 1 if labels else 0
    for i, lab in enumerate(labels):
        if lab == -1:
            labels[i] = next_id
            summaries.append(f"unclustered_{i}")
            next_id += 1

    log.info(f"LLM 鑱氱被: {len(tasks)} 鏉?鈫?{len(set(labels))} cluster")
    return labels


CLUSTER_SUMMARY_PROMPT = """涓嬮潰鏄?{n} 涓敤鎴风棝鐐?cluster锛屾瘡涓湁缂栧彿鍜?top 3 鐨?implied_task銆?
璇蜂负姣忎釜 cluster 鐢熸垚涓€鍙?10 璇嶅唴鐨勮嫳鏂囦富棰樻憳瑕侊紝绮剧‘姒傛嫭鏍稿績鐥涚偣銆?

銆愯緭鍑?schema, 涓ユ牸 JSON銆?
{{"summaries": ["cluster 0 鐨勬憳瑕?, "cluster 1 鐨勬憳瑕?, ...]}}

銆愯緭鍏?clusters銆?
{clusters_text}

杈撳嚭 JSON:"""


def _generate_cluster_summaries(cluster_tasks: dict, cfg: dict) -> dict:
    """鐢?LLM 鎵归噺鐢熸垚鑱氱被鎽樿銆傝緭鍏?{cluster_id: [task1, task2, task3]}锛岃緭鍑?{cluster_id: summary}銆?""
    if not cluster_tasks:
        return {}

    clusters_text = ""
    for cid, tasks in sorted(cluster_tasks.items()):
        top3 = tasks[:3]
        tasks_str = " | ".join(top3)
        clusters_text += f"\ncluster {cid}: {tasks_str}"

    prompt = CLUSTER_SUMMARY_PROMPT.format(
        n=len(cluster_tasks),
        clusters_text=clusters_text,
    )

    try:
        provider_type, client, p_cfg = _make_llm_client(cfg)
        resp_text, _, _ = _llm_call(provider_type, client, p_cfg, prompt, cfg.get("llm", {}).get("max_tokens_per_call", 3500))
        # 鎻愬彇 JSON
        match = re.search(r'\{[\s\S]*"summaries"[\s\S]*\}', resp_text)
        if match:
            data = json.loads(match.group())
            result = {}
            for i, s in enumerate(data.get("summaries", [])):
                if i < len(cluster_tasks):
                    cid = sorted(cluster_tasks.keys())[i]
                    result[cid] = s.strip()[:80]
            return result
    except Exception as e:
        log.warning(f"LLM 鑱氱被鎽樿鐢熸垚澶辫触: {e}")
    return {}


VALIDATION_KIT_PROMPT = """You are a product validation expert. Below are {n} GO-level demand signal clusters.
Generate $50 ad test materials for each cluster.

For EACH cluster, output:
1. Landing page hero copy (3 variants: pain-driven, solution-driven, identity-driven)
2. Google Ads: 3 titles (max 30 chars each) + 2 descriptions (max 90 chars each)
3. Reddit Ad: 50-80 words, user voice, NOT salesy

Output ONLY valid JSON array, no markdown, no explanation:
[{{"cluster_id": 0, "landing_page": {{"variant_a": "...", "variant_b": "...", "variant_c": "..."}}, "google_ads": {{"titles": ["t1","t2","t3"], "desc": ["d1","d2"]}}, "reddit_ad": "..."}}]

Rules: All English. Be specific with numbers and scenarios. No "Introducing" or "Try now".

Input clusters:
{clusters_text}

JSON:"""


def _generate_validation_kit(go_clusters: list, cfg: dict) -> dict:
    """涓?GO 鍊欓€夌敓鎴愰獙璇佺墿鏂欙紙landing page + 骞垮憡鏂囨锛夈€傝繑鍥?{cluster_id: kit_dict}銆?""
    if not go_clusters:
        return {}

    def _sanitize(text: str) -> str:
        """鍘绘帀鍙兘骞叉壈 LLM 鐨勭壒娈婂瓧绗?""
        text = re.sub(r'[{}]', '', text)
        text = text.replace('"', "'").replace('\\', '/')
        return text[:200]

    clusters_text = ""
    for i, c in enumerate(go_clusters[:5]):  # 鏈€澶?5 涓紝閬垮厤 prompt 杩囬暱
        quotes_str = " | ".join(_sanitize(q) for q in c.get("top_quotes", [])[:2])
        role = _sanitize(c.get("top_role", "unknown"))
        summary = _sanitize(c['summary'])
        clusters_text += (
            f"\n--- cluster {i} ---"
            f"\nsummary: {summary}"
            f"\nuser_role: {role}"
            f"\npain: {c['avg_pain']:.1f}/5 | pay_ratio: {c['pay_ratio']:.0%}"
            f"\nquotes: {quotes_str}"
        )

    prompt = VALIDATION_KIT_PROMPT.format(n=min(len(go_clusters), 5), clusters_text=clusters_text)

    try:
        provider_type, client, p_cfg = _make_llm_client(cfg)
        resp_text, _, _ = _llm_call(provider_type, client, p_cfg, prompt, cfg.get("llm", {}).get("max_tokens_per_call", 3500))
        # 灏濊瘯瑙ｆ瀽 JSON 鏁扮粍鎴?{"kits": [...]} 鏍煎紡
        match = re.search(r'\[[\s\S]*\]', resp_text)
        if not match:
            match = re.search(r'\{[\s\S]*"kits"[\s\S]*\}', resp_text)
        if match:
            parsed = json.loads(match.group())
            kits_list = parsed if isinstance(parsed, list) else parsed.get("kits", [])
            result = {}
            for kit in kits_list:
                cid = kit.get("cluster_id", -1)
                if 0 <= cid < len(go_clusters):
                    result[cid] = kit
            return result
    except Exception as e:
        log.warning(f"楠岃瘉鐗╂枡鐢熸垚澶辫触: {e}")
        log.debug(f"楠岃瘉鐗╂枡 prompt 鍓?500 瀛? {prompt[:500]}")
    return {}


def _ddg_search_count(query: str, max_results: int = 5) -> Tuple[int, List[dict]]:
    """鐢?DDG 鎼滅储锛岃繑鍥?(浼扮畻缁撴灉鏁? 鍓峃鏉＄粨鏋滃垪琛?銆?""
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
        # DDG 涓嶇洿鎺ヨ繑鍥炴€绘暟锛岀敤缁撴灉鏁扮矖浼帮細鏈夌粨鏋?鈮?鏈夊競鍦?
        count = len(results)
        return count, results
    except Exception as e:
        log.debug(f"DDG 鎼滅储澶辫触 '{query[:40]}': {e}")
        return 0, []


def scan_competitors(cluster_summary: str, top_quotes: List[str]) -> dict:
    """鑷姩绔炲搧鎵弿锛氱敤 DDG 鎼?"[鐥涚偣] tool/app/SaaS"锛岃繑鍥炵珵鍝佷俊鎭€?

    Returns:
        {
            "competitor_count": int,    # 浼扮畻绔炲搧鏁伴噺 (0-5+)
            "competitors": [str],       # 绔炲搧鍚嶇О鍒楄〃
            "maturity": str,            # "none" / "early" / "growing" / "saturated"
            "search_query": str,        # 瀹為檯鎼滅储璇?
        }
    """
    import time

    # 浠?cluster summary 鍜?top quotes 鎻愬彇鏍稿績鍏抽敭璇?
    # 鍙?summary 鐨勫墠 60 瀛楃浣滀负鎼滅储鍩虹
    base = cluster_summary[:60].strip()

    # 鎼滀袱杞細涓€杞壘宸ュ叿锛屼竴杞壘鏇夸唬鍝?
    queries = [
        f'"{base}" tool OR app OR SaaS',
        f'"{base}" alternative OR competitor',
    ]

    all_results = []
    for q in queries:
        _, results = _ddg_search_count(q, max_results=5)
        all_results.extend(results)
        time.sleep(1)  # 绀艰矊闄愰€?

    # 浠庣粨鏋滈噷鎻愬彇绔炲搧鍚嶏紙鏍囬閲屽惈 tool/app/SaaS/alternative 鐨勶級
    competitor_names = []
    tool_keywords = {'tool', 'app', 'saas', 'software', 'platform', 'alternative',
                     'service', 'solution', 'app', 'extension', 'plugin'}
    for hit in all_results:
        title = (hit.get("title") or "").lower()
        body = (hit.get("body") or "").lower()
        combined = title + " " + body
        # 濡傛灉鏍囬/鎽樿閲屾湁宸ュ叿绫诲叧閿瘝锛岀畻涓€涓珵鍝?
        if any(kw in combined for kw in tool_keywords):
            name = hit.get("title", "").split(" - ")[0].split(" | ")[0].strip()
            if name and len(name) < 60:
                competitor_names.append(name)

    # 鍘婚噸
    competitor_names = list(dict.fromkeys(competitor_names))[:10]
    count = len(competitor_names)

    # 鍒ゆ柇鎴愮啛搴?
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
    """TAM 浼扮畻锛氱敤 DDG 鎼滅储缁撴灉鏁?+ 鐢ㄦ埛瑙掕壊鎺ㄧ畻甯傚満瑙勬ā銆?

    Returns:
        {
            "tam_score": int,        # 1-10 鍒?(10=宸ㄥぇ甯傚満)
            "search_volume": str,    # "high" / "medium" / "low" / "unvalidated"
            "evidence": str,         # 浼扮畻渚濇嵁
        }
    """
    import time

    base = cluster_summary[:60].strip()

    # 澶氳疆鎼滅储闄嶇骇锛氱簿纭尮閰?鈫?鍘诲紩鍙?鈫?缂╃煭 鈫?鍔?site:reddit.com
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

    # 缁撴灉澶?= 甯傚満鍏虫敞搴﹂珮
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

    # 鏈変笓涓氳鑹诧紙lawyer, developer 绛夛級鍔犲垎锛岃鏄庢槸涓撲笟甯傚満
    pro_roles = {'lawyer', 'attorney', 'developer', 'engineer', 'doctor',
                 'accountant', 'designer', 'marketer', 'founder', 'ceo'}
    role_set = set(r.lower() for r in user_roles)
    if role_set & pro_roles:
        tam = min(10, tam + 2)  # 涓撲笟甯傚満浠樿垂鎰忔効鏇撮珮

    evidence = f"DDG 鎼滅储杩斿洖 {result_count} 鏉＄粨鏋滐紝鐢ㄦ埛瑙掕壊: {', '.join(user_roles[:3])}"

    return {
        "tam_score": tam,
        "search_volume": volume,
        "evidence": evidence,
    }


def _verdict(score: float, competitor_count: int, tam_score: int,
             pay_ratio: float, avg_pain: float) -> str:
    """缁煎悎鍒ゆ柇锛欸O / MAYBE / SKIP銆?

    閫昏緫锛?
    - GO: 楂樺垎 + 绔炲搧灏?+ TAM 澶?+ 鏈変粯璐逛俊鍙?
    - MAYBE: 鏈夋綔鍔涗絾鏌愪釜缁村害鏈夌煭鏉?
    - SKIP: 绔炲搧楗卞拰 鎴?TAM 澶皬 鎴?鏃犱粯璐规剰鎰?
    """
    # 涓€绁ㄥ惁鍐?
    if competitor_count >= 5:
        return "SKIP"  # 绔炲搧楗卞拰
    if tam_score <= 2:
        return "SKIP"  # 甯傚満澶皬
    if pay_ratio == 0 and avg_pain < 4.0:
        return "SKIP"  # 鏃犱粯璐?+ 鐥涙劅涓嶅

    # GO 鏉′欢
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
    """SOP 搂7 澶氱淮鎵撳垎鍏紡"""
    import math
    gap = 5 - min(5, competitor_count)
    return (
        2.0 * math.log(cnt + 1) * 2          # frequency
        + 2.0 * avg_pain                      # intensity
        + 4.0 * pay_ratio * 10                # pay signal (楂樻潈閲?
        + 1.5 * ai_ratio * 5                  # ai fit
        + 2.0 * gap                           # gap
    )


_cfg_for_clustering: Optional[dict] = None  # gen_weekly_report 閫忎紶缁?_cluster_signals_llm


def gen_weekly_report(conn: sqlite3.Connection, report_dir: str,
                       cluster_threshold: float = 0.95,
                       cfg_for_clustering: Optional[dict] = None):
    """v0.3 LLM 鑱氱被鐗堝懆鎶? LLM 涓€娆℃€ц仛绫?+ 澶氱淮鎵撳垎

    cluster_threshold: 浠?TF-IDF 鍥為€€璺緞鐢?
    cfg_for_clustering: 浼犲叆涓?cfg 璁?_cluster_signals_llm 鑳借闂?LLM provider
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
        # 鏁版嵁澶皯锛岀洿鎺ュ垪鍑?
        lines = [f"# Web 鍑烘捣鍛ㄦ姤 {today}\n",
                 f"> 浠?{len(rows)} 鏉′俊鍙凤紝璺宠繃鑱氱被鐩存帴鍒楀嚭\n"]
        for r in rows:
            lines.append(f"- [{r[6]}] {r[1]} | pain={r[2]} pay={r[3]} ai={r[4]}")
            lines.append(f"  {r[5]}")
        report_path.write_text("\n".join(lines), encoding="utf-8")
        log.info(f"鎶ュ憡宸插啓鍏? {report_path}")
        return

    # 鑱氱被锛堥粯璁ょ敤 LLM 涓€娆℃€ц仛绫伙紝鏇磋涔夊寲锛涘彲鍦?config 閲屾崲鍥?TF-IDF锛?
    cluster_method = (cluster_threshold > 0  # 鍏煎鏃?signature
                      and "llm")  # 榛樿 llm
    tasks = [r[1] for r in rows]
    # 淇″彿鏁拌繃澶氭椂 LLM prompt 浼氳秴闀夸笖瀹规槗鎴柇锛岀洿鎺ヨ蛋 TF-IDF 鏇寸ǔ
    LLM_CLUSTER_MAX = 200
    try:
        if len(tasks) > LLM_CLUSTER_MAX:
            log.info(f"淇″彿鏁?{len(tasks)} > {LLM_CLUSTER_MAX}锛岀洿鎺ヤ娇鐢?TF-IDF 鑱氱被")
            labels = _cluster_signals_tfidf(tasks, distance_threshold=cluster_threshold)
        else:
            # 鐢?LLM 鑱氱被锛堣涔夋渶寮猴級
            labels = _cluster_signals_llm(tasks, _cfg_for_clustering or {})
    except Exception as e:
        log.warning(f"LLM 鑱氱被澶辫触 ({e})锛屽洖閫€鍒?TF-IDF")
        try:
            labels = _cluster_signals_tfidf(tasks, distance_threshold=cluster_threshold)
        except Exception as e2:
            log.warning(f"TF-IDF 涔熷け璐?({e2})锛屽洖閫€鍒版湸绱犲垎缁?)
            labels = [hash(t.lower()) for t in tasks]

    # 鎸?cluster 鑱氬悎
    from collections import defaultdict
    clusters = defaultdict(list)
    for row, label in zip(rows, labels):
        clusters[label].append(row)

    # LLM 鎵归噺鐢熸垚鑱氱被鎽樿锛堟瘮鍙栨渶鐭?task 鏇寸簿鍑嗭級
    llm_summaries = {}
    if cfg_for_clustering and len(clusters) <= 50:
        cluster_tasks_for_summary = {
            label: [s[1] for s in sigs if s[1]]
            for label, sigs in clusters.items()
        }
        llm_summaries = _generate_cluster_summaries(cluster_tasks_for_summary, cfg_for_clustering)
        if llm_summaries:
            log.info(f"LLM 涓?{len(llm_summaries)} 涓?cluster 鐢熸垚浜嗘憳瑕?)

    # 璁＄畻姣忎釜 cluster 鐨勭粺璁?+ 鍒濇鍒嗘暟
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

        # cluster summary: 浼樺厛鐢?LLM 鐢熸垚鐨勬憳瑕侊紝鍥為€€鍒版渶鐭?task
        summary_task = llm_summaries.get(label) or min((s[1] for s in sigs), key=lambda t: len(t or ""))

        # 鐢ㄦ埛韬唤 top 3
        roles = defaultdict(int)
        for s in sigs:
            roles[s[7] or "unknown"] += 1
        top_roles = sorted(roles.items(), key=lambda x: -x[1])[:3]

        # 鏉ユ簮鍘婚噸
        urls = list({s[5] for s in sigs})[:5]
        channels = list({s[6] for s in sigs})

        # 绀轰緥鍘熸枃锛堝彇 pain_intensity 鏈€楂樼殑锛?
        best_sig = max(sigs, key=lambda s: s[2])
        sample_quote = (best_sig[8] or "")[:200]

        cluster_summaries.append({
            "cnt": cnt, "avg_pain": avg_pain, "pay_count": pay_count, "pay_ratio": pay_ratio,
            "ai_yes": ai_yes, "ai_partial": ai_partial, "summary": summary_task,
            "roles": top_roles, "urls": urls, "channels": channels,
            "sample_quote": sample_quote, "score": score,
            # 鍗犱綅锛屽悗闈㈠～
            "competitor_count": 0, "competitors": [], "maturity": "unknown",
            "tam_score": 0, "tam_volume": "unknown", "tam_evidence": "",
            "verdict": "MAYBE",
        })

    cluster_summaries.sort(key=lambda c: -c["score"])

    # ---------- 鑷姩绔炲搧鎵弿 + TAM 浼扮畻锛堝彧鎵?top 20锛屾帶鍒?DDG 鏌ヨ閲忥級----------
    SCAN_TOP_N = 20
    top_clusters = cluster_summaries[:SCAN_TOP_N]
    log.info(f"寮€濮嬬珵鍝佹壂鎻?+ TAM 浼扮畻 (top {len(top_clusters)} clusters)...")

    for i, c in enumerate(top_clusters):
        # 鎻愬彇璇?cluster 鐨?top quotes 鐢ㄤ簬鎼滅储
        top_quotes = [s[8][:100] for s in list(clusters.values())[i][:3] if s[8]]

        try:
            comp = scan_competitors(c["summary"], top_quotes)
            c["competitor_count"] = comp["competitor_count"]
            c["competitors"] = comp["competitors"]
            c["maturity"] = comp["maturity"]
        except Exception as e:
            log.warning(f"绔炲搧鎵弿澶辫触 cluster#{i}: {e}")

        try:
            role_names = [r[0] for r in c["roles"]]
            tam = estimate_tam(c["summary"], role_names)
            c["tam_score"] = tam["tam_score"]
            c["tam_volume"] = tam["search_volume"]
            c["tam_evidence"] = tam["evidence"]
        except Exception as e:
            log.warning(f"TAM 浼扮畻澶辫触 cluster#{i}: {e}")

        # 閲嶆柊鎵撳垎锛堝姞鍏ョ湡瀹炵珵鍝佹暟鎹級
        c["score"] = _score_cluster(
            c["cnt"], c["avg_pain"], c["pay_ratio"],
            (c["ai_yes"] + 0.5 * c["ai_partial"]) / max(1, c["cnt"]),
            c["competitor_count"],
        )

        # 缁煎悎鍒ゆ柇
        c["verdict"] = _verdict(
            c["score"], c["competitor_count"], c["tam_score"],
            c["pay_ratio"], c["avg_pain"],
        )

        if (i + 1) % 5 == 0:
            log.info(f"  鎵弿杩涘害 {i+1}/{len(top_clusters)}")

    # 閲嶆柊鎺掑簭
    cluster_summaries.sort(key=lambda c: -c["score"])
    top_n = min(20, len(cluster_summaries))

    # 缁熻 verdict 鍒嗗竷
    verdict_counts = defaultdict(int)
    for c in top_clusters:
        verdict_counts[c["verdict"]] += 1

    # 鍐欐姤鍛?
    lines = [
        f"# Web 鍑烘捣鍛ㄦ姤 {today}",
        "",
        f"> v0.5 鑷姩楠岃瘉鐗堬紙鑱氱被 + 绔炲搧鎵弿 + TAM 浼扮畻 + GO/MAYBE/SKIP + 楠岃瘉鐗╂枡锛?,
        f"> 鎬讳俊鍙? {len(rows)} | 鑱氱被鏁? {len(clusters)} | 灞曠ず Top {top_n}",
        f"> 缁撹鍒嗗竷: GO={verdict_counts.get('GO',0)} MAYBE={verdict_counts.get('MAYBE',0)} SKIP={verdict_counts.get('SKIP',0)}",
        "",
    ]

    for i, c in enumerate(cluster_summaries[:top_n], 1):
        verdict_icon = {"GO": "馃煝", "MAYBE": "馃煛", "SKIP": "馃敶"}.get(c["verdict"], "鈿?)
        lines.append(f"## #{i} {verdict_icon} [{c['verdict']}] {c['summary']}")
        lines.append("")
        lines.append(f"- **缁煎悎鍒? {c['score']:.1f}** | 鑱氱被淇″彿鏁? **{c['cnt']}** | "
                     f"骞冲潎鐥涙劅: **{c['avg_pain']:.1f}/5**")
        lines.append(f"- 浠樿垂淇″彿: **{c['pay_count']}/{c['cnt']}** ({c['pay_ratio']*100:.0f}%) "
                     f"| AI 鍙В: yes={c['ai_yes']} partial={c['ai_partial']}")
        lines.append(f"- 绔炲搧: **{c['competitor_count']} 涓?* ({c['maturity']})"
                     + (f" 鈥?{', '.join(c['competitors'][:3])}" if c["competitors"] else ""))
        lines.append(f"- TAM: **{c['tam_score']}/10** ({c['tam_volume']}) 鈥?{c['tam_evidence']}")
        lines.append(f"- 鏉ユ簮閫氶亾: {', '.join(c['channels'])}")
        roles_str = ", ".join(f"{r}({n})" for r, n in c["roles"])
        lines.append(f"- 鐢ㄦ埛韬唤 Top3: {roles_str}")
        if c["sample_quote"]:
            lines.append(f"- 绀轰緥鍘熸枃: > {c['sample_quote']}")
        lines.append("- 鍘熸枃鏍锋湰:")
        for u in c["urls"]:
            lines.append(f"  - {u}")
        lines.append("")

    # ---------- GO 鍊欓€夐獙璇佺墿鏂欑敓鎴?----------
    go_clusters_data = []
    for i, c in enumerate(cluster_summaries[:top_n]):
        if c["verdict"] == "GO":
            top_quotes = [s[8][:150] for s in list(clusters.values())[i][:3] if s[8]]
            go_clusters_data.append({
                "summary": c["summary"],
                "avg_pain": c["avg_pain"],
                "pay_ratio": c["pay_ratio"],
                "top_quotes": top_quotes,
                "top_role": c["roles"][0][0] if c["roles"] else "unknown",
            })

    if go_clusters_data and cfg_for_clustering:
        log.info(f"涓?{len(go_clusters_data)} 涓?GO 鍊欓€夌敓鎴愰獙璇佺墿鏂?..")
        kits = _generate_validation_kit(go_clusters_data, cfg_for_clustering)
        if kits:
            lines.append("---")
            lines.append("## 楠岃瘉鐗╂枡锛圙O 鍊欓€夎嚜鍔ㄧ敓鎴愶級")
            lines.append("")
            for idx, kit in kits.items():
                if idx < len(go_clusters_data):
                    summary = go_clusters_data[idx]["summary"]
                    lines.append(f"### {summary}")
                    lines.append("")
                    lp = kit.get("landing_page", {})
                    if lp:
                        lines.append("**Landing Page Hero Copy:**")
                        lines.append(f"- A (鐥涚偣椹卞姩): {lp.get('variant_a', '-')}")
                        lines.append(f"- B (鏂规椹卞姩): {lp.get('variant_b', '-')}")
                        lines.append(f"- C (韬唤椹卞姩): {lp.get('variant_c', '-')}")
                        lines.append("")
                    ga = kit.get("google_ads", {})
                    if ga:
                        lines.append("**Google Ads:**")
                        for j, t in enumerate(ga.get("titles", []), 1):
                            lines.append(f"- 鏍囬{j}: {t}")
                        for j, d in enumerate(ga.get("descriptions", []), 1):
                            lines.append(f"- 鎻忚堪{j}: {d}")
                        lines.append("")
                    ra = kit.get("reddit_ad", "")
                    if ra:
                        lines.append(f"**Reddit Ad:** {ra}")
                        lines.append("")

    # 灏鹃儴缁熻
    lines.append("---")
    lines.append("## 缁熻")
    lines.append(f"- 鍏ュ簱淇″彿鎬绘暟: {len(rows)}")
    lines.append(f"- 鑱氱被鍚庡敮涓€璁鏁? {len(clusters)}")
    pay_yes_total = sum(1 for r in rows if r[3] == "yes")
    lines.append(f"- 浠樿垂淇″彿 yes 鍗犳瘮: {pay_yes_total}/{len(rows)} ({pay_yes_total/len(rows)*100:.0f}%)")
    ch_counts = defaultdict(int)
    for r in rows:
        ch_counts[r[6]] += 1
    lines.append(f"- 閫氶亾鍒嗗竷: {dict(ch_counts)}")
    lines.append(f"- 缁撹: GO={verdict_counts.get('GO',0)} | MAYBE={verdict_counts.get('MAYBE',0)} | SKIP={verdict_counts.get('SKIP',0)}")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"鎶ュ憡宸插啓鍏? {report_path}")


# ---------- 涓绘祦绋?----------

RESCORE_PROMPT = """鏍规嵁涓嬮潰杩欐寮曟枃锛屽垽瀹氬叾涓槸鍚﹀寘鍚粯璐规剰鎰夸俊鍙枫€傚彧杈撳嚭涓€涓?JSON 瀵硅薄銆?

銆恗onetization_signal 鍒ゅ畾 - 3 涓眰娆★紝鍛戒腑浠讳竴鍗?yes銆?
  1. 鏄庣‘浠樿垂: "I'd pay", "I pay X for", "$X/mo", "would pay good money", "happy to pay"
  2. 杞€т粯璐? "I would use this", "looking for something like this", "would consider paying",
     "would buy", "wish there was a paid option", "happy to subscribe", "would sign up"
  3. 闂存帴浠樿垂: 鐢ㄦ埛宸插湪鐢ㄤ汉宸?澶栧寘/璁㈤槄鏇夸唬鏂规 (璇存槑闂鐪熷疄涓旀効鎰忚姳閽?锛屼緥濡?
     "I currently pay a VA $500/mo to do this", "we hired someone to handle this"
"no" 浠呭綋: 绾悙妲?/ 浠呰瘎璁哄姛鑳界己澶?/ 瀹屽叏娌℃彁瑙ｅ喅鏂规鎴栦娇鐢ㄦ剰鎰裤€?

杈撳嚭鏍煎紡 (涓ユ牸 JSON, 涓嶈瑙ｉ噴):
{"monetization_signal": "yes" | "no", "reason": "鍛戒腑鍝潯鏍囧噯鐨勪竴鍙ヨ瘽璇存槑"}

寮曟枃:
{quote}

JSON:"""


def rescore_monetization(conn: sqlite3.Connection, cfg: dict, concurrency: int = 8):
    """鐢ㄦ渶鏂?prompt 閲嶆柊鍒ゅ畾 DB 閲屾墍鏈変俊鍙风殑 monetization_signal銆備粎 raw_quote 涓嶉渶瑕侀噸鎶撳師鏂囥€?""
    try:
        provider_type, client, p_cfg = _make_llm_client(cfg)
    except RuntimeError as e:
        log.error(str(e))
        return

    rows = list(conn.execute("SELECT id, raw_quote, monetization_signal FROM signals"))
    log.info(f"瀵?{len(rows)} 鏉＄幇鏈変俊鍙烽噸鏂板垽瀹?monetization_signal (provider={cfg['llm']['provider']}, concurrency={concurrency})")

    in_rate = p_cfg.get("price_in", 0.5) / 1_000_000
    out_rate = p_cfg.get("price_out", 1.0) / 1_000_000
    currency = p_cfg.get("currency", "CNY")
    max_tokens = cfg["llm"]["max_tokens_per_call"]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    cost_lock = threading.Lock()
    total_cost = [0.0]
    flipped = [0]   # yes <- no 鍙樺寲鏁?

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
            log.warning(f"  rescore[{rid}] 澶辫触: {type(e).__name__}: {str(e)[:80]}")
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
                log.info(f"  rescore 杩涘害 {done}/{len(rows)} | 绱 {total_cost[0]:.3f} {currency} | no鈫抷es 缈昏浆: {flipped[0]}")
    conn.commit()
    log.info(f"rescore 瀹屾垚: {flipped[0]} 鏉?no鈫抷es 缈昏浆, 鎴愭湰 {total_cost[0]:.3f} {currency}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--dry-run", action="store_true", help="涓嶈皟 LLM锛屽彧楠岃瘉鎶撳彇")
    parser.add_argument("--channel", choices=["reddit", "hn", "stackex", "ddg", "brave"],
                        help="鍙窇鎸囧畾閫氶亾锛堥粯璁よ窇 hn + stackex + ddg锛況eddit 鍜?brave 宸插簾寮冿級")
    parser.add_argument("--report", action="store_true", help="鍙噸鏂扮敓鎴愭姤鍛?)
    parser.add_argument("--rescore-monetization", action="store_true",
                        help="瀵圭幇鏈?DB 閲屾墍鏈変俊鍙烽噸鏂板垽瀹?monetization_signal锛堢敤鏈€鏂?prompt锛夈€傜害 楼0.003/鏉?)
    parser.add_argument("--limit", type=int, default=0,
                        help="LLM 鎶藉彇鏈€澶氬鐞?N 鏉″師鏂?(0=涓嶉檺). 鐢ㄤ簬蹇€熼獙璇? reasoning 妯″瀷鎺ㄨ崘 30-50.")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="LLM 骞跺彂鏁般€係enseNova/GLM 涓€鑸彲璁?4-8 鍔犻€?(榛樿 4)")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        log.error(f"閰嶇疆鏂囦欢涓嶅瓨鍦? {cfg_path}锛堝厛 cp config.example.yaml config.yaml锛?)
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

    # Module 1: 淇″彿閲囬泦锛堥粯璁?hn + stackex + brave锛況eddit 鐩存姄宸茶 reddit.com 鍏ㄩ潰 403锛岄渶鎵嬪姩鍔?--channel reddit 鍚敤锛?
    if args.channel == "reddit":
        log.warning("Reddit 鐩存姄鍦?2024+ 璧疯 reddit.com 鍙嶇埇鍏ㄩ潰鎷︽埅銆備粎鍦ㄤ綘宸叉湁 OAuth 鍑瘉鏃舵墠鍚敤銆?)
        log.info("=== 閫氶亾 A: Reddit (鍏紑 JSON 绔偣) ===")
        all_docs.extend(fetch_reddit(cfg))

    if not args.channel or args.channel == "hn":
        log.info("=== 閫氶亾 B: HackerNews ===")
        all_docs.extend(fetch_hn(cfg))

    if not args.channel or args.channel == "stackex":
        log.info("=== 閫氶亾 C: Stack Exchange ===")
        all_docs.extend(fetch_stackexchange(cfg))

    if not args.channel or args.channel == "ddg":
        log.info("=== 閫氶亾 D: DuckDuckGo (鍙嶆煡 Reddit/Quora锛屽厤璐规棤闇€ key) ===")
        all_docs.extend(fetch_ddg(cfg))

    if args.channel == "brave":
        log.warning("Brave Search 閫氶亾宸插簾寮冿紙鍏嶈垂灞傞渶淇＄敤鍗★級銆傝鐢?--channel ddg銆?)
        all_docs.extend(fetch_bravesearch(cfg))

    # TODO: 鍏朵粬閫氶亾 (鎸?SOP 绗?13 鑺備紭鍏堢骇)
    # if not args.channel or args.channel == "g2":
    #     all_docs.extend(fetch_g2(cfg))
    # if not args.channel or args.channel == "upwork":
    #     all_docs.extend(fetch_upwork(cfg))

    log.info(f"=== 鍏遍噰闆?{len(all_docs)} 鏉″師鏂?===")

    # Module 2: 鎶藉彇
    signals = extract_signals(all_docs, cfg, dry_run=args.dry_run,
                              limit=args.limit, concurrency=args.concurrency)

    # 鍏ュ簱
    new_count = 0
    for s in signals:
        if insert_signal(conn, s):
            new_count += 1
    log.info(f"=== 鍏ュ簱 {new_count} 鏉℃柊淇″彿锛坽len(signals) - new_count} 鏉￠噸澶嶏級===")

    # 鏃ュ織
    conn.execute(
        """INSERT INTO run_log (started_at, ended_at, channel, docs_fetched,
                                signals_extracted, status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (started_at, datetime.now(timezone.utc).isoformat(),
         args.channel or "all", len(all_docs), new_count,
         "ok" if not args.dry_run else "dry-run", ""),
    )
    conn.commit()

    # 鐢熸垚鎶ュ憡
    if not args.dry_run:
        gen_weekly_report(conn, str((ROOT / cfg["output"]["report_dir"]).resolve()), cluster_threshold=cfg.get("report", {}).get("cluster_threshold", 0.95), cfg_for_clustering=cfg)


if __name__ == "__main__":
    main()
