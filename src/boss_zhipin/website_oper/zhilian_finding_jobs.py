"""nodriver-backed 智联招聘 automation.

功能：自动抓取智联招聘推荐岗位 JD，生成招呼语并发送。
登录方式：扫码登录（微信/手机均可）

注意：智联招聘的 DOM 结构与 BOSS 直聘完全不同，需要根据实际页面结构调整选择器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys

import nodriver as uc
from nodriver import Config

# 复用 BOSS 模块里已经验证过的那套：异常类型、重载探测、session 失效判断。
# **必须复用同一批异常类** —— write_response 里的 `except PageReloadingError` 是直接从
# finding_jobs 导入的；智联这边抛自己的类就抓不住，会被当成"主循环未知异常"草草收场。
from boss_zhipin.website_oper.finding_jobs import (
    LoginRequiredError,
    PageReloadingError,
    _MIN_RENDERED_BODY_LEN,
    _RELOAD_LOOP_THRESHOLD,
    _RELOAD_WINDOW_SECONDS,
    _is_stale_session_error,
    _login_wait_seconds,
    _record_document_origin,
)

log = logging.getLogger(__name__)

# ========== 配置区：根据智联招聘实际页面结构修改 ==========

# 智联招聘 Chrome profile 目录（独立于 BOSS 的 profile）
ZHLIAN_PROFILE_DIR = os.path.abspath(
    os.environ.get("ZHLIAN_CHROME_PROFILE", "./chrome_profile_zhilian")
)

# 智联招聘推荐 feed URL。
# 实测：首页 `a[href]` 里「职位」指向 https://www.zhaopin.com/recommend —— 那才是岗位
# 列表页；老默认值 "https://www.zhaopin.com/" 是**首页**，上面一张岗位卡都没有。
# 注意 /recommend 未登录时会直接 302 到 passport.zhaopin.com/login。
ZHLIAN_RECOMMEND_URL = os.getenv(
    "ZHILIAN_RECOMMEND_URL", "https://www.zhaopin.com/recommend"
)

# 选择器配置。
#
# 2026-09 对着**登录后的真实页面**（www.zhaopin.com/jobs/?pageMode=recommend）量过一遍：
#   .job-card                         → 20 个（岗位卡，✓ 原值就对）
#   .job-description                  → 1 个（JD 正文，干净）
#   .job-detail-panel                 → 1 个（整个右栏，含标题/薪资/收藏分享举报 + JD）
#   button.job-detail-summary__apply  → 1 个，文案「立即投递」（✓ 原值就对）
#   .deliver-greeting-modal           → 投递后会弹（"已向对方发送简历和打招呼语"）
# 老值 `.job-description__content` 实测命中 0 —— 所以以前整个流程卡在"读不到 JD"。
SELECTORS = {
    # 岗位卡片（用于点击打开详情）
    "job_card": ".job-card",

    # JD 详情区域。按顺序试：先最干净的正文，再退回整个右栏面板。
    "job_detail": ".job-description",
    "job_detail_candidates": [".job-description", ".job-detail-panel"],

    # 投递按钮（点击后直接投递）
    "apply_btn": "button.job-detail-summary__apply",

    # 投递后弹的确认框（必须关掉，否则盖住页面，后面点不动岗位卡）
    "apply_dialog": ".deliver-greeting-modal",

    # 登录相关
    "login_dialog": ".login-dialog",
    "qr_code": ".qr-code",
}

# 登录检测选择器
LOGIN_CHECK_SELECTORS = {
    # 未登录时可见的元素
    "header_login": "a.home-header__c-no-login",
    "login_wall": ".login-wall, .login-modal, .popup-login",  # 需要根据实际页面调整

    # 已登录时可见的元素
    "user_avatar": "img.c-login__top__img",
}

# JD 噪声行（需要从 JD 文本开头剥离）
JD_NOISE_LINES = frozenset({
    "举报", "分享", "职位描述", "收藏", "立即沟通",
    "公司信息", "职位详情", "相似职位",
    "相似职位推荐", "职位分享",
})

# ========== 模块状态 ==========

_browser: uc.Browser | None = None
_tab: uc.Tab | None = None

# ========== 工具函数 ==========


def _env_flag_true(name: str) -> bool:
    """环境变量布尔开关解析"""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _should_disable_sandbox() -> bool:
    """是否关闭 Chrome sandbox"""
    if _env_flag_true("BOSS_NO_SANDBOX"):
        return True
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        try:
            return geteuid() == 0
        except Exception:
            return False
    return False


def _apply_sandbox_setting(config) -> bool:
    """按需关闭 Chrome sandbox"""
    if _should_disable_sandbox():
        config.sandbox = False
        return False
    return bool(getattr(config, "sandbox", True))


def get_tab() -> uc.Tab | None:
    """获取当前控制的 Tab"""
    return _tab


# ========== 登录检测 ==========


def _on_login_page(url: str) -> bool:
    """判断是否在登录页面"""
    return any(s in url for s in ("login", "signin", "passport", "account"))


async def _is_logged_in() -> bool:
    """判断登录状态：URL + DOM 双重检测"""
    if _tab is None:
        return False
    
    # URL 在登录页 → 未登录
    if _on_login_page(_tab.url):
        return False
    
    # DOM 检测：查找登录按钮/遮罩
    js = """
    JSON.stringify((() => {
      const visible = (el) => {
        if (!el) return false;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      
      // 未登录信号
      const headerLogin = document.querySelector('""" + LOGIN_CHECK_SELECTORS["header_login"] + """');
      const loginWall = document.querySelector('""" + LOGIN_CHECK_SELECTORS["login_wall"] + """');
      
      // 已登录信号
      const userAvatar = document.querySelector('""" + LOGIN_CHECK_SELECTORS["user_avatar"] + """');
      const bodyText = document.body ? (document.body.innerText || '') : '';

      return {
        // 页面有没有真的渲染出来（见 finding_jobs._MIN_RENDERED_BODY_LEN 的注释）
        readyState: document.readyState,
        bodyLength: bodyText.length,
        // 顶部「登录/注册」是未登录最直接的证据，不再只靠 class 名兜
        headerLoginText: bodyText.includes('登录/注册'),
        headerLoginVisible: visible(headerLogin),
        loginWallVisible: visible(loginWall),
        userAvatarVisible: visible(userAvatar),
      };
    })());
    """
    info = await _safe_evaluate(js, timeout=5)

    # 页面没渲染出来一律判未登录 —— 空白文档里什么信号都查不到，"没信号"不等于"已登录"
    # （BOSS 那边踩过：未登录的一轮里报「检测到已登录」，页面顶上明明写着「登录/注册」）。
    if (info.get("bodyLength") or 0) < _MIN_RENDERED_BODY_LEN:
        return False
    ready = info.get("readyState")
    if ready is not None and ready != "complete":
        return False

    # 有未登录信号 → 未登录
    if info.get("headerLoginVisible") or info.get("loginWallVisible"):
        return False
    if info.get("headerLoginText"):
        return False

    # 有已登录信号 → 已登录
    if info.get("userAvatarVisible"):
        return True

    # 无明确信号，退回 URL 判断
    return not _on_login_page(_tab.url)


# ========== URL 稳定性检测 ==========


async def _wait_url_stable(stable_for: float = 2.0, timeout: float = 30) -> str:
    """等待 URL 稳定"""
    end = asyncio.get_event_loop().time() + timeout
    last_url = _tab.url
    last_change = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() < end:
        cur = _tab.url
        now = asyncio.get_event_loop().time()
        if cur != last_url:
            last_url = cur
            last_change = now
        elif now - last_change >= stable_for:
            return cur
        await asyncio.sleep(0.2)
    return last_url


# ========== 浏览器生命周期 ==========


async def shutdown() -> None:
    """关闭 Chrome"""
    global _browser, _tab
    if _browser is not None:
        try:
            _browser.stop()
        except Exception as e:
            log.warning("browser.stop() 失败: %s", e)
    _browser = None
    _tab = None


def _clear_singleton_locks(profile_dir: str) -> None:
    """清理 profile 锁文件"""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(profile_dir, name))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.debug("清 Singleton 锁 %s 跳过：%s", name, e)


def _ensure_localhost_bypasses_proxy() -> None:
    """确保 127.0.0.1/localhost 不走代理"""
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    existing = [s.strip() for s in no_proxy.split(",") if s.strip()]
    lowered = {s.lower() for s in existing}
    missing = [h for h in ("127.0.0.1", "localhost") if h not in lowered]
    if not missing:
        return
    new_val = ",".join(existing + missing)
    os.environ["no_proxy"] = new_val
    os.environ["NO_PROXY"] = new_val


def _kill_profile_chrome(profile_dir: str) -> None:
    """杀掉占 profile 的残留 Chrome"""
    if sys.platform == "win32":
        return
    try:
        subprocess.run(
            ["pkill", "-f", os.path.abspath(profile_dir)],
            capture_output=True,
            timeout=5,
        )
    except Exception as e:
        log.debug("pkill 残留 Chrome 跳过：%s", e)


def _reap_profile_chrome(profile_dir: str) -> None:
    """起浏览器前清理残留"""
    _kill_profile_chrome(profile_dir)
    _clear_singleton_locks(profile_dir)


async def _start_browser_with_retry(config: Config, attempts: int = 3) -> uc.Browser:
    """启动 Chrome 并重试"""
    last_err: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            browser = await uc.start(config=config)
            # 额外验证：确保浏览器实际上可以连接
            try:
                # 获取第一个tab来验证连接
                if browser.tabs:
                    tab = browser.tabs[0]
                    await tab.evaluate("1 + 1")
                    log.debug("浏览器连接验证成功")
                else:
                    # 如果没有tab，等待一下再试
                    await asyncio.sleep(1.0)
                    if browser.tabs:
                        tab = browser.tabs[0]
                        await tab.evaluate("1 + 1")
                        log.debug("浏览器连接验证成功")
                    else:
                        raise Exception("No tabs available after startup")
            except Exception as verify_err:
                log.warning("浏览器已启动但CDP连接验证失败（第 %d/%d 次）：%s", i, attempts, verify_err)
                # 如果是连接验证失败，我们仍然重试，因为Chrome可能需要更时间来完全启动
                if i >= attempts:
                    raise
                await asyncio.sleep(1.0 * i)  # 短暂等待后重试
                continue

            return browser
        except Exception as e:
            last_err = e
            log.warning("浏览器启动失败（第 %d/%d 次）：%s", i, attempts, e)
            _reap_profile_chrome(config.user_data_dir)
            if i < attempts:
                # 指数退避加抖动，避免厉害模式
                delay = (2.0 ** i) + (0.5 * i)  # 2, 4.5, 8秒等
                await asyncio.sleep(min(delay, 10.0))  # 最大10秒
    assert last_err is not None
    raise last_err


async def open_browser_with_options(url: str, browser: str) -> None:
    """启动 Chrome 并打开指定 URL"""
    global _browser, _tab
    if browser != "chrome":
        raise NotImplementedError(f"browser={browser!r} 不支持，只走 Chrome")
    
    os.makedirs(ZHLIAN_PROFILE_DIR, exist_ok=True)
    _ensure_localhost_bypasses_proxy()
    _reap_profile_chrome(ZHLIAN_PROFILE_DIR)
    
    config = Config()
    config.user_data_dir = ZHLIAN_PROFILE_DIR
    config.headless = False
    _apply_sandbox_setting(config)
    
    _browser = await _start_browser_with_retry(config)
    # 新浏览器会话 = 新的重载探测窗口，别把上一次 run 的采样带进来。
    _reload_probe_window.clear()
    # 自己开一个窗口放控制 tab（别去猜启动时那些 tab 哪个是我们的）；
    # **不要事后去关「多余」窗口** —— 在 GUI 的 portal loop 里 close 会把我们自己的
    # CDP 连接一起带走（BOSS 那边实测过：之后每次 evaluate 都 ConnectionClosedError）。
    _tab = await _browser.get(url, new_window=True)
    
    try:
        await _tab.activate()
        await _tab.bring_to_front()
    except Exception as e:
        log.warning("激活控制 tab 失败: %s", e)
    
    log.info("页面加载中... 当前URL: %s", _tab.url)
    stable_url = await _wait_url_stable(stable_for=2.0, timeout=30)
    log.info("页面已稳定，当前URL: %s", stable_url)


async def log_in() -> None:
    """扫码登录"""
    if await _is_logged_in():
        log.info("检测到已登录，跳过扫码")
        return
    
    cur_url = _tab.url
    log.info("log_in 入口 URL: %s", cur_url)
    
    # 尝试点击登录按钮
    if not _on_login_page(cur_url):
        try:
            login_btn = await _tab.find("登录", best_match=True, timeout=15)
            if login_btn:
                await login_btn.click()
                log.info("已点击登录入口")
                await _wait_url_stable(stable_for=2.0, timeout=15)
        except Exception as e:
            log.warning("找不到登录入口: %s", e)
    
    # 等待用户登录（智联支持手机号+验证码 / 扫码）
    wait_seconds = _login_wait_seconds()
    log.warning(
        "未检测到登录态：请在打开的 Chrome 窗口里完成登录。程序每 2 秒检查一次，"
        "登录成功后自动继续，最多等 %.0f 秒（BOSS_LOGIN_TIMEOUT 可调）。",
        wait_seconds,
    )
    deadline = asyncio.get_event_loop().time() + wait_seconds
    while asyncio.get_event_loop().time() < deadline:
        if await _is_logged_in():
            log.info("登录成功！cookie 已写入 profile")
            return
        await asyncio.sleep(2)

    # 以前这里只 warning 一句就放行，未登录状态下会拿游客页一路空跑 —— 直接停。
    raise LoginRequiredError(
        f"等了 {wait_seconds:.0f} 秒仍没检测到登录态，已停下。"
        "智联的职位页（/recommend）未登录会 302 到 passport.zhaopin.com/login，"
        "所以必须登录后才能跑。建议：在 GUI 里点「登录」开一个干净窗口登录，"
        "登录成功、能看到岗位列表后关掉那个窗口，再点「开始」。"
    )


# ========== JS 评估辅助 ==========


async def _refresh_tab() -> bool:
    """当前 tab 的 target 已经没了，从 browser 里另挑一个页面 target 接上。

    Chrome 报 `No target with given id found` 就是这种情况：手里那个 target 被换掉了。
    ``attach()`` 救不了一个不存在的 target，只能重新拿一个（``newtab`` 优先排除）。
    """
    global _tab
    if _browser is None:
        return False
    try:
        await _browser.update_targets()
    except Exception as e:  # noqa: BLE001
        log.warning("刷新 target 列表失败：%s", e)
    candidates = [
        t for t in (_browser.tabs or [])
        if "newtab" not in (getattr(t.target, "url", "") or "")
    ] or list(_browser.tabs or [])
    if not candidates:
        return False
    _tab = candidates[0]
    try:
        await _tab.attach()
    except Exception as e:  # noqa: BLE001
        log.warning("接入新 tab 失败：%s", e)
        return False
    log.info("控制 tab 的 target 已失效，已改接 %s", (getattr(_tab.target, "url", "") or "")[:80])
    return True


async def _recover_tab() -> bool:
    """CDP 会话/目标失效后的自愈：先重新 attach，不行就换一个 target 接上。"""
    if _tab is None:
        return False
    try:
        await _tab.attach()
        log.info("CDP session 失效，已重新 attach 控制 tab")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("重新 attach 控制 tab 失败（%s），改为另接一个 tab", e)
    return await _refresh_tab()


_UNSET = object()


async def _safe_evaluate(js: str, timeout: float = 10) -> dict:
    """安全执行 JS 并返回 dict。

    session/target 失效（-32001 / -32602）会自愈一次再重试 —— GUI 的 portal loop 下会
    偶发，不救的话 evaluate 全返回空 dict：登录判定永远"未登录"，整个 run 卡在等待登录里
    空转，而 stdout 看着一切正常。
    """
    js_head = js.strip()[:80].replace("\n", " ")
    raw = _UNSET
    for attempt in (1, 2):
        try:
            raw = await asyncio.wait_for(_tab.evaluate(js), timeout=timeout)
            break
        except asyncio.TimeoutError:
            log.warning("evaluate 超时（JS: %s...）", js_head)
            return {}
        except Exception as e:  # noqa: BLE001
            if attempt == 1 and _is_stale_session_error(e) and await _recover_tab():
                continue
            log.warning("evaluate 出错: %s（JS: %s...）", type(e).__name__, js_head)
            return {}
    if raw is _UNSET:
        return {}
    if isinstance(raw, tuple):
        raw = raw[0]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("JS 返回非法 JSON：%s", e)
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


async def _js_click_at_index(css_selector: str, index_1: int) -> dict:
    """点击第 N 个元素（1-indexed）"""
    js = f"""
    JSON.stringify((() => {{
      try {{
        const els = document.querySelectorAll({json.dumps(css_selector)});
        if (els.length < {index_1}) return {{ok: false, total: els.length}};
        els[{index_1 - 1}].click();
        return {{ok: true, total: els.length}};
      }} catch (e) {{
        return {{ok: false, error: String(e)}};
      }}
    }})())
    """
    return await _safe_evaluate(js)


async def _js_wait_text(css_selector: str, min_len: int, timeout_s: float) -> str | None:
    """等待元素出现且文本长度足够"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        js = f"""
        JSON.stringify((() => {{
          try {{
            const el = document.querySelector({json.dumps(css_selector)});
            if (!el) return {{ok: false, reason: 'not_found'}};
            const text = (el.innerText || '').trim();
            if (text.length < {min_len}) return {{ok: false, reason: 'too_short', len: text.length}};
            return {{ok: true, text: text}};
          }} catch (e) {{
            return {{ok: false, error: String(e)}};
          }}
        }})())
        """
        result = await _safe_evaluate(js, timeout=5)
        if result.get("ok"):
            return result["text"]
        await asyncio.sleep(0.5)
    return None


async def _xpath_safe(xp: str, timeout: float = 3.0) -> list:
    """安全执行 xpath"""
    try:
        result = await asyncio.wait_for(
            _tab.xpath(xp, timeout=timeout),
            timeout=timeout + 2,
        )
        return result or []
    except asyncio.TimeoutError:
        log.warning("xpath 超时: %s", xp[:80])
        return []
    except Exception as e:
        log.warning("xpath 出错: %s", type(e).__name__)
        return []


def _strip_jd_noise(text: str) -> str:
    """剥离 JD 开头的噪声行"""
    lines = (text or "").split("\n")
    i = 0
    while i < len(lines) and lines[i].strip() in JD_NOISE_LINES:
        i += 1
    return "\n".join(lines[i:]).strip()


# ========== 业务 helper ==========


async def select_dropdown_option(label: str) -> None:
    """选择职位筛选标签（智联招聘的实现）"""
    if not label:
        log.info("label 为空，使用默认推荐 feed")
        return

    log.info("尝试选择标签: %r", label)
    try:
        # 智联招聘常见的筛选触发器选择器
        filter_selectors = [
            ".job-search-condition-item",  # 条件项容器
            ".search-condition-item",      # 搜索条件项
            ".filter-item",                # 过滤项
            "[class*='condition']",        # 包含condition的类
            "[class*='filter']",           # 包含filter的类
            "div.tab-item",                # 标签项
            "li.tab-item",                 # 列表标签项
        ]

        # 尝试找到并点击筛选触发器
        filter_triggered = False
        for selector in filter_selectors:
            try:
                elements = await _tab.select_all(selector, timeout=3)
                for element in elements:
                    # 检查元素文本是否包含目标标签
                    element_text = await element.evaluate("(el) => el.textContent || el.innerText || ''")
                    if label in element_text:
                        await element.click()
                        log.info("找到并点击匹配的筛选项: %s", label)
                        filter_triggered = True
                        await asyncio.sleep(2)  # 等待选项展开
                        break
                if filter_triggered:
                    break
            except Exception:
                continue

        if not filter_triggered:
            # 如果没找到精确匹配，尝试点击通用的筛选/更多按钮
            more_selectors = [
                "text=更多条件",
                "text=更多筛选",
                ".more-filter",
                "[title='更多条件']",
                "[title='更多筛选']"
            ]

            for selector in more_selectors:
                try:
                    element = await _tab.select(selector, timeout=2)
                    if element:
                        await element.click()
                        log.info("点击了更多筛选按钮")
                        await asyncio.sleep(2)
                        filter_triggered = True
                        break
                except Exception:
                    continue

        if filter_triggered:
            # 尝试在展开的选项中找到匹配的标签
            option_selectors = [
                f"text={label}",
                f"[title='{label}']",
                f".option:contains('{label}')",
                f".filter-option:contains('{label}')",
                f".condition-item:contains('{label}')",
                f"li:contains('{label}')",
                f"div:contains('{label}')"
            ]

            option_found = False
            for selector in option_selectors:
                try:
                    # 尝试不同的选择器类型
                    if selector.startswith("text=") or selector.startswith("[title="):
                        # 这些是文本选择器，需要特殊处理
                        text_content = selector.split("=", 1)[1].rstrip("'\"")
                        elements = await _tab.select_all(f"*:contains('{text_content}')", timeout=3)
                    else:
                        elements = await _tab.select_all(selector, timeout=3)

                    for element in elements:
                        element_text = await element.evaluate("(el) => el.textContent || el.innerText || ''")
                        if label in element_text:
                            await element.click()
                            log.info("在展开的选项中找到并点击: %s", label)
                            option_found = True
                            await asyncio.sleep(2)  # 等待选择生效
                            break
                    if option_found:
                        break
                except Exception:
                    continue

            if not option_found:
                log.warning("未能在展开的选项中找到精确匹配的标签: %s，已展开筛选菜单", label)
        else:
            log.warning("未能触发筛选菜单，请检查页面结构或手动选择标签: %s", label)

    except Exception as e:
        log.error("选择标签时发生错误: %s", e)
        log.warning("标签选择失败，请手动在浏览器中选择: %s", label)


async def get_job_description_by_index(index: int) -> str | None:
    """点击第 N 个岗位卡，返回 JD 文本"""
    log.info("get_job_description_by_index: index=%d", index)
    
    click_result = await _js_click_at_index(SELECTORS["job_card"], index)
    log.info("点击岗位卡: %s", click_result)
    if not click_result.get("ok"):
        return None
    
    # 按候选顺序试：先最干净的 JD 正文，再退回整个右栏面板（实测两个 class 都在，
    # 但正文那个更干净 —— 面板里混着"收藏/分享/举报/立即投递"这些 UI 文字）。
    candidates = SELECTORS.get("job_detail_candidates") or [SELECTORS["job_detail"]]
    jd = None
    for i, selector in enumerate(candidates):
        jd = await _js_wait_text(selector, min_len=50, timeout_s=10 if i == 0 else 5)
        if not jd:
            log.info("  %s 没等到 JD 文本，试下一个候选", selector)
            continue
        # JD 是分片渲染的：`_js_wait_text` 一达到 50 字就返回，这时候可能只挂了一半。
        # 实测同一张卡能先读到 107 字、等一下再读就是 500+ 字。等一拍取更长的那个，
        # 否则喂给匹配/LLM 的就是残缺 JD。
        await asyncio.sleep(0.8)
        again = await _js_wait_text(selector, min_len=50, timeout_s=2)
        if again and len(again) > len(jd):
            jd = again
        break
    if jd is None:
        log.info("所有 JD 候选选择器都没等到足够长的文本：%s", candidates)
        return None
    
    jd = _strip_jd_noise(jd)
    log.info("JD 长度 %d 字符", len(jd))
    return jd


async def get_loaded_job_count() -> int:
    """返回当前已加载的岗位卡数量"""
    result = await _safe_evaluate(
        f"""
        JSON.stringify((() => {{
          return {{count: document.querySelectorAll('{SELECTORS["job_card"]}').length}};
        }})())
        """,
        timeout=5,
    )
    return int(result.get("count") or 0)


_reload_probe_window: list[tuple[float, float]] = []


async def _document_time_origin() -> float | None:
    """当前文档的 performance.timeOrigin；拿不到返回 None。"""
    result = await _safe_evaluate(
        "JSON.stringify((() => ({t: performance.timeOrigin}))())", timeout=5
    )
    value = result.get("t")
    return float(value) if isinstance(value, (int, float)) else None


async def _raise_if_page_reloading() -> None:
    """页面卡在整页反复重载时抛 PageReloadingError。

    只有整页导航才会重置 timeOrigin（SPA 重渲染不会），所以"窗口内出现过几个不同的
    timeOrigin"就等于这几十秒里页面整页刷新了几次。

    ⚠️ 异常类是从 finding_jobs 借的**同一个类**：write_response 里那个
    except PageReloadingError 才能抓到智联这条路上的重载。
    """
    origin = await _document_time_origin()
    count = _record_document_origin(
        _reload_probe_window, origin, asyncio.get_event_loop().time()
    )
    if count >= _RELOAD_LOOP_THRESHOLD:
        raise PageReloadingError(
            f"智联页面卡在整页反复重载：最近 {_RELOAD_WINDOW_SECONDS:.0f} 秒内整页刷新了 "
            f"{count} 次。DOM 里的岗位卡时有时无，脚本每次点卡片都撞在重载空档上。"
            "最常见的原因：未登录被 passport 反复弹回登录页（/recommend 未登录会 302），"
            "其次是平台风控在刷。建议先在 GUI 里点「登录」开干净窗口登录，"
            "能稳定看到岗位列表之后再跑。"
        )


async def _wait_for_job_cards(timeout: float = 5.0) -> int:
    """等岗位卡重新出现在 DOM 里，返回最终数量（一直是 0 就返回 0）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    count = await get_loaded_job_count()
    while count == 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.3)
        count = await get_loaded_job_count()
    await _raise_if_page_reloading()
    return count


