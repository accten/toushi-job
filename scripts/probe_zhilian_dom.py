# -*- coding: utf-8 -*-
"""智联招聘岗位页 DOM 侦察（**只读**：不点投递、不点沟通）。

什么时候用：登录智联之后，需要重新确认职位页的真实 DOM 结构（选择器适配）。

用法：
    uv run python scripts/probe_zhilian_dom.py

前置：
    先登录 —— GUI 里把平台切到「智联招聘」→ 点「登录」→ 在弹出的干净 Chrome 里
    扫码/验证码登录成功 → 关掉那个窗口。然后再跑本脚本（它会用同一个 profile）。

输出什么：
    - 当前 URL / 是否还停在 passport 登录页
    - 岗位卡 / JD 详情 / 投递按钮 等候选选择器的命中数量
    - 命中的元素样本（tag + class + 文本）
把这些贴给作者，就能把 zhilian_finding_jobs.SELECTORS 改成真实值。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from toushi.website_oper import zhilian_finding_jobs as z  # noqa: E402

CANDIDATES = [
    # 岗位卡（列表项）
    ".job-card", "[class*=job-card]", "[class*=jobcard]", "[class*=position-card]",
    "[class*=jobs-list] li", "[class*=joblist] li", "[class*=list-item]",
    # JD 详情
    "[class*=job-description]", "[class*=describe]", "[class*=job-detail]",
    "[class*=detail-content]", "[class*=position-detail]",
    # 投递按钮
    "button[class*=apply]", "[class*=apply]", "[class*=deliver]",
    "button[class*=btn]", "[class*=resume-submit]",
    # 登录信号
    "a.home-header__c-no-login", "img.c-login__top__img", "[class*=login]",
]

JS = r"""JSON.stringify((() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
  };
  const wanted = %s;
  const counts = {};
  for (const sel of wanted) {
    try { counts[sel] = document.querySelectorAll(sel).length; } catch (e) { counts[sel] = -1; }
  }
  // 岗位卡候选：找"很多个同样 class 的兄弟元素"，通常就是列表项
  const groupByClass = {};
  document.querySelectorAll('div,li,article,a').forEach((el) => {
    if (!visible(el)) return;
    const cls = typeof el.className === 'string' ? el.className.trim() : '';
    if (!cls) return;
    const text = (el.innerText || '').trim();
    if (text.length < 20 || text.length > 400) return;
    groupByClass[cls] = (groupByClass[cls] || 0) + 1;
  });
  const repeated = Object.entries(groupByClass)
    .filter(([, n]) => n >= 5)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 15)
    .map(([cls, n]) => ({cls: cls.slice(0, 90), n}));
  const sample = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return {
      tag: el.tagName,
      cls: String(el.className || '').slice(0, 90),
      text: (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 120),
    };
  };
  return {
    url: location.href,
    title: document.title,
    onLoginPage: /login|passport/.test(location.href),
    head: (document.body ? document.body.innerText : '').slice(0, 200).replace(/\s+/g, ' '),
    counts,
    repeated,
    samples: wanted.map((s) => [s, sample(s)]).filter(([, v]) => v),
  };
})())"""


async def main() -> None:
    await z.open_browser_with_options(z.ZHLIAN_RECOMMEND_URL, "chrome")
    await asyncio.sleep(6)

    print("tab.url:", z._tab.url)
    try:
        print("判定已登录:", await z._is_logged_in())
    except Exception as e:  # noqa: BLE001
        print("登录判定抛错:", type(e).__name__, e)
    print("get_loaded_job_count():", await z.get_loaded_job_count())

    data = await z._safe_evaluate(JS % json.dumps(CANDIDATES), timeout=10)
    print("url:", data.get("url"))
    print("title:", data.get("title"))
    print("还在登录页:", data.get("onLoginPage"))
    print("head:", data.get("head"))
    print("--- 候选选择器命中数 ---")
    for sel, n in (data.get("counts") or {}).items():
        print(f"  {n:5d}  {sel}")
    print("--- 重复出现的 class（>=5 个，很可能是列表项）---")
    for item in data.get("repeated") or []:
        print(f"  x{item['n']:<4d} {item['cls']}")
    print("--- 命中元素样本 ---")
    for sel, s in data.get("samples") or []:
        print(f"  {sel}\n     <{s['tag']} class={s['cls']!r}> {s['text']}")

    await z.shutdown()
    print("=== done ===")


if __name__ == "__main__":
    import nodriver as uc

    uc.loop().run_until_complete(main())
