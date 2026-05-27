# Web 出海需求发现 Agent SOP v0.1

> 给 Claude Code（或任何具备 MCP 工具调用能力的 LLM agent）的保姆级操作规范。
> 目标：每周自动产出 10 个排序后的候选需求信号，附原文链接和验证物料，由人类决定是否进入"50 美元广告测试"环节。

---

## 0. 设计原则（不可违反）

| # | 原则 | 为什么 |
|---|---|---|
| P1 | **LLM 只做 transform，不做 generate** | LLM 在没有真实信号时会幻觉填空。Agent 的输入必须是真实抓取的原文，LLM 只负责"提取/聚类/打分"。 |
| P2 | **人类把守验证闸门** | Agent 不决定"做不做"，只决定"哪 10 个最值得你看"。决策权 100% 在人。 |
| P3 | **多通道并行 + 失败回退** | 单通道有盲区。Reddit 没信号不代表市场没需求，必须有备选路径。 |
| P4 | **窄垂直 > 广撒网** | 每次 run 只针对 1–3 个垂直（例如"独立律师 + 房产经纪 + Etsy 卖家"），不要"找所有机会"。 |
| P5 | **原文可追溯** | 每条信号必须带 URL + 原文片段 + 抓取时间。任何无源信号视为污染，丢弃。 |
| P6 | **预算硬上限** | 每次 run 设置 LLM token / API call 上限，超限即熔断，避免失控烧钱。 |

---

## 1. 系统架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Claude Code Agent (主控)                        │
│  - 读 config.yaml (垂直、关键词种子、预算)                              │
│  - 调度各 Module，串联结果                                              │
│  - 失败时按回退树切换路径                                                │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────┐
│  Module 1: 信号采集 (并行)                                │
│  ┌──────────────┐ ┌─────────────────┐ ┌──────────────┐ │
│  │ B: HN        │ │ C: Stack Exchange│ │ D: DDG       │ │
│  │ (algolia)    │ │ (api, 300/day)  │ │ (反查 Reddit) │ │
│  └──────────────┘ └─────────────────┘ └──────────────┘ │
│  ⚠️ A: Reddit 直抓 (废弃, 403)  ⚠️ Brave (废弃, 需信用卡)  │
└────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────┐    ┌──────────────────┐  ┌──────────────────┐
│  Module 2: 抱怨提取(LLM)    │ →  │ Module 3: 聚类   │→ │ Module 4: 多维打分│
│  原文 → JSON 抱怨记录       │    │ embedding+DBSCAN │  │ frequency × pain │
└────────────────────────────┘    └──────────────────┘  │  × pay × gap     │
                                                         └──────────────────┘
        │
        ▼
┌────────────────────────────┐
│  Module 5: 验证物料生成     │
│  - landing page 文案 (3 变体)│
│  - 3 条广告变体             │
│  - 验证检查清单             │
└────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  SQLite 持久化层  signals / clusters / scores / experiments          │
└──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  每周 Top10 报告 (Markdown) → 人类审核 → 选 3 个进入广告测试           │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. 技术栈与 MCP 服务器

### 推荐栈
- **Agent 运行时**：Claude Code（用于编排和文档读写）
- **LLM 抽取**：默认 SenseNova `sensenova-6.7-flash-lite`（CNY 计费，零 VPN）；备选 GLM `glm-4-flash`；可切 Claude `claude-haiku-4-5-20251001`
  - ⚠️ **重要：SenseNova 6.7 是 reasoning 模型**，输出前会先思考。`max_tokens_per_call` 必须给 3000+，否则会卡在 `finish_reason=length` 拿不到 content。已在 `config.example.yaml` 默认设为 3500。
- **持久化**：SQLite（轻量，单文件，方便备份）
- **嵌入**：OpenAI `text-embedding-3-small` 或本地 `bge-small-en`（看预算）
- **聚类**：scikit-learn 的 DBSCAN / HDBSCAN
- **调度**：先手动跑 → 跑稳后用 cron / GitHub Actions

### 需要装的 MCP 服务器

| MCP 名称 | 用途 | 备注 |
|---|---|---|
| `brave-search` | Google 替代搜索 | 免费层 2000 次/月够用；用于找子版/职业关键词 |
| `firecrawl` | 抓动态/反爬站点 | 付费 $16/月起，G2/Capterra 这种 SPA 必备 |
| `filesystem` | 读写本地 data/ | Claude Code 自带 Read/Write 也行 |
| Reddit MCP（社区） | Reddit 搜索/抓帖 | 可选；用 PRAW 直接写 Python 也行 |
| `sqlite` MCP | 操作 SQLite | 可选；agent 直接执行 Python 脚本也行 |