async def scroll_to_load_more_jobs(timeout: float = 8.0) -> bool:
    """滚动加载更多岗位。

    **卡片数为 0 时一次都不滚**：那是 SPA 在重卸载/重挂的瞬间，window.scrollTo 只会把它
    踹得更狠（页面猛跳 → 再重挂 → 又是 0），日志刷一串 0 -> 15 的假进展，用户看到的是
    「页面一直跳、像在刷新」。等到卡片回来就返回 True，让主循环重试同一个 index。
    """
    before = await get_loaded_job_count()
    if before == 0:
        restored = await _wait_for_job_cards()
        if restored:
            log.info("岗位卡正在重渲染（0 -> %d），等它回来，本轮不滚动", restored)
            return True
        log.info("岗位卡持续为 0，本轮不滚动")
        return False

    # 简单滚动实现
    await _safe_evaluate(
        """
        JSON.stringify((() => {
          window.scrollTo(0, document.body.scrollHeight);
          return {};
        })())
        """,
        timeout=5,
    )
    
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        after = await get_loaded_job_count()
        if after > before:
            log.info("滚动加载更多岗位：%d -> %d", before, after)
            return True
    
    log.info("滚动后岗位数量未变化：%d", before)
    return False


async def get_text_by_css(selector: str, timeout: float = 5) -> str | None:
    """返回 CSS 选择器命中元素的 text"""
    try:
        el = await _tab.select(selector, timeout=timeout)
    except Exception:
        return None
    return el.text if el else None


