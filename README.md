# Web 出海 Agent - 快速启动

> 5 分钟跑起来。完整设计见 [SOP.md](./SOP.md)。

---

## 目录结构

```
web-chuhai-agent/
├── SOP.md                     # 保姆级操作规范（设计文档）
├── README.md                  # 本文件
├── code/
│   ├── run_pipeline.py        # 主入口，可跑
│   ├── requirements.txt       # Python 依赖
│   ├── config.example.yaml    # 配置模板（复制为 config.yaml 再改）
│   └── .env.example           # 凭证模板（复制为 .env 再改）
├── prompts/                   # 各模块 prompt（SOP 内嵌，单独抽出方便调）
└── data/
    ├── signals.db             # SQLite 数据库（首次运行自动创建）
    └── reports/               # 每周报告 markdown
```

---

## 第一次跑（10 分钟）

### 1. 装依赖

```bash
cd DemandGet/code
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. 三个数据通道，全部免费 + 不需要信用卡

| 通道 | 数据源 | 是否需要 key |
|---|---|---|
| HackerNews | hn.algolia.com | ❌ 无 |
| Stack Exchange | api.stackexchange.com | ❌ 无（300/天）/ 可选注册升 10k/天 |
| **DuckDuckGo 反查** | `ddgs` Python 库 | ❌ 完全免费，无需注册 |

**DDG 反查 = 间接拿 Reddit / Quora / IndieHackers 数据**：搜 `site:reddit.com "I wish there was" lawyer`，DDG 返回的 snippet 里就包含 Reddit 帖子的痛点表述，**绕过 Reddit 自己反爬，也不需要 Brave Search 付费 key**。

### 3. LLM 选一家（国产推荐）

| Provider | 申请地址 | 推荐模型 | 价格 |
|---|---|---|---|
| **SenseNova 商汤** ⭐ 默认 | https://platform.sensenova.cn/ | `sensenova-6.7-flash-lite` | ¥0.5/MTok in, ¥1/MTok out |
| **GLM 智谱** | https://open.bigmodel.cn/ | `glm-4-flash` | 类似 |
| Claude (海外) | https://console.anthropic.com/ | `claude-haiku-4-5` | $1/MTok in, $5/MTok out (~10x 贵) |

**国产 LLM 优势**：调用不需要 VPN（数据源的 VPN 依然需要），价格便宜 10–20 倍。

### 4. 准备配置

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

编辑 `.env`：
```
# 选一家填即可
SENSENOVA_API_KEY=...       # 默认用这个
GLM_API_KEY=...             # 或者填这个
ANTHROPIC_API_KEY=sk-ant-... # 或者填这个
```

编辑 `config.yaml`：
- `llm.provider`: 改成 `sensenova` / `glm` / `anthropic` 之一
- `verticals`: 改成你要盯的垂直 + 配 `se_sites`

完整 Stack Exchange 站点列表：https://stackexchange.com/sites
常用站点：`workplace`, `law`, `parenting`, `photo`, `writers`, `freelancing`, `ecommerce`, `money`, `diy`, `cooking`, `pets`, `politics`

### 5. 第一次跑（dry-run，不烧 LLM 钱）

```bash
python run_pipeline.py --dry-run
```

确认三个通道都能抓到数据（看日志里"HN 命中 N 条 / Stack Exchange 命中 N 条 / DDG 命中 N 条"）。

### 6. 正式跑

```bash
python run_pipeline.py                  # 跑全部通道 (hn + stackex + ddg)
python run_pipeline.py --channel hn       # 只跑 HackerNews
python run_pipeline.py --channel stackex  # 只跑 Stack Exchange
python run_pipeline.py --channel ddg      # 只跑 DuckDuckGo (反查 Reddit/Quora)
```

按 config 里 `batch_budget: 30.0` 元，SenseNova flash-lite 可处理 ~1000+ 条原文。
跑完看 `../data/reports/weekly-YYYY-MM-DD.md`。

---

## Claude Code + MCP 模式（推荐）

如果你想让 Claude Code 来调度这个管线（而不是手动跑 Python），把下面放到项目根目录的 `.mcp.json`：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/DemandGet"]
    },
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": { "BRAVE_API_KEY": "${BRAVE_API_KEY}" }
    }
  }
}
```

然后在 Claude Code 里这样指挥：

```
读 SOP.md → 跑 run_pipeline.py → 看 data/reports/ 最新报告 → 把 Top5 用 brave-search MCP 查竞品 → 给我一份带竞品分析的版本
```

Agent 会按 SOP 的回退树自己处理失败。

可选 MCP（按 SOP 第 11 节判断何时加）：
- `firecrawl` MCP — 抓 G2/Capterra/Upwork（动态 SPA）
- 社区版 Reddit MCP — 替代直接调 PRAW
- `sqlite` MCP — 让 agent 直接查询 signals.db 生成自定义报告

---

## 当前 v0.3 的实现状态

✅ 已实现:
- **HackerNews 通道（Algolia API，完全免费）**
- **Stack Exchange 通道（免费 300/天）** — 按垂直配 se_sites
- **DuckDuckGo 反查通道**（完全免费，无需 key，无需信用卡）— 用 `ddgs` 库反查 site:reddit.com / site:quora.com / site:indiehackers.com
- **多 LLM provider 抽象** — SenseNova（默认）/ GLM / Anthropic 三选一，OpenAI SDK 统一调用
- Claude/SenseNova/GLM 抽取（带 hallucination 防御: raw_quote 必须在原文中）
- SQLite 持久化 + 去重（UNIQUE 约束）
- 朴素分组周报
- LLM 预算熔断（按选中 provider 的 currency 计）
- dry-run 模式
- `--channel hn|stackex|ddg` 单通道运行

⚠️ 已废弃:
- Reddit 直抓 (`fetch_reddit`)：reddit.com 在 2024+ 对所有匿名请求返回 403。代码保留但默认 disabled。如果你拿到 OAuth 凭证可改 PRAW 重启。
- Brave Search (`fetch_bravesearch`)：免费层激活需要绑定信用卡，已用 DuckDuckGo 完全免费替代。

🚧 待实现（按优先级）:
1. embedding + HDBSCAN 真聚类（替换朴素分组）
2. G2/Capterra 差评通道（需 firecrawl $16/月）
3. Upwork/Fiverr 通道（需 firecrawl 或 Apify）
4. 多维打分公式（SOP 第 7 节）
5. 验证物料自动生成（SOP 第 8 节）
6. GitHub Actions 周调度

---

## 常见问题

**Q: Reddit 抓到的全是垃圾贴怎么办？**
A: 看 SOP 第 4.1 节回退树。先调高 `min_score` 到 20，再不行换更窄的子版。

**Q: LLM 一直返回 `{"signals": []}` 怎么办？**
A: 大概率是预过滤的 keywords 太严了。把 `pain_keywords` 扩成 SOP 第 4.1 节"扩展关键词"那一组，再跑。

**Q: 跑了一周还是没产出可用候选？**
A: 不是 agent 的问题，是垂直选错了。换 1-2 个垂直再跑两周。如果连续 3 个垂直都不出货，回头看 SOP 第 0 节"设计原则"，可能你在违反某条。

**Q: 我要不要现在就上 Ahrefs / Firecrawl？**
A: 不要。Reddit + HN + Claude 跑通 2 周，看是不是真能稳定产出候选。再决定加什么。
