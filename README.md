# 投石 · Toushi

> **投石问路** —— 读岗位 JD，用 LLM 判断「这个岗位跟我简历到底配不配」，配得上才打招呼 / 投递。
> 招呼语由 LLM 结合你的简历（RAG 召回）生成，**发送前必过校验**，防止把模型抽风的输出发给真招聘者。

支持 **BOSS 直聘**（打招呼 + 发招呼语）和 **智联招聘**（只投递简历、不发消息）。
桌面端（PyTauri）和命令行都能跑，LLM 侧走**任意 OpenAI 兼容端点** —— DeepSeek / OpenAI / Claude / 通义千问·百炼 / 智谱 GLM / 豆包 / Kimi / 硅基流动 / 本地 Ollama / 任意中转，配好 `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` 三个变量就起跑。

---

## 这个仓库是怎么来的

- **起因很朴素：我自己要用。** 求职那阵子每天要翻几十个岗位——点开、读 JD、判断配不配、再想开场白，重复劳动占掉大半时间，于是开始写脚本。
- **改着改着就改多了。** 加了智联招聘、加了 LLM 岗位匹配打分、加了招呼语发送前校验、加了风控识别与页面自愈、加了桌面 App 和成本统计……跟最开始那份脚本已经不是一个东西了。
- **既然都改了这么多，就整理出来分享。** 你用得上最好；用不上，也可以单独看看其中某一块是怎么做的。
- **欢迎提交 issue。** 跑不通、有想法、想要某个功能，都直接开 issue 说。不过麻烦先读一遍下面这两节和 FAQ —— 很多坑那里已经写过了。
- 这个仓库我本人一直在用，所以会长期维护；但**不接"提高频率 / 并发多开 / 绕风控"这一类需求**，理由见下。

---

## ⚠️ 先说清楚：这是个什么项目，不是什么项目

- **它是**：一个**个人求职辅助工具**。帮你把「一份份点开岗位、看 JD、判断配不配、想开场白」这件重复劳动自动化，把省下的时间留给你真正想聊的那几家。
- **它不是**：爬虫、批量灌水工具、也不是"绕过平台风控"的研究项目。
- **所以本仓库明确不做**：提高发送频率、并发/多开、模拟人类行为骗过风控、破解验证码之类的改动。BOSS 上的反爬越激进，对真实求职者伤害越大 —— 这个脚本不希望成为推手。
- 请勿用它割韭菜。能被逼到用脚本投简历的人，身上没啥油水可榨。
- 使用者需自行遵守各招聘平台的服务条款。因使用本工具导致的账号风险由使用者自负。

---

## 功能特性

| 能力 | 说明 |
|---|---|
| **双平台** | BOSS 直聘（打招呼 + 发招呼语）、智联招聘（只投递简历，不发消息） |
| **多层筛选** | 黑名单 → 简历关键词命中 → 向量语义粗筛 → **LLM 打分**；只有超过阈值的岗位才会被操作 |
| **RAG 招呼语** | 从简历向量库召回与 JD 最相关的片段，拼进 prompt 生成中文招呼语 |
| **发送前校验** | 长度区间 + 必须含中文 + 黑名单（`Error` / `As an AI` / `````` …），不通过只记日志不发 |
| **Dry Run** | 只生成、只记录、**一次都不点发送/投递**，用来调 prompt 和阈值 |
| **桌面 App** | PyTauri + React + Tailwind。左侧导航 + 底部状态栏常驻；运行页 = 参数/简历卡片 + 实时事件时间线 + 日志终端；配置页按用途分组；历史页可过滤 / 搜索并展开岗位预览 |
| **深浅色主题** | 跟随系统，顶栏一键切换。全站颜色走 CSS 变量，深色就是同一套排版的墨色反转 |
| **节流** | 岗位之间随机间隔（默认 2~6s）、发送后随机等待（默认 10~60s）、单次运行条数上限 |
| **审计日志** | `logs/letters.jsonl`（每条招呼语 + 校验结果 + 是否发出）、`logs/llm_calls.jsonl`（token / 延迟 / 估算成本） |
| **自愈** | 页面整页反复重载、CDP session 失效、虚拟列表重渲染…… 都能识别并给出人话提示，而不是空转 |

---

## 工作原理

