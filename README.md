# ModelRadar · AI 模型情报雷达 🛰️

> 比公众号和媒体快 12-24 小时感知 AI 模型动态。
> 多源采集 → 变动检测 → 自动告警 → 周报生成，一条龙。

ModelRadar 是一套面向 AI 从业者的实时模型情报监控系统。它同时盯着榜单、GitHub、HuggingFace、Reddit、厂商博客和 OpenRouter 用量数据，检测到 P0 级变动立即邮件告警，每周一自动生成精美 HTML 周报。

---

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| **📊 多源榜单监控** | LMArena（文本/文生图/文生视频/图生视频）、Artificial Analysis、SuperCLUE（7 赛道），共 14 个独立赛道 |
| **🐙 GitHub 组织监控** | 追踪 deepseek-ai / THUDM / MoonshotAI / MiniMax-AI / stepfun-ai / QwenLM 等组织的新 Repo 和 Release |
| **🤗 HuggingFace 趋势** | 实时下载量 & 讨论热度 Top 10，跨周对比 NEW 标签 |
| **🔌 OpenRouter 调用量** | 开发者真金白银的 Token 消耗周榜 Top 10，周环比变化 |
| **📰 厂商博客 RSS** | OpenAI / Anthropic / Google / Meta 官方博客新文章即时感知 |
| **💬 Reddit 社区分析** | LocalLLaMA / StableDiffusion / singularity / ChatGPT 四个 subreddit，LLM 提炼社区观点和热议主题 |
| **🔴 P0/P1 告警** | 登顶 Top 1、新 Repo、新 Release 等关键事件 → 邮件即时推送，严格去重 |
| **📬 周报** | 每周一自动生成 HTML 邮件周报（7 个板块），存档可回看 |
| **🔥 热度评分** | 榜单综合排名 + GitHub star 增速双维打分 |
| **🔗 模型名归一化** | `GPT-4o` / `openai/gpt-4o` / `gpt-4o-2024-05-13` → 统一 canonical，自动学习别名 |

---

## 🏗️ 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                      Data Sources                        │
│  LMArena · AA · SuperCLUE · GitHub · HuggingFace         │
│  OpenRouter · Reddit · 厂商博客 RSS                       │
└───────────────────┬──────────────────────────────────────┘
                    │ Collectors (定时采集)
                    ▼
┌──────────────────────────────────────────────────────────┐
│                    SQLite (WAL)                           │
│  leaderboard_snapshots · github_snapshots · github_      │
│  releases · change_events · heat_scores · reddit_posts   │
│  · hf_trending · openrouter_rankings · weekly_reports    │
└───────────────────┬──────────────────────────────────────┘
                    │ Engine (变动检测 + 分析)
                    ▼
┌──────────────────────────────────────────────────────────┐
│  diff_engine · heat_scorer · alert_manager               │
│  leaderboard_digest · hf_digest · openrouter_digest      │
│  release_digest · reddit_opinions · reddit_themes        │
│  alias_learner · weekly_report                           │
└───────────────────┬──────────────────────────────────────┘
                    │
          ┌─────────┴──────────┐
          ▼                    ▼
   📧 邮件告警/周报      🖥️ Web Dashboard
   (163 SMTP)           (FastAPI + 原生 JS)
