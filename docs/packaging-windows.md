# 打包成 Windows exe 安装程序

这篇讲怎么把「投石」变成一个能双击安装的 Windows 安装包，以及**踩过的坑**。
里面的数字都是 CI 上真跑出来的，不是估的。

---

## TL;DR

| 你想干什么 | 怎么做 | 产物 |
|---|---|---|
| **出可发布的安装包**（推荐） | push 一个 v* tag，GitHub Actions 自动构建 | 草稿 release 里挂 NSIS 安装器 + macOS dmg |
| 本机自己打 | pwsh -File scripts/build_standalone.ps1 | 需要 Rust + VS Build Tools |
| 只要个能跑的 exe | 不推荐，见下 | 缺 DLL 必然起不来 |

**用户机器上不需要装 Python，也不需要装 Node。** 嵌入式解释器 + 你的包一起进安装目录，
Rust 二进制启动时把它拉起来，再执行 toushi.tauri 模块。

---

## 一、出包流程

    git tag v0.7.0
    git push origin v0.7.0

约 25-40 分钟后 Actions 里出现一个 **draft release**，挂着：

- Toushi_0.7.0_x64-setup.exe（NSIS 安装器，约 450MB）
- Toushi_0.7.0_aarch64.dmg（macOS）

**草稿是刻意的**：CI 不直接发布，防止错版本产物跑出去。看一眼再点 Publish。

⚠️ 重跑同一个 tag 要先删掉本地 tag 再重建（远端已存在时 push 需要 --force）：

    git tag -d v0.7.0 && git tag -a v0.7.0 -m 'Toushi v0.7.0'
    git push --force origin v0.7.0

### 相关文件

| 文件 | 作用 |
|---|---|
| scripts/build_standalone.ps1 | Windows 全流程：拉嵌入式 Python → 装业务包 → 编前端 → tauri build → 自检 |
| scripts/build_standalone.sh | macOS 同构版本（多一步 libpython 的 install_name 修补） |
| src-tauri/tauri.bundle.json | 用 --config 叠加到 tauri.conf.json，把 pyembed/python 作为 resources 打进包 |
| .github/workflows/release.yml | 打 tag 后在 windows-latest + macos-latest 各跑一次，产物进草稿 release |
| scripts/set_version.py | 把 tag 的版本号灌进 pyproject / Cargo.toml / tauri.conf.json / Tauri.toml |
| scripts/set_app_icon.py | 铺开应用图标（PNG / ico / icns） |

---

## 二、体积账（实测）

| 部分 | Windows | macOS |
|---|---|---|
| pyembed/python（解释器 + 所有依赖） | 1352 MB | 1526 MB |
| 模型（**不进包**，用户首次使用时下） | — | — |
| tauri-ui/node_modules（不进包，只是诊断打印） | 121 MB | 138 MB |
| **NSIS 安装器（压缩后）** | **约 450 MB** | dmg 类似量级 |

大头是 torch（约 470MB）和 transformers 那条链。想再砍一半，唯一的路是换成
**Chroma 自带的 ONNX 嵌入**（all-MiniLM-L6-v2，约 80MB，onnxruntime 本来就在依赖里），
能省掉 torch + transformers + 大部分模型体积，约 950MB。代价是向量质量略降，
并且已有的 vectorstores/ 必须重建（维度变了）。**没做**，先记在这里。

---

## 三、模型不再随包分发

模型（all-mpnet-base-v2，约 418MB）**不打包**，改由 App 首次使用时下载：

- 后端：toushi/vectorization.py 的 download_model()，下到应用数据目录
  （%APPDATA%/com.longsizhuo.toushi/models），走 huggingface_hub.snapshot_download，
  固定 revision，只拉推理要的文件（config / tokenizer / model.safetensors）。
- 前端：运行页挂载时问一次 get_embed_model_status，缺了就显示引导卡片 +
  「下载模型」按钮；没下完不让点「开始」。
- 重装 / 升级不会丢：模型在应用数据目录，不在安装目录。

换来三个好处：安装包少 400MB 左右；CI 每轮少下 420MB；模型版本不再被钉死在安装包里，
以后换模型不用重新发版。

**注意**：打包脚本现在会主动**删掉** pyembed/models（老版本打进过安装包，留着白白多
418MB）。别"顺手优化"回去。

---

## 四、打包时必须知道的几个坑

这些都是真踩过、脚本里已经处理了的，改脚本时别改回去。

### 4.1 WiX / MSI 扛不住这个体积

    Error failed to bundle project: failed to run WixTools314\light.exe

targets = "all" 会在 Windows 上同时出 NSIS 和 MSI，而 MSI 的 cabinet 处理不了 GB 级
payload，light.exe 直接失败 —— **整个 tauri build 就算挂了**（NSIS 明明已经产出来了）。
所以两个平台各自显式指定：Windows 用 --bundles nsis，macOS 用 --bundles dmg。
个人用户双击 -setup.exe 就够了，MSI 是给企业 GPO 批量部署用的。

### 4.2 pnpm-lock 必须跟 package.json 同步

    ERR_PNPM_OUTDATED_LOCKFILE
      * 1 dependencies were added: lucide-react@^1.55.0

仓库里同时有 package-lock.json（npm）和 pnpm-lock.yaml（pnpm）。**本地用 npm 构建、
CI 用 pnpm --frozen-lockfile** —— 只更新 npm 那份，CI 就挂。改了 package.json 之后：

    cd tauri-ui && npx -y pnpm@10 install --lockfile-only