async def click_by_xpath(xpath: str, timeout: float = 10) -> bool:
    """通过 xpath 点击元素"""
    els = await _xpath_safe(xpath, timeout=timeout)
    if not els:
        return False
    await els[0].click()
    return True


async def wait_for_css(selector: str, timeout: float = 50) -> bool:
    """等待 CSS 选择器命中"""
    try:
        el = await _tab.select(selector, timeout=timeout)
    except Exception:
        return False
    return el is not None


_CLICK_APPLY_JS = """JSON.stringify((() => {
  const wanted = %s;
  const els = Array.from(document.querySelectorAll(wanted + ", button"));
  const el = els.find((e) => {
    const r = e.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    const cls = typeof e.className === "string" ? e.className : "";
    if (cls.indexOf("job-detail-summary__apply") >= 0) return true;
    return (e.innerText || "").trim() === "立即投递";
  });
  if (!el) return {ok: false};
  el.click();
  return {ok: true, text: (el.innerText || "").trim().slice(0, 20), cls: String(el.className || "").slice(0, 60)};
})())"""


_DISMISS_APPLY_JS = """JSON.stringify((() => {
  const modal = document.querySelector(%s);
  if (!modal) return {absent: true};
  const r = modal.getBoundingClientRect();
  const visible = r.width > 0 && r.height > 0
    && getComputedStyle(modal).display !== "none";
  if (!visible) return {absent: true};
  const btns = Array.from(modal.querySelectorAll("button, a, span"));
  const target = btns.find((b) => {
    const t = (b.innerText || "").trim();
    return t === "留在此页" || t === "继续沟通";
  });
  if (!target) return {found: true, closed: false,
    text: (modal.innerText || "").trim().slice(0, 60)};
  target.click();
  return {found: true, closed: true, 
    clicked: (target.innerText || "").trim().slice(0, 20)};
})())"""