```

---

## 🚀 快速开始

### 前置要求

- Python 3.10+
- GitHub Personal Access Token（只需 `public_repo` 只读权限）
- 163 邮箱 + SMTP 授权码（用于发送告警和周报）
- *(可选)* DeepSeek API Key（用于 LLM 总结功能）

### 1. 克隆 & 安装

```bash
git clone https://github.com/winnieAI123/model-radar.git
cd model-radar
python -m pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 用你的编辑器打开 .env，填入真实值
```

**必填项：**

| 变量 | 获取方式 |
|------|---------|
| `GITHUB_TOKEN` | [GitHub Settings → Tokens](https://github.com/settings/tokens) — 生成 classic PAT，勾选 `public_repo` |
| `EMAIL_SENDER` | 你的 163 邮箱地址 |
| `EMAIL_PASSWORD` | 163 邮箱 → 设置 → POP3/SMTP/IMAP → 开启服务并生成**授权码**（不是登录密码） |
| `EMAIL_RECEIVERS` | 收件人邮箱，多人用逗号分隔 |

**可选项：**

| 变量 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key，用于 Reddit 社区总结、榜单跨平台分析、周报 LLM 摘要 |
| `HF_TOKEN` | HuggingFace Token，提高 API 速率限制 |
| `DASHBOARD_PASS` | Dashboard 访问密码。留空 = 本地无密码模式 |
| `REDDIT_PROXY` | 本地调试用代理（Railway 美国机可直连，留空） |

### 3. 启动

```bash
# 推荐：FastAPI + Dashboard + 内嵌调度器（一体化）
python -m backend.api.main
# → 打开 http://localhost:8000/ 查看 Dashboard

# 或：仅 CLI 采集模式（无 Dashboard）
python -m backend.worker
```

首次启动会执行**冷启动**（全量采集一轮），之后按 `.env` 配置的间隔定时运行。

### 4. 手动触发周报

```bash
# 预览（不发邮件，仅归档）
python -m backend.engine.weekly_report

# 生成并发送
python -m backend.engine.weekly_report --send
```

---

## ☁️ Railway 部署

1. 在 [Railway](https://railway.app) 新建 Project → **Deploy from GitHub repo**
2. 添加 **Persistent Volume**，挂载到 `/app/data`（SQLite 数据持久化）
3. 在 **Variables** 中填入 `.env` 里的所有变量
4. Railway 读取 `Procfile` 自动启动：`uvicorn backend.api.main:app`
5. Settings → Networking → **Generate Domain** → 拿到公网地址
6. 健康探针：`GET /healthz`

---

## 📁 项目结构

```
model-radar/
├── backend/
│   ├── api/
│   │   ├── main.py                  # FastAPI 入口 + APScheduler 调度
│   │   ├── routes.py                # REST API: /api/alerts, /heat, /timeline, /status...
│   │   └── auth.py                  # Dashboard Basic Auth
│   ├── collectors/
│   │   ├── leaderboard_scrapers.py  # LMArena / Artificial Analysis / SuperCLUE 爬虫
│   │   ├── leaderboard.py           # 榜单采集适配层 (scrape → DB)
│   │   ├── github_monitor.py        # GitHub 组织新 Repo / Release 监控
│   │   ├── huggingface.py           # HuggingFace trending & downloads 采集
│   │   ├── openrouter.py            # OpenRouter API 用量周榜采集
│   │   ├── reddit.py                # Reddit 帖子采集 (JSON API)
│   │   └── blog_rss.py              # 厂商官方博客 RSS 采集
│   ├── engine/
│   │   ├── diff_engine.py           # 新旧快照对比 → 生成 change_events
│   │   ├── heat_scorer.py           # 多维热度评分
│   │   ├── alert_manager.py         # P0/P1 告警 (去重 + 邮件推送)
│   │   ├── leaderboard_digest.py    # 榜单数据聚合 + LLM 跨平台分析
│   │   ├── hf_digest.py             # HuggingFace 趋势摘要 + LLM 总结
│   │   ├── openrouter_digest.py     # OpenRouter 调用量分析 + LLM 总结
│   │   ├── release_digest.py        # GitHub Release 去噪 + LLM 分类/摘要
│   │   ├── reddit_opinions.py       # Reddit 按模型聚合社区观点 + LLM 提炼
│   │   ├── reddit_themes.py         # Reddit 热帖 LLM 归纳主题
│   │   ├── community_digest.py      # 社区综合摘要
│   │   ├── alias_learner.py         # 模型别名自动学习 (从 Reddit + HF 学习新名)
│   │   ├── leaderboard_summary.py   # 排名变动模板化描述
│   │   └── weekly_report.py         # 周报生成 (7 板块 HTML 渲染 + 邮件推送)
│   ├── config/
│   │   └── leaderboard.json         # 榜单源 & 赛道 & 厂商配置
│   ├── utils/
│   │   ├── config.py                # 环境变量统一读取 (dotenv)
│   │   ├── email_sender.py          # 163 SMTP 发送 + DoH DNS 降级 + 重试
│   │   ├── llm_client.py            # DeepSeek API 封装 (兼容 OpenAI 协议)
│   │   ├── model_alias.py           # 跨源模型名归一化 + pending_mapping
│   │   └── retry.py                 # 指数退避重试装饰器
│   ├── db.py                        # SQLite WAL + 建表 + 连接池
│   └── worker.py                    # CLI 模式入口 (仅采集调度)
├── frontend/
│   ├── index.html                   # Dashboard SPA 主页
│   ├── styles.css                   # 深色主题样式
│   └── app.js                       # 原生 JS，fetch 调用后端 API
├── data/                            # SQLite 文件 (被 .gitignore 排除)
├── requirements.txt                 # Python 依赖
├── Procfile                         # Railway 启动命令
├── railway.json                     # Railway 构建配置
└── .env.example                     # 环境变量模板
```

---

## 📊 数据采集频率（默认值）

| 数据源 | 间隔 | 说明 |
|--------|------|------|
| 榜单 (LMArena/AA/SuperCLUE) | 2h | 对比上一周快照检测排名变动 |
| GitHub 组织 | 1h | 检测新 Repo / Release |
| Diff 引擎 | 1h | 扫描快照生成 change_events |
| P0 告警发送 | 30min | 检查未发送的 P0 事件并推送 |
| 热度评分 | 3h | 重新计算多维热度分 |
| Reddit 采集 | 6h | 抓取 Top 帖 + LLM 分析 |
| HuggingFace | 4h | Trending 模型 + 下载量 |
| 厂商博客 RSS | 1h | OpenAI / Anthropic / Google / Meta |
| OpenRouter 周榜 | 7d | OpenRouter 本身是周粒度聚合 |

---

## 📬 周报内容

每周一自动生成的 HTML 邮件包含 7 个板块：

1. **🔴 本周关键信号** — P0/P1 事件（新模型登顶、新 Release 等）
2. **📦 新模型/开源发布** — 按 Repo 列出，含参数变化、突破点、论文链接
3. **📊 榜单变化** — 14 赛道 Top 5 + LLM 跨平台总结
4. **🤗 HuggingFace 趋势** — Top 10 + 下载量 + 社区热度 + NEW 标签
5. **🔌 OpenRouter 真实调用量** — 开发者生产环境 Token 消耗排行
6. **💬 社区声音** — Reddit 按模型聚合用户观点，附原帖链接
7. **💭 社区热议** — LLM 归纳 3-5 个讨论主题

---

## 🔧 技术特性

- **零前端构建** — Dashboard 用原生 HTML/CSS/JS，无 React/Vue，无 npm
- **完全独立** — 无外部路径依赖，换台机器填完 `.env` 就能跑
- **健壮邮件** — 163 SMTP + DoH DNS 降级（cloudflare/google）+ 3 次指数退避重试
- **冷启动过滤** — 首次扫描产生的 bootstrap 误报（存量 Repo 被当成"新增"）会被自动过滤
- **模型别名自愈** — alias_learner 自动从 Reddit/HF 学习新模型名，减少归一化遗漏
- **LLM 降级** — DeepSeek API 不可用时跳过摘要板块，不影响数据采集和告警

---

## 🗺️ 路线图

- [x] Phase 1：榜单 + GitHub + P0 邮件
- [x] Phase 2：FastAPI Dashboard + 模型名归一化 + 热度评分
- [x] Phase 3：Reddit 社区分析 + DeepSeek LLM 总结 + 周报
- [x] Phase 4：HuggingFace + 厂商博客 RSS + OpenRouter + 别名自动学习
- [ ] Phase 5：移动端推送 + 多维热度（5 维）+ 历史趋势图表

---

## 📄 License

MIT
