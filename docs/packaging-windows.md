# 打包成 Windows exe 安装程序

这篇只讲一件事：**把「投石」变成一个能双击安装的 Windows 安装包**。
macOS 那条线（.app / .dmg）脚本是同构的，差异处会单独标注。

---

## TL;DR

| 你想干什么 | 怎么做 | 前置 |
|---|---|---|
| **出可发布的安装包**（推荐） | push 一个 v* tag，GitHub Actions 自动出 NSIS + MSI + DMG 草稿 release | 什么都不用装 |
| 本机自己打一个 | pwsh -File scripts/build_standalone.ps1 | Rust + VS Build Tools（本机目前没装） |
| 只要个能跑的 exe，不要安装器 | 同上，但产物取 target/bundle-release/toushi.exe | 同上；注意它**离不开同目录的 python313.dll** |

**但在动手之前**：先看第三节的两个 blocker。照现在这样直接打，装出来的 App 会在「上传简历后第一次生成招呼语」时崩掉。

---

## 一、已经有的家当

打包这条流水线仓库里已经有了，不用从零开始：

| 文件 | 作用 |
|---|---|
| scripts/build_standalone.ps1 | Windows 全流程：拉嵌入式 Python → 装业务包 → 编前端 → tauri build → 产物自检 |
| scripts/build_standalone.sh | macOS 同构版本（多了 libpython 的 install_name 修补） |
| src-tauri/tauri.bundle.json | 用 --config 叠加到 tauri.conf.json，把 pyembed/python 作为 resources 打进包 |
| .github/workflows/release.yml | 打 tag 后在 windows-latest + macos-latest 上各跑一次上面的脚本，产物进**草稿** release |
| scripts/set_version.py | 把 tag 的版本号灌进 pyproject / Cargo.toml / tauri.conf.json / Tauri.toml 四处 |

**原理**（一句话）：把 python-build-standalone 的嵌入式解释器 + 你的业务包一起放进安装目录，
Rust 二进制启动时用 PythonInterpreterBuilder 把它拉起来，再执行 toushi.tauri 模块。
用户机器上**不需要装 Python**，也不需要装 Node。

---

## 二、三条路线

### A. 交给 GitHub Actions（推荐）

仓库里 release.yml 已经写好了，你只需要打 tag：

    git tag v0.7.0
    git push origin v0.7.0

约 15 分钟后 Actions 里会出现一个 **draft release**，挂三个产物：

- Toushi_0.7.0_x64-setup.exe   ← NSIS 安装器，这就是你要的
- Toushi_0.7.0_x64_en-US.msi   ← WiX/MSI
- Toushi_0.7.0_aarch64.dmg     ← macOS

看完安装界面、确认版本号，再点 Publish。**草稿是刻意的**：CI 不直接发布，防止错版本产物跑出去。

注意事项：

- tag 必须是 v 开头，set_version.py 从它取版本号；仓库里 committed 的版本号只是本地占位。
- 免费额度：公开仓库的 Actions 分钟数不计费；私有仓库 Windows runner 消耗 2x 分钟。
- 第一次跑要下 torch 等大轮子，慢（30-50 分钟）；release.yml 里给 uv 和 cargo 都配了 cache，第二次会快很多。

### B. 本机自己打

前置（按顺序装）：

1. **Rust**：https://rustup.rs → rustup default stable（需要 >= 1.78，脚本会检查，低了直接 fail-fast）
2. **Visual Studio Build Tools**，勾 "Desktop development with C++"（要 MSVC 链接器；只装 VS Code 不算）
3. **pnpm**：npm i -g pnpm（现在这台机器上跑的是 DSH 自带的临时 pnpm，不建议依赖它）
4. WebView2 Runtime：Win11 自带；Win10 需要装 Evergreen Runtime（安装器会自动带上）

然后：

    pwsh -ExecutionPolicy Bypass -File scripts/build_standalone.ps1

想强制重建嵌入式 Python：先设环境变量 BOSS_PYEMBED_REBUILD=1 再跑。

**当前这台机器缺 cargo / rustc**，所以这条路线现在跑不了。

### C. 只要 raw exe

不推荐单独分发 toushi.exe：python313.dll、Lib/、models/ 都靠安装目录的相对位置找，
拷一个 exe 出去必然起不来。要"绿色版"得把整个 bundle-release 目录打包成 zip，
而且路径里不能有空格（见第九节）。

---

## 三、开工前必须修的两个 blocker

### Blocker 1：嵌入模型路径在 wheel 里会失效

