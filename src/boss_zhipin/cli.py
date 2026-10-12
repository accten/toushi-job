"""CLI 入口。

启动流程：
1. 读 ``.env``，校验 ``LLM_API_KEY`` 配好了没（端点 / model 由 ``LLM_*`` 决定）。
2. 兜底 ``BOSS_USR_NAME`` / ``BOSS_LABEL`` / ``RESUME_PATH`` 三个可选配置：
   不设就 prompt 用户输入或用默认值。
3. 进入主循环——统一走一个 OpenAI 兼容端点，不再分 provider 分支。

跑法：``uv run main.py`` / ``uv run python -m boss_zhipin`` / ``toushi``（pyproject script）
三种等价。

设计：用户输入提示用 ``print``，业务流程用 ``logging``。logging 在
``_cli_main`` 里 ``basicConfig`` 统一初始化——**不在 module top**，避免
``import boss_zhipin.cli`` 时改掉 GUI 进程的 logging 配置。

注意：本模块自己不在 import-time 调 ``load_dotenv``，但顶层 import 的
``models.*`` 子模块会（历史行为）。所以 ``import boss_zhipin.cli`` 仍会把
``.env`` 读进 ``os.environ``——GUI 的 ``detect_providers`` 依赖这一点，
不要为了"纯净 import"把这些 import 延迟掉。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import nodriver as uc
from dotenv import load_dotenv

from boss_zhipin.models.job_matcher import extract_keywords_from_text, extract_resume_text
# LLM_PRESETS / is_llm_configured 住在轻量的 boss_zhipin.providers，
# 让 PyTauri 的 Config 命令不被 cli.py 的重 import 链拖累
# （cli 的 vectorization import 会触发 sentence_transformers → torch，3-10s）。
from boss_zhipin.providers import (
    LLM_PRESETS,
    is_llm_configured,
)
from boss_zhipin.vectorization import embed_resume
from boss_zhipin.website_oper.write_response import send_job_descriptions_to_chat

log = logging.getLogger(__name__)

# 平台配置
PLATFORM_CONFIG = {
    "boss": {
        "url": os.getenv("BOSS_RECOMMEND_URL", "https://www.zhipin.com/web/geek/job-recommend?ka=header-job-recommend"),
        "send_message": True,
        "description": "BOSS Zhipin (sending cover letters)",
    },
    "zhilian": {
        # 默认必须是**岗位列表页**，不是首页：首页 `a[href]` 里「职位」指向 /recommend，
        # 实测未登录 /recommend 会 302 到 passport 登录页，所以这里只能给"岗位页"的地址。
        "url": os.getenv("ZHILIAN_RECOMMEND_URL", "https://www.zhaopin.com/recommend"),
        "send_message": False,
        "description": "Zhaopin (only applying)",
    },
}

# BOSS 推荐 feed 入口（向后兼容）
RECOMMEND_URL = PLATFORM_CONFIG["boss"]["url"]

# 简历默认路径。CLI（ensure_resume_path）和 GUI（tauri 的 factory）共用。
DEFAULT_RESUME_PATH = "resume/my_cover.pdf"

__all__ = [
    "LLM_PRESETS",
    "is_llm_configured",
    "RECOMMEND_URL",
    "DEFAULT_RESUME_PATH",
    "ensure_llm_configured",
    "ensure_usr_name",
    "ensure_resume_path",
    "run_provider",
]


def ensure_llm_configured() -> None:
    """没填 LLM_API_KEY 就打印各家申请地址并退出。

    端点 / model 统一由 ``LLM_*`` 决定（GUI 选预设会自动填），CLI 这里只兜底
    检查 key 在不在；真正构造 client 在 ``llm._build_client``（会校验 LLM_MODEL）。
    """
    if is_llm_configured():
        return
    print("ERROR: LLM_API_KEY is not set.")
    print("   Please select a provider and obtain an API key, then set LLM_API_KEY in .env (see .env.example):")
    for preset in LLM_PRESETS.values():
        print(f"     - {preset['label']:<18} {preset['signup_url']}")
        print(f"       LLM_BASE_URL={preset['base_url']}  LLM_MODEL={preset['model']}")
    sys.exit(1)


def ensure_usr_name() -> str:
    name = os.getenv("BOSS_USR_NAME", "").strip()
    if name:
        return name
    while True:
        name = input("请输入你的名字（用于打招呼语结尾的署名）: ").strip()
        if name:
            return name
        print("不能为空")


def ensure_resume_path() -> str:
    resume_path = os.getenv("RESUME_PATH", "").strip() or DEFAULT_RESUME_PATH
    if not Path(resume_path).is_file():
        print(f"错误：找不到简历文件：{resume_path}")
        print(f"   请将PDF简历放置在此路径，或在.env中设置RESUME_PATH指向其他位置。")
        sys.exit(1)
    return resume_path


def get_label() -> str:
    """求职 tag 是可选的——为空就让 BOSS 给默认推荐 feed。"""
    return os.getenv("BOSS_LABEL", "").strip()


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    """读一个整数环境变量，坏值 / 越界都降级到 [lo, hi] 内，**永不抛**。

    ``BOSS_MIN_MATCH_SCORE`` 这类是 GUI 配置页能直接填的字段，裸 ``int()`` 遇到
    ``abc`` / 空串 / ``150`` 会把整个 run 崩掉，用户只看到"点开始就崩"。这里把它
    收敛成"坏值回退默认 + 一条 warning"，让跑得起来比跑得精确重要。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("%s=%r 不是整数，回退默认 %d", name, raw, default)
        return default
    if val < lo or val > hi:
        clamped = max(lo, min(hi, val))
        log.warning("%s=%d 超出 [%d, %d]，收敛到 %d", name, val, lo, hi, clamped)
        return clamped
    return val