### 不需要 MCP，直接用 Python 库
- `requests` → Reddit 公开 JSON 端点（无需 API key、无需账号、无需 PRAW）
- `requests` → Hacker News Algolia API（完全免费，无需 key）
- `requests` → Stack Exchange API（免费，每天 10k 请求）

### 我不再依赖
- ~~`praw`（Reddit 官方 SDK）~~ — 已移除，因 Reddit API 审批困难
- ~~`tweepy` / X API~~ — 现在贵且限流严，暂跳过

---

## 3. Agent 角色与系统提示词

把下面这段作为 `D:\docs\智控平台\web-chuhai-agent\prompts\system.md`，调用 agent 时载入。

```markdown
你是一个出海需求发现的执行 agent。你的唯一任务是：从真实网络文本中抓取并归纳"用户抱怨"和"愿望表达"，输出结构化候选信号，绝不发明信号。

【硬约束】
1. 任何输出条目必须带 source_url + raw_quote + fetched_at。三者缺一不可。
2. 如果某个通道 0 结果，不要编内容补齐。直接报告"该通道 0 召回"并触发回退路径。
3. 你不判断"这个 idea 好不好"。你只按规则打分，把决策权交给人类。
4. 同一信号在不同来源重复出现是好事（提升频率分），不要去重原文。去重只发生在 Module 3 的聚类阶段。
5. 你必须先读 config.yaml 中的目标垂直，所有抓取/搜索必须在垂直范围内。不要扩大范围。

【失败时的优先级】
通道失败 → 触发该通道的 fallback → 仍失败 → 记录日志并跳过 → 不要重试超过 2 次
Token 用量接近预算上限 → 立刻停止抓取，输出当前已收集的数据 → 报告中标"unfinished due to budget"
```

---

## 4. Module 1: 多通道信号采集

每个通道独立设计，主路径失败有回退。Agent 并行调用，结果汇总到 `signals_raw` 表。

### 4.1 通道 A — Reddit（⚠️ 已废弃，仅作参考）

> **2024+ 现状**：reddit.com 对所有匿名 HTTP 请求（包括 `.json` 公开端点）一律返回 **HTTP 403**，
> 实测无论 User-Agent 怎么伪装、用 `www` 或 `old` 子域，都被拦截。
> 唯一通路是注册 Reddit 账号 → 申请 OAuth API key → 用 PRAW。
> 当前用户无法完成审批，因此本通道**默认 disabled**。
>
> **替代方案**：用 **通道 D Brave Search** 反查 `site:reddit.com [keyword]`，
> 通过搜索引擎拿到 Reddit 帖子的标题 + 200 字 snippet。Snippet 本身已经包含我们要的痛点表述。
>
> 如果你后续拿到 Reddit OAuth 凭证，把 `fetch_reddit` 改成 PRAW 实现即可重启此通道。代码框架仍保留。

### 4.2 通道 B — Hacker News ⭐ 与 Reddit 同等优先级

> Algolia HN API 完全免费、无需 key、无审批。地址：`https://hn.algolia.com/api/v1/search`

**主路径**：
```
For each vertical:
  seeds = [vertical.name, *vertical.keywords_extra]
  templates = ['wish there was', 'recommend tool', 'what do you use for',
               'is there a tool', 'tired of', 'spending hours']
  For each (seed × template) 组合:
    GET hn.algolia.com/api/v1/search?query=[seed+template]&tags=story
        &numericFilters=created_at_i>[180天前],points>=3
    For each hit:
      抓 title + story_text
      若 num_comments >= 3: 抓前 8 条 top comments
      喂 Module 2 提取
```

**回退**：
- 信号 < 3 条 → 把 templates 扩成 SOP 4.1 那一组宽 keywords
- API 不返回 → 改用 firebase 接口 `https://hacker-news.firebaseio.com/v0/topstories.json`（原始 HN API，无搜索但可拉所有 story）
- 仍 0 → 跳过本通道，记录日志

### 4.3 通道 C — Stack Exchange ⭐ 高含金量

> Stack Exchange API：每天 300 次免费配额（无需 key），注册 app 后升到 10000 次/天。

**为什么含金量高**：Stack Exchange 旗下有 170+ 个**职业/兴趣垂直站**，每个站都是"问问题 + 投票回答"模式。**高分问题 ≈ 高频痛点 + 已被验证**。

