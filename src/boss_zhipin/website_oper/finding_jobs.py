"""nodriver-backed BOSS automation.

整个模块对外**全部是 async 函数**。调用方（``write_response.py`` 主循环）
也必须 async，并由 ``main.py`` 用 ``uc.loop().run_until_complete(...)`` **整段
跑在一个事件循环里**。

为什么这么设计：之前对外是 sync facade（每个公开函数内部 ``_run`` 调一次
``uc.loop().run_until_complete``），结果每次 enter/exit 事件循环都让 nodriver
的 CDP websocket 进入半死状态，下一次 evaluate 直接 hang 到 timeout。
``scripts/probe_click_card.py`` 单 coroutine 跑同样的 evaluate 是秒返回，
证实就是 sync facade 模式跟 nodriver 的活跃性需求不兼容。
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

log = logging.getLogger(__name__)

# 持久化 Chrome profile：第一次手动扫码后 cookie 会留下，之后跑就跳过登录。
# 可用 BOSS_CHROME_PROFILE 环境变量覆盖路径。
CHROME_PROFILE_DIR = os.path.abspath(
    os.environ.get("BOSS_CHROME_PROFILE", "./chrome_profile")
)

_browser: uc.Browser | None = None
_tab: uc.Tab | None = None


def _env_flag_true(name: str) -> bool:
    """环境变量布尔开关解析：1/true/yes/on 视为真。"""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _should_disable_sandbox() -> bool:
    """是否关闭 Chrome sandbox。

    优先级：
    1) 显式环境变量 ``BOSS_NO_SANDBOX=1`` 强制关闭；
    2) Linux/macOS 下检测到 root 用户时自动关闭；
    3) 其他情况保持开启。

    第 2 条 nodriver 的 ``Config.__init__`` 自己也会做（posix + root 时置
    ``sandbox=False``），这里重复一遍只为能在日志里提前告知用户。
    """
    if _env_flag_true("BOSS_NO_SANDBOX"):
        return True

    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        try:
            return geteuid() == 0
        except Exception:  # noqa: BLE001
            return False
    return False


def _apply_sandbox_setting(config) -> bool:
    """按需在 ``config`` 上关掉 Chrome sandbox，返回最终是否启用 sandbox。

    **必须改 ``config.sandbox``，不能走 ``uc.start(sandbox=...)``**：nodriver 的
    ``util.start`` 只在 ``if not config:`` 分支里把 ``sandbox`` 传进 ``Config``，
    我们这里一定自己建 Config 再传进去，那个 kwarg 会被静默丢弃（不报错、不生效）。
    ``--no-sandbox`` 最终由 ``Config.__call__`` 依据 ``self.sandbox`` 生成。

    只在需要关闭时赋值：nodriver 在 posix + root 下已自行置 False，无条件赋 True
    会把上游这份自动处理覆盖掉。
    """
    if _should_disable_sandbox():
        config.sandbox = False
        return False
    return bool(getattr(config, "sandbox", True))


def get_tab() -> uc.Tab | None:
    """同步读当前控制的 Tab 引用。仅用作内省。"""
    return _tab


def get_driver():
    raise RuntimeError(
        "get_driver() 已移除：项目已迁到 nodriver，没有 Selenium driver。"
        "请改用 finding_jobs 里的 async helper（get_text_by_css/click_contact_button/"
        "wait_for_css/send_chat_message/navigate_back）。"
    )


def _on_login_page(url: str) -> bool:
    return any(s in url for s in ("/web/user/", "passport-zp", "/login"))


# 判定"页面真的渲染出来了"的最小正文长度。BOSS 整页导航的瞬间，DOM 会先是一具
# 空壳（readyState 可能已经是 complete，但 body 里几乎没内容）——那时候三个未登录
# 信号一个都查不到，"没有信号"就会被误读成"已登录"。
# 实测踩过：未登录的一轮里 log_in 报「检测到已登录，跳过扫码」，而页面顶部明明写着
# 「登录/注册」，于是主流程直接开抓游客推荐流。
_MIN_RENDERED_BODY_LEN = 200


def _is_logged_in_from_page_state(url: str, info: dict) -> bool:
    """从 URL + DOM 探测结果判断登录态。

    BOSS 未登录时不一定停在登录页：可能落在职位列表页，但顶部仍有
    "登录/注册"，详情正文也会显示"登录查看完整内容"。这些都必须判为未登录，
    否则主流程会误以为已登录，跳过扫码等待后直接开始抓 JD。

    **页面没渲染出来时一律判未登录**：空白文档里什么信号都查不到，而"没有未登录
    信号"不等于"已登录"。宁可多等一次扫码，也不能拿游客 feed 空跑。
    """
    if _on_login_page(url):
        return False
    if (info.get("bodyLength") or 0) < _MIN_RENDERED_BODY_LEN:
        return False
    ready = info.get("readyState")
    if ready is not None and ready != "complete":
        return False
    if info.get("loginWallVisible"):
        return False
    if info.get("headerLoginVisible"):
        return False
    if info.get("headerLoginText"):
        return False
    if info.get("loginRequiredVisible"):
        return False
    return True


async def _is_logged_in() -> bool:
    """判定登录态：URL + DOM 双 check。

    早期 BOSS 对未登录用户一定 redirect 到 ``/web/user/`` 类路径，只看 URL 够用。
    现在它经常**不 redirect**，而是直接在 ``/web/geek/...`` 原地盖一个登录浮层。
    只看 URL 会把这种情况误判成已登录，后续 ``get_job_description`` 抓到的是
    浮层背后的 ``<style>`` 噪音（关键词命中 0/2 → feed_exhausted 假阴性）。

    所以加一层 DOM 探测：页面里有可见的登录浮层就强制判未登录。选不到浮层时
    退回原 URL 判定，避免 BOSS 改 class 名后把真实已登录态误杀。
    """
    if _tab is None:
        return False
    # URL 命中已知登录页路径 → 直接未登录，连 DOM 都不用问
    if _on_login_page(_tab.url):
        return False

    # BOSS 的登录浮层 class 名换过几版：boss-login-dialog / login-dialog-wrap /
    # loginDialog 等，统一用 attribute selector 兜
    js = """
    JSON.stringify((() => {
      const visible = (el) => {
        if (!el) return false;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const wall = document.querySelector(
        '[class*="login-dialog"], [class*="boss-login"], '
        + '[class*="loginDialog"], [class*="login-wrap"]'
      );
      const headerLogin = document.querySelector(
        '.header-login-btn, a[ka="header-login"], [ka="guide_login_btn_click"], '
        + '.guide-login-btn, .zp-job-list-login-card'
      );
      const bodyText = document.body ? document.body.innerText || '' : '';
      return {
        // 页面有没有真的渲染出来（见 _MIN_RENDERED_BODY_LEN）：空白文档不能当已登录
        readyState: document.readyState,
        bodyLength: bodyText.length,
        // 顶部「登录/注册」是未登录最直接的证据，不再只靠 class 名兜
        headerLoginText: bodyText.includes('登录/注册'),
        loginWallVisible: visible(wall),
        headerLoginVisible: visible(headerLogin),
        loginRequiredVisible: bodyText.includes('登录查看完整内容')
          || bodyText.includes('登录账号，查看更多好职位')
      };
    })());
    """
    info = await _safe_evaluate(js, timeout=5)
    logged_in = _is_logged_in_from_page_state(_tab.url, info)
    if not logged_in:
        log.info("判定未登录（页面信号 %s）→ 先让用户登录，不碰岗位列表", info)
        return False
    return True


async def _wait_url_stable(stable_for: float = 2.0, timeout: float = 30) -> str:
    """等到 tab.url 连续 stable_for 秒不变，避开 BOSS 登录页的重定向抖动。"""
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


# ---------- 浏览器生命周期 ----------


async def shutdown() -> None:
    """关 Chrome 并清空模块级 ``_browser`` / ``_tab``。

    给 GUI 用——用户点"重置"想从头来一遍时调一次，否则下次
    ``open_browser_with_options`` 会留旧 Chrome 进程。CLI 不需要：``main.py``
    退出时 OS 会清理子进程。

    ``Browser.stop()`` 是同步函数，但本函数声明 ``async`` 是为了让 GUI
    runner 能 ``await shutdown()``（runner 一律 await，sync/async 不混用）。

    重启注意：这只关 Chrome 进程；nodriver 跟 uvloop 的重启不兼容问题需要
    GUI 入口传 ``loop="asyncio"`` 才能彻底解决（见 project memory）。
    """
    global _browser, _tab
    if _browser is not None:
        try:
            _browser.stop()
        except Exception as e:
            log.warning("browser.stop() 失败（不致命）: %s", e)
    _browser = None
    _tab = None


def _clear_singleton_locks(profile_dir: str) -> None:
    """删掉 profile 里残留的 Singleton 锁文件。

    上次 Chrome 没退干净会留下 ``SingletonLock/Cookie/Socket``，新 Chrome 看到会
    误判 profile 被占。纯文件操作，便于单测。
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(profile_dir, name))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 — best-effort 清理，失败不致命
            log.debug("清 Singleton 锁 %s 跳过：%s", name, e)


def _ensure_localhost_bypasses_proxy() -> None:
    """确保 127.0.0.1/localhost 不走代理。

    用户开了 Clash 等代理后 http_proxy 环境变量会拦截所有 HTTP 请求，
    包括 nodriver 连 Chrome CDP 的 http://127.0.0.1:<port>/json/version，
    代理返回 502 → nodriver 报 "Failed to connect to browser"。

    设 env 就够：``ProxyHandler.proxy_open`` 每次请求都现调 ``proxy_bypass``
    重读环境变量，不受 urllib 缓存 opener 的影响。
    """
    # 必须用 or 串联：POSIX 上这俩是独立变量，no_proxy 被设成空串时 get 的
    # default 分支不执行，读不到 NO_PROXY 就会把用户原有的 bypass 列表覆盖掉。
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    existing = [s.strip() for s in no_proxy.split(",") if s.strip()]
    lowered = {s.lower() for s in existing}  # urllib 比对前也 .lower()
    missing = [h for h in ("127.0.0.1", "localhost") if h not in lowered]
    if not missing:
        return
    new_val = ",".join(existing + missing)  # 追加，保留原有条目和顺序
    os.environ["no_proxy"] = new_val
    os.environ["NO_PROXY"] = new_val
    log.debug("no_proxy 补上 %s → %s", missing, new_val)


def _kill_profile_chrome(profile_dir: str) -> None:
    """杀掉占着本 profile 的残留 Chrome（上次 run 崩了没收掉的孤儿）。

    profile 目录本工具独占，``--user-data-dir=<profile>`` 命中的一定是我们自己的
    残留，杀掉安全。这是用户实测的"连接失败"根因：上次启动起了 Chrome 但 CDP 没
    连上，孤儿一直占着 profile，下次再起就被锁。
    """
    if sys.platform == "win32":
        # Windows 按命令行过滤进程不便（无 pkill），跳过——靠清锁 + Chrome 自身的
        # stale-lock 接管兜底；macOS/Linux 才主动收孤儿。
        return
    try:
        subprocess.run(
            ["pkill", "-f", os.path.abspath(profile_dir)],
            capture_output=True,
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001 — 没装 pkill / 无匹配都不致命
        log.debug("pkill 残留 Chrome 跳过：%s", e)


def _reap_profile_chrome(profile_dir: str) -> None:
    """起浏览器前的自清理：先收掉占 profile 的孤儿 Chrome，再清 Singleton 锁。"""
    _kill_profile_chrome(profile_dir)
    _clear_singleton_locks(profile_dir)


async def _start_browser_with_retry(config: Config, attempts: int = 3) -> uc.Browser:
    """启动并连上 Chrome，失败重试。

    新版 Chrome（如 149）+ 新 macOS 冷启动时，CDP 端口起得慢，nodriver 第一次连
    经常 timeout 报 "Failed to connect to browser"，但 Chrome 其实已经起来了 →
    变孤儿占住 profile。所以每次失败都先 reap（杀掉这次起的、没连上的 Chrome +
    清锁），再退避重试。
    """
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
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("浏览器启动/连接失败（第 %d/%d 次）：%s", i, attempts, e)
            _reap_profile_chrome(config.user_data_dir)
            if i < attempts:
                # 指数退避加抖动，避免厉害模式
                delay = (2.0 ** i) + (0.5 * i)  # 2, 4.5, 8秒等
                await asyncio.sleep(min(delay, 10.0))  # 最大10秒
    assert last_err is not None
    raise last_err


async def open_browser_with_options(url: str, browser: str) -> None:
    """启动 Chrome 并打开 url。``browser`` 仅接受 ``"chrome"``。"""
    global _browser, _tab
    if browser != "chrome":
        raise NotImplementedError(
            f"browser={browser!r} 不再支持；nodriver 只走 Chrome。"
        )
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    # nodriver 用 urllib 连 Chrome CDP（http://127.0.0.1:<port>/json/version）。
    # 如果用户开了代理（如 Clash），http_proxy 会拦截 localhost 请求导致 502。
    _ensure_localhost_bypasses_proxy()
    # 起之前先收掉上次没退干净、占着本 profile 的孤儿 Chrome + 清残留锁，
    # 否则会复现用户实测的 "Failed to connect to browser"（profile 被旧实例锁住）。
    _reap_profile_chrome(CHROME_PROFILE_DIR)
    config = Config()
    config.user_data_dir = CHROME_PROFILE_DIR
    config.headless = False
    # 注意：这里只影响 Chrome 进程能不能起来，跟 nodriver 连不上 CDP 时抛的
    # "Failed to connect to browser" 是两条不同的链路，别拿它当那个报错的解药。
    if _apply_sandbox_setting(config):
        log.info("Chrome sandbox: enabled")
    else:
        log.warning("Chrome sandbox: disabled（root 或 BOSS_NO_SANDBOX=1，按需使用）")
    _browser = await _start_browser_with_retry(config)
    # 新浏览器会话 = 新的重载探测窗口，别把上一次 run 的采样带进来。
    _reload_probe_window.clear()

    # 自己开一个窗口放控制 tab，不去猜启动时那些 tab 哪个是我们要的。
    #
    # 这么写是被三件事逼出来的，改之前先看清楚：
    #
    # 1. **不要事后去关"多余"的窗口**。在 GUI 的 portal loop 里
    #    （`gui.runner.start_run` 起的 asyncio task），`tab.close()` 会把我们自己的
    #    CDP 连接一起带走 —— 之后每次 evaluate 都 ConnectionClosedError，表现成
    #    "Chrome 窗口打开两三秒就自己关了、GUI 只显示个 browser_started"。CLI 的
    #    `uc.loop().run_until_complete` 路径碰不到，所以只在 GUI 里炸。多出来的那个
    #    窗口就让它留着。
    # 2. **不要 `new_tab=False`**（直接导航启动时那个 tab）。那个 tab 常常是
    #    `chrome://newtab/`，而 CDP 把它导航到网页并不可靠：`Page.navigate` 返回
    #    frameId 看着像成功，文档其实还停在 new-tab-page，用户看到的就是"只开了个
    #    浏览器主窗口、没跳到 BOSS"，脚本一路读到 bodyLength=0。
    # 3. 持久化 profile 还会把上次的会话恢复出来，启动时的 `browser.tabs` 顺序
    #    不稳定；自己开一个窗口能保证拿到的一定是我们在用的那个。
    _tab = await _browser.get(url, new_window=True)
    try:
        await _tab.activate()
        await _tab.bring_to_front()
    except Exception as e:
        log.warning("激活控制 tab 失败（%s），不影响后续操作", e)
    # 这里原来会主动 enable DOM/Page/Input/Runtime 四个 CDP 域（为了绕当时 xpath 报的
    # 'DOM.enable' wasn't found）。现在主路径全部走 JS，那四个域一个都不需要了 ——
    # 而且**主动开 Runtime/DOM 是典型的自动化探测面**，风控看得见。少开一个是一个。
    log.info("页面加载中... 当前URL: %s", _tab.url)
    stable_url = await _wait_url_stable(stable_for=2.0, timeout=30)
    log.info("页面已稳定，当前URL: %s", stable_url)


class LoginRequiredError(RuntimeError):
    """未登录、且扫码等待超时。

    单独一个异常类型：以前这里只 ``log.warning`` 一句就放行，主循环会拿着
    **游客通用推荐 feed** 一路空跑（岗位跟简历全对不上 → 一条招呼语都发不出去），
    用户只看到"窗口一直在跳"却不知道卡在哪。现在直接抛出来，让 GUI / CLI 停下
    并把原因打给用户。仍是 ``RuntimeError`` 子类，对外行为不变。
    """


def _login_wait_seconds() -> float:
    """扫码等待时长（秒）。``BOSS_LOGIN_TIMEOUT`` 覆盖，默认 300。

    非法值（非数字 / 负数）回退默认并告警——GUI 配置页能填任意字符串，不能让它
    把整个 run 弄崩。
    """
    raw = os.getenv("BOSS_LOGIN_TIMEOUT", "").strip()
    if not raw:
        return 300.0
    try:
        value = float(raw)
    except ValueError:
        log.warning("BOSS_LOGIN_TIMEOUT=%r 不是数字，回退默认 300 秒", raw)
        return 300.0
    if value < 0:
        log.warning("BOSS_LOGIN_TIMEOUT=%r 为负，按 0 处理", raw)
        return 0.0
    return value


async def log_in() -> None:
    """识别登录状态；未登录则点开微信扫码，等用户扫码登录。

    超时未登录抛 ``LoginRequiredError``（见该类 docstring）。
    """
    if await _is_logged_in():
        log.info("检测到已登录（profile: %s），跳过扫码", CHROME_PROFILE_DIR)
        return

    cur_url = _tab.url
    log.info("log_in 入口 URL: %s", cur_url)

    if not _on_login_page(cur_url):
        try:
            login_btn = await _tab.find("登录", best_match=True, timeout=15)
            if login_btn:
                await login_btn.click()
                log.info("已点击 header 登录入口")
                await _wait_url_stable(stable_for=2.0, timeout=15)
        except Exception as e:
            log.warning("找不到 header 登录入口（%s），尝试直接在当前页找微信入口", e)

    try:
        wechat_btn = await _tab.find("微信", best_match=True, timeout=10)
        if wechat_btn:
            await wechat_btn.click()
            log.info("已点击微信登录入口，请扫码...")
        else:
            log.warning("未自动点上微信入口，请在浏览器里手动选择登录方式")
    except Exception as e:
        log.warning("查找微信入口出错（%s），请手动选择登录方式", e)

    wait_seconds = _login_wait_seconds()
    log.warning(
        "未检测到登录态：请在打开的 Chrome 窗口里登录（微信扫码或账号密码；"
        "如果弹出安全验证，也一并过掉）。程序每 2 秒检查一次，登录成功后自动继续，"
        "最多等 %.0f 秒（BOSS_LOGIN_TIMEOUT 可调）。",
        wait_seconds,
    )
    deadline = asyncio.get_event_loop().time() + wait_seconds
    while asyncio.get_event_loop().time() < deadline:
        if await _is_logged_in():
            log.info("登录成功！cookie 已写入 profile，下次跑应该不用再扫")
            return
        await asyncio.sleep(2)
    raise LoginRequiredError(
        f"等了 {wait_seconds:.0f} 秒仍没检测到登录态，已停下（没有拿游客 feed 空跑）。"
        "请登录后重跑。如果那个 Chrome 窗口本身在不停整页刷新（地址栏一闪而过 "
        "security.html / _security_check），是 BOSS 风控在反复要求安全验证——"
        "脚本过不了，需要人工：先关掉脚本，用同一个 profile 手动开一个不带调试端口的 "
        "Chrome（chrome.exe --user-data-dir=<profile 目录>），在里面过掉验证并登录，"
        "再回来重跑脚本。"
    )


# ---------- JS 评估辅助 ----------


def _is_stale_session_error(exc: BaseException) -> bool:
    """Chrome 报 -32001 / "Session with given id not found" —— 我们的 CDP session 过期了。

    纯函数，便于单测。
    """
    text = str(exc)
    return "Session with given id not found" in text or "-32001" in text


async def _refresh_tab() -> bool:
    """当前 tab 的 target 已经没了，从 browser 里另挑一个页面 target 接上。

    Chrome 报 `No target with given id found` 就是这种情况：我们手里那个 target 被换掉
    了（页面被风控/会话恢复重新创建）。``attach()`` 救不了一个不存在的 target，只能
    重新拿一个；``newtab`` 那种空页优先排除。
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
    """CDP 会话/目标失效后的自愈：先重新 attach，不行就换一个 target 接上。

    ``Connection.attach()`` 每次都会重新 ``Target.attachToTarget`` 拿一个新 session id
    （不是"已附加就跳过"），所以 session 过期（-32001）再调一次就能救回来；
    如果连 target 都没了（-32602），就只能换一个 tab。

    实测在 GUI 的 portal loop 下这两种失效都会偶发 —— 不救的话 evaluate 全返回空
    dict，登录判定永远"未登录"，整个 run 卡在等待登录里空转，页面上看着"没反应"。
    """
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
    """跑 ``tab.evaluate(js)``，期望 JS 自己 ``JSON.stringify`` 返回字符串。

    自带 ``asyncio.wait_for`` 兜底 + JSON 解析容错 —— 永远不抛、永远返回 dict
    （失败时空 dict）。

    session 过期（-32001）会重新 attach 后再试一次：这是 GUI 下偶发但致命的一种
    失败 —— 不救的话登录判定永远拿到空 dict，整个 run 就卡在"等待登录"里空转。
    """
    js_head = js.strip()[:80].replace("\n", " ")
    raw = _UNSET
    for attempt in (1, 2):
        try:
            raw = await asyncio.wait_for(_tab.evaluate(js), timeout=timeout)
            break
        except asyncio.TimeoutError:
            log.warning("evaluate 超过 %ss 没返回（JS: %s...）", timeout, js_head)
            return {}
        except Exception as e:  # noqa: BLE001
            if attempt == 1 and _is_stale_session_error(e) and await _recover_tab():
                continue
            log.warning("evaluate 抛 %s: %s（JS: %s...）", type(e).__name__, e, js_head)
            return {}
    if raw is _UNSET:
        return {}
    if isinstance(raw, tuple):
        raw = raw[0]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("JS 返回不是合法 JSON：%s（前 200 字符：%r）", e, raw[:200])
            return {}
    if isinstance(raw, dict):
        return raw
    log.warning(
        "evaluate 返回类型 %s（不是 str/dict）：%r", type(raw).__name__, repr(raw)[:200]
    )
    return {}


async def _js_click_at_index(css_selector: str, index_1: int) -> dict:
    """在 JS 里点 ``css_selector`` 命中的第 N 个元素（1-indexed）。"""
    js = f"""
    JSON.stringify((() => {{
      try {{
        const els = document.querySelectorAll({json.dumps(css_selector)});
        if (els.length < {index_1}) return {{ok: false, total: els.length}};
        els[{index_1 - 1}].click();
        return {{ok: true, total: els.length}};
      }} catch (e) {{
        return {{ok: false, error: String(e), stack: e.stack || ''}};
      }}
    }})())
    """
    return await _safe_evaluate(js)


async def _js_wait_text(
    css_selector: str, min_len: int, timeout_s: float
) -> str | None:
    """轮询直到 ``css_selector`` 命中且 ``text.length >= min_len``，返回 text；超时返回 None。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        js = f"""
        JSON.stringify((() => {{
          try {{
            const el = document.querySelector({json.dumps(css_selector)});
            if (!el) return {{ok: false, reason: 'not_found'}};
            // 用 innerText 而非 textContent：BOSS 反爬往 JD 里塞了 <style> 块（其 CSS
            // 源码会被 textContent 读出来）+ display:none / width:0.1px 的隐藏诱饵 span
            // （逐字插 "来自BOSS直聘"/"kanzhun" 把真词劈开，导致关键词匹配全废、命中骤降）。
            // innerText 只返回「渲染可见」文本，自动排除 <style> 源码和隐藏元素，拿到干净
            // JD 正文。诊断见 scripts/probe_jd_extract.py（s1=textContent 脏 / s2=innerText 净）。
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


# ---------- 业务 helper ----------


_LABEL_SEPARATORS = "，,、/|;；"


def _label_tokens(label: str) -> list[str]:
    """把用户填的 label 拆成候选 tag：「python，算法」→ ['python', '算法']。

    纯函数，便于单测。BOSS 的求职 tag chip 是**单个**标签（"后端开发(贵阳)"），而用户
    习惯在 BOSS_LABEL 里写逗号/顿号分隔的一串，拿整串去比永远匹配不上。
    """
    text = (label or "").strip()
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if ch in _LABEL_SEPARATORS:
            if buf.strip():
                tokens.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        tokens.append(buf.strip())
    return tokens


# 当前 BOSS（2026）职位页的求职 tag chip 是 ``a.expect-item``，容器 `.expect-list` /
# `.c-expect-select`，「更多」入口是 `.expect-select-filter`。老代码用 xpath 找
# `recommend-job-btn` + `has-tooltip`，那两个 class 早就不存在了（实测页面上一个都选不到）。
_TAG_ITEM_SELECTOR = ".expect-item, .expect-list a, .expect-list li, .c-expect-select a"


def _click_tag_js(tokens: list[str]) -> str:
    """生成"按文本点求职 tag chip"的 JS：命中就 click，并把可见候选一起回传。

    点击在 JS 里一次做完（不用先查索引再点），避免两次 evaluate 之间 DOM 变了导致
    点错元素。
    """
    return (
        "JSON.stringify((() => {\n"
        "  const norm = (s) => (s || '').replace(/\\s+/g, '');\n"
        f"  const els = Array.from(document.querySelectorAll({json.dumps(_TAG_ITEM_SELECTOR)}));\n"
        f"  const wanted = {json.dumps(tokens, ensure_ascii=False)};\n"
        "  const available = [];\n"
        "  for (const el of els) {\n"
        "    const r = el.getBoundingClientRect();\n"
        "    if (!(r.width > 0 && r.height > 0)) continue;\n"
        "    const text = norm(el.innerText || el.textContent || '');\n"
        "    if (!text) continue;\n"
        "    if (!available.includes(text)) available.push(text);\n"
        "    if (wanted.some((w) => text === w || text.includes(w))) {\n"
        "      el.click();\n"
        "      return {ok: true, clicked: text, available};\n"
        "    }\n"
        "  }\n"
        "  return {ok: false, clicked: null, available};\n"
        "})())"
    )


async def _click_expect_tag(tokens: list[str], log_available: bool = True) -> bool:
    """按文本点击求职 tag chip；返回是否点到。

    ``log_available=False`` 时不打候选列表 —— 重试期间每个 tick 都打一遍太吵，
    只在最终放弃前打一次。
    """
    result = await _safe_evaluate(_click_tag_js(tokens), timeout=5)
    if result.get("ok"):
        log.info("    → 命中 tag chip %r，已点击", result.get("clicked"))
        return True
    if log_available:
        if result.get("available"):
            log.info("    当前可见 tag chip：%s", "、".join(result["available"]))
        else:
            log.info("    页面上没找到求职 tag chip")
    return False


# chip 是 SPA 异步渲染出来的：页面刚打开时经常一个都还没挂上去，实测要 3~5 秒
# 才排出来。多等几秒远好过悄悄退回默认 feed（那等于 tag 白设了）。
_TAG_LOOKUP_ATTEMPTS = 5


async def select_dropdown_option(label: str) -> None:
    """选中求职 tag（``BOSS_LABEL``）；空 label 表示沿用 BOSS 默认推荐 feed。

    **全程走 JS/CSS，不再用 ``tab.xpath``**：那条路在当前 Chrome 上第一步就抛
    ``ProtocolException: 'DOM.enable' wasn't found``（-32601），三条路径全挂在开头，
    结果永远 fallback 到默认推荐 feed —— 用户填的 tag 形同虚设，feed 里全是跟简历无关
    的岗位，LLM 全判低分跳过，一条招呼语都发不出去。
    """
    if not label:
        log.info("[select_dropdown_option] label 为空，沿用当前推荐 feed")
        return

    tokens = _label_tokens(label)
    log.info("[select_dropdown_option] label=%r → 候选 tag %s", label, tokens)

    log.info("  路径 1: 直接在求职 tag chip 里找 ...")
    for attempt in range(1, _TAG_LOOKUP_ATTEMPTS + 1):
        if await _click_expect_tag(tokens, log_available=False):
            return
        if attempt < _TAG_LOOKUP_ATTEMPTS:
            # chip 还没渲染出来，等一下再找（实测差一两秒就从「只有推荐」变成整排）
            await asyncio.sleep(1.0)

    log.info("  路径 2: chip 可能折在「更多」里，点开再找一次 ...")
    more = await _safe_evaluate(
        "JSON.stringify((() => {"
        "  const el = document.querySelector('.expect-select-filter');"
        "  if (!el) return {ok: false};"
        "  el.click(); return {ok: true};"
        "})())",
        timeout=5,
    )
    if more.get("ok"):
        await asyncio.sleep(1.0)
        if await _click_expect_tag(tokens):
            return
    else:
        log.info("    页面上没有「更多」入口")

    await _click_expect_tag(tokens, log_available=True)  # 只为打一次可见候选
    log.warning(
        "  路径 3: 没找到 %r 对应的求职 tag，只能沿用 BOSS 默认推荐 feed。"
        " 想要精确筛选，把 BOSS_LABEL 改成上面列出的可见 tag 原名（例如「后端开发(贵阳)」）",
        label,
    )


# BOSS 详情面板顶部的 UI 文字（按钮 / 区块标题），不是 JD 正文。innerText 会把它们
# 带在最前面（"举报\n微信扫码分享\n职位描述\n…"），剥掉让喂给匹配/LLM 的更纯。
_JD_NOISE_LINES = frozenset(
    {"举报", "微信扫码分享", "微信分享", "分享", "职位描述", "立即沟通", "收藏"}
)


def _strip_jd_noise(text: str) -> str:
    """剥掉 JD 文本**开头连续**的页面 UI 噪声行。

    只剥开头那几行（举报 / 微信扫码分享 / 职位描述 …）；正文里万一再出现同样的词
    不动。纯函数，便于单测（见 tests/test_finding_jobs_text.py）。
    """
    lines = (text or "").split("\n")
    i = 0
    while i < len(lines) and lines[i].strip() in _JD_NOISE_LINES:
        i += 1
    return "\n".join(lines[i:]).strip()


async def get_job_description_by_index(index: int) -> str | None:
    """点开第 N 个岗位卡（1-indexed），返回右侧 JD 详情面板的文本；失败返回 None。"""
    log.info("[get_job_description_by_index] index=%d", index)
    # 全程走 tab.evaluate(JS)，CSS selector + 浏览器内 click 都在 JS 里做完。
    click_result = await _js_click_at_index(".job-card-box", index)
    log.info("  点击 .job-card-box[%d]: %s", index, click_result)
    if not click_result.get("ok"):
        return None

    jd = await _js_wait_text(".job-detail-body", min_len=50, timeout_s=10)
    if jd is None:
        log.info("  10s 内 .job-detail-body 没出现或文本太短")
        return None
    jd = _strip_jd_noise(jd)
    log.info("  JD 长度 %d 字符", len(jd))
    return jd


async def get_loaded_job_count() -> int:
    """返回当前 DOM 中已加载的岗位卡数量。"""
    result = await _safe_evaluate(
        """
        JSON.stringify((() => {
          return {count: document.querySelectorAll('.job-card-box').length};
        })())
        """,
        timeout=5,
    )
    return int(result.get("count") or 0)


class PageReloadingError(RuntimeError):
    """BOSS 页面卡在「整页反复重载」状态。

    症状：document 平均每 2~3 秒被重新导航一次（``performance.timeOrigin`` 一直
    被重置），DOM 里的岗位卡时有时无，脚本每次去点卡片都撞在重载的空档上。
    以前这种状态会被误判成「已经到推荐列表底部」（连续 5 次拿不到 JD →
    ``feed_exhausted``），用户看到的是"浏览器一直在刷新，程序却说跑完了"。

    为什么会刷是 BOSS 那边的事，脚本控制不了；能做的是**认出来并停下**。
    """


# 判定「页面卡在重载循环」的滚动窗口与阈值：30 秒内出现 3 个不同文档就算
# （正常跑一次里，除了启动兜底那次 reload_page，不该有这么密的整页导航）。
_RELOAD_WINDOW_SECONDS = 30.0
_RELOAD_LOOP_THRESHOLD = 3

# (采样时刻, performance.timeOrigin) 的滚动窗口。见 _record_document_origin。
_reload_probe_window: list[tuple[float, float]] = []


def _record_document_origin(samples: list, origin: float | None, now: float) -> int:
    """记录一次 timeOrigin 采样，返回滚动窗口内出现过的**不同文档**数。

    纯函数（写传入的 list），便于单测。只有整页导航才会重置 ``timeOrigin``，
    虚拟列表重渲染不会——所以窗口里不同 timeOrigin 的个数就等于这几十秒内页面
    整页刷新了几次。
    """
    if origin is not None and (not samples or samples[-1][1] != origin):
        samples.append((now, origin))
    samples[:] = [s for s in samples if now - s[0] <= _RELOAD_WINDOW_SECONDS]
    return len({time_origin for _, time_origin in samples})


async def _document_time_origin() -> float | None:
    """当前文档的 ``performance.timeOrigin``；拿不到返回 None。"""
    result = await _safe_evaluate(
        "JSON.stringify((() => ({t: performance.timeOrigin}))())", timeout=5
    )
    value = result.get("t")
    return float(value) if isinstance(value, (int, float)) else None


async def _raise_if_page_reloading() -> None:
    """页面卡在整页反复重载时抛 ``PageReloadingError``。

    每次调用采样一次 timeOrigin；同一个文档重复采样不会重复计数。
    """
    origin = await _document_time_origin()
    count = _record_document_origin(
        _reload_probe_window, origin, asyncio.get_event_loop().time()
    )
    if count >= _RELOAD_LOOP_THRESHOLD:
        raise PageReloadingError(
            f"BOSS 页面卡在整页反复重载：最近 {_RELOAD_WINDOW_SECONDS:.0f} 秒内整页刷新了 "
            f"{count} 次。最常见的原因是 BOSS 风控在反复把你送去安全验证"
            "（浏览记录里能看到 web/passport/zp/security.html 和 _security_check=1_1 来回跳），"
            "其次是未登录状态下前端路由自己在循环。DOM 里的岗位卡时有时无，"
            "脚本每次点卡片都撞在重载空档上，继续跑只会把岗位一个个当成"
            "「拿不到 JD」跳过，最后误报成「已到推荐列表底部」。"
            "这一步脚本救不了，需要人工过验证/登录：先关掉脚本，用同一个 Chrome profile "
            "手动开一个不带调试端口的窗口（chrome.exe --user-data-dir=<profile 目录>），"
            "在里面过掉安全验证并登录成功，再回来重跑。",
        )


async def _wait_for_job_cards(timeout: float = 5.0) -> int:
    """等岗位卡重新出现在 DOM 里，返回最终数量（一直是 0 就返回 0）。

    虚拟列表在滚动/重渲染的瞬间会把卡片**整片卸载**，``.job-card-box`` 数量会
    掉到 0。这个窗口里去读列表、去滚动都是在跟渲染赛跑。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    count = await get_loaded_job_count()
    while count == 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.3)
        count = await get_loaded_job_count()
    # 列表时有时无、而且是"整页导航"级别地在变 → 页面卡在重载循环，认出来就停。
    await _raise_if_page_reloading()
    return count


async def scroll_to_load_more_jobs(timeout: float = 8.0) -> bool:
    """滚动岗位 feed 并等待更多岗位卡出现。

    BOSS 职位页有两种形态：有些滚动左侧岗位列表容器，有些滚动整个页面到底
    触发 feed 懒加载。页面可能复用固定数量的卡片 DOM，所以左侧列表首卡变化
    也算进展；window 滚动只有岗位卡数量增加才算成功。

    **卡片数为 0 时不滚**：那是虚拟列表正在重渲染的瞬间，此时 ``window.scrollTo``
    只会把它踹得更狠——页面猛跳到底 → SPA 再卸载重挂一遍 → 又是 0，日志里就会
    刷出一串「滚动加载更多岗位：0 -> 15」的假进展（用户看到的是"页面一直跳、
    像在刷新"）。这时候只等它自己挂回来，等到了就返回 True 让主循环重试同一个
    index（不推进 job_index），期间一次都不滚。
    """
    before = await get_loaded_job_count()
    if before == 0:
        restored = await _wait_for_job_cards()
        if restored:
            log.info("岗位卡正在重渲染（0 -> %d），等它回来，本轮不滚动", restored)
            return True
        log.info("岗位卡持续为 0，本轮不滚动")
        return False
    scroll_result = await _safe_evaluate(
        """
        JSON.stringify((() => {
          const firstCardText = () => {
            const card = document.querySelector('.job-card-box');
            return card ? (card.innerText || '').trim().slice(0, 120) : '';
          };
          const cards = Array.from(document.querySelectorAll('.job-card-box'));
          const selectorCandidates = [
            document.querySelector('.job-list-box'),
            document.querySelector('.job-list'),
            document.querySelector('.job-list-container'),
            document.querySelector('[class*="job-list"]'),
          ].filter(Boolean);
          const ancestors = [];
          let node = cards[0] || null;
          while (
            node
            && node !== document.body
            && node !== document.documentElement
          ) {
            ancestors.push(node);
            node = node.parentElement;
          }
          const seen = new Set();
          const candidates = [...selectorCandidates, ...ancestors].filter((el) => {
            if (!cards[0] || !el.contains(cards[0]) || seen.has(el)) return false;
            seen.add(el);
            return true;
          });
          const scrollable = candidates.find((el) => (
            el.scrollHeight > el.clientHeight + 20
            && getComputedStyle(el).overflowY !== 'hidden'
          ));
          if (!scrollable) {
            const target = document.scrollingElement
              || document.documentElement
              || document.body;
            const beforeTop = window.scrollY || target.scrollTop || 0;
            const beforeFirst = firstCardText();
            const bottom = Math.max(
              document.documentElement.scrollHeight,
              document.body.scrollHeight,
              target.scrollHeight
            );
            window.scrollTo(0, bottom);
            if (target.scrollTop === beforeTop) {
              target.scrollTop = bottom;
            }
            return {
              ok: true,
              target: 'window',
              beforeTop,
              afterTop: window.scrollY || target.scrollTop || 0,
              beforeFirst,
              afterFirst: firstCardText(),
            };
          }
          const target = scrollable;
          const beforeTop = target.scrollTop;
          const beforeFirst = firstCardText();
          const step = Math.max(
            600,
            Math.floor((target.clientHeight || window.innerHeight || 600) * 0.85)
          );
          target.dispatchEvent(
            new WheelEvent(
              'wheel',
              {deltaY: step, bubbles: true, cancelable: true}
            )
          );
          if (target.scrollTop === beforeTop && target.scrollBy) {
            target.scrollBy(0, step);
          }
          if (target.scrollTop === beforeTop) {
            target.scrollTop = beforeTop + step;
          }
          const afterTop = target.scrollTop;
          const afterFirst = firstCardText();
          return {
            ok: true,
            target: 'left-list',
            targetClass: String(target.className || ''),
            beforeTop,
            afterTop,
            beforeFirst,
            afterFirst,
          };
        })())
        """,
        timeout=5,
    )
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        after = await get_loaded_job_count()
        # before 一定 > 0（0 的分支上面已经 return 了），所以这里不会把
        # 「重渲染补回卡片」当成"加载了更多"。
        if after > before:
            log.info(
                "滚动加载更多岗位：%d -> %d（target=%s）",
                before,
                after,
                scroll_result.get("target", ""),
            )
            return True
        if scroll_result.get("target") != "left-list":
            continue
        if scroll_result.get("afterTop") != scroll_result.get(
            "beforeTop"
        ) or scroll_result.get("afterFirst") != scroll_result.get("beforeFirst"):
            log.info(
                "岗位列表已滚动（target=%s，count=%d，DOM 数量未变，按虚拟列表继续）",
                scroll_result.get("target", ""),
                before,
            )
            return True
    log.info("滚动后岗位数量和可见内容均未变化：%d", before)
    return False


async def get_text_by_css(selector: str, timeout: float = 5) -> str | None:
    """通用：返回 CSS 选择器命中元素的 text，找不到返回 None。"""
    try:
        el = await _tab.select(selector, timeout=timeout)
    except Exception:
        return None
    return el.text if el else None


# 岗位卡上的「立即沟通」按钮。跟 ``get_text_by_css('.op-btn.op-btn-chat')`` 读的是
# 同一个元素——那边读文字判断能不能投，这边点它。
_CONTACT_BUTTON_SELECTOR = ".op-btn-chat, .op-btn.op-btn-chat"


async def click_contact_button(timeout: float = 10) -> bool:
    """点岗位卡上的「立即沟通」；成功返回 True。

    **走 JS 而不是 xpath**：``tab.xpath()`` 在当前 Chrome 上第一步就抛
    ``ProtocolException: 'DOM.enable' wasn't found``，老实现是静默返回 False ——
    招呼语生成、校验、日志全都正常，只有最后那一下点不出去，日志里连个错都没有。
    顺带也去掉了对 DOM/Runtime 这些 CDP 域的依赖（能被风控探测的面越小越好）。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        result = await _safe_evaluate(
            "JSON.stringify((() => {"
            f"  const els = Array.from(document.querySelectorAll({json.dumps(_CONTACT_BUTTON_SELECTOR)}));"
            "  const el = els.find((e) => {"
            "    const r = e.getBoundingClientRect();"
            "    return r.width > 0 && r.height > 0;"
            "  });"
            "  if (!el) return {ok: false};"
            "  el.click();"
            "  return {ok: true, text: (el.innerText || '').trim().slice(0, 20)};"
            "})())",
            timeout=5,
        )
        if result.get("ok"):
            log.info("已点击「立即沟通」（按钮文案 %r）", result.get("text"))
            return True
        if asyncio.get_event_loop().time() >= deadline:
            log.warning("没找到「立即沟通」按钮（%.0fs 超时）", timeout)
            return False
        await asyncio.sleep(0.5)


async def dismiss_greeting_dialog(timeout: float = 5) -> bool:
    """关掉"立即沟通"后弹出的默认招呼语确认框。

    BOSS 2026-08 改版后，点"立即沟通"会自动发一条默认招呼语，然后弹框提示
    "已向BOSS发送消息"，需要点"继续沟通"才进入聊天页。
    如果弹框没出现（旧版 BOSS 或已经跳过），静默返回 False。
    """
    try:
        btn = await _tab.find("继续沟通", best_match=True, timeout=timeout)
    except Exception:
        btn = None
    if btn:
        await btn.click()
        log.info("已关闭默认招呼语弹框（点击'继续沟通'）")
        await asyncio.sleep(1)
        return True
    log.debug("未检测到招呼语弹框，跳过")
    return False


async def wait_for_css(selector: str, timeout: float = 50) -> bool:
    """等 CSS 选择器命中。成功返回 True，超时返回 False。"""
    try:
        el = await _tab.select(selector, timeout=timeout)
    except Exception:
        return False
    return el is not None


async def send_chat_message(text: str) -> None:
    """把 text 打进 ``#chat-input`` 然后点发送按钮。"""
    chat = await _tab.select("#chat-input", timeout=10)
    if not chat:
        raise RuntimeError("chat input (#chat-input) 未找到")
    await chat.send_keys(text)
    await asyncio.sleep(3)
    # BOSS 改版后回车不再触发发送，需要点击发送按钮
    try:
        send_btn = await _tab.find("发送", best_match=True, timeout=5)
    except Exception:
        send_btn = None
    if send_btn:
        await send_btn.click()
        log.info("已点击'发送'按钮")
    else:
        log.warning("未找到'发送'按钮，尝试回车发送")
        await chat.send_keys("\n")
    await asyncio.sleep(1)


async def click_send_resume(timeout: float = 5) -> bool:
    """在聊天页点击"发简历"按钮。

    发完招呼语后聊天页可能出现"发简历"按钮，点击即可附带简历。
    按钮不存在时静默返回 False（不影响主流程）。
    """
    try:
        btn = await _tab.find("发简历", best_match=True, timeout=timeout)
    except Exception:
        btn = None
    if btn:
        await btn.click()
        log.info("已点击'发简历'")
        await asyncio.sleep(2)
        return True
    log.debug("未找到'发简历'按钮，跳过")


async def reload_page() -> None:
    """刷新当前页面，然后给它一点时间开始重新加载。

    走 ``_safe_evaluate`` 而不是裸 ``_tab.evaluate``：reload 会换掉 renderer
    进程，pending 的 ``Runtime.evaluate`` 响应可能永远回不来，而 nodriver 的
    ``Connection.send`` 是没有 timeout 的裸 Future —— 裸调会把整个流程挂死。

    这里只等固定 2 秒（reload 前后 URL 不变，等 URL 稳定没有意义）；真正
    「加载好了没」由调用方的 ``wait_for_real_job_cards`` 负责。
    """
    await _safe_evaluate("JSON.stringify((() => {location.reload(); return {};})())")
    await asyncio.sleep(2)


