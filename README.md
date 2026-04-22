# ModelRadar · 模型雷达

为云服务团队设计的 AI 模型实时监控雷达。比公众号/媒体快 12-24 小时感知关键模型动态。

## 功能概览（当前 Phase 2）

- 实时监控 LMArena / ArtificialAnalysis / SuperCLUE 榜单（14 个赛道）
- 监控 6 大中国 AI 头部组织 (deepseek-ai / THUDM / MoonshotAI / MiniMax-AI / stepfun-ai / QwenLM) 的 GitHub repo 与 release
- 检测 P0 级变动（登顶榜单 Top 1 / 新仓库 / 新 Release）立即邮件告警，严格去重
- 跨源模型名归一化（`GPT-4o` / `openai/gpt-4o` / `gpt-4o-2024-05-13` → 同一个 canonical）
- 两维热度评分（榜单综合排名 + GitHub 24h star 增速）
- **Web Dashboard**：告警信号 / 热度 Top 10 / Timeline / 系统健康 / 待归一化名单，Basic Auth 保护
- 全部数据持久化到 SQLite（WAL 模式）

## 项目特性

- **完全独立**：无外部路径依赖，换台机器填完 `.env` 就能跑
- **零构建**：纯 Python，无前端框架，Railway NIXPACKS 自动部署
- **健壮邮件**：163 SMTP + DoH DNS 降级 + 3 次重试

## 快速开始

### 1. 克隆项目

```bash
git clone <this-repo> model-radar
cd model-radar
```

### 2. 装依赖（Python 3.10+）

```bash
python -m pip install -r requirements.txt
```

### 3. 填环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的 GITHUB_TOKEN / EMAIL_* 等
```

#### 必填项说明

| 变量 | 获取方式 |
|------|---------|
| `GITHUB_TOKEN` | https://github.com/settings/tokens - 生成 classic PAT，勾选 `public_repo` 只读即可 |
| `EMAIL_SENDER` | 163 邮箱地址 |
| `EMAIL_PASSWORD` | 163 邮箱 → 设置 → POP3/SMTP/IMAP → 开启服务并生成授权码（不是登录密码） |
| `EMAIL_RECEIVERS` | 收件人列表，逗号分隔 |
| `DASHBOARD_PASS` | Dashboard Basic Auth 密码（留空 = 本地无密码模式） |

### 4. 本地运行

```bash
python -m backend.api.main
# 打开 http://localhost:8000/ 查看 Dashboard
# 如设置了 DASHBOARD_PASS，浏览器会弹 Basic Auth，用户名默认 radar
```

进程是 FastAPI + 内嵌 APScheduler（合并 service），首次启动会执行一次冷启动（全量采集），之后按 `.env` 里的间隔定时跑。

不想起 Dashboard 只跑采集，用旧入口：`python -m backend.worker`（仅 CLI 模式，调度等价）。

### 5. Railway 部署

1. 在 Railway 新建 project → Deploy from GitHub repo
2. 加 Persistent Volume 挂载到 `/app/data`
3. 在 Variables 里填 `.env` 里的所有变量（含 `DASHBOARD_PASS`）
4. Railway 会读 `Procfile` 自动启动：`uvicorn backend.api.main:app`
5. 点 Settings → Networking → Generate Domain 拿到公网域名，访问 `/` 就是 Dashboard
6. 健康探针：`/healthz`

## 项目结构

```
model-radar/
├── backend/
│   ├── api/
│   │   ├── main.py                  # FastAPI 入口 + 内嵌 APScheduler
│   │   ├── routes.py                # /api/alerts /heat /timeline /status /pending-mapping
│   │   └── auth.py                  # Basic Auth 依赖
│   ├── worker.py                    # CLI 模式（仅采集，不起 Dashboard）
│   ├── db.py                        # SQLite 连接 + 建表 + WAL
│   ├── config/
│   │   └── leaderboard.json         # 榜单源配置
│   ├── collectors/
│   │   ├── leaderboard_scrapers.py  # 三源榜单 scraper（LMArena/AA/SuperCLUE）
│   │   ├── leaderboard.py           # 榜单采集适配层（scrape → DB）
│   │   └── github_monitor.py        # GitHub 6 组织监控
│   ├── engine/
│   │   ├── diff_engine.py           # 变动检测（新旧快照对比）
│   │   ├── heat_scorer.py           # 两维热度评分
│   │   ├── leaderboard_summary.py   # 排名变动模板化描述
│   │   └── alert_manager.py         # P0 告警（去重 + 邮件）
│   └── utils/
│       ├── config.py                # dotenv 读取
│       ├── retry.py                 # 指数退避装饰器
│       ├── email_sender.py          # 163 SMTP + DoH 降级
│       └── model_alias.py           # 跨源模型名归一化 + pending_mapping
├── frontend/
│   ├── index.html                   # Dashboard 主页
│   ├── styles.css                   # 深色主题
│   └── app.js                       # 原生 fetch，无构建
├── data/                            # SQLite 文件（Railway Volume 挂载点）
├── requirements.txt
├── Procfile                         # Railway 启动命令（uvicorn）
├── railway.json
└── .env.example
```

## 路线图

- [x] MVP：榜单 + GitHub + P0 邮件
- [x] Phase 2（当前）：FastAPI + Web Dashboard + 模型名归一化 + 热度评分
- [ ] Phase 3：Reddit 社区声音 + DeepSeek 总结 + 周一周报
- [ ] Phase 4：HuggingFace + 技术博客 RSS + 完整 5 维热度评分
- [ ] Phase 5：移动端 + SQLite 自动备份 + 系统健康页

## License

Internal use only.