src/toushi/vectorization.py 现在是这么定位模型的：

    _PROJECT_ROOT = Path(__file__).resolve().parents[2]
    _DEFAULT_HF_HOME = _PROJECT_ROOT / "models"
    EMBED_MODEL_NAME = str(_LOCAL_MODEL_SNAPSHOT)
    embedder = SentenceTransformer(EMBED_MODEL_NAME, local_files_only=True)

在仓库里 src/toushi/vectorization.py 往上两级正好是仓库根目录 —— 本地能跑。
但**装成 wheel 之后**它在 Lib/site-packages/toushi/vectorization.py，
parents[2] 变成 Lib/，模型路径指向一个不存在的地方；
local_files_only=True 又不允许联网兜底，于是直接抛异常。
表现就是：**App 能开、能填表，一点「开始」生成招呼语就崩**。

建议改法（最小改动）：

1. 模型目录按优先级解析：BOSS_MODEL_DIR 环境变量 → 应用数据目录下 models/ → 仓库根 models/。
   前两者覆盖"装完之后"，最后一个保住本地开发。
2. local_files_only 保持 True（离线优先，也避免用户机器上偷偷下几百 MB），
   但**找不到时给一句人话报错**，而不是让 OSError 冒到日志里。
3. 安装器把模型作为 resource 拷进应用目录。

### Blocker 2：模型没被打进安装包

