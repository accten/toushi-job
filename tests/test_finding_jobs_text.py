"""finding_jobs 的纯文本处理（不碰浏览器的部分）。

``_strip_jd_noise`` 剥掉 JD 开头的页面 UI 噪声行（举报 / 微信扫码分享 / 职位描述…），
这些是 ``.job-detail-body`` 的 innerText 带进来的页面 chrome，不是 JD 正文。
"""

import asyncio
import os
import types

from toushi.website_oper import finding_jobs
from toushi.website_oper.finding_jobs import (
    LoginRequiredError,
    PageReloadingError,
    _apply_sandbox_setting,
    _env_flag_true,
    _login_wait_seconds,
    _should_disable_sandbox,
    _clear_singleton_locks,
    _count_real_job_cards,
    _ensure_localhost_bypasses_proxy,
    _is_logged_in_from_page_state,
    _is_stale_session_error,
    _label_tokens,
    _raise_if_page_reloading,
    _safe_evaluate,
    _record_document_origin,
    _wait_for_job_cards,
    get_loaded_job_count,
    log_in,
    return_to_job_list,
    scroll_to_load_more_jobs,
    _strip_jd_noise,
)


def test_strips_leading_ui_noise():
    raw = "举报\n微信扫码分享\n职位描述\n\n1、参与 AI 功能模块设计\n岗位要求：本科"
    out = _strip_jd_noise(raw)
    assert out.startswith("1、参与 AI 功能模块设计")
    assert "举报" not in out
    assert "微信扫码分享" not in out


def test_only_strips_leading_run_not_body():
    # 正文里再出现"职位描述"不该被剥——只剥开头连续的噪声行
    raw = "举报\n职位描述\n这个职位描述很详细\n岗位要求"
    assert _strip_jd_noise(raw) == "这个职位描述很详细\n岗位要求"


def test_no_noise_unchanged():
    raw = "1、岗位职责\n2、岗位要求"
    assert _strip_jd_noise(raw) == raw


def test_empty_and_none():
    assert _strip_jd_noise("") == ""
    assert _strip_jd_noise(None) == ""


def test_env_flag_true_variants(monkeypatch):
    monkeypatch.setenv("BOSS_NO_SANDBOX", "1")
    assert _env_flag_true("BOSS_NO_SANDBOX") is True

    monkeypatch.setenv("BOSS_NO_SANDBOX", "TrUe")
    assert _env_flag_true("BOSS_NO_SANDBOX") is True

    monkeypatch.setenv("BOSS_NO_SANDBOX", "off")
    assert _env_flag_true("BOSS_NO_SANDBOX") is False


def test_should_disable_sandbox_prefers_env_flag(monkeypatch):
    monkeypatch.setenv("BOSS_NO_SANDBOX", "yes")
    monkeypatch.delattr(finding_jobs.os, "geteuid", raising=False)
    assert _should_disable_sandbox() is True


def test_should_disable_sandbox_uses_root_detection(monkeypatch):
    monkeypatch.delenv("BOSS_NO_SANDBOX", raising=False)
    monkeypatch.setattr(finding_jobs.os, "geteuid", lambda: 0, raising=False)
    assert _should_disable_sandbox() is True


def test_should_disable_sandbox_default_false(monkeypatch):
    monkeypatch.delenv("BOSS_NO_SANDBOX", raising=False)
    monkeypatch.setattr(finding_jobs.os, "geteuid", lambda: 1000, raising=False)
    assert _should_disable_sandbox() is False


class _FakeConfig:
    """替身：只关心 nodriver Config 的 sandbox 属性。"""

    def __init__(self, sandbox: bool = True):
        self.sandbox = sandbox


def test_apply_sandbox_setting_writes_config_when_disabled(monkeypatch):
    # 关键回归：必须落到 config.sandbox 上。nodriver 的 uc.start(sandbox=...)
    # 只在不传 config 时才消费该参数，我们一定传 config，走 kwarg 会静默失效。
    monkeypatch.setenv("BOSS_NO_SANDBOX", "1")
    config = _FakeConfig(sandbox=True)
    assert _apply_sandbox_setting(config) is False
    assert config.sandbox is False


def test_apply_sandbox_setting_keeps_enabled_by_default(monkeypatch):
    monkeypatch.delenv("BOSS_NO_SANDBOX", raising=False)
    monkeypatch.setattr(finding_jobs.os, "geteuid", lambda: 1000, raising=False)
    config = _FakeConfig(sandbox=True)
    assert _apply_sandbox_setting(config) is True
    assert config.sandbox is True