**常用站点速查**：
| 你的垂直 | 推荐 SE 站 |
|---|---|
| 律师 / HR / 自由职业 | law, workplace, freelancing |
| 摄影 / 视觉 | photo, graphicdesign |
| 父母 / 教育 | parenting |
| 财务 / 个人理财 | money, personalfinance |
| 写作 / 内容 | writers |
| 程序员 / SaaS | stackoverflow, softwareengineering, askubuntu, superuser |
| DIY / 园艺 / 烹饪 | diy, gardening, cooking, pets |

**主路径**：
```
For each vertical:
  For each site in vertical.se_sites:
    For each (seed × template) 组合:
      GET api.stackexchange.com/2.3/search/advanced
          ?q=[seed+template]&site=[site]&filter=withbody
          &sort=votes&fromdate=[1年前]&pagesize=20
      For each question (score >= 2 OR 命中关键词):
        喂 Module 2 提取
      监控 quota_remaining，剩 < 20 时停止
```

**回退**：
- 配额耗尽 → 注册 stackapps.com app 拿 key，升到 10k/天
- 某个 site 0 结果 → 该 site 与垂直不匹配，从 config 删除
- 全部站点 0 结果 → 垂直可能不在 SE 覆盖范围（如美容、单亲），跳过本通道


### 4.4 通道 D — DuckDuckGo 反查 ⭐ Reddit/Quora 免费替代

> 用 `ddgs` Python 库直接调 DuckDuckGo 搜索。
> 完全免费、无需 API key、无需信用卡、无需注册。是 Brave Search 的免费替代品。

**核心机制**：

```
你: 我想拿 Reddit 上的痛点贴
Reddit: 403 滚开
DDG: 我把 Reddit 早就爬过了，给你 search snippet (标题 + 200 字摘要)
你: snippet 里恰好就有 "I wish there was a tool for X" 这种原话
LLM: 提取出来当 signal
```

**主路径**：
```
For each vertical:
  query_budget = config.ddg.max_queries_per_vertical (默认 12)
  For each (site_op, pain_q) in [
    ('site:reddit.com', 'I wish there was'),
    ('site:reddit.com', 'why is there no'),
    ('site:reddit.com', 'tired of'),
    ('site:reddit.com', 'would pay for'),
    ('site:reddit.com', 'frustrated with'),
    ('site:quora.com', 'is there a tool'),
    ('site:quora.com', 'how do I automate'),
    ('site:indiehackers.com', 'looking for tool'),
  ]:
    For each seed in vertical.keywords_extra[:3]:
      query = f'{site_op} "{pain_q}" {seed}'
      DDGS().text(query, max_results=15)
      For each result:
        text = title + "\n\n" + body (snippet 200 char)
        喂 Module 2 提取
      query_budget -= 1
      sleep 1s (避免触发 DDG 反爬)
```

**回退**：
- DDG 返回 0 结果 → 改用更宽的关键词或删 `"..."` 引号
- DDG 触发限流 (429 或 Ratelimit exception) → sleep 3s 重试；连续 3 次失败跳过
- 整个通道失效 → 切换到 SearXNG 公共实例（备选方案，见附录 C）
- 想加更多 site → 在 `DDG_SITE_TEMPLATES` 里追加（如 `site:trustpilot.com`、`site:producthunt.com`）

**实测样本（2026-05-23）**：
- 查询 `site:reddit.com "I wish there was" etsy seller` 拿到："I wish there was a way to review buyers"
- 查询 `site:reddit.com "frustrated with" etsy seller` 拿到："Etsy's copycat problem destroyed my nearly 7 figure shop"
- 信号质量 ≈ 直接抓 Reddit，且零成本

---

## 5. Module 2: 抱怨提取（LLM Prompt）

存为 `prompts/extract.md`：

```markdown
你将收到一段网络原文（Reddit 帖、评论、HN 讨论、G2 差评等）。

任务：识别其中是否包含"用户抱怨"或"愿望表达"。如果有，输出 JSON；如果没有，输出 {"signals": []}。

【判定标准】抱怨 = 用户花时间/钱/精力做了某件他认为不应该这么麻烦的事，或在表达"我希望有 X 但没找到"。
不算抱怨 = 单纯吐槽天气/政治/价格、对工具的赞美、bug 报告（除非是缺失功能）。

【输出 schema】
{
  "signals": [
    {
      "raw_quote": "原文片段，30-200 词，必须是原话不能改写",
      "user_role": "推测用户身份，如 'solo lawyer' / 'etsy seller' / unknown",
      "pain_category": "workflow_friction | manual_repetition | missing_tool | bad_existing_tool | communication_overhead | other",
      "pain_intensity": "1-5，1=轻微吐槽，3=每天受其困扰，5=明确表达 willing to pay",
      "implied_task": "用一句话描述用户想完成的具体任务",
      "ai_solvable": "yes | partial | no — 当前 LLM/AI 能否解决",
      "monetization_signal": "yes 仅当原文出现 'I'd pay' / 'I pay X for' / '$X/mo' / 类似明确付费意愿；其他一律 no"
    }
  ]
}

【硬约束】
- raw_quote 必须能在原文中精确找到，不能改写、不能翻译
- 一段原文如有多个抱怨，分开输出多条
- 不确定就标 unknown，绝不编造
```