```text
┌─ 启动 Chrome（nodriver，独立 profile，不动你的日常浏览器）
│
├─ 登录态检查 ── 未登录 → 明确停下并提示你扫码（不会拿游客 feed 空跑）
│
├─ 选中求职 tag（BOSS）→ 进入推荐 feed
│
└─ 逐个岗位循环：
     抓 JD
       │
       ├─ 黑名单命中？──────────────→ 跳过
       ├─ 简历关键词命中不足？──────→ 跳过
       ├─ 向量语义不相关？──────────→ 跳过
       ├─ LLM 打分 < 阈值？─────────→ 跳过
       │
       ▼ 通过
   BOSS：生成招呼语 → 校验 → 点「立即沟通」→ 写入输入框 → 点「发送」→ 回列表
   智联：点「立即投递」→ 关掉投递后的确认弹窗 → 留在列表
       │
       └─ 随机间隔 → 下一个岗位
```

---

## 快速开始

### 0. 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.11 | |
| [uv](https://docs.astral.sh/uv/) | 最新 | 依赖与虚拟环境管理 |
| Google Chrome | 较新版本 | 自动化跑在独立 profile 上 |
| Node.js | ≥ 18 | **桌面 App 必需**：前端产物不入库，需本地构建一次（只用命令行的话可以不装） |

### 1. 安装

```bash
git clone https://github.com/accten/toushi-job.git
cd toushi-job
uv sync                     # 安装全部依赖（含桌面 App 的 tauri 组）
# 只要命令行、不要桌面 App：
# uv sync --no-group tauri

# 桌面 App 还要构建一次前端（产物落在 src/toushi/tauri/frontend/）
cd tauri-ui && npm install && npm run build && cd ..
```

### 2. 配置

```bash
cp .env.example .env
```

最少要填这些：

```ini
# —— LLM 端点（任意 OpenAI 兼容端点）——
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-xxxxxxxx

# —— 求职者信息 ——
BOSS_USR_NAME=你的名字
RESUME_PATH=./resume/简历.pdf
BOSS_LABEL=后端开发(贵阳)      # BOSS 上真实存在的求职 tag（见下方 FAQ）
```

**常用端点预设**（GUI 配置页选一个会自动填 base_url + model，代码不认牌子）：

| 平台 | `LLM_BASE_URL` | 示例 `LLM_MODEL` |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| Anthropic | `https://api.anthropic.com/v1/` | `claude-sonnet-4-6` |
| 通义千问·百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` |
| 本机 Ollama | `http://localhost:11434/v1` | `llama3` |

> ⚠️ **不要用 reasoning 模型**（deepseek-r1 / nemotron / 各种 thinking）：它们的思维链会被当成正文返回，运气不好就原样发给招聘者了。也**别用带 `:free` 的免费额度模型** —— 实测限流严重、上游经常过载，招呼语会直接生成失败。

### 3. 首次登录（重要）

招聘平台的职位页未登录会跳登录页，而且**挂着自动化调试端口的浏览器会被风控反复送去做安全验证**（登录二维码根本来不及扫）。所以登录这一步刻意不碰自动化：

- **桌面 App**：运行页把平台切到对应平台 → 点 **「登录」** → 会开一个**干净 Chrome**（不带任何调试端口，用的还是脚本那份 profile）→ 在里面扫码/输验证码 → **关掉这个窗口** → 再点「开始」。
- **命令行**：自己用同一个 profile 开一个普通 Chrome 登录：

  ```bash
  # BOSS
  chrome --user-data-dir="<仓库目录>/chrome_profile"
  # 智联
  chrome --user-data-dir="<仓库目录>/chrome_profile_zhilian"
  ```

登录成功后 cookie 留在 profile 里，之后就不用再扫了（除非平台把会话踢了，或触发风控）。

### 4. 运行

```bash
# 桌面 App（推荐，能实时看日志/进度/成本）
uv run python -m toushi.tauri
# 或根目录的兼容入口（同样启动桌面 App）
uv run main.py

# 命令行
uv run python -m toushi.cli --platform boss      # BOSS：发招呼语
uv run python -m toushi.cli --platform zhilian   # 智联：只投递

# 先干跑一遍（只生成不发送，强烈建议第一次这么跑）
DRY_RUN=1 uv run python -m toushi.cli --platform boss
```

---

## 参数详解：怎么控制"投多少、给谁投"

| 变量 | 默认 | 作用 |
|---|---|---|
| `BOSS_MIN_MATCH_SCORE` | `70` | **LLM 匹配分阈值**，低于它直接跳过。分数由 LLM 结合简历与 JD 给出（0~100） |
| `BOSS_MIN_KEYWORD_MATCH` | `2` | 进入 LLM 评分前，JD 至少要命中几个简历关键词。嫌跳过太多可以调低到 1 |
| `BOSS_EXCLUDE_KEYWORDS` | 空 | 岗位黑名单，逗号分隔，命中即跳过。例：`外包,驻场,派遣` |
| `BOSS_AUTO_SEND_MAX_SENT` | `50` | **单次运行最多投递/发送多少条**。建议第一次设 5~10 |
| `BOSS_AUTO_SEND_DELAY_MIN` / `MAX` | `10` / `60` | 每次**发送**之后的随机等待（秒） |
| `BOSS_JOB_GAP_MIN` / `MAX` | `2` / `6` | **岗位之间**的随机间隔（秒）。固定节拍最容易被判定为机器行为，所以这里刻意随机 |
| `BOSS_LOGIN_TIMEOUT` | `300` | 等待扫码登录的秒数；超时会明确报错停下 |
| `BOSS_LETTER_PROMPT` | 内置默认 | 自定义招呼语 system prompt，`{usr_name}` 会替换成你的名字 |
| `BOSS_DRY_RUN` / `DRY_RUN` | 空 | 非空即 Dry Run：不发送、不投递，只生成和记录 |

智联相关：

| 变量 | 默认 | 作用 |
|---|---|---|
| `ZHILIAN_CHROME_PROFILE` | `./chrome_profile_zhilian` | 智联用的独立 Chrome profile |
| `ZHILIAN_RECOMMEND_URL` | `https://www.zhaopin.com/recommend` | 智联岗位列表页（**不是首页**，首页上没有岗位卡） |

> 智联的岗位页未登录会 302 到 `passport.zhaopin.com/login`，所以必须先登录。

---

## 目录结构

```text
.
├── main.py                     # 兼容入口，委托桌面 App
├── pyproject.toml / uv.lock    # 依赖（uv 管理）
├── .env.example                # 环境变量样板
├── src/toushi/
│   ├── cli.py                  # CLI 入口 + 平台配置（BOSS / 智联）
│   ├── providers.py            # LLM 端点预设（只是便利，不是支持范围）
│   ├── audit/                  # 招呼语校验（validate_letter）+ 审计日志 + telemetry
│   ├── models/
│   │   ├── llm.py              # OpenAI 兼容端点 client + RAG 招呼语生成
│   │   ├── prompts.py          # 招呼语 prompt 模板
│   │   └── job_matcher.py      # 多层过滤：黑名单 / 关键词 / 向量 / LLM 评分
│   ├── vectorization.py        # 简历 PDF → 向量库 → 召回
│   ├── website_oper/
│   │   ├── finding_jobs.py     # BOSS 浏览器自动化（nodriver）
│   │   ├── zhilian_finding_jobs.py  # 智联浏览器自动化
│   │   └── write_response.py   # 主循环：抓 JD → 过滤 → 生成 → 校验 → 发送/投递
│   ├── gui/                    # 桌面 App 胶水层（事件 / runner / 日志桥 / env 读写）
│   └── tauri/                  # PyTauri 桌面 App（含构建好的前端）
├── tauri-ui/                   # 桌面 App 前端源码（React + Vite + Tailwind + lucide-react）
├── tests/                      # pytest 单测
├── scripts/                    # 诊断 / 侦察脚本
└── docs/wiki/                  # 中文 wiki + ADR（架构决策记录）
```

---

## 开发与测试

```bash
uv run pytest tests/ -v                 # 全部单测
BOSS_RETRY_BASE_DELAY=0.01 uv run pytest tests/ -v   # 加速 retry 相关用例

# 改前端后要重新构建（产物落在 src/toushi/tauri/frontend/）
cd tauri-ui && npm install && npm run build
```

改浏览器自动化代码后，**务必**本地 `DRY_RUN=1` 真跑一遍确认能走到生成招呼语那一步 —— 这部分不靠 mock 测试（mock 出来的东西在生产里 100% 不能用）。

关键设计决策（为什么是 nodriver 而不是 Selenium、为什么统一 OpenAI 兼容端点、为什么 telemetry 单独落盘……）都记在 `docs/wiki/adr/`。

---

## 常见问题

**Q：桌面 App 打开是白屏？**
WebView2 需要往 `%LOCALAPPDATA%<app-id>\EBWebView` 写用户数据目录。如果被安全软件/沙箱拦住，窗口就只有标题栏、内容全白。用普通权限从仓库目录启动即可。

**Q：桌面 App 启动报错 / 窗口里什么都没有，但命令行能跑？**
八成是漏了前端构建。前端产物被 `.gitignore` 排除、不在仓库里，克隆后必须自己跑一次 `cd tauri-ui && npm install && npm run build`。

**Q：页面一直在整页刷新，日志刷屏？**
这是平台的风控安全验证（BOSS 上表现为 `security.html` / `_security_check` 来回跳）。脚本识别到会明确报错停下。**处理办法**：按上面的「首次登录」用干净窗口登录并人工过掉验证；别在那种状态下反复重跑，每次重跑都是给风控"续杯"。

**Q：岗位列表一直"没渲染出来"？**
多数是 profile 的 HTTP 缓存里存了 SPA 脚本的坏响应。删掉 `chrome_profile/Default/Cache` 和 `Default/Code Cache` 即可（**别删 `Default/Network`**，cookie 在里面）。

**Q：选了智联，跑起来却是 BOSS 的页面？**
先看日志第一行 `本次运行平台：xxx`。这条日志就是为排查这类问题加的。

**Q：BOSS 的 tag 筛选不生效 / feed 里全是无关岗位？**
`BOSS_LABEL` 必须填页面上**真实存在**的求职 tag（如 `后端开发(贵阳)`）。日志里会把当前可见的 tag 都列出来，照抄一个即可。想搜"算法"得先去 BOSS 网站上添加这条求职期望。

**Q：LLM 生成的招呼语是空的 / 是一段英文思考过程？**
换个正经的对话模型。reasoning 模型会把思维链当正文返回，免费额度模型（`:free`）则经常直接超时/过载。

**Q：招呼语生成正常，但一条都没发出去？**
看 `logs/letters.jsonl` 的 `validation_reasons`：`too_long` / `too_short` / `no_chinese_characters` / `blacklist:*`。也可以在 GUI 历史页直接看每条招呼语和成本。

---

## 致谢

### 上游项目

「投石」不是从零开始的，它脱胎于 **BOSS 直聘自动打招呼脚本**：

- 仓库：[**longsizhuo/BossZhiPin_Job_Search**](https://github.com/longsizhuo/BossZhiPin_Job_Search)
- 作者 / 维护者：[@longsizhuo](https://github.com/longsizhuo)、[@TinyAlmond](https://github.com/TinyAlmond)

沿用了上游的这些东西，功劳属于原作者：浏览器自动化骨架（`nodriver` 选型、sync facade + async impl
的桥接方式）、`validate_letter` 招呼语校验的思路与规则、prompt 骨架、`.env` 配置约定、
持久化 Chrome profile 的登录态方案，以及中文 wiki / ADR 的写法。

智联招聘适配、LLM 岗位匹配打分、页面重载与 CDP 会话自愈、岗位间随机节流、前端界面重做、
成本统计等，是本仓库新增或重写的部分。

> **原仓库仍在维护。** 如果你只需要 BOSS 直聘那一半，直接用它就好；顺手给它点个 star ——
> 没有它就没有这个项目。上游的部分 commit 链接仍指向原仓库，这是有意保留的，方便对照改动来源。

### 依赖

- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) —— CDP 直连的浏览器自动化
- [Chroma](https://www.trychroma.com/) + [sentence-transformers](https://www.sbert.net/) —— 简历向量召回
- [PyTauri](https://pytauri.github.io/) / [Tauri](https://tauri.app/) —— 桌面 App
- [Tailwind CSS](https://tailwindcss.com/) / [Lucide](https://lucide.dev/) —— 界面

## License

见 [LICENSE](LICENSE)。