async def dismiss_apply_dialog(timeout: float = 6.0) -> bool:
    """关掉投递后弹的确认框（``.deliver-greeting-modal``）。

    实测：点「立即投递」后会弹一个 "已向对方发送简历和打招呼语"，带「留在此页」/
    「继续沟通」。**不关掉会一直盖住页面**，后面每一轮点岗位卡都会失败（点不到）。
    点「留在此页」= 留在当前列表继续投下一个；「继续沟通」会跳到聊天页，不适合批投。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        result = await _safe_evaluate(
            _DISMISS_APPLY_JS % json.dumps(SELECTORS["apply_dialog"]), timeout=5
        )
        if result.get("absent"):
            log.debug("没有投递弹框，跳过")
            return False
        if result.get("closed"):
            log.info("已关闭投递弹框（点击 %r）", result.get("clicked"))
            await asyncio.sleep(1)
            return True
        if asyncio.get_event_loop().time() >= deadline:
            log.warning("投递弹框没能关掉（%r），后面几轮可能点不动岗位卡",
                        result.get("text"))
            return False
        await asyncio.sleep(0.5)


async def click_apply(timeout: float = 5) -> bool:
    """点「立即投递」（走 JS），点完把确认弹框关掉。

    以前用 ``_tab.find("立即投递")`` + ``_tab.select()``：前者靠 nodriver 的文本匹配
    （不稳），后者是 CSS 但命中不到就静默返回 False；而且**完全没管投完弹出来的
    ``.deliver-greeting-modal``** —— 那个弹框会把页面盖住，下一轮点卡必然失败，
    表现成"投了一两个就卡住"。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        result = await _safe_evaluate(
            _CLICK_APPLY_JS % json.dumps(SELECTORS["apply_btn"]), timeout=5
        )
        if result.get("ok"):
            log.info("已点击「立即投递」（按钮 %r）", result.get("text"))
            break
        if asyncio.get_event_loop().time() >= deadline:
            log.warning("没找到「立即投递」按钮（%.0fs 超时）", timeout)
            return False
        await asyncio.sleep(0.5)

    await asyncio.sleep(2)
    await dismiss_apply_dialog()
    return True


