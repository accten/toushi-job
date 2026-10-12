"""单个岗位的主循环：抓 JD → 生成招呼语 → 校验 → 发送（或 dry-run）。

调用图：

.. code-block:: text

    main.py
      └─ send_job_descriptions_to_chat
           ├─ finding_jobs.open_browser_with_options / log_in
           ├─ finding_jobs.select_dropdown_option (一次)
           └─ while True:
                ├─ finding_jobs.get_job_description_by_index
                ├─ generate_letter (RAG + OpenAI 兼容端点)
                ├─ validate_letter / log_attempt
                └─ finding_jobs.{click_contact_button,wait_for_css,send_chat_message,navigate_back}

终止条件：
- 连续 ``MAX_CONSECUTIVE_MISSES`` 次拿不到 JD（推测到列表底部）
- 任何 exception 命中外层 try
"""

from __future__ import annotations

import asyncio
import logging
import os
import random

from boss_zhipin.audit import ValidationResult, log_attempt, validate_letter
from boss_zhipin.gui.events import emit as _emit_progress
from boss_zhipin.models.job_matcher import should_apply
from boss_zhipin.models.llm import current_provider_label, generate_letter
from boss_zhipin.website_oper import finding_jobs
# 直接 import 异常类，不走模块属性：CLI 跑智联时 wr_module.finding_jobs 会被换成
# zhilian_finding_jobs，那时 finding_jobs.PageReloadingError 未必存在，except
# 子句里做属性查找会在异常处理途中再炸一次。
from boss_zhipin.website_oper.finding_jobs import PageReloadingError

log = logging.getLogger(__name__)


class ReturnToListError(RuntimeError):
    """招呼语已经发出、但没能退回岗位列表。

    单独一个异常类型，让主循环能区分「消息压根没发出去」和「消息已送达、只是没回到
    列表」——后者必须照常记 ``sent=True``，否则会漏记真实已发送的招呼语，重跑时可能
    重复联系同一个招聘者。仍是 ``RuntimeError`` 子类，对外行为不变。
    """


async def send_response_and_go_back(response: str) -> None:
    """在 BOSS 沟通页发送 ``response`` 然后退回到列表。

    ``send_chat_message`` 真正把招呼语发出去之后才尝试返回列表；返回失败抛
    ``ReturnToListError``（此时消息已送达招聘者，调用方要照常记 ``sent=True``）。
    """
    await finding_jobs.send_chat_message(response)
    await finding_jobs.click_send_resume(timeout=5)
    # 等待发送完成并返回列表，使用可配置的延迟避免硬编码
    post_send_delay = _float_env("BOSS_POST_SEND_DELAY", 3.0, minimum=0.0)
    await asyncio.sleep(post_send_delay)
    if not await finding_jobs.return_to_job_list():
        raise ReturnToListError("发送后未能返回岗位列表")