src-tauri/tauri.bundle.json 现在只声明了嵌入式 Python，**没有 models/**：

    { "bundle": { "active": true, "targets": "all",
      "resources": { "pyembed/python": "./" } } }

要么按下面这样加进去，要么走第四节说的瘦身方案（换 ONNX，模型缩到约 80 MB）。
加的话建议只加**去重后的快照目录**，别整个 models/hub 拷进去：

    "resources": {
      "pyembed/python": "./",
      "models/hub/models--sentence-transformers--all-mpnet-base-v2/snapshots": "./models/"
    }

（Windows 上 HuggingFace 缓存是**复制**不是硬链接，所以 models/ 目录的 837 MB 里
有 418 MB 是重复的：blobs 一份、snapshots 一份。真正要打进去的是 418 MB。）

---

## 四、体积账

现在直接打的话：

| 部分 | 体积 |
|---|---|
| torch 2.12.1（PyPI 的 Windows 轮子是 **CPU-only**，不是 2.5 GB 的 CUDA 版） | 468 MB |
| transformers / tokenizers / scipy / chromadb / onnxruntime 等 | 约 550 MB |
| all-mpnet-base-v2 模型（safetensors） | 418 MB |
| **合计（解压后）** | **约 1.4 GB** |
| **NSIS 安装包（LZMA 压缩后）** | **约 400–550 MB** |

能接受，但不优雅。**想砍到 200 MB 左右**，唯一的大头是换掉嵌入后端：

| 方案 | 省下 | 代价 |
|---|---|---|
| 用 Chroma 自带的 ONNX 默认嵌入（all-MiniLM-L6-v2，约 80 MB，onnxruntime 本来就在依赖里） | torch 468 MB + 模型 340 MB + transformers 链约 150 MB ≈ **950 MB** | 向量质量略降；**已有的 vectorstores/ 必须重建**（维度/空间都变了） |
| 保留 mpnet，但只打 safetensors | 0（已经是这样） | — |
| 保留现状 | — | 安装包 400–550 MB |

我的建议：**先按现状出一版能装的**，把流程打通；瘦身作为独立的一步来做
（换后端要连带处理 COLLECTION_NAME 版本号 + 老向量库迁移，不该和"第一次打包"混在一起）。

---

## 五、图标（已接好）

- 母版：assets/app-icons/*.svg（9 个候选）+ preview.png 对照表
- 生成器：uv run python scripts/set_app_icon.py 05-target
  一次写出 16 个 PNG + 多尺寸 icon.ico（16/24/32/48/64/128/256）+ icon.icns，
  同时落**两处**：src-tauri/icons/（standalone 用）和 src/toushi/tauri/icons/（wheel dev 用）。
- 配置：src-tauri/tauri.conf.json 与 src/toushi/tauri/Tauri.toml 的 bundle.icon 都指向各自那份；
  pyproject.toml 的 package-data 已把 icons/* 打进 wheel。

安装器里的图标、开始菜单图标、任务栏图标、窗口左上角图标，全部来自这几个文件，改图标只重跑脚本。

---

## 六、NSIS 还是 MSI

tauri.conf.json 里 "targets": "all"，Windows 上两份都出：

| | NSIS（*-setup.exe） | MSI（.msi） |
|---|---|---|
| 适合 | 个人用户直接双击 | 企业批量部署 / GPO |
| 装到哪 | 默认 %LOCALAPPDATA%/Toushi（用户级，不需要管理员） | 可指定 Program Files |
| 卸载 | 控制面板里有 | 同 |
| 体积 | 略小 | 略大 |

**给个人用户发就用 NSIS 那个 -setup.exe。** 想只出一种，把 bundle.targets 从 "all" 改成 ["nsis"]，
能顺带省掉 WiX 的下载和编译时间（CI 每次要拉 WiX 工具集）。

---

## 七、签名与 SmartScreen

不签名的话，用户第一次运行会看到：
**「Windows 已保护你的电脑」→ 更多信息 → 仍要运行**。功能正常，只是观感差。

- **代码签名证书**：OV 证书（约 1000-2000 元/年）能去掉大部分警告；
  EV 证书（更贵、要硬件 token）才有"立即建立信誉"的效果。个人项目通常不值得。
- **不签也能发**：在 README / release notes 里把"点更多信息→仍要运行"写清楚，
  比让用户自己猜好得多。
- 如果以后要签，tauri.conf.json 的 bundle.windows.certificateThumbprint +
  digestAlgorithm 填上，证书放 CI secret 里。

---

## 八、发布前验收清单

按这个顺序走一遍，每步都能独立发现问题：

1. **安装**：双击 -setup.exe，装完能在开始菜单找到「投石 · Toushi」，图标是新的
2. **启动**：不装 Python、不装 Node 的干净机器上双击能起（这是嵌入式 Python 是否生效的唯一验证）
3. **图标**：任务栏 + 窗口左上角是同一张
4. **配置页**：填 API key → 保存 → 重开 App 还在（验证 .env 落在应用数据目录）
5. **上传简历**：拖 PDF 进去 → 显示文件名（这一步还不需要模型）
6. **点开始（Dry Run 开）**：能走到"生成招呼语" —— **这一步才真正验证嵌入模型和 LLM 都通了**
7. **检查加载的 DLL**：Process Explorer 里看 python313.dll 的路径是不是安装目录下那份
   （拿错版本是这套方案最经典的翻车方式）
8. **卸载**：控制面板卸载后，%LOCALAPPDATA%/Toushi 里的用户数据保留与否，符合你的预期

---

## 九、已知坑（脚本里已经处理，别改回去）

| 坑 | 现象 | 现有处理 |
|---|---|---|
| 没设 PYO3_PYTHON | pyo3 去链系统 Python，装完 LoadLibrary 失败 | 脚本显式指向 pyembed 的 python.exe |
| 没设 RUSTFLAGS=-L native=&lt;libs&gt; | 链接期找不到 python313.lib | 脚本设置；**前提是仓库路径没有空格**（RUSTFLAGS 按空格切） |
| pyembed 里的 BUILD 标记文件 | Windows 大小写不敏感，撞上 cargo 的 build 目录 → os error 5 拒绝访问 | 脚本删掉它（Python 运行时不读这个文件） |
| uv pip install 被 PEP 668 拦 | "externally managed environment" | 加 --break-system-packages |
| tauri CLI 找不到 src-tauri/ | 从 tauri-ui/ 跑会找不到配置 | 脚本从仓库根目录调 tauri-ui/node_modules/.bin/tauri |
| 缓存里的旧安装包 | CI 上传 glob 匹配到上个版本的产物 | release.yml build 前先 rm -rf bundle-release/bundle |

还有一个**没处理**的：脚本里这句

    uv pip install --exact --compile-bytecode --break-system-packages "$RepoRoot[standalone]"

是**绕开 uv.lock 现场解析依赖**的。也就是说本地 uv sync 装的版本和安装包里装的版本
可能不是同一套（torch / transformers 这类大包升级时尤其明显）。
想锁死，可以改成从 lockfile 导出的 requirements：

    uv export --frozen --no-dev --no-emit-project --extra standalone -o requirements-standalone.txt
    uv pip install --exact --break-system-packages --python &lt;pyembed&gt; -r requirements-standalone.txt

要不要现在就改，取决于你多介意"本地能跑、装出来跑不了"这类问题。第一次打包建议先按原样跑通。

---

## 十、建议顺序

1. 先修 Blocker 1 的模型路径（**必须先修，不然装出来是废的**）
2. 模型加进 bundle.resources
3. 打 tag 走一遍 CI，拿到 -setup.exe
4. 在**另一台没装过 Python 的机器**上按第八节验收
5. 流程通了之后，再单独开一个 PR 做 ONNX 瘦身 + 签名
