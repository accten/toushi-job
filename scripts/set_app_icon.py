#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一张 1024x1024 的图标母版铺开成 Tauri 需要的整套图标。

用法：
    uv run python scripts/set_app_icon.py 05-target        # assets/app-icons/05-target.svg
    uv run python scripts/set_app_icon.py path/to/icon.png # 也可以用现成 PNG

产出（全部落在 src-tauri/icons/）：
    32x32.png / 64x64.png / 128x128.png / 128x128@2x.png / icon.png / icon-512.png
    icon.ico   多尺寸（16/24/32/48/64/128/256），Windows 安装器与窗口图标都用它
    icon.icns  PNG 块封装的 ICNS，给 macOS bundle 用
    Square*Logo.png / StoreLogo.png   Windows Store 清单要的那几个尺寸
    source-1024.png / source.svg      母版留档，下次改图标从这里改

为什么自己写而不是调 tauri icon：
- 仓库里没有 Node 的 @tauri-apps/cli 之外的图标依赖，少一个下载；
- Pillow 已经在环境里，ICO / PNG 直接出；
- ICNS 是纯容器格式（magic + 若干块），90 行能写完，省得为了 mac 装个额外工具。

注意：SVG 走 Chrome 无头渲染。SVG 里的字体（比如汉字的宋体）由系统提供，
换机器渲染结果可能有细微差异 —— 所以生成的 source-1024.png 也要一起提交。
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile

from PIL import Image

REPO = pathlib.Path(__file__).resolve().parents[1]
# 两处都要写：
# - src-tauri/icons/        给 standalone 打包（src-tauri/tauri.conf.json 的 bundle.icon）
# - src/toushi/tauri/icons/ 给 wheel dev 模式（python -m toushi.tauri 读的是 Tauri.toml，
#   里面的相对路径必须相对**包内**解析，否则装成 wheel 之后找不到图标）
ICON_DIRS = [
    REPO / "src-tauri" / "icons",
    REPO / "src" / "toushi" / "tauri" / "icons",
]
CANDIDATE_DIR = REPO / "assets" / "app-icons"
MASTER_SIZE = 1024

PNG_SIZES = {
    "32x32.png": 32,
    "64x64.png": 64,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
    "icon-512.png": 512,
}
STORE_SIZES = {
    "Square30x30Logo.png": 30,
    "Square44x44Logo.png": 44,
    "Square71x71Logo.png": 71,
    "Square89x89Logo.png": 89,
    "Square107x107Logo.png": 107,
    "Square142x142Logo.png": 142,
    "Square150x150Logo.png": 150,
    "Square284x284Logo.png": 284,
    "Square310x310Logo.png": 310,
    "StoreLogo.png": 50,
}
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
# ICNS 的块类型 -> 边长。macOS 只认这几个 typecode，不能自造。
ICNS_TYPES = [
    (b"icp4", 16),
    (b"icp5", 32),
    (b"icp6", 64),
    (b"ic07", 128),
    (b"ic08", 256),
    (b"ic09", 512),
    (b"ic10", 1024),
    (b"ic11", 32),
    (b"ic12", 64),
    (b"ic13", 256),
    (b"ic14", 512),
]


def find_chrome() -> str:
    """找 Chrome：SVG 母版要靠它渲染成位图。"""
    import os

    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in candidates:
        if c and pathlib.Path(c).exists():
            return c
    raise SystemExit("找不到 Chrome —— SVG 母版需要它渲染；也可以直接给一张 PNG。")


def render_svg(svg_path: pathlib.Path, out_png: pathlib.Path) -> None:
    """无头 Chrome 把 SVG 按原尺寸截成 PNG，保留透明背景。"""
    chrome = find_chrome()
    profile = pathlib.Path(tempfile.mkdtemp(prefix="toushi-icon-"))
    args = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--default-background-color=00000000",
        "--log-level=3",
        f"--user-data-dir={profile}",
        f"--screenshot={out_png}",
        f"--window-size={MASTER_SIZE},{MASTER_SIZE}",
        svg_path.resolve().as_uri(),
    ]
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=120)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not out_png.exists():
        raise SystemExit("Chrome 没产出 PNG —— 检查 SVG 是否合法（要有 width/height 属性）")


def write_icns(master: Image.Image, path: pathlib.Path) -> None:
    """极简 ICNS writer：PNG 块直接拼，够 macOS 认。"""
    chunks = b""
    for code, size in ICNS_TYPES:
        buf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        buf.close()
        tmp = pathlib.Path(buf.name)
        master.resize((size, size), Image.LANCZOS).save(tmp, format="PNG")
        data = tmp.read_bytes()
        tmp.unlink()
        chunks += code + struct.pack(">I", len(data) + 8) + data
    path.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)


def resolve_source(token: str) -> tuple[pathlib.Path, pathlib.Path | None]:
    """把命令行参数解析成 (位图源, 原始 SVG 或 None)。"""
    p = pathlib.Path(token)
    if p.exists():
        if p.suffix.lower() == ".svg":
            return p, p
        return p, None
    svg = CANDIDATE_DIR / (token if token.endswith(".svg") else token + ".svg")
    png = CANDIDATE_DIR / (token + ".png")
    if svg.exists():
        return svg, svg
    if png.exists():
        return png, None
    available = sorted(x.stem for x in CANDIDATE_DIR.glob("*.svg"))
    raise SystemExit(f"找不到图标 {token}。assets/app-icons 下有：{', '.join(available)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="把母版图标铺开成 Tauri 图标集")
    ap.add_argument("source", help="assets/app-icons 里的名字（如 05-target），或任意 PNG/SVG 路径")
    args = ap.parse_args()

    source, svg_origin = resolve_source(args.source)

    if source.suffix.lower() == ".svg":
        master_png = pathlib.Path(tempfile.mkdtemp(prefix="toushi-master-")) / "master.png"
        render_svg(source, master_png)
    else:
        master_png = source

    master = Image.open(master_png).convert("RGBA")
    if master.size != (MASTER_SIZE, MASTER_SIZE):
        print(f"提示：母版是 {master.size[0]}x{master.size[1]}，缩放到 {MASTER_SIZE}x{MASTER_SIZE}")
        master = master.resize((MASTER_SIZE, MASTER_SIZE), Image.LANCZOS)

    for icon_dir in ICON_DIRS:
        icon_dir.mkdir(parents=True, exist_ok=True)
        master.save(icon_dir / "source-1024.png", format="PNG")
        if svg_origin is not None:
            shutil.copyfile(svg_origin, icon_dir / "source.svg")
        for name, size in {**PNG_SIZES, **STORE_SIZES}.items():
            master.resize((size, size), Image.LANCZOS).save(icon_dir / name, format="PNG")
        master.save(icon_dir / "icon.ico", format="ICO", sizes=[(s, s) for s in ICO_SIZES])
        write_icns(master, icon_dir / "icon.icns")
        print(f"已写入 {len(PNG_SIZES) + len(STORE_SIZES)} 个 PNG + icon.ico + icon.icns → {icon_dir}")
    print("提示：Tauri 的窗口/安装器图标来自 bundle.icon，两份配置（src-tauri/tauri.conf.json")
    print("      和 src/toushi/tauri/Tauri.toml）都已指向这些文件，改完图标不用再改配置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