def _int_env(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r 不是整数，回退默认 %d", name, raw, default)
        return default
    if minimum is not None:
        return max(minimum, value)
    return value


def _float_env(name: str, default: float, minimum: float | None = None) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r 不是数字，回退默认 %.1f", name, raw, default)
        return default
    if minimum is not None:
        return max(minimum, value)
    return value


def _send_delay_range() -> tuple[float, float]:
    min_delay = _float_env("BOSS_AUTO_SEND_DELAY_MIN", 10.0, minimum=0.0)
    max_delay = _float_env("BOSS_AUTO_SEND_DELAY_MAX", 60.0, minimum=0.0)
    if max_delay < min_delay:
        log.warning(
            "BOSS_AUTO_SEND_DELAY_MAX=%.1f 小于 MIN=%.1f，已交换两者",
            max_delay,
            min_delay,
        )
        return max_delay, min_delay
    return min_delay, max_delay


def _job_gap_range() -> tuple[float, float]:
    """岗位之间的随机间隔（秒）。

    ``BOSS_JOB_GAP_MIN`` / ``BOSS_JOB_GAP_MAX`` 覆盖，默认 2~6 秒。以前这里是硬编码
    的 ``sleep(3)``：一轮下来就是「每 3 秒点一张岗位卡」的固定拍子，是最好认的机器
    节奏。随机化之后至少不再是一条直线。
    """
    min_gap = _float_env("BOSS_JOB_GAP_MIN", 2.0, minimum=0.0)
    max_gap = _float_env("BOSS_JOB_GAP_MAX", 6.0, minimum=0.0)
    if max_gap < min_gap:
        log.warning(
            "BOSS_JOB_GAP_MAX=%.1f 小于 MIN=%.1f，已交换两者", max_gap, min_gap
        )
        return max_gap, min_gap
    return min_gap, max_gap


async def _wait_job_gap(min_gap: float, max_gap: float) -> None:
    """按 ``[min_gap, max_gap]`` 随机等一会儿，再处理下一个岗位。"""
    gap = random.uniform(min_gap, max_gap)
    log.info("岗位间隔 %.1f 秒", gap)
    await asyncio.sleep(gap)


async def send_job_descriptions_to_chat(
    usr_name: str,
    url: str,
    browser_type: str,
    label: str,
    vectorstore=None,
    dry_run: bool = False,
    resume_keywords: list[str] | None = None,
    resume_text: str | None = None,
    min_keyword_match: int = 2,
    min_llm_score: int = 70,
    exclude_keywords: list[str] | None = None,
    send_message: bool = True,
) -> None:
    """主循环（async）。

    用哪个 LLM 端点 / model 由 ``LLM_*`` 环境变量决定（见 ``llm._build_client``），
    不再按 provider 分支。``label`` 为空字符串时跳过下拉筛选，沿用 BOSS 默认推荐 feed。

    ``dry_run=True`` 时不点"立即沟通"，但 LLM 仍会调、招呼语仍会校验和写日志，
    用来调 prompt。

    ``send_message=False`` 时只点击投递按钮，不生成/发送招呼语（用于智联招聘等
    不需要打招呼语的平台）。

    整段必须包在 ``uc.loop().run_until_complete(...)`` 里一次性跑完，
    不能拆成多个 ``run_until_complete`` —— nodriver CDP 在事件循环停顿期间
    会进入半死状态，下次 evaluate 直接 hang。详见模块 docstring。
    """
    # audit log（letters.jsonl）那一列的 provider/model 标签——从当前 LLM_* 推。
    # 真实运行时 token / 成本由 generate_letter 里的 telemetry 单独记。
    llm_model = os.getenv("LLM_MODEL", "").strip()
    provider_label = current_provider_label()

    await finding_jobs.open_browser_with_options(url, browser_type)
    _emit_progress("browser_started")
    await finding_jobs.log_in()
    _emit_progress("login_ok")

    job_index = 1
    visible_index = 1
    iteration = 0
    consecutive_misses = 0
    sent_count = 0
    max_sent = _int_env("BOSS_AUTO_SEND_MAX_SENT", 50, minimum=1)
    send_delay_min, send_delay_max = _send_delay_range()
    job_gap_min, job_gap_max = _job_gap_range()
    # 一旦 LLM 评分走了 fail-open（缺配置 / 调用挂 / 解析不出分），第二层过滤其实在
    # 全放行。只在本轮**首次**遇到时提示一次，别每个岗位刷屏。
    degraded_warned = False
    # 推荐 feed 末尾、或者某条岗位卡 DOM 没渲染好，都会让 get_job_description
    # 返回 None。连续 N 次拿不到就当列表到底了停掉，否则 job_index 会无限涨。
    MAX_CONSECUTIVE_MISSES = 5
    await finding_jobs.select_dropdown_option(label)
    # 岗位卡必须真的渲染出内容才能开跑。只判 .job-card-box 存在不够：SPA 没
    # boot 起来时 DOM 里可能已有骨架屏空卡，选择器命中但正文全空。
    if not await finding_jobs.wait_for_real_job_cards(timeout=30):
        log.warning("等待 30s 仍无真实岗位卡，刷新页面重试")
        await finding_jobs.reload_page()
        # reload 冲掉了 select_dropdown_option 点出来的客户端筛选状态（它只点
        # chip / 下拉，不改 URL）。不重选就会拿 BOSS 默认推荐 feed 当成用户选的
        # tag 一路发下去，所以这里必须重来一次。
        await finding_jobs.select_dropdown_option(label)
        if not await finding_jobs.wait_for_real_job_cards(timeout=30):
            # 实测过的根因：profile 的 HTTP 缓存里存了 SPA vendor 脚本的 5xx 错误
            # 响应（CDN 抖动时缓存下来的），Chrome 一直重放这份坏缓存，Vue app 永远
            # boot 不起来，页面死在「加载中，请稍候」。清缓存即可，登录态不受影响。
            message = (
                "岗位列表始终没渲染出来，页面可能卡在「加载中，请稍候」。"
                "常见原因是 Chrome profile 缓存里存了 SPA 脚本的错误响应。"
                f"解法：关掉脚本，删掉 {finding_jobs.CHROME_PROFILE_DIR} 里 "
                "Default/Cache 和 Default/Code Cache 两个目录"
                "（别删 Default/Network，登录 cookie 在里面），然后重跑。"
            )
            log.error(message)
            # 必须在这里收尾。继续进主循环的话，骨架屏空卡是点得动的，每轮都会
            # 白等 10s JD 超时，5 轮后照样上报 feed_exhausted —— 那正是本次要修掉
            # 的误报：明明是页面没加载出来，却告诉用户「已经到底了」。
            _emit_progress("error", stage="job_list_never_rendered", message=message)
            return


    async def _apply_resume_filter(jd: str, idx: int) -> tuple[bool, dict]:
        """多层过滤：黑名单 → 关键词 → 向量 → LLM 评分。返回 (是否通过, details)。

        没配简历（``resume_text`` 为空）时直接放行 —— 跟以前一样。

        抽成闭包是因为**两条路都要用它**：「只投递」（智联）和「发招呼语」（BOSS）。
        以前智联那条分支排在过滤**前面**，等于对每一张岗位卡无差别点投递 ——
        黑名单 / 关键词 / RAG / LLM 评分全白设，投出去的东西跟简历没关系。
        """
        nonlocal degraded_warned
        if not resume_text:
            return True, {}

        apply, details = await asyncio.to_thread(
            should_apply,
            jd,
            resume_keywords,
            resume_text,
            min_keyword_match,
            min_llm_score,
            exclude_keywords,
            vectorstore,
        )
        # 评分降级（fail-open）首次出现时显式告警一次——否则用户只看到一堆岗位"通过"，
        # 不知道第二层 LLM 过滤其实没在跑。
        if details.get("scoring_degraded") and not degraded_warned:
            degraded_warned = True
            log.warning(
                "⚠️ LLM 评分暂时不可用（%s），本轮所有岗位按放行处理",
                details.get("reason", ""),
            )
            _emit_progress("scoring_degraded", detail=details.get("reason", ""))
        if apply:
            log.info("✅ [匹配 #%d] 关键词命中: %s", idx, details["matched_keywords"])
            if "score" in details:
                log.info("   LLM 评分: %s/100 - %s", details["score"], details["reason"])
            return True, details

        stage = details.get("stage", "unknown")
        if stage == "blacklist":
            log.info("⏭️ [跳过 #%d] 触发黑名单: %s", idx, details["reason"])
        elif stage == "keyword":
            log.info(
                "⏭️ [跳过 #%d] 关键词匹配不足: 命中 %s - %s",
                idx, details["matched_keywords"], details["reason"],
            )
        elif stage == "vector_search":
            log.info("⏭️ [跳过 #%d] 语义不匹配: %s", idx, details["reason"])
        else:
            log.info(
                "⏭️ [跳过 #%d] LLM 评分 %s/%s: %s",
                idx, details.get("score"), details.get("threshold"), details.get("reason"),
            )
        _emit_progress(
            "job_skipped",
            index=idx,
            reason=stage,
            detail=details.get("reason", ""),
            score=details.get("score"),
            threshold=details.get("threshold"),
            matched=details.get("matched_keywords"),
        )
        return False, details


    while True:
        try:
            iteration += 1
            log.info(
                "=== 第 %d 轮: 处理 job_index=%d visible_index=%d ===",
                iteration,
                job_index,
                visible_index,
            )
            job_description = await finding_jobs.get_job_description_by_index(
                visible_index
            )
            if job_description:
                consecutive_misses = 0
                _emit_progress(
                    "job_found", index=job_index, jd_preview=job_description[:80]
                )
                
                if not send_message:
                    # ====== 简化模式（智联）：只投递，不发送招呼语 ======
                    #
                    # 简化模式没有招呼语可校验，所以 validation 传一个 ok=True 的空结果：
                    # audit.log_attempt 会直接读 validation.ok / validation.reasons，
                    # 以前这里传 None → AttributeError，**第一次投递成功就把整个 run 崩掉**。
                    log.debug("简化模式：无招呼语，validation 记 ok=True")

                    # 跟 BOSS 走**同一条过滤链**（黑名单 → 关键词 → 向量 → LLM 评分）：
                    # 投递是不可逆的（招聘者那边直接收到），宁可少投也别盲投。
                    # 以前这条分支排在过滤前面 —— 等于对每一张岗位卡无差别点投递。
                    apply, details = await _apply_resume_filter(
                        job_description, job_index
                    )
                    if not apply:
                        job_index += 1
                        visible_index += 1
                        await _wait_job_gap(job_gap_min, job_gap_max)
                        continue
                    match_score = details.get("score") if resume_text else None

                    if dry_run:
                        # DRY-RUN 在这条路上以前是**失效的**：分支里没看 dry_run，直接
                        # click_apply 真投出去了，日志还记成 dry_run=False。勾了 Dry Run
                        # 却真投递，是最不能接受的那种 bug。
                        log.info(
                            "[DRY-RUN] 不点投递（智联模式），只记录。JD %d 字符",
                            len(job_description),
                        )
                        log_attempt(
                            provider="none",
                            model="none",
                            job_description=job_description,
                            letter="",
                            validation=ValidationResult(ok=True),
                            dry_run=True,
                            sent=False,
                        )
                        _emit_progress(
                            "letter_sent",
                            index=job_index,
                            status="dry_run",
                            score=match_score,
                            letter_len=0,
                        )
                    else:
                        log.info("简化模式：点击投递按钮")
                        applied = await finding_jobs.click_apply(timeout=10)
                        if applied:
                            sent_count += 1
                            log_attempt(
                                provider="none",
                                model="none",
                                job_description=job_description,
                                letter="",
                                validation=ValidationResult(ok=True),
                                dry_run=False,
                                sent=True,
                            )
                            _emit_progress(
                                "letter_sent",
                                index=job_index,
                                status="applied",
                                score=match_score,
                                letter_len=0,
                            )
                            if sent_count >= max_sent:
                                log.info(
                                    "已投递 %d 条，达到上限，结束",
                                    sent_count,
                                )
                                _emit_progress("feed_exhausted", total=job_index)
                                break
                            delay = random.uniform(send_delay_min, send_delay_max)
                            log.info("投递后等待 %.1f 秒再继续", delay)
                            await asyncio.sleep(delay)
                        else:
                            log.warning("投递按钮未找到，跳过此岗位")
                else:
                    # ====== 原有模式：发送招呼语 ======
                    element = await finding_jobs.get_text_by_css(".op-btn.op-btn-chat")
                    log.info("chat 按钮文字: %r", element)
                    if element == "立即沟通":
                        # ====== 多层过滤：黑名单 + 关键词 + 向量 + LLM ======
                        apply, details = await _apply_resume_filter(
                            job_description, job_index
                        )
                        if not apply:
                            job_index += 1
                            visible_index += 1
                            # 跳过也要按随机间隔走：不然一轮下来就是固定拍子的
                            # "点卡→跳过→点卡"，机器节奏比做什么都显眼。
                            await _wait_job_gap(job_gap_min, job_gap_max)
                            continue
                        # ====== 过滤结束 ======

                        # LLM 评分（resume_text 为空时没跑过滤 → None），带进 letter_sent 事件，
                        # 让 GUI 进度面板能看到"这条招呼语对应的岗位匹配多少分"。
                        match_score = details.get("score") if resume_text else None

                        # LLM 调用是同步阻塞的 HTTP 请求，扔到 thread pool 跑
                        # 避免阻塞事件循环 → 卡死 nodriver CDP heartbeat
                        response = await asyncio.to_thread(
                            generate_letter,
                            usr_name,
                            vectorstore,
                            job_description,
                        )

                        validation = validate_letter(response)

                        if not validation.ok:
                            log.warning(
                                "[BLOCKED] %s — preview: %r",
                                validation.reasons,
                                response[:80],
                            )
                            log_attempt(
                                provider=provider_label,
                                model=llm_model,
                                job_description=job_description,
                                letter=response,
                                validation=validation,
                                dry_run=dry_run,
                                sent=False,
                            )
                            _emit_progress(
                                "letter_sent",
                                index=job_index,
                                status="blocked",
                                score=match_score,
                                letter_len=len(response),
                            )
                        elif dry_run:
                            log.info(
                                "[DRY-RUN] 招呼语 (%d 字符) 不发送。--- letter ---\n%s\n--------------",
                                len(response),
                                response,
                            )
                            log_attempt(
                                provider=provider_label,
                                model=llm_model,
                                job_description=job_description,
                                letter=response,
                                validation=validation,
                                dry_run=True,
                                sent=False,
                            )
                            _emit_progress(
                                "letter_sent",
                                index=job_index,
                                status="dry_run",
                                score=match_score,
                                letter_len=len(response),
                            )
                        else:
                            log.info("发送招呼语：%s", response)
                            await asyncio.sleep(1)
                            # 走 JS 点按钮（跟 get_text_by_css('.op-btn.op-btn-chat') 同一个元素）：
                            # 老实现走 tab.xpath，在当前 Chrome 上直接抛 'DOM.enable' wasn't
                            # found 然后静默 return False —— 招呼语照样生成、日志照样写，
                            # 只有最后那一下没点出去，而且不留任何错。
                            await finding_jobs.click_contact_button(timeout=10)
                            # BOSS 改版后点"立即沟通"会弹一个确认框（自动发默认招呼语），
                            # 需要点"继续沟通"才能进聊天页。
                            await finding_jobs.dismiss_greeting_dialog(timeout=5)
                            await finding_jobs.wait_for_css("#chat-input", timeout=50)
                            # 消息在 send_chat_message 就送达招聘者，ReturnToListError 只表示
                            # 之后没能回到列表。「发送成功」跟「回到列表」必须分开记：无论能否
                            # 回列表都要记 sent=True + 计数，否则漏记真实发送、重跑会重复联系。
                            try:
                                await send_response_and_go_back(response)
                                returned_to_list = True
                            except ReturnToListError as e:
                                log.warning("%s；招呼语已发送，照常记录后结束本轮", e)
                                returned_to_list = False
                            sent_count += 1
                            log_attempt(
                                provider=provider_label,
                                model=llm_model,
                                job_description=job_description,
                                letter=response,
                                validation=validation,
                                dry_run=False,
                                sent=True,
                            )
                            _emit_progress(
                                "letter_sent",
                                index=job_index,
                                status="sent",
                                score=match_score,
                                letter_len=len(response),
                            )
                            if not returned_to_list:
                                # 已经卡在沟通页、回不到列表，继续循环只会一直拿不到 JD；
                                # 招呼语已记录，干净收尾而不是让异常冒泡把这条发送吞掉。
                                _emit_progress("feed_exhausted", total=job_index)
                                break
                            if sent_count >= max_sent:
                                log.info(
                                    "已发送 %d 条，达到 BOSS_AUTO_SEND_MAX_SENT 上限，结束",
                                    sent_count,
                                )
                                _emit_progress("feed_exhausted", total=job_index)
                                break
                            delay = random.uniform(send_delay_min, send_delay_max)
                            log.info("发送后等待 %.1f 秒再继续", delay)
                            await asyncio.sleep(delay)
            else:
                consecutive_misses += 1
                log.info(
                    "job_index=%d 拿不到 JD（连续第 %d 次）",
                    job_index,
                    consecutive_misses,
                )
                # 到底判断放在滚动之前：哪怕下面滚动每次都"成功"，只要累计够多次拿不到
                # JD 就收尾。否则在近乎无限的推荐 feed 上，滚动永远返回 True → 每轮都
                # continue，这个 break 永远够不着，循环空转到发送上限或异常才停。
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    log.info(
                        "连续 %d 个岗位拿不到，推测已到推荐 feed 列表底部，结束",
                        MAX_CONSECUTIVE_MISSES,
                    )
                    _emit_progress("feed_exhausted", total=job_index - 1)
                    break
                loaded_count = await finding_jobs.get_loaded_job_count()
                if loaded_count and visible_index > loaded_count:
                    log.info(
                        "当前只加载了 %d 个岗位，尝试滚动加载第 %d 个岗位",
                        loaded_count,
                        visible_index,
                    )
                if await finding_jobs.scroll_to_load_more_jobs():
                    # 不在这里把 consecutive_misses 归零：滚动成功只说明页面还能动，不代表
                    # 这个 index 拿得到 JD。只有真正抓到 JD（循环顶部那次归零）才算有进展，
                    # 这样上面的到底判断才不会被"滚得动"无限推迟。
                    latest_loaded_count = await finding_jobs.get_loaded_job_count()
                    if (
                        latest_loaded_count >= 5
                        and visible_index > latest_loaded_count
                    ):
                        log.info(
                            "岗位列表是虚拟滚动，重置可见索引：%d -> %d",
                            visible_index,
                            latest_loaded_count,
                        )
                        visible_index = latest_loaded_count
                    await asyncio.sleep(1)
                    continue

            await _wait_job_gap(job_gap_min, job_gap_max)
            job_index += 1
            visible_index += 1

        except PageReloadingError as e:
            # 页面卡在整页反复重载：继续跑只会把每个岗位当成「拿不到 JD」跳过，
            # 最后误报成「已到推荐列表底部」。这里明确停下，并把它说清楚。
            log.error("%s", e)
            _emit_progress("error", stage="page_reloading", message=str(e))
            break
        except Exception as e:
            log.exception("主循环抛异常: %s", e)
            _emit_progress(
                "error",
                stage=f"job_index={job_index}",
                message=f"{type(e).__name__}: {e}",
            )
            break