async def reload_page() -> None:
    """刷新页面"""
    await _safe_evaluate("JSON.stringify((() => {location.reload(); return {};})())")
    await asyncio.sleep(2)


async def wait_for_real_job_cards(timeout: float = 30) -> bool:
    """等待岗位卡片渲染完成"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await _safe_evaluate(
            f"""
            JSON.stringify((() => {{
              const cards = document.querySelectorAll('{SELECTORS["job_card"]}');
              return {{
                texts: Array.from(cards).map(c => (c.innerText || '').trim()),
              }};
            }})())
            """,
            timeout=5,
        )
        # 检查是否有实际内容的卡片（排除骨架屏）
        texts = result.get("texts", [])
        real_count = sum(1 for t in texts if len(t.strip()) > 10)
        if real_count:
            log.info("岗位卡已渲染（有内容的 %d 张）", real_count)
            return True
        await asyncio.sleep(1)
    return False


async def navigate_back() -> None:
    """返回上一页"""
    await _tab.evaluate("history.back()")
    await asyncio.sleep(3)


async def return_to_job_list(timeout: float = 12.0, max_attempts: int = 2) -> bool:
    """返回岗位列表"""
    for attempt in range(1, max_attempts + 1):
        await navigate_back()
        if await wait_for_css(SELECTORS["job_card"], timeout=timeout):
            return True
        log.warning("返回岗位列表第 %d 次失败", attempt)
    return False


# ========== 常量（向后兼容） ==========

url = ZHLIAN_RECOMMEND_URL
browser_type = "chrome"