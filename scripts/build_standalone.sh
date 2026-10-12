#!/usr/bin/env bash
# Phase D：打 standalone .app（macOS）。
#
# 把"嵌入式 Python 组装 + 业务包安装 + Rust 链接参数 + tauri bundle"整条
# 流程固化下来。**不要手工跑其中某几步**——漏掉 PYO3_PYTHON / RUSTFLAGS
# 会让 pyo3 链接到系统 Python（CLT 的 Python3.framework 3.9），.app 启动
# 即 dyld crash（2026-06-07 实测翻车过一次，见 ADR-005）。
#
# 用法：
#   ./scripts/build_standalone.sh            # 全量构建
#   BOSS_PYEMBED_REBUILD=1 ./scripts/...     # 强制重建 pyembed
#
# 产物：src-tauri/target/bundle-release/bundle/macos/Toushi.app
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT=$(pwd)

PYEMBED_DIR="$REPO_ROOT/src-tauri/pyembed"
PYTHON_SERIES="3.13"   # 跟 pyproject requires-python 对齐

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "❌ 目前只支持 macOS（Linux/Windows 的 rpath / install_name 处理不同）"
    echo "   Windows 端用 scripts/build_standalone.ps1"
    exit 1
fi

# Cargo.lock v4 需要 Cargo >=1.78，老版本在 step 5 才炸太晚
cargo_ver=$(cargo --version 2>/dev/null | sed -n 's/cargo \([0-9]*\)\.\([0-9]*\).*/\1.\2/p')
if [[ -z "$cargo_ver" ]]; then
    echo "❌ 找不到 cargo，请先装 rustup"; exit 1