def test_apply_sandbox_setting_does_not_override_upstream_disable(monkeypatch):
    # nodriver 在 posix + root 下自己会把 sandbox 置 False；我们不该无条件赋 True
    # 把上游这份自动处理覆盖回去。
    monkeypatch.delenv("BOSS_NO_SANDBOX", raising=False)
    monkeypatch.setattr(finding_jobs.os, "geteuid", lambda: 1000, raising=False)
    config = _FakeConfig(sandbox=False)
    assert _apply_sandbox_setting(config) is False
    assert config.sandbox is False


def test_counts_only_cards_with_real_text():
    texts = [
        "全栈工程师\n25-35K\n某某科技",  # 真实卡
        "后端开发\n20-30K\n另一家公司",  # 真实卡
        "",  # 骨架屏空卡
        "   \n \n ",  # 只有空白，仍算空卡
    ]
    assert _count_real_job_cards(texts) == 2


def test_skeleton_only_page_counts_zero():
    # SPA 没 boot 起来时的典型形态：选择器命中一堆卡，但正文全空
    assert _count_real_job_cards(["", "  ", None]) == 0
    assert _count_real_job_cards([]) == 0
    assert _count_real_job_cards(None) == 0


def _read_no_proxy() -> str:
    return os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""


def _set_no_proxy(monkeypatch, value: str | None) -> None:
    # 必须先清两个变体再设：Windows 的 os.environ 大小写不敏感，先 setenv
    # 再 delenv 另一个变体会把刚设的值一起删掉。
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    if value is not None:
        monkeypatch.setenv("no_proxy", value)


def test_ensure_localhost_bypasses_proxy_adds_both_hosts(monkeypatch):
    _set_no_proxy(monkeypatch, None)
    _ensure_localhost_bypasses_proxy()
    entries = _read_no_proxy().split(",")
    assert "127.0.0.1" in entries
    assert "localhost" in entries


def test_ensure_localhost_bypasses_proxy_keeps_existing_entries(monkeypatch):
    _set_no_proxy(monkeypatch, "corp.example.com,10.0.0.1")
    _ensure_localhost_bypasses_proxy()
    entries = _read_no_proxy().split(",")
    # 用户原有的 bypass 条目一条都不能丢
    assert "corp.example.com" in entries
    assert "10.0.0.1" in entries
    assert "127.0.0.1" in entries


def test_ensure_localhost_bypasses_proxy_falls_back_to_uppercase(monkeypatch):
    # 回归：no_proxy 为空串时要回退读 NO_PROXY。只有 POSIX 分得开这两个变量，
    # 所以这条在 ubuntu CI 上才真正生效。
    _set_no_proxy(monkeypatch, "")
    monkeypatch.setenv("NO_PROXY", "corp.example.com")
    _ensure_localhost_bypasses_proxy()
    assert "corp.example.com" in _read_no_proxy().split(",")


def test_ensure_localhost_bypasses_proxy_is_noop_when_present(monkeypatch):
    _set_no_proxy(monkeypatch, "127.0.0.1,localhost")
    _ensure_localhost_bypasses_proxy()
    assert _read_no_proxy() == "127.0.0.1,localhost"


def test_ensure_localhost_bypasses_proxy_dedupes_case_insensitively(monkeypatch):
    # LOCALHOST 已经命中（urllib 比对前 .lower()），不该每次启动再追加一条
    _set_no_proxy(monkeypatch, "LOCALHOST,127.0.0.1")
    _ensure_localhost_bypasses_proxy()
    assert _read_no_proxy().lower().split(",").count("localhost") == 1


def test_clear_singleton_locks_removes_locks_keeps_cookies(tmp_path):
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "Cookies"):
        (tmp_path / name).write_text("x")
    _clear_singleton_locks(str(tmp_path))
    # 三个 Singleton 锁删掉；登录态文件（Cookies）保留
    assert not (tmp_path / "SingletonLock").exists()
    assert not (tmp_path / "SingletonCookie").exists()
    assert not (tmp_path / "SingletonSocket").exists()
    assert (tmp_path / "Cookies").exists()


def test_clear_singleton_locks_missing_ok(tmp_path):
    _clear_singleton_locks(str(tmp_path))  # 没有锁文件也不报错


def test_login_page_url_is_not_logged_in():
    assert (
        _is_logged_in_from_page_state(
            "https://www.zhipin.com/web/user/?ka=header-login", {}
        )
        is False
    )


def test_jobs_page_with_header_login_is_not_logged_in():
    assert (
        _is_logged_in_from_page_state(
            "https://www.zhipin.com/web/geek/jobs",
            {"headerLoginVisible": True},
        )
        is False
    )


def test_jobs_page_with_login_required_text_is_not_logged_in():
    assert (
        _is_logged_in_from_page_state(
            "https://www.zhipin.com/web/geek/jobs",
            {"loginRequiredVisible": True},
        )
        is False
    )


def _rendered_page_state(**overrides):
    """一个"已渲染且没有任何未登录信号"的页面探测结果。"""
    state = {
        "readyState": "complete",
        "bodyLength": 4000,
        "headerLoginText": False,
        "loginWallVisible": False,
        "headerLoginVisible": False,
        "loginRequiredVisible": False,
    }
    state.update(overrides)
    return state


def test_jobs_page_without_login_signals_is_logged_in():
    assert (
        _is_logged_in_from_page_state(
            "https://www.zhipin.com/web/geek/job-recommend",
            _rendered_page_state(),
        )
        is True
    )


def test_blank_document_is_not_treated_as_logged_in():
    """整页重载的空档里 body 是空的，不能因为"没看到未登录信号"就判已登录。"""
    assert (
        _is_logged_in_from_page_state(
            "https://www.zhipin.com/web/geek/job-recommend",
            _rendered_page_state(bodyLength=0),
        )
        is False
    )
    # readyState 还是 complete，但正文只有几十个字符 —— 一样不算渲染出来
    assert (
        _is_logged_in_from_page_state(
            "https://www.zhipin.com/web/geek/job-recommend",
            _rendered_page_state(bodyLength=42),
        )
        is False
    )


def test_not_ready_document_is_not_treated_as_logged_in():
    assert (
        _is_logged_in_from_page_state(
            "https://www.zhipin.com/web/geek/job-recommend",
            _rendered_page_state(readyState="loading"),
        )
        is False
    )


def test_header_login_text_means_not_logged_in():
    """顶部写着「登录/注册」就是没登录，不看 class 名。"""
    assert (
        _is_logged_in_from_page_state(
            "https://www.zhipin.com/web/geek/job-recommend",
            _rendered_page_state(headerLoginText=True),
        )
        is False
    )


def test_get_loaded_job_count_reads_job_card_count(monkeypatch):
    async def scenario():
        async def fake_evaluate(js: str, timeout: float = 10):
            assert ".job-card-box" in js
            assert timeout == 5
            return {"count": 15}

        monkeypatch.setattr(finding_jobs, "_safe_evaluate", fake_evaluate)
        assert await get_loaded_job_count() == 15

    asyncio.run(scenario())


def test_scroll_to_load_more_jobs_returns_true_when_card_count_grows(monkeypatch):
    async def scenario():
        counts = iter([15, 30])
        evaluate_calls: list[str] = []

        async def fake_count():
            return next(counts)

        async def fake_evaluate(js: str, timeout: float = 10):
            evaluate_calls.append(js)
            return {
                "target": "left-list",
                "beforeTop": 0,
                "afterTop": 700,
                "beforeFirst": "old",
                "afterFirst": "old",
            }

        async def fake_sleep(delay: float):
            return None

        monkeypatch.setattr(finding_jobs, "get_loaded_job_count", fake_count)
        monkeypatch.setattr(finding_jobs, "_safe_evaluate", fake_evaluate)
        monkeypatch.setattr(finding_jobs.asyncio, "sleep", fake_sleep)

        assert await scroll_to_load_more_jobs(timeout=1.0) is True
        assert evaluate_calls
        assert "job-list-box" in evaluate_calls[0]

    asyncio.run(scenario())


def test_scroll_to_load_more_jobs_treats_visible_card_change_as_progress(monkeypatch):
    async def scenario():
        async def fake_count():
            return 15

        async def fake_evaluate(js: str, timeout: float = 10):
            return {
                "target": "left-list",
                "beforeTop": 0,
                "afterTop": 700,
                "beforeFirst": "old card",
                "afterFirst": "new card",
            }

        async def fake_sleep(delay: float):
            return None

        monkeypatch.setattr(finding_jobs, "get_loaded_job_count", fake_count)
        monkeypatch.setattr(finding_jobs, "_safe_evaluate", fake_evaluate)
        monkeypatch.setattr(finding_jobs.asyncio, "sleep", fake_sleep)

        assert await scroll_to_load_more_jobs(timeout=1.0) is True

    asyncio.run(scenario())