# 骨架屏占位卡的 innerText 基本是空的（只有几个装饰用的空 span）；真实岗位卡
# 至少带职位名 + 公司名，远超这个长度。
_MIN_REAL_CARD_TEXT_LEN = 10


def _count_real_job_cards(card_texts: list[str] | None) -> int:
    """统计有实际内容的岗位卡数量，排除骨架屏空卡。

    纯函数，便于单测（见 tests/test_finding_jobs_text.py）。
    """
    return sum(
        1
        for text in (card_texts or [])
        if len((text or "").strip()) >= _MIN_REAL_CARD_TEXT_LEN
    )


async def wait_for_real_job_cards(timeout: float = 30) -> bool:
    """轮询直到出现**有文字内容**的岗位卡；超时返回 False。

    为什么不用 ``wait_for_css('.job-card-box')``：SPA 没 boot 起来时页面停在
    「加载中，请稍候」，DOM 里可能已经有骨架屏占位卡，选择器命中但正文全空，
    主循环随后每一轮都抓不到 JD，最后误报「已到推荐 feed 列表底部」。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await _safe_evaluate(
            """
            JSON.stringify((() => {
              const cards = document.querySelectorAll('.job-card-box');
              return {
                texts: Array.from(cards).map(c => (c.innerText || '').trim()),
              };
            })())
            """,
            timeout=5,
        )
        real = _count_real_job_cards(result.get("texts"))
        if real:
            log.info("岗位卡已渲染（有内容的 %d 张）", real)
            return True
        await asyncio.sleep(1)
    return False


async def navigate_back() -> None:
    """``history.back()`` —— 浏览器返回上一页。"""
    await _tab.evaluate("history.back()")
    await asyncio.sleep(3)


async def return_to_job_list(timeout: float = 12.0, max_attempts: int = 2) -> bool:
    """尽量回到岗位列表页，并确认列表卡片已恢复。"""
    for attempt in range(1, max_attempts + 1):
        await navigate_back()
        if await wait_for_css(".job-card-box", timeout=timeout):
            return True
        log.warning("返回岗位列表第 %d 次失败，继续重试", attempt)
    return False


# Variables（保持向后兼容）
url = "https://www.zhipin.com/web/geek/job-recommend?ka=header-job-recommend"
browser_type = "chrome"