fi
cargo_major=${cargo_ver%.*}; cargo_minor=${cargo_ver#*.}
if (( cargo_major < 1 || (cargo_major == 1 && cargo_minor < 78) )); then
    echo "❌ Cargo $cargo_ver 太老（需 >=1.78 解析 lockfile v4），跑 'rustup update stable'"
    exit 1
fi

# ---------- 1. 嵌入式 Python（python-build-standalone，经 uv 下载） ----------
if [[ "${BOSS_PYEMBED_REBUILD:-}" == "1" ]]; then
    rm -rf "$PYEMBED_DIR"
fi
if [[ ! -x "$PYEMBED_DIR/python/bin/python3" ]]; then
    # ${} 花括号必须保留：CI runner 的 locale 是 C/POSIX（非 UTF-8），
    # bash 不把后面的全角中文 `（` 当作变量名边界，会把它的首字节粘进变量名
    # → `set -u` 下报 "PYTHON_SERIES…: unbound variable"（2026-06-08 CI 实测）。
    echo "==> 下载嵌入式 Python ${PYTHON_SERIES}（python-build-standalone）"
    mkdir -p "$PYEMBED_DIR"
    UV_PYTHON_INSTALL_DIR="$PYEMBED_DIR/_download" uv python install "$PYTHON_SERIES"
    # uv 落盘目录名带完整版本号（cpython-3.13.x-macos-aarch64-none），规范成 python/
    src_dir=$(find "$PYEMBED_DIR/_download" -maxdepth 1 -type d -name "cpython-*" | head -1)
    [[ -n "$src_dir" ]] || { echo "❌ 没找到 uv 下载的 cpython 目录"; exit 1; }
    mv "$src_dir" "$PYEMBED_DIR/python"
    rm -rf "$PYEMBED_DIR/_download"
fi
EMBED_PY="$PYEMBED_DIR/python/bin/python3"
# 纯增兜底（当前扁平布局走不到，行为不变）：uv 个别版本把 python 嵌在多一层，
# 导致 python/bin/python3 不在预期位置（.ps1 在 Windows 上已遇过同款漂移）。
# 仅当预期路径缺失时，递归找真正的 bin/python3 并把它所在的 python 根扶正，
# 保证 python/bin 与 python/lib 子路径都成立；找不到就给清晰诊断而非 set -e 裸退。
if [[ ! -x "$EMBED_PY" ]]; then
    real_py=$(find "$PYEMBED_DIR/python" -type f -name python3 -path '*/bin/*' 2>/dev/null | sort | head -1)
    if [[ -n "$real_py" ]]; then
        real_root=$(cd "$(dirname "$real_py")/.." && pwd)
        if [[ "$real_root" != "$PYEMBED_DIR/python" ]]; then
            mv "$real_root" "$PYEMBED_DIR/_relayout"
            rm -rf "$PYEMBED_DIR/python"
            mv "$PYEMBED_DIR/_relayout" "$PYEMBED_DIR/python"
        fi
    fi
fi
[[ -x "$EMBED_PY" ]] || { echo "❌ 没找到嵌入式 python3，pyembed/python 实际布局："; find "$PYEMBED_DIR/python" -maxdepth 3 2>/dev/null; exit 1; }
echo "==> 嵌入式 Python: $("$EMBED_PY" --version)"

# ---------- 2. macOS：libpython install_name 打 @rpath 补丁 ----------
# python-build-standalone 的 libpython install_name 不带 @rpath，导致给
# executable 设的 rpath 失效（pytauri build-standalone 教程明确要求这步）。
libpython=$(find "$PYEMBED_DIR/python/lib" -maxdepth 1 -name "libpython3.*.dylib" | head -1)
[[ -n "$libpython" ]] || { echo "❌ 没找到 libpython3.*.dylib"; exit 1; }
install_name_tool -id "@rpath/$(basename "$libpython")" "$libpython"
echo "==> libpython install_name 已设为 @rpath/$(basename "$libpython")"

# ---------- 3. 把业务包 + 依赖装进嵌入式环境 ----------
# [standalone] extra：pytauri 但不带 pytauri-wheel（ext_mod 由我们的 Rust
# binary 提供）。--exact 保证环境里只有声明过的东西。
# --break-system-packages：uv 把 python-build-standalone 标 PEP 668
# "externally managed"，默认拒绝 pip 写入；本场景就是要往里写，加 flag 放行。
# （2026-06-07 在 Windows 端 .ps1 上踩到，mac 上当时 uv 版本旧没拦，新 uv 两边都会拦。）
echo "==> 安装 toushi + 依赖到 pyembed（首次会拉 torch，较慢）"
PYTAURI_STANDALONE=1 uv pip install \
    --exact \
    --compile-bytecode \
    --break-system-packages \
    --python="$EMBED_PY" \
    --reinstall-package=toushi \
    "$REPO_ROOT[standalone]"

# ---------- 3.5 删掉 pyembed 里的 BUILD 标记文件 ----------
# python-build-standalone 自带一个 8 字节的 BUILD 元数据文件。macOS 默认 APFS
# **大小写不敏感**，tauri-build 把 pyembed 资源（含这个 BUILD 文件）拷进 target
# 时，BUILD 跟 cargo 自己的 build/ 目录撞名 → fs::copy 往目录上写文件报
# "Is a directory (os error 21)"（2026-06-09 CI 实测；.ps1 在 Windows 上同因
# 报 os error 5，早已删它，.sh 之前漏了这步——老 uv 下的 pyembed 不带 BUILD
# 文件所以本地没翻车）。Python 运行时不读这个文件，删掉无副作用。
build_marker="$PYEMBED_DIR/python/BUILD"
if [[ -f "$build_marker" ]]; then
    echo "==> 删除 pyembed BUILD 标记文件（避开 cargo build/ 目录大小写冲突）"
    rm -f "$build_marker"
fi

# ---------- 3.6 把嵌入模型拷进 resources ----------
# 装出来的 App 要在完全离线的机器上向量化简历，模型必须随包走。
# 放在 pyembed 旁边（src-tauri/pyembed/models），由 tauri.bundle.json 的
# resources 映射到安装目录 —— 安装后是 <安装目录>/models，正好落在
# vectorization._candidate_model_roots() 的候选里。
#
# 只拷 snapshots 那一层：HF 缓存里 blobs 和 snapshots 各占一份（837MB），
# 真正要的只有约 418MB 那份。
# 模型有三种来路，按优先级：
#   1. pyembed/models 已经有了（上次跑过）→ 跳过
#   2. 仓库里有 models/hub/...（本地开发机）→ 拷去重后的快照目录
#   3. 都没有（CI / 新克隆）→ 直接从 HuggingFace 拉一份平铺在 pyembed/models
MODEL_REPO='sentence-transformers/all-mpnet-base-v2'
MODEL_REVISION='e8c3b32edf5434bc2275fc9bab85f82640a19130'
MODEL_DST="$PYEMBED_DIR/models"
if [[ -f "$MODEL_DST/config.json" ]]; then
    echo "==> 嵌入模型已在 pyembed/models，跳过"
elif [[ -d "$REPO_ROOT/models/hub" ]]; then
    SNAPSHOT=$(find "$REPO_ROOT/models/hub" -maxdepth 3 -type d -name snapshots 2>/dev/null \
        | while read -r d; do find "$d" -mindepth 1 -maxdepth 1 -type d; done \
        | while read -r s; do if [[ -f "$s/config.json" ]]; then echo "$s"; fi; done \
        | head -n 1)
    if [[ -z "$SNAPSHOT" ]]; then
        echo "仓库里有 models/hub 但找不到完整快照（缺 config.json）—— 见 docs/packaging-windows.md" >&2
        exit 1
    fi
    REPO_ID=$(basename "$(dirname "$(dirname "$SNAPSHOT")")")
    TARGET="$MODEL_DST/hub/$REPO_ID/snapshots/$(basename "$SNAPSHOT")"
    echo "==> 拷贝嵌入模型：$SNAPSHOT -> $TARGET"
    mkdir -p "$TARGET"
    cp -R "$SNAPSHOT/." "$TARGET/"
else
    echo "==> 仓库里没有 models/，从 HuggingFace 拉一份到 pyembed/models（约 418MB）"
    mkdir -p "$MODEL_DST"
    # --include 只拉推理要的文件：不加的话 HF 仓库里的 pytorch_model.bin /
    # onnx/ 变体也会一起下来，白白多几百 MB。
    uv run --no-project --with huggingface_hub hf download "$MODEL_REPO" \
        --revision "$MODEL_REVISION" \
        --include '*.json' --include '*.txt' --include 'model.safetensors' \
        --local-dir "$MODEL_DST"
    if [[ ! -f "$MODEL_DST/config.json" ]]; then
        echo "模型下载完了但 $MODEL_DST 里没有 config.json，打包会出个没模型的安装包" >&2
        exit 1
    fi
fi

# ---------- 4. Rust 链接参数 ----------
# PYO3_PYTHON：不设的话 pyo3 自动探测系统 Python → 链到 CLT 的
# Python3.framework → .app 启动 dyld crash。这是整个脚本最关键的两行。
export PYO3_PYTHON="$EMBED_PY"
export RUSTFLAGS=" \
    -C link-arg=-Wl,-rpath,@executable_path/../Resources/lib \
    -L $PYEMBED_DIR/python/lib"

# ---------- 5. 前端 + tauri bundle ----------
echo "==> pnpm install（tauri-ui）"
pnpm --dir "$REPO_ROOT/tauri-ui" install --frozen-lockfile

echo "==> tauri build（bundle-release profile）"
# --config tauri.bundle.json：把 pyembed/python 作为 resources 打进 .app；
# 不直接写进 tauri.conf.json 是因为 wheel dev 模式不需要也不该带这坨。
# 注意必须从 repo root 跑——tauri CLI 只往**子目录**找 src-tauri/tauri.conf.json，
# 从 tauri-ui/ 里跑会 "Couldn't recognize the current folder as a Tauri project"。
cd "$REPO_ROOT"
# 体积诊断：安装包为什么这么大，先看是哪一块占的
for d in "$MODEL_DST" "$PYEMBED_DIR/python" "$PYEMBED_DIR" "$REPO_ROOT/tauri-ui/node_modules"; do
    if [[ -e "$d" ]]; then
        line="体积 $d = $(du -sm "$d" 2>/dev/null | cut -f1) MB"
        echo "==> $line"
        # CI 里额外发一条 notice：公开仓库的 job log 走 API 拉要 admin，
        # 只有 annotation 读得到 —— 体积数据得走这条路才能被看见。
        if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then echo "::notice::$line"; fi
    fi
done

# --bundles dmg：只出 DMG。配 Windows 那边的 --bundles nsis 一起看：
# tauri.conf.json 里 targets = "all" 是给本地 dev 用的，CI 上显式指定免得
# 平台不认识对方的 bundle 类型（Windows 的 MSI 也会因 WiX light.exe 扛不住
# 这个体积直接失败）。
"$REPO_ROOT/tauri-ui/node_modules/.bin/tauri" build \
    --bundles dmg \
    --config "$REPO_ROOT/src-tauri/tauri.bundle.json" \
    -- --profile bundle-release

# ---------- 6. 产物自检 ----------
APP=$(find "$REPO_ROOT/src-tauri/target" -maxdepth 4 -name "*.app" -path "*bundle-release*" | head -1)
[[ -n "$APP" ]] || { echo "❌ 没找到 .app 产物"; exit 1; }
BIN="$APP/Contents/MacOS/toushi"
echo "==> 自检：binary 应链接 @rpath/libpython3.x，不能出现 Python3.framework"
if otool -L "$BIN" | grep -q "Python3.framework"; then
    echo "❌ binary 链接到了系统 Python3.framework——PYO3_PYTHON 没生效？"
    otool -L "$BIN" | grep -i python
    exit 1
fi
otool -L "$BIN" | grep -i python || true

# ---------- 7. codesign 自检（ad-hoc 签名由 tauri 自动完成）----------
# tauri.conf.json 里 bundle.macOS.signingIdentity = "-" 告诉 tauri 在 bundle
# 阶段做 ad-hoc 签名（无需 Apple Developer 账号）。**不签会触发 macOS
# Sequoia 的 "damaged and can't be opened" 误报**——Sequoia 对未签名 +
# quarantine xattr 的 .app 直接报 damaged，"右键→打开" 也绕不过去。
# ad-hoc 签完之后会回到老的 "无法验证开发者" 对话框，右键→打开能绕过。
echo "==> codesign 自检（应该是 ad-hoc 已签）"
# codesign -dv 把签名状态写到 stderr；用 Signature=adhoc 行作为判定锚点。
sig_info=$(codesign -dv "$APP" 2>&1 || true)
if echo "$sig_info" | grep -q "Signature=adhoc"; then
    echo "  ✓ ad-hoc 签名生效（$(echo "$sig_info" | grep -E '^Signature='))"
elif echo "$sig_info" | grep -q "code object is not signed"; then
    echo "❌ .app **未签名**——检查 tauri.conf.json 的 bundle.macOS.signingIdentity 是不是 \"-\""
    exit 1
else
    echo "⚠️  签名状态不是 ad-hoc，可能是别的 identity（也行，只要 codesign --verify 过）："
    echo "$sig_info" | grep -E '^(Signature|Authority|Identifier)='
fi
# 跑一遍 verify 兜底
if ! codesign --verify "$APP" 2>/dev/null; then
    echo "❌ codesign --verify 失败"
    codesign --verify --verbose=2 "$APP" 2>&1 | head -10
    exit 1
fi

# ---------- 8. entitlements 自检 ----------
# hardened runtime（tauri ad-hoc 签名默认开）会启用 library validation，
# 而 pyembed 的 libpython / .so 都是 linker-signed 无 Team ID，没有
# disable-library-validation entitlement 的话 .app 启动即 dyld crash
# （"Library not loaded: @rpath/libpython3.13.dylib"，2026-06-08 macOS 26.2
# 实测）。entitlements 由 tauri.conf.json bundle.macOS.entitlements 指向
# src-tauri/entitlements.plist 注入。
echo "==> entitlements 自检（必须含 disable-library-validation）"
if ! codesign -d --entitlements - "$APP" 2>/dev/null | grep -q "disable-library-validation"; then
    echo "❌ 签名里没有 com.apple.security.cs.disable-library-validation —— "
    echo "   检查 tauri.conf.json 的 bundle.macOS.entitlements 是否指向 entitlements.plist"
    exit 1
fi
echo "  ✓ disable-library-validation 已注入"

echo "✅ 构建完成：$APP"