async def run_provider(
    usr_name: str,
    label: str,
    dry_run: bool,
    resume_path: str,
    platform: str = "boss",
) -> None:
    """简历预处理 + 主循环，CLI 和 GUI 共用的唯一入口。

    用哪个 LLM 端点 / model 由 ``LLM_*`` 环境变量决定（见 ``llm._build_client``），
    不再按 provider 分支——三家都是 OpenAI 兼容端点，走同一条 RAG + chat
    completions 通路。

    ``platform`` 指定求职平台：``"boss"``（BOSS 直聘）或 ``"zhilian"``（智联招聘）。

    必须在同一个事件循环里 await（nodriver CDP 跨 run_until_complete 会半死，
    见 ``_cli_main`` 的注释）。
    """
    # 获取平台配置
    platform_config = PLATFORM_CONFIG.get(platform)
    if not platform_config:
        log.error("Unsupported platform: %s, supported: %s", platform, list(PLATFORM_CONFIG.keys()))
        return
    
    url = platform_config["url"]
    send_message = platform_config["send_message"]
    
    log.info("Starting platform: %s - %s", platform.upper(), platform_config["description"])
    
    # 动态导入对应的 finding_jobs 模块
    if platform == "zhilian":
        from boss_zhipin.website_oper import zhilian_finding_jobs as finding_jobs_module
    else:
        from boss_zhipin.website_oper import finding_jobs as finding_jobs_module
    
    # 替换 write_response 里的 finding_jobs 引用
    import boss_zhipin.website_oper.write_response as wr_module
    wr_module.finding_jobs = finding_jobs_module
    
    # 从简历自动提取关键词和全文（用于职位匹配过滤）
    resume_text = extract_resume_text(resume_path)
    resume_keywords = extract_keywords_from_text(resume_text)
    log.info("📋 从简历中提取到 %d 个关键词: %s", len(resume_keywords), resume_keywords)
    # 提前为所有 provider 创建向量库，用于语义粗筛
    vectorstore = embed_resume(resume_text, "./vectorstores")
    
    # LLM 匹配分阈值，低于该分跳过不投；可用 BOSS_MIN_MATCH_SCORE 覆盖。
    # 走 _int_env：GUI 填了非数字 / 越界也不崩 run，回退到 50 / 收敛到 0-100。
    min_llm_score = _int_env("BOSS_MIN_MATCH_SCORE", 70, 0, 100)
    # 关键词粗筛门槛：JD 至少命中几个简历关键词才进 LLM 评分。可用 BOSS_MIN_KEYWORD_MATCH
    # 覆盖（默认 2）。觉得跳过太多（推荐 feed 跟简历词对不上）就调低到 1。
    min_keyword_match = _int_env("BOSS_MIN_KEYWORD_MATCH", 2, 0, 20)
    exclude_str = os.getenv("BOSS_EXCLUDE_KEYWORDS", "")
    exclude_keywords = [k.strip() for k in exclude_str.split(",") if k.strip()] if exclude_str else None

    await send_job_descriptions_to_chat(
        usr_name=usr_name,
        url=url,
        browser_type="chrome",
        label=label,
        dry_run=dry_run,
        resume_keywords=resume_keywords,
        resume_text=resume_text,
        min_keyword_match=min_keyword_match,
        min_llm_score=min_llm_score,
        exclude_keywords=exclude_keywords,
        vectorstore=vectorstore,
        send_message=send_message,
    )