def test_scroll_to_load_more_jobs_ignores_window_scroll(monkeypatch):
    async def scenario():
        async def fake_count():
            return 15

        async def fake_evaluate(js: str, timeout: float = 10):
            return {
                "ok": True,
                "target": "window",
                "beforeTop": 0,
                "afterTop": 700,
                "beforeFirst": "old card",
                "afterFirst": "old card",
            }

        async def fake_sleep(delay: float):
            return None

        monkeypatch.setattr(finding_jobs, "get_loaded_job_count", fake_count)
        monkeypatch.setattr(finding_jobs, "_safe_evaluate", fake_evaluate)
        monkeypatch.setattr(finding_jobs.asyncio, "sleep", fake_sleep)

        assert await scroll_to_load_more_jobs(timeout=1.0) is False

    asyncio.run(scenario())


def test_scroll_to_load_more_jobs_accepts_window_scroll_when_count_grows(monkeypatch):
    async def scenario():
        counts = iter([15, 30])
        evaluate_calls: list[str] = []

        async def fake_count():
            return next(counts)

        async def fake_evaluate(js: str, timeout: float = 10):
            evaluate_calls.append(js)
            return {
                "ok": True,
                "target": "window",
                "beforeTop": 0,
                "afterTop": 1500,
                "beforeFirst": "old card",
                "afterFirst": "old card",
            }

        async def fake_sleep(delay: float):
            return None

        monkeypatch.setattr(finding_jobs, "get_loaded_job_count", fake_count)
        monkeypatch.setattr(finding_jobs, "_safe_evaluate", fake_evaluate)
        monkeypatch.setattr(finding_jobs.asyncio, "sleep", fake_sleep)

        assert await scroll_to_load_more_jobs(timeout=1.0) is True
        assert "window.scrollTo" in evaluate_calls[0]

    asyncio.run(scenario())


def test_return_to_job_list_retries_until_cards_are_visible(monkeypatch):
    async def scenario():
        back_calls: list[bool] = []
        visible_results = iter([False, True])

        async def fake_back():
            back_calls.append(True)

        async def fake_wait(selector: str, timeout: float = 50):
            assert selector == ".job-card-box"
            assert timeout == 12.0
            return next(visible_results)

        monkeypatch.setattr(finding_jobs, "navigate_back", fake_back)
        monkeypatch.setattr(finding_jobs, "wait_for_css", fake_wait)

        assert await return_to_job_list() is True
        assert len(back_calls) == 2

    asyncio.run(scenario())


# ---------- 虚拟列表重渲染时不要滚窗口 ----------


def test_scroll_does_not_touch_window_while_list_is_rerendering(monkeypatch):
    """卡片数为 0（虚拟列表正在卸载/重挂）时一次都不许滚，等回来重试同一个 index。"""

    async def scenario():
        counts = iter([0, 0, 15])
        evaluate_calls: list[str] = []

        async def fake_count():
            return next(counts)

        async def fake_evaluate(js: str, timeout: float = 10):
            evaluate_calls.append(js)
            return {"ok": True, "target": "window"}

        async def fake_sleep(delay: float):
            return None

        monkeypatch.setattr(finding_jobs, "get_loaded_job_count", fake_count)
        monkeypatch.setattr(finding_jobs, "_safe_evaluate", fake_evaluate)
        monkeypatch.setattr(finding_jobs.asyncio, "sleep", fake_sleep)

        assert await scroll_to_load_more_jobs(timeout=1.0) is True
        # 关键：没有任何一次 window.scrollTo
        assert all("scrollTo" not in js for js in evaluate_calls)

    asyncio.run(scenario())


def test_wait_for_job_cards_gives_up_after_timeout(monkeypatch):
    async def scenario():
        async def always_zero():
            return 0

        async def fake_sleep(delay: float):
            return None

        monkeypatch.setattr(finding_jobs, "get_loaded_job_count", always_zero)
        monkeypatch.setattr(finding_jobs.asyncio, "sleep", fake_sleep)

        assert await _wait_for_job_cards(timeout=0.01) == 0

    asyncio.run(scenario())