**模型选择**：批量预筛用 Haiku（便宜），高质量复评用 Opus。一般 90% 走 Haiku，10% 走 Opus。

---

## 6. Module 3: 去重与聚类

**步骤**：
1. 对每条 `signals.implied_task` 用 `text-embedding-3-small` 生成 1536 维向量
2. 用 HDBSCAN（min_cluster_size=3, min_samples=2）聚类
3. 噪声点（cluster=-1）也保留，但单独列
4. 每个 cluster 用 Claude 生成一句 `cluster_summary`（10 词内）

**输出**：`clusters` 表，字段：`cluster_id, summary, signal_count, sources_set, first_seen, last_seen`。

---

## 7. Module 4: 多维打分

打分公式（每周可校准）：

```
score = w1*frequency + w2*intensity + w3*pay_signal + w4*ai_fit + w5*gap

frequency  = log(signal_count + 1) × 2        # 出现次数，对数避免单一爆款主导
intensity  = avg(pain_intensity)              # 平均强度 1-5
pay_signal = (monetization_signal=yes 占比) × 10  # 付费信号占比
ai_fit     = (ai_solvable=yes 占比) × 5       # AI 可解占比
gap        = 5 - min(5, competitor_count)     # 竞品越少分越高（0 个=5 分，>=5 个=0 分）

默认权重：w1=2, w2=2, w3=4, w4=1.5, w5=2
```

**为什么 pay_signal 权重最高**：你已经踩过伪需求的坑。任何有人明确说"I'd pay for this"的信号，优先级直接拉满。

**竞品数量获取**：用 brave-search 搜 cluster_summary 的核心关键词，统计前 20 条结果中真正解决该问题的产品数。

---

## 8. Module 5: 验证物料生成

每周 Top10 候选都自动生成：

```
prompts/validation_kit.md →

输入：cluster_summary + top 3 raw_quotes

输出（每个候选 1 份）：
1. Landing page hero copy（3 个变体）
   - 变体 A: 痛点驱动 "Stop spending 3 hours on X"
   - 变体 B: 解决方案驱动 "AI does X in 30 seconds"
   - 变体 C: 身份驱动 "Built for [user_role] who [pain]"

2. Google Ads 文案（3 个标题 + 2 个描述）

3. Reddit Ad 文案（不要广告腔，要像用户自来水）

4. 验证检查清单：
   [] Carrd 落地页 24h 内上线
   [] $50 广告预算分配 (Google $25, Reddit $15, Twitter $10)
   [] 跑 5 个工作日
   [] 通过标准: CTR ≥ 3% AND waitlist 转化 ≥ 15%
   [] 不通过即毙掉，进入下一个候选
```

---

## 9. 全局回退决策树

```
agent 启动
  │
  ▼
读 config.yaml 成功? ── 否 → 报错退出
  │ 是
  ▼
Module 1 并行采集（5 通道）
  │
  ├─ 5 个通道全部 0 召回 → 报告"垂直选择可能有问题"，建议人工换垂直
  ├─ ≥ 3 个通道有召回 → 继续
  └─ 仅 1-2 个通道有召回 → 标记低置信，继续但提示
  │
  ▼
Module 2 提取
  │
  ├─ 抽取出的 signals < 20 条 → 触发 Module 1 扩展关键词第二轮
  └─ ≥ 20 条 → 继续
  │
  ▼
Module 3 聚类
  │
  ├─ 所有 signals 都是噪声（无 cluster）→ 报告"信号过于分散，需要更窄垂直"
  └─ 有 ≥ 3 个 cluster → 继续
  │
  ▼
Module 4 打分
  │
  ├─ Top10 中 ≥ 5 个 pay_signal=0 → 标记"付费意愿低，建议本周不下场"
  └─ 否则正常 → Module 5
  │
  ▼
Module 5 验证物料 → 输出周报 → 等待人类决策
```

---

## 10. 数据 Schema（SQLite）

```sql
-- 原始信号
CREATE TABLE signals (
  id INTEGER PRIMARY KEY,
  source_channel TEXT,        -- reddit/hn/g2/upwork/keyword
  source_url TEXT NOT NULL,
  raw_quote TEXT NOT NULL,
  user_role TEXT,
  pain_category TEXT,
  pain_intensity INTEGER,
  implied_task TEXT,
  ai_solvable TEXT,
  monetization_signal TEXT,
  fetched_at TEXT NOT NULL,
  cluster_id INTEGER
);

-- 聚类
CREATE TABLE clusters (
  id INTEGER PRIMARY KEY,
  summary TEXT,
  signal_count INTEGER,
  avg_intensity REAL,
  pay_signal_ratio REAL,
  ai_fit_ratio REAL,
  competitor_count INTEGER,
  score REAL,
  first_seen TEXT,
  last_seen TEXT,
  week_of TEXT
);

-- 验证实验
CREATE TABLE experiments (
  id INTEGER PRIMARY KEY,
  cluster_id INTEGER,
  status TEXT,                -- proposed / running / pass / fail / killed
  landing_url TEXT,
  spend_usd REAL,
  ctr REAL,
  conversion_rate REAL,
  notes TEXT,
  started_at TEXT,
  ended_at TEXT
);
```

---

## 11. 调度与运维

### 第一个月（手动）
- 每周日晚上跑一次完整管线，~30 分钟
- 周一审 Top10 报告，选 3 个候选
- 周二上 Carrd 落地页 + 广告
- 周日复盘上周实验

### 跑稳后（自动）
- GitHub Actions cron `0 18 * * 0`（每周日 18 点）
- 跑完发飞书机器人 / Discord webhook 通知
- 人类只在通知里点链接审报告

---

## 12. 质量门（每周必检）

| 指标 | 健康值 | 异常处理 |
|---|---|---|
| signals 抓取量 | ≥ 200/周 | 触发回退树或更换垂直 |
| LLM 抽取成功率 | ≥ 80% | 检查 prompt，看是否被 hallucinate |
| 原文可追溯率 | 100% | 抽 5 条随机校验，未对应即丢弃 |
| Top10 中 pay_signal>0 占比 | ≥ 40% | 否则该垂直商业化弱 |
| 每周实验完成数 | 2–3 个 | 超过 5 个说明不够严，低于 1 说明卡住了 |

---

## 13. 第一周操作清单 ✅

**周一**
- [ ] 装 Python 环境 + 依赖（见 `code/requirements.txt`）
- [ ] ~~申请 Reddit API key~~ → **不需要了**！本管线用公开 JSON 端点
- [ ] 确认能访问 reddit.com 和 hn.algolia.com（如在大陆，需要 VPN）
- [ ] 配置 `.env`（只需要 `ANTHROPIC_API_KEY`）

**周二**
- [ ] 在 `config.yaml` 里定 3 个目标垂直 + 5–10 个种子子版
- [ ] 跑 `python code/run_pipeline.py --dry-run` 验证 Reddit + HN 两个通道都能抓

**周三**
- [ ] 跑正式管线 `python code/run_pipeline.py`
- [ ] 检查 `data/signals.db` 是否有数据（Reddit 和 HN 各至少几十条）

**周四**
- [ ] 看 Top10 报告，人工抽样 5 条核对原文（防 hallucination）
- [ ] 调整 keyword / 子版列表
- [ ] 注意: 同一 cluster 出现在 Reddit AND HN 两个来源 = 强信号

**周五–周日**
- [ ] 选 1 个 cluster 上 Carrd 落地页
- [ ] 跑 $50 广告测试
- [ ] 复盘指标

---

## 附录 A: 常见踩坑

1. **Agent 被 LLM 幻觉污染** → 每周抽 5 条人工对原文，错一条就调 prompt
2. **垂直选太宽（如"程序员"）** → 召回噪音爆炸，必须细分到职业 + 场景
3. **过于依赖 Reddit** → Reddit 抱怨偏 IT/年轻人，传统行业要靠 Upwork/差评
4. **跑两周就看 ROI** → 验证管线 = 长期资产，前 2 个月会感觉"很多噪音"，第 3 个月开始浮现高质量候选
5. **看到一个好 cluster 就跳过广告测试** → 这就是你之前踩坑的地方。**任何 idea 都跑 $50 测试**，无一例外

---

## 附录 B: 何时升级到付费 API

| 信号 | 该升级什么 |
|---|---|
| Reddit/HN 信号充足但 G2/Capterra 抓不到 | Firecrawl $16/月 |
| 想看竞品 SEO 缺口 | Ahrefs Lite $129/月 |
| 想做关键词矩阵反挖 | DataForSEO $50/月 |
| 跑稳后想自动化通知 | 飞书 webhook（免费）+ GitHub Actions（免费） |