def _cli_main() -> None:
    """``python -m boss_zhipin`` / 根目录 ``main.py`` shim / pyproject script
    都委托到这个函数。

    把 ``load_dotenv`` 和 ``logging.basicConfig`` 收进来——``basicConfig``
    只有真正"以 CLI 方式跑"才生效，单纯 ``import boss_zhipin.cli`` 不会污染
    logging。（``.env`` 本身仍会在 import-time 经由 ``models.*`` 读入，见
    module docstring；这里的 ``load_dotenv()`` 是给"models 还没 import 就
    需要 env"的将来留的显式入口，幂等。）
    """
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="BOSS 直聘 / 智联招聘自动投递脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  uv run main.py                          # 默认使用 BOSS 直聘
  uv run main.py --platform zhilian       # 使用智联招聘（只投递）
  uv run main.py --platform boss          # 使用 BOSS 直聘（发送招呼语）
        """,
    )
    parser.add_argument(
        "--platform",
        "-p",
        choices=list(PLATFORM_CONFIG.keys()),
        default=os.getenv("PLATFORM", "boss").lower(),
        help="求职平台（默认: boss）。可选: boss, zhilian",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="DRY_RUN 模式：只生成不发送",
    )
    
    # 先解析参数，以便后续使用
    args, _ = parser.parse_known_args()
    
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOGLEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # 命令行参数优先，其次环境变量
    dry_run = args.dry_run or os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
    if dry_run:
        print("WARNING: DRY_RUN=1 - Only generating cover letters, not sending them.")

    # 根据平台决定是否需要配置 LLM
    platform_config = PLATFORM_CONFIG[args.platform]
    if platform_config["send_message"]:
        ensure_llm_configured()
    else:
        log.info("智联招聘模式：不需要配置 LLM（只投递，不发送招呼语）")
    
    usr_name = ensure_usr_name()
    resume_path = ensure_resume_path()
    label = get_label()
    
    print(f"Job platform: {platform_config['description']}")
    if label:
        print(f"   Job tag: {label}")
    else:
        print("   BOSS_LABEL not set, using default recommendation feed")

    # run_provider 是 async 的（整段必须跑在同一个事件循环里，否则 nodriver
    # CDP 会在 run_until_complete 之间进入半死态导致 evaluate hang）。
    # 这里用 ``uc.loop().run_until_complete(...)`` 一次性跑完。
    uc.loop().run_until_complete(
        run_provider(usr_name, label, dry_run, resume_path, args.platform)
    )


if __name__ == "__main__":
    _cli_main()