def test_scroll_reports_no_progress_when_list_stays_empty(monkeypatch):
    """列表一直没回来时返回 False，让调用方走"拿不到 JD"的正常计数。"""

    async def scenario():
        async def always_zero():
            return 0

        async def fake_evaluate(js: str, timeout: float = 10):
            raise AssertionError("列表为空时不该去滚窗口")

        async def fake_sleep(delay: float):
            return None

        monkeypatch.setattr(finding_jobs, "get_loaded_job_count", always_zero)
        monkeypatch.setattr(finding_jobs, "_safe_evaluate", fake_evaluate)
        monkeypatch.setattr(finding_jobs.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(finding_jobs, "_wait_for_job_cards", lambda timeout=5.0: always_zero())

        assert await scroll_to_load_more_jobs(timeout=1.0) is False

    asyncio.run(scenario())


# ---------- 页面卡在整页反复重载 ----------


def test_record_document_origin_counts_distinct_documents_only():
    samples: list = []
    assert _record_document_origin(samples, 100.0, 0.0) == 1
    # 同一个文档反复采样，不能重复计数（虚拟列表重渲染也是同一个文档）
    assert _record_document_origin(samples, 100.0, 1.0) == 1
    assert _record_document_origin(samples, 100.0, 2.0) == 1
    # 拿不到 timeOrigin 时什么都不动
    assert _record_document_origin(samples, None, 2.5) == 1
    assert _record_document_origin(samples, 200.0, 3.0) == 2
    assert _record_document_origin(samples, 300.0, 4.0) == 3
    # 滚出窗口的老采样被清掉，只剩当前这个文档
    assert _record_document_origin(samples, 400.0, 999.0) == 1


def test_page_reloading_detected_after_repeated_new_documents(monkeypatch):
    async def scenario():
        origins = iter([1.0, 2.0, 3.0])

        async def fake_origin():
            return next(origins)

        monkeypatch.setattr(finding_jobs, "_document_time_origin", fake_origin)
        monkeypatch.setattr(finding_jobs, "_reload_probe_window", [])

        await _raise_if_page_reloading()  # 第 1 个文档
        await _raise_if_page_reloading()  # 第 2 个
        try:
            await _raise_if_page_reloading()  # 第 3 个 → 超过阈值
        except PageReloadingError as e:
            return str(e)
        raise AssertionError("应该抛 PageReloadingError")

    message = asyncio.run(scenario())
    assert "整页反复重载" in message
    # 报错信息要能指导用户下一步，而不是只丢一句异常名
    assert "安全验证" in message
    assert "user-data-dir" in message


def test_rerender_without_navigation_never_triggers_reload_guard(monkeypatch):
    """同一个文档里怎么采样都不该命中——虚拟列表重渲染不是整页重载。"""

    async def scenario():
        async def same_document():
            return 42.0

        monkeypatch.setattr(finding_jobs, "_document_time_origin", same_document)
        monkeypatch.setattr(finding_jobs, "_reload_probe_window", [])

        for _ in range(10):
            await _raise_if_page_reloading()

    asyncio.run(scenario())


# ---------- 未登录直接停下 ----------


def test_login_wait_seconds_default_is_300(monkeypatch):
    monkeypatch.delenv("BOSS_LOGIN_TIMEOUT", raising=False)
    assert _login_wait_seconds() == 300.0


def test_login_wait_seconds_override_and_bad_values(monkeypatch):
    monkeypatch.setenv("BOSS_LOGIN_TIMEOUT", "45")
    assert _login_wait_seconds() == 45.0

    # GUI 配置页能填任意字符串，非法值只能回退默认，不能把 run 弄崩
    monkeypatch.setenv("BOSS_LOGIN_TIMEOUT", "not-a-number")
    assert _login_wait_seconds() == 300.0

    monkeypatch.setenv("BOSS_LOGIN_TIMEOUT", "-5")
    assert _login_wait_seconds() == 0.0


def test_log_in_raises_when_login_never_happens(monkeypatch):
    """未登录且等不到扫码时必须抛错停下，而不是拿游客 feed 继续空跑。"""

    class _FakeTab:
        url = "https://www.zhipin.com/web/geek/job-recommend"

        async def find(self, *args, **kwargs):
            return None

    async def never_logged_in():
        return False

    monkeypatch.setattr(finding_jobs, "_tab", _FakeTab())
    monkeypatch.setattr(finding_jobs, "_is_logged_in", never_logged_in)
    monkeypatch.setenv("BOSS_LOGIN_TIMEOUT", "0")

    async def scenario():
        try:
            await log_in()
        except LoginRequiredError as e:
            return str(e)
        raise AssertionError("log_in 应该抛 LoginRequiredError")

    message = asyncio.run(scenario())
    assert "没检测到登录态" in message
    # 光说"超时"没用，要给出下一步怎么做
    assert "登录" in message


# ---------- 求职 tag 拆词 ----------


def test_label_tokens_splits_chinese_and_ascii_separators():
    assert _label_tokens("python，算法") == ["python", "算法"]
    assert _label_tokens("python,算法") == ["python", "算法"]
    assert _label_tokens("后端开发、算法/测试") == ["后端开发", "算法", "测试"]


def test_label_tokens_keeps_single_and_handles_empty():
    # 单个 tag 原样返回（BOSS 的 chip 本来就是单标签）
    assert _label_tokens("后端开发(贵阳)") == ["后端开发(贵阳)"]
    assert _label_tokens("   ") == []
    assert _label_tokens("") == []
    assert _label_tokens(None) == []


# ---------- CDP session 过期自救 ----------


def test_stale_session_error_detection():
    assert _is_stale_session_error(
        RuntimeError("ProtocolException: Session with given id not found. [code: -32001]")
    )
    assert _is_stale_session_error(RuntimeError("something [code: -32001]"))
    assert not _is_stale_session_error(RuntimeError("Connection closed"))
    assert not _is_stale_session_error(TimeoutError())


class _FlakyTab:
    """第一次 evaluate 抛 session 失效，重新 attach 后恢复正常。"""

    def __init__(self, error: Exception | None = None, ok_payload: str = '{"ok": 1}'):
        self.evaluate_calls = 0
        self.attach_calls = 0
        self._error = error
        self._ok_payload = ok_payload

    async def evaluate(self, js: str):
        self.evaluate_calls += 1
        if self._error is not None and self.evaluate_calls == 1:
            raise self._error
        return self._ok_payload

    async def attach(self):
        self.attach_calls += 1


def test_safe_evaluate_reattaches_once_on_stale_session(monkeypatch):
    """GUI 下偶发 -32001：必须重新 attach 后重试，否则登录判定永远读到空 dict。"""

    async def scenario():
        tab = _FlakyTab(RuntimeError("Session with given id not found. [code: -32001]"))
        monkeypatch.setattr(finding_jobs, "_tab", tab)
        result = await _safe_evaluate("1+1")
        return tab, result

    tab, result = asyncio.run(scenario())
    assert result == {"ok": 1}
    assert tab.attach_calls == 1
    assert tab.evaluate_calls == 2


def test_safe_evaluate_does_not_reattach_on_unrelated_error(monkeypatch):
    async def scenario():
        tab = _FlakyTab(RuntimeError("some other protocol error"))
        monkeypatch.setattr(finding_jobs, "_tab", tab)
        result = await _safe_evaluate("1+1")
        return tab, result

    tab, result = asyncio.run(scenario())
    assert result == {}
    assert tab.attach_calls == 0
    assert tab.evaluate_calls == 1


class _DeadTargetTab:
    """target 已经不在了：evaluate 抛 -32001，attach 抛 -32602。"""

    def __init__(self, url: str):
        self.target = types.SimpleNamespace(url=url)
        self.evaluate_calls = 0

    async def evaluate(self, js: str):
        self.evaluate_calls += 1
        raise RuntimeError("Session with given id not found. [code: -32001]")

    async def attach(self):
        raise RuntimeError("No target with given id found [code: -32602]")


class _LiveTab:
    def __init__(self, url: str, payload: str = '{"ok": 9}'):
        self.target = types.SimpleNamespace(url=url)
        self.attach_calls = 0
        self._payload = payload

    async def evaluate(self, js: str):
        return self._payload

    async def attach(self):
        self.attach_calls += 1


class _FakeBrowser:
    def __init__(self, tabs):
        self.tabs = tabs

    async def update_targets(self):
        return None


def test_safe_evaluate_switches_tab_when_target_is_gone(monkeypatch):
    """-32602（target 没了）时 attach 救不回来，必须另接一个页面 target。"""

    async def scenario():
        dead = _DeadTargetTab("https://www.zhipin.com/web/geek/job-recommend")
        newtab = _LiveTab("chrome://newtab/")
        live = _LiveTab("https://www.zhipin.com/web/geek/jobs")
        monkeypatch.setattr(finding_jobs, "_tab", dead)
        monkeypatch.setattr(finding_jobs, "_browser", _FakeBrowser([newtab, live]))
        result = await _safe_evaluate("1+1")
        return live, result

    live, result = asyncio.run(scenario())
    assert result == {"ok": 9}
    # 接的是真页面，不是 chrome://newtab/
    assert finding_jobs._tab is live
    assert live.attach_calls == 1