用 pnpm **10** 而不是更新的版本，跟 release.yml 里 pnpm/action-setup 的 version 对齐
（lockfileVersion 会不一样）。

### 4.3 tauri v2 打完 dmg 会清掉 .app

脚本原来的自检是 find *.app，CI 上必然失败（日志里能看到 "Cleaning .../Toushi.app"）。
现在改成**挂载 dmg，对用户真正拿到的那份做自检**：otool 链接检查 + codesign +
entitlements 都在挂载出来的 .app 上做，做完再 detach。

### 4.4 PowerShell 5.1 会把中文 .ps1 读成 ANSI

scripts/build_standalone.ps1 里有中文，没有 BOM 的话 Windows PowerShell 5.1
（powershell.exe，不是 pwsh 7）**直接解析失败** —— 不是乱码那么简单。
文件带 UTF-8 BOM，别去掉。

### 4.5 其他（脚本已处理）

| 坑 | 现象 | 处理 |
|---|---|---|
| 没设 PYO3_PYTHON | pyo3 链到系统 Python，装完 LoadLibrary 失败 | 脚本显式指向 pyembed 的 python.exe |
| 没设 RUSTFLAGS=-L native=libs 目录 | 链接期找不到 python313.lib | 脚本设置；前提是仓库路径没有空格 |
| pyembed 里的 BUILD 标记文件 | 大小写不敏感撞上 cargo 的 build 目录 → os error 5 | 脚本删掉（运行时不读它） |
| uv pip install 被 PEP 668 拦 | externally managed environment | 加 --break-system-packages |
| 缓存里的旧安装包 | CI 上传 glob 匹配到上个版本的产物 | build 前先 rm -rf bundle-release/bundle |

### 4.6 CI 上怎么看到失败原因

**公开仓库的 job log 走 API 拉需要 admin 权限**，拿不到任何输出，只能看到一个 exit code 1。
所以：

- release.yml 的 build step 外面套了一层 bash，失败时把日志尾部 8 行吐成
  ::error:: annotation，并额外上传完整日志为 artifact；
- 两个构建脚本会把各部分的体积发成 ::notice:: annotation。

annotation 不需要 admin，是目前唯一能远程读到的东西。加这个之前，三轮 CI 全靠猜。

---

## 五、CI 上的 Linux 检查（跟打包无关，但会被它带红）

.github/workflows/ci.yml 的 smoke import 跑在 Ubuntu 上，需要两个额外条件：

1. **WebKitGTK**：main.py → toushi.tauri → pytauri-wheel 的原生扩展链到
   libwebkit2gtk-4.1，headless runner 上没有 → 加了一步 apt 装
   libwebkit2gtk-4.1-dev / libgtk-3-dev / librsvg2-dev。
2. **pnpm-lock 同步**：见 4.2。

---

## 六、签名与 SmartScreen

不签名的话，用户第一次运行会看到「Windows 已保护你的电脑」→ 更多信息 → 仍要运行。
功能正常，只是观感差。

- OV 证书（约 1000-2000 元/年）能去掉大部分警告；EV 才有"立即建立信誉"的效果。
- 不签也能发：在 README / release notes 里把"点更多信息→仍要运行"写清楚就够了。
- 要签：tauri.conf.json 的 bundle.windows.certificateThumbprint + digestAlgorithm，
  证书放 CI secret。

---

## 七、发布前验收清单

1. **安装**：双击 -setup.exe，开始菜单里有「投石 · Toushi」，图标是新的
2. **启动**：在**没装过 Python 的干净机器**上双击能起（嵌入式 Python 是否生效的唯一验证）
3. **图标**：任务栏 + 窗口左上角是同一张
4. **下载模型**：运行页出现引导卡片 → 点下载 → 日志面板能看到进度 → 卡片消失
5. **配置页**：填 API key → 保存 → 重开 App 还在
6. **上传简历**：拖 PDF 进去 → 显示文件名
7. **点开始（Dry Run 开）**：能走到"生成招呼语" —— 这一步才真正验证模型和 LLM 都通了
8. **检查加载的 DLL**：Process Explorer 里看 python313.dll 的路径是不是安装目录下那份
9. **卸载**：控制面板卸载后，%APPDATA%/com.longsizhuo.toushi 里的模型和配置是否保留，符合预期

---

## 八、本机构建要装什么

1. **Rust**：https://rustup.rs → rustup default stable（需要 >= 1.78，脚本会检查）
2. **Visual Studio Build Tools**，勾 "Desktop development with C++"（要 MSVC 链接器）
3. **pnpm**：npm i -g pnpm
4. WebView2 Runtime：Win11 自带；Win10 需要 Evergreen Runtime

然后 pwsh -ExecutionPolicy Bypass -File scripts/build_standalone.ps1。
强制重建嵌入式 Python：先设 BOSS_PYEMBED_REBUILD=1。

---

## 九、还没做的

- **ONNX 嵌入瘦身**（见第二节，约 950MB）
- **代码签名**
- **让 uv pip install 走 uv.lock**：现在是绕开 lockfile 现场解析依赖的，本地 uv sync
  装的版本和安装包里装的可能不是同一套。想锁死可以 uv export 出
  requirements-standalone.txt 再 uv pip install -r。
