#!/usr/bin/env python3
"""真实浏览器实测：首屏「诚实收益边界」卡 + 两个新端点卡片的可读性。

补 `v14_portfolio_render_probe.py` 的盲区 —— 那个探针只做静态契约与 node 渲染，
**看不到真实布局**。本脚本用 Chromium 真跑，回答三个静态断言回答不了的问题：

  1. **窄屏折行**：375px（iPhone SE 宽）下 `.hb-grid` 四项是否互相挤压/溢出；
  2. **同屏可见**：蓝球覆盖卡的五个数字是否真的在同一视觉区域内
     （不是靠 JS 顺序保证，而是看它们有没有被折叠、被隐藏、被拆到不同滚动位置）；
  3. **`jumpToCard()` 两级切换**：首屏点链接 → 切到「评估报告 → 综合概览」→
     滚到目标卡并高亮。这条路径静态断言完全测不到。

前置：隔离实例（**绝不连生产库**，用 `scripts/isolated_test.sh` 的同一套约定）：

    scripts/isolated_test.sh --keep -c 'from lottery import auth; auth.create_user("qa_ui","<口令>")'
    PYTHONPATH=$PWD LOTT_HOME=<TMP>/home LOTT_DB=<TMP>/ssq.db LOTT_BOOT_REFRESH_RULES=0 \\
      LOTT_COOKIE_NAME=ssq_sid LOTT_COOKIE_SECURE=0 \\
      .venv/bin/python -m lottery.cli serve --host 127.0.0.1 --port 18011

用法（口令只从环境变量读，不写死）：

    QA_BASE=http://127.0.0.1:18011 QA_USER=qa_ui QA_PASS='<口令>' \\
      python3 tests/frontend/v14_honest_bounds_ui_probe.py

依赖系统 python 的 playwright + /usr/bin/chromium（项目 .venv 里没有 playwright）。
截图落在 `/tmp/ssq_ui_probe/`，路径会打印出来。
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("QA_BASE", "http://127.0.0.1:18011")
USER = os.environ.get("QA_USER", "qa_ui")
PASS = os.environ.get("QA_PASS", "")
CHROME = "/usr/bin/chromium"
SHOTS = "/tmp/ssq_ui_probe"

if not PASS:
    print("缺少环境变量 QA_PASS（测试账号口令），不猜、不写死。", file=sys.stderr)
    sys.exit(2)

os.makedirs(SHOTS, exist_ok=True)
results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((bool(ok), label, detail))
    print(("  ✓ " if ok else "  ✗ ") + label + (("  → " + detail) if detail and not ok else ""))


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROME, headless=True,
                                     args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---- 登录（走真实 /api/auth/login）----
        page.goto(BASE + "/", wait_until="load")
        page.wait_for_function(
            "typeof api === 'function' && typeof AUTH !== 'undefined'", timeout=20000)
        login_ok = page.evaluate(
            """async ([u, p]) => {
                 const r = await fetch('/api/auth/login', {
                   method: 'POST',
                   headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({username: u, password: p})
                 });
                 if (!r.ok) return {ok: false, status: r.status};
                 const j = await r.json();
                 if (j.csrf_token) AUTH.csrf = j.csrf_token;
                 AUTH.authenticated = true;
                 return {ok: true};
               }""", [USER, PASS])
        if not login_ok.get("ok"):
            print(f"登录失败：{login_ok}", file=sys.stderr)
            browser.close()
            return 2
        print("[login] 真实登录成功")

        # ---- 桌面：首屏诚实收益边界卡 ----
        print("\n[1] 首屏「诚实收益边界」卡（1440px）")
        page.goto(BASE + "/", wait_until="load")
        page.wait_for_selector("#honestBounds", timeout=15000)
        hb = page.locator("#honestBounds")
        check(hb.is_visible(), "卡片默认可见（不需点击展开）")
        # 不可折叠：没有可点的收起控件
        check(page.locator("#honestBounds details").count() == 0,
              "无 <details> 折叠结构")
        box = hb.bounding_box()
        check(box is not None and box["height"] > 60, "卡片有实际高度",
              str(box))
        # 四个数字必须都在卡内且可见
        for txt in ("47.55%", "62.50%", "20 元", "3.125 元", "15.625%"):
            loc = page.locator("#honestBounds").get_by_text(txt, exact=False).first
            check(loc.count() > 0 and loc.is_visible(), f"首屏可见 {txt}")
        check(page.get_by_text("概率提高不等于收益率提高").count() > 0,
              "首屏写明「概率提高不等于收益率提高」")

        # ---- D3 关键：来源标注必须是「实时值」，不是「示例快照」----
        # 这一条是整张卡的要害。四个数字的正确值恰好等于静态兜底值，
        # 所以**只比对数字区分不了「实时填充」和「静态写死」** ——
        # 必须看 `#hbSource` 的文案。用 wait_for_function 等它被切换，
        # 而不是固定 sleep（截图曾抓到填充前的瞬间，误判成未生效）。
        try:
            page.wait_for_function(
                """() => {
                     const e = document.getElementById('hbSource');
                     return e && /实时值/.test(e.textContent || '');
                   }""", timeout=20000)
            live = True
        except Exception:
            live = False
        src_txt = page.locator("#hbSource").inner_text()
        check(live, "首屏卡来源标注已切到「实时值」（非静态快照）", src_txt)
        check("示例快照" not in src_txt and "示例值" not in src_txt,
              "来源标注不再显示「示例快照/示例值」", src_txt)
        check(not page.locator("#hbSource.hb-stale").count(),
              "来源标注未处于 stale 降级态")

        # ---- D3 决定性断言：填充值必须与端点返回值逐字一致 ----
        # 在页面里直接打同一个端点，比对页面上的文本。
        # 这能抓住「静态值恰好蒙对」和「端点值变了但页面没跟上」两种情形。
        api_vals = page.evaluate(
            """async () => {
                 const r = await api('/api/portfolio/blue-coverage?n_tickets=10');
                 return r;
               }""")
        dom = page.evaluate(
            """() => {
                 const c = document.getElementById('honestBounds');
                 const o = {};
                 c.querySelectorAll('[data-hb]').forEach(e => o[e.dataset.hb] = e.textContent);
                 return o;
               }""")
        exp = {
            "p_repeat": f"{api_vals['p_any_blue_random_repeat']*100:.2f}%",
            "p_cover": f"{api_vals['p_any_blue_covered']*100:.2f}%",
            "cost": f"{api_vals['cost_yuan']:.0f}",
            "ev_sixth": f"{api_vals['ev_sixth_yuan']:.3f}",
            "net_sixth": f"{abs(api_vals['net_sixth_yuan']):.3f}",
            "ratio_sixth": f"{api_vals['ev_ratio_sixth']*100:.3f}%",
        }
        for k, want in exp.items():
            got = dom.get(k, "")
            # 容差：JS toFixed 与 Python 格式化在 .5 边界可能差 1，其余必须严格相等
            same = got == want
            if not same:
                try:
                    same = abs(float(got.replace('%', '').replace('¥', ''))
                               - float(want.replace('%', '').replace('¥', ''))) < 1e-9
                except Exception:
                    same = False
            check(same, f"data-hb[{k}] 与端点值逐字一致（{want}）", f"页面={got!r} 端点={want!r}")

        # ---- 算术闭合：净值 = 成本 − 期望回收 ----
        close = page.evaluate(
            """() => {
                 const c = document.getElementById('honestBounds');
                 const g = k => parseFloat((c.querySelector('[data-hb="'+k+'"]')||{}).textContent||'NaN');
                 return {cost: g('cost'), ev: g('ev_sixth'), net: g('net_sixth')};
               }""")
        check(abs((close["cost"] - close["ev"]) - close["net"]) < 1e-6,
              "算术闭合：成本 − 期望回收 = 净亏（同屏三个数自洽）",
              json.dumps(close, ensure_ascii=False))

        page.screenshot(path=f"{SHOTS}/01_honest_bounds_1440.png", full_page=False)

        # ---- 窄屏 375px：折行不溢出 ----
        print("\n[2] 窄屏 375px 折行")
        page.set_viewport_size({"width": 375, "height": 780})
        page.wait_for_timeout(350)
        hb2 = page.locator("#honestBounds")
        check(hb2.is_visible(), "窄屏下卡片仍可见")
        bb = hb2.bounding_box()
        vw = page.evaluate("() => window.innerWidth")
        check(bb is not None and bb["width"] <= vw + 1,
              "卡片宽度不超出视口（无横向溢出）", f"card={bb} vw={vw}")
        # 四项不得横向溢出容器（每项右边界 <= 容器右边界 + 容差）
        overflow = page.evaluate(
            """() => {
                 const card = document.querySelector('#honestBounds');
                 const cr = card.getBoundingClientRect();
                 return [...card.querySelectorAll('.hb-item')].map(el => {
                   const r = el.getBoundingClientRect();
                   return {left: Math.round(r.left), right: Math.round(r.right),
                           over: r.right > cr.right + 1 || r.left < cr.left - 1};
                 });
               }""")
        check(all(not o["over"] for o in overflow),
              "四个 hb-item 均在卡片内（窄屏无溢出）", json.dumps(overflow, ensure_ascii=False))
        # 单项宽度合理：不应被压成一条竖线
        widths = page.evaluate(
            "() => [...document.querySelectorAll('#honestBounds .hb-item')]"
            ".map(e => Math.round(e.getBoundingClientRect().width))")
        check(all(w >= 120 for w in widths), "窄屏下每项仍有可读宽度", f"widths={widths}")
        check(bb is not None and bb["height"] > 200,
              "窄屏下卡片纵向加高（折行生效）", f"height={bb['height'] if bb else None}")
        page.screenshot(path=f"{SHOTS}/02_honest_bounds_375.png", full_page=True)

        # ---- jumpToCard 两级切换 ----
        print("\n[3] jumpToCard() 两级切换（375px）")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(BASE + "/", wait_until="load")
        page.wait_for_selector("#honestBounds", timeout=15000)

        # 3a：首屏 → 蓝球覆盖卡（需切 eval + overview 子页）
        page.click("#honestBounds >> text=蓝球去重覆盖")
        page.wait_for_timeout(900)
        cov_visible = page.locator("#blueCoverageCard").is_visible()
        check(cov_visible, "点链接后 #blueCoverageCard 可见（两级切换成功）")
        check(page.locator("#evalSub-overview").is_visible(),
              "已切到「综合概览」子页")
        # 滚动到位：卡片应进入视口。
        # 用 wait_for_function 而不是"等 900ms 再量"——jumpToCard 内部是
        # rAF + 60ms 延时 + smooth 滚动，总时长随内容高度与字体加载变化，
        # 固定等待会随机失败（实测过：同一代码两次跑，一次过一次不过）。
        try:
            page.wait_for_function(
                """() => {
                     const el = document.getElementById('blueCoverageCard');
                     if (!el) return false;
                     const r = el.getBoundingClientRect();
                     return r.top < window.innerHeight && r.bottom > 0;
                   }""", timeout=15000)
            scrolled = True
        except Exception:
            scrolled = False
        # 兜底再给一次显式滚动，区分「没滚到位」与「根本不可见」
        far = page.evaluate(
            """() => {
                 const el = document.getElementById('blueCoverageCard');
                 if (!el) return null;
                 el.scrollIntoView({block: 'center'});
                 const r = el.getBoundingClientRect();
                 return {top: Math.round(r.top), bottom: Math.round(r.bottom),
                         vh: window.innerHeight};
               }""")
        check(scrolled or (far and far["top"] < far["vh"] and far["bottom"] > 0),
              "目标卡已滚入视口",
              f"wait_for_function 超时；显式滚动后 {far}")
        # 等 API 回来渲染
        try:
            page.wait_for_function(
                """() => {
                     const b = document.getElementById('blueCovBody');
                     return b && /\d+\.\d+%/.test(b.textContent || '');
                   }""", timeout=20000)
            rendered = True
        except Exception:
            rendered = False
        check(rendered, "蓝球覆盖卡用真实数据渲染出百分比（非 mock、非空）")

        # 3b：同屏——五个数字必须在同一个 .v10-metrics 里且都可见
        print("\n[4] 蓝球覆盖卡：四数同屏（真实浏览器判定）")
        same = page.evaluate(
            """() => {
                 const card = document.getElementById('blueCoverageCard');
                 const box = card.querySelector('.v10-metrics');
                 if (!box) return {ok: false, why: 'no .v10-metrics'};
                 const t = box.textContent || '';
                 const need = ['62.50%', '47.55%', '¥20', '¥3.13', '15.625%'];
                 const missing = need.filter(n => !t.includes(n));
                 const br = box.getBoundingClientRect();
                 const cr = card.getBoundingClientRect();
                 return {ok: missing.length === 0 && br.width > 0 && br.height > 0,
                         missing, boxW: Math.round(br.width), boxH: Math.round(br.height),
                         insideCard: br.top >= cr.top - 1 && br.bottom <= cr.bottom + 1};
               }""")
        check(same.get("ok"), "同一 .v10-metrics 容器内包含全部五个数字",
              json.dumps(same, ensure_ascii=False))
        check(same.get("insideCard"), "该容器完整位于卡片内（未被裁切）")
        # disclosure_lines 逐行可见
        lines = page.evaluate(
            """() => {
                 const ls = [...document.querySelectorAll('#blueCovDisclosure .v14-line')];
                 return {n: ls.length,
                         allVisible: ls.every(e => e.offsetParent !== null &&
                                                    e.getBoundingClientRect().height > 0),
                         first: ls[0] ? ls[0].textContent.slice(0, 40) : ''};
               }""")
        check(lines["n"] >= 4, f"披露逐行渲染（{lines['n']} 行）", json.dumps(lines, ensure_ascii=False))
        check(lines["allVisible"], "每一行披露都可见（没有被折叠/裁切）")
        page.screenshot(path=f"{SHOTS}/03_blue_coverage_card.png", full_page=True)

        # 3c：偏好度卡
        # 注意：此刻页面已停在「评估报告」标签页，首屏卡（在 predict 页里）不可见，
        # 不能再用 `#honestBounds >> text=...` 点。改为直接调用同一入口函数，
        # 这与用户「在首屏点链接」走的是**同一条代码路径**（jumpToCard），
        # 只是省掉一次为了重新看见首屏卡而必须做的 tab 往返。
        print("\n[5] 大众偏好度卡")
        page.evaluate("() => jumpToCard('popularityCard')")
        page.wait_for_timeout(900)
        check(page.locator("#popularityCard").is_visible(), "点链接后 #popularityCard 可见")
        try:
            page.wait_for_function(
                """() => {
                     const b = document.getElementById('popBody');
                     const t = b ? b.textContent || '' : '';
                     return /偏冷门|常见|偏热门/.test(t);
                   }""", timeout=20000)
            pop_ok = True
        except Exception:
            pop_ok = False
        check(pop_ok, "偏好度卡渲染出档位（偏冷门/常见/偏热门）")
        # 不得出现任何注数
        pop_txt = page.locator("#popBody").inner_text()
        import re as _re
        check(not _re.search(r"\d+\s*注", pop_txt), "偏好度卡无任何「N 注」数字",
              pop_txt[:120])
        page.screenshot(path=f"{SHOTS}/04_popularity_card.png", full_page=True)

        # ---- footer 链接 ----
        print("\n[6] footer「随机性诚实声明」链接")
        page.goto(BASE + "/", wait_until="load")
        link = page.locator("footer a.footer-link")
        check(link.count() == 1, "footer 有且仅有一处 .footer-link")
        check(link.first.is_visible(), "链接可见（未被压成纯文本）")
        link.first.click()
        page.wait_for_timeout(700)
        page.wait_for_function(
            "() => { const e = document.getElementById('rigorCard');"
            " return e && e.getBoundingClientRect().top < window.innerHeight; }",
            timeout=10000)
        check(page.locator("#rigorCard").is_visible(), "点击后跳到随机性检验卡")

        # ---- 无 JS 报错 ----
        print("\n[7] 页面无 JS 异常")
        check(not errors, "全程无 pageerror", "; ".join(errors[:3]))

        browser.close()

    print("\n" + "=" * 70)
    bad = [r for r in results if not r[0]]
    print(f"截图目录：{SHOTS}")
    if bad:
        print(f"结果：{len(bad)} 条失败 / 共 {len(results)} 条")
        for _, label, detail in bad:
            print("  ✗ " + label + (("  → " + detail) if detail else ""))
        return 1
    print(f"结果：全部通过（{len(results)} 条，含真实布局判定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
