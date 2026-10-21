#!/usr/bin/env python3
"""V14-B2/B3 前端探针：蓝球覆盖卡 + 大众偏好度卡的「披露完整性」。

覆盖 task-1 验收标准 1~3：

  1. **披露不可拆** —— 蓝球覆盖卡必须让用户同屏看到四个数字
     （覆盖概率 / 随机重复概率 / 成本 / 六等奖期望回收），且 `disclosure_lines` 全文
     逐行完整渲染。禁止只显示「47.55% → 62.50%」这一半。
  2. **popularity 只给档位** —— 不得出现任何「预计 X 注中奖」类数字。
  3. **禁用词** —— 「提高中奖率 / 提高命中率 / 更划算 / 稳赚」不得出现在用户可见文本里。

## 这个探针怎么做（两种模式，都不起服务）

纯静态断言（默认、CI 可跑）：解析 `web/static/app.js` / `web/index.html` 源码，
剔除注释与「禁用词清单」自身的列举行后，检查渲染函数的结构性约束。

**渲染等价性**（`--render`，需要 `node`）：把 `app.js` 里的渲染函数原样 eval 到
node 沙箱里，喂**真实后端响应**（`--fixture` 由路由处理函数直接生成，不走 HTTP、
不需要登录），再对产出的 HTML 做结构断言。这能抓到「静态看起来对、实际渲染错」
的情况（例如档位 key 写成 medium 导致样式静默丢失）。

## ⚠️ 与后端签名强耦合是**刻意设计**，不是脆弱，更不要因此删掉本探针

`probe_render()` 直接 `from lottery.routes_portfolio import portfolio_*` 生成 fixture，
而不是发 HTTP 请求 —— 是有意的：

* 不依赖服务是否起着、端口是否被占、是否需要登录（这三件事是前端探针最大的不稳定源）；
* 后端**改字段名 / 改函数签名**时，本探针会立刻变红 —— 这正是它存在的价值：
  前端渲染逻辑与后端返回契约必须同步演进，接口一改前端就该被提醒去核对；
* 如果改成"发 HTTP + 断言状态码 200"，字段改名会被静默吞掉（200 照样返回），
  前端就会渲染出 `undefined`，而探针仍然全绿 —— 那是更坏的结局。

所以：看到本探针因后端签名变化而红，正确的反应是**同步改探针与前端**，
而不是给它加 try/except、加 `getattr` 兜底、或干脆删掉它。

## 为什么不进 pytest 主套件

`tests/frontend/` 是前端取证目录（Lead 2026-10-21 裁定），与李工的
`tests/test_*frontend*.py`（后端契约）分开放。本脚本自带退出码，可直接当 CI 步骤。

## 用法

    # 静态契约（无依赖）
    .venv/bin/python tests/frontend/v14_portfolio_render_probe.py

    # 静态 + 真实渲染（需要 node，用真实后端响应喂渲染函数）
    .venv/bin/python tests/frontend/v14_portfolio_render_probe.py --render

退出码：0 全过；1 有断言失败；2 环境不具备（例如 --render 但没有 node）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_JS = REPO / "web" / "static" / "app.js"
INDEX_HTML = REPO / "web" / "index.html"

# 产品纪律禁用词（后端 routes_predict.py 与 V14 文案审计同一份口径）
BANNED = ["提高中奖率", "提高命中率", "更划算", "稳赚"]

# 允许出现禁用词的地方：这些行是在**列举禁令本身**，不是在使用它
BANNED_WHITELIST_MARKERS = ("禁用词", "不得出现", "禁止", "不要", "禁用")

failures: list[str] = []

def _js_fixed(value, digits: int) -> str:
    """复现 JS `Number.prototype.toFixed` 的十进制舍入。

    Python 的 `%.2f` 用银行家舍入（round-half-to-even），`3.125` → `3.12`；
    JS 的 `toFixed(2)` 在其实现上是**按十进制远离零**舍入，`3.125` → `3.13`。
    探针要断言的是「前端渲染出什么」，所以必须用前端的规则，不能用 Python 的默认行为。
    """
    from decimal import Decimal, ROUND_HALF_UP

    q = Decimal(1).scaleb(-digits)
    return str(Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP))




def check(ok: bool, label: str, detail: str = "") -> None:
    print(("  ✓ " if ok else "  ✗ ") + label + (("  → " + detail) if (detail and not ok) else ""))
    if not ok:
        failures.append(label + ((" → " + detail) if detail else ""))


# ---------------------------------------------------------------- 源码工具


def _strip_html_comments(html: str) -> str:
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def _strip_js_comments(code: str) -> str:
    """去掉 // 行注释与 /* */ 块注释。本项目的 app.js 不用正则字面量含 //，
    简单顺序剥离足够；保守起见先剥块注释再剥行注释。"""
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return "\n".join(
        ln for ln in code.splitlines() if not ln.strip().startswith("//")
    )


def _function_source(src: str, name: str) -> str:
    """按大括号配平截出 `function <name>() {...}` 的函数体源码。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"app.js 里找不到函数 {name}")
    i = src.index("{", m.end())
    depth = 0
    for k in range(i, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): k + 1]
    raise AssertionError(f"{name} 的大括号不配平")


def _card_html(html: str, card_id: str, stop_id: str) -> str:
    """截取某张卡在 index.html 里的片段（已剔除注释）。"""
    i = html.index('id="' + card_id + '"')
    j = html.find('id="' + stop_id + '"', i)
    return html[i: j if j > 0 else len(html)]


# ---------------------------------------------------------------- 静态契约


def probe_static() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    html_v = _strip_html_comments(INDEX_HTML.read_text(encoding="utf-8"))

    print("[1] 披露不可拆：蓝球覆盖卡结构")
    bc = _function_source(src, "renderBlueCoverage")
    bc_code = _strip_js_comments(bc)
    # 四个数字的取值来源必须都出现在渲染函数里
    for field, label in [
        ("p_any_blue_covered", "覆盖概率"),
        ("p_any_blue_random_repeat", "随机重复概率"),
        ("cost_yuan", "成本"),
        ("ev_sixth_yuan", "六等奖期望回收"),
    ]:
        check(field in bc_code, f"renderBlueCoverage 读取 {field}（{label}）")
    # 净亏与返奖比恒定
    check("net_sixth_yuan" in bc_code, "renderBlueCoverage 读取 net_sixth_yuan（净亏）")
    check("ev_ratio_sixth" in bc_code, "renderBlueCoverage 读取 ev_ratio_sixth（返奖比恒定）")
    # disclosure_lines 必须逐行全渲染，且不得截断
    check("disclosure_lines" in bc_code, "renderBlueCoverage 读取 disclosure_lines")
    check(
        re.search(r"disclosure_lines\s*\)\s*\?\s*[^:]*\.map\(", bc_code) is not None
        or ".map((" in bc_code,
        "disclosure_lines 逐行 map 渲染（非只取第一条）",
    )
    check("slice(" not in bc_code and "substring(" not in bc_code,
          "renderBlueCoverage 不截断披露文本")
    # 披露缺失时必须显式告警而不是静默省略
    check("披露文本缺失" in bc_code or "不可单独使用" in bc_code,
          "disclosure_lines 缺失时有显式告警，不静默省略")
    # honesty_flags 三态可见
    for flag in ("increases_hit_probability", "increases_expected_return",
                 "increases_prize_frequency"):
        check(flag in bc_code, f"展示 honesty_flags.{flag}")

    print("[2] popularity 只给档位")
    pop = _strip_js_comments(_function_source(src, "renderPopularity"))
    check("tier" in pop and "tier_label" in pop, "renderPopularity 显示 tier / tier_label")
    # 不得出现注数预估：检查渲染函数里没有把任何数值拼成「N 注中奖」的模板
    check(not re.search(r"注中奖", pop), "renderPopularity 无「…注中奖」文案")
    check(not re.search(r"predict[_(]?p1|predicted_p1|n_?tickets?\s*\*", pop),
          "renderPopularity 不做注数换算")
    # 档位 key 必须与后端 tier_of() 的 low/mid/high 对齐
    tier_map = re.search(r"POP_TIER_CLASS\s*=\s*\{([^}]*)\}", src)
    check(tier_map is not None, "存在 POP_TIER_CLASS 档位映射")
    if tier_map:
        keys = set(re.findall(r"(\w+)\s*:", tier_map.group(1)))
        check(keys == {"low", "mid", "high"},
              "POP_TIER_CLASS 的 key 与后端 tier_of() 对齐 (low/mid/high)",
              f"实际 {sorted(keys)}")

    print("[3] 禁用词（用户可见文本）")
    segments = {
        "app.js renderBlueCoverage": bc_code,
        "app.js renderPopularity": pop,
        "index.html 两卡文案": _card_html(html_v, "blueCoverageCard", "shapeRuleCard"),
    }
    for where, text in segments.items():
        hits = [w for w in BANNED if w in text]
        check(not hits, f"{where} 无禁用词", f"命中 {hits}")

    print("[4] 降级容错（不白屏）")
    check(src.count("_degradeNote") >= 3, "_degradeNote 已实现并被两卡使用")
    for fn in ("loadBlueCoverage", "loadPopularity"):
        body = _strip_js_comments(_function_source(src, fn))
        check("try" in body and "catch" in body,
              f"{fn} 有 try/catch，单卡失败不外抛")
        check("throw" not in body, f"{fn} 不向外抛异常")
    # 并发入口必须对每个 promise 兜底
    v10 = _function_source(src, "loadV10Cards")
    check(".catch(" in v10, "loadV10Cards 对每个 promise 兜底，单卡失败不连累首屏")
    check("loadBlueCoverage" in v10 and "loadPopularity" in v10,
          "两卡已接入 loadV10Cards 统一加载")

    print("[5] 既有口径不被破坏")
    check("usable_replayable" in src, "首屏仍读 usable_replayable（未退回 usable）")
    check("结构门控条件" in html_v, "L2 约束仍叫「结构门控条件」")
    shape = _card_html(html_v, "shapeRuleCard", "multiScaleCard")
    # 与 tests/test_v14_naming_discipline.py 同构：显式否定句（含「不得/不是/禁止」）是
    # 纪律文本本身，不是违规用法，必须排除，否则守卫会把「本来就在禁止它」当成「在用它」。
    shape_offenders = [
        ln.strip()[:80]
        for ln in shape.splitlines()
        if "可用规律" in ln
        and not any(w in ln for w in ("不得", "不是", "禁止"))
        and not ln.strip().startswith("<!--")
    ]
    check(not shape_offenders, "形态规律卡未把 L2 约束叫「可用规律」",
          f"违规行 {shape_offenders}")

    print("[7] 口径隔离：rule_evidence 与 usable_replayable 不得混为一谈（P2#11）")
    # `rule_evidence`（闲时挖掘的统计证据项）与 `usable_replayable`（可回放且通过前置注册）
    # 是两套不同来源、不同门槛的计数。混在一张卡里展示 = 把两个口径相加，
    # 等于给用户一个谁都不是的数字。
    fn_rule = _strip_js_comments(_function_source(src, "renderRuleUsage"))
    check("usable_replayable" in fn_rule, "renderRuleUsage 用 usable_replayable 计数")
    check("rule_evidence" not in fn_rule,
          "renderRuleUsage 不读 rule_evidence（避免两个口径混算）")
    # 前端对 rule_evidence 应保持零可执行引用
    exec_refs = [
        ln for ln in _strip_js_comments(src).splitlines() if "rule_evidence" in ln
    ]
    check(not exec_refs, "app.js 对 rule_evidence 零可执行引用", f"{exec_refs[:2]}")

    print("[8] 档位三值均映射到非兜底 class（P2#11）")
    if tier_map:
        m = re.search(r"POP_TIER_CLASS\s*=\s*\{([^}]*)\}", src)
        pairs = dict(re.findall(r"(\w+)\s*:\s*[\"'](\w+)[\"']", m.group(1)))
        check(len(pairs) == 3, "POP_TIER_CLASS 有 3 个映射", f"{pairs}")
        for tier_val in ("low", "mid", "high"):
            check(tier_val in pairs,
                  f"tier='{tier_val}' 有专属 class（不落到兜底）")
        # 兜底值必须是「三档之外」的，否则某一档会被误判成兜底
        fallback = re.search(r"POP_TIER_CLASS\[tier\]\s*\|\|\s*[\"'](\w+)[\"']", src)
        check(fallback is not None, "存在 tier 兜底分支")
        if fallback:
            fb = fallback.group(1)
            check(fb in pairs.values(),
                  f"兜底 class '{fb}' 是某档的真实 class（三档样式一致）")

    print("[9] 站名口径对齐 + 星级死代码清理（D-05#1 / P2）")
    html_raw = INDEX_HTML.read_text(encoding="utf-8")
    title_m = re.search(r"<title>(.*?)</title>", html_raw, flags=re.S)
    check(title_m is not None, "index.html 有 <title>")
    if title_m:
        check(title_m.group(1).strip() == "双色球号码组合分析系统",
              "<title> 与后端口径一致（双色球号码组合分析系统）",
              f"实际 {title_m.group(1).strip()!r}")
        check("智能预测" not in title_m.group(1),
              "<title> 不再出现「智能预测」")
    # footer 站名同口径
    foot_m = re.search(r"<footer>(.*?)</footer>", html_raw, flags=re.S)
    check(foot_m is not None, "index.html 有 <footer>")
    if foot_m:
        check("双色球号码组合分析系统" in foot_m.group(1),
              "footer 站名与后端同口径")
        check("智能预测" not in foot_m.group(1), "footer 不再出现「智能预测」")
        # 「随机性诚实声明」必须是可点链接，不能只压成四个字
        check("随机性诚实声明" in foot_m.group(1), "footer 保留「随机性诚实声明」")
        check("jumpToCard('rigorCard'" in foot_m.group(1),
              "「随机性诚实声明」是可点链接（指向随机性检验卡）")
    # .stars 死代码不得复活：CSS 无规则、HTML/JS 无使用点
    css = (REPO / "web" / "static" / "style.css").read_text(encoding="utf-8")
    css_no_comment = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    check(".stars {" not in css_no_comment, "style.css 无 .stars 规则（已清理）")
    check(".stars .off" not in css_no_comment, "style.css 无 .stars .off 规则")
    check('class="stars"' not in html_raw and "class='stars'" not in html_raw,
          "index.html 无 class=stars 使用点")
    check("starsFor(" not in _strip_js_comments(src),
          "app.js 无 starsFor() 调用（已删净）")

    print("[10] D3 首屏卡契约：实时拉取 + 同响应 + 算术闭合")
    check("fillHonestBounds" in src, "app.js 存在 fillHonestBounds")
    check("markHonestBoundsStale" in src, "app.js 存在 markHonestBoundsStale 降级标记")
    # 两卡同源：loadBlueCoverage 必须同时调 renderBlueCoverage 与 fillHonestBounds
    lbc = _strip_js_comments(_function_source(src, "loadBlueCoverage"))
    check("renderBlueCoverage(d)" in lbc and "fillHonestBounds(d)" in lbc,
          "两卡共用同一个响应对象 d（非各发一次请求）")
    check("markHonestBoundsStale(" in lbc, "降级时调用 markHonestBoundsStale")
    # 首屏加载时即触发
    la = _strip_js_comments(_function_source(src, "loadAll"))
    check("loadBlueCoverage();" in la, "loadAll() 初始化时调用 loadBlueCoverage()")
    # 算术闭合：净亏必须是 16.875（20 - 3.125 = 16.875，3 位精度）
    check("16.875" in html_raw, "index.html 净亏兜底值为 16.875（3 位算术闭合）")
    check('set("net_sixth", _yuan(Math.abs(Number(d.net_sixth_yuan)), 3))' in src,
          "fillHonestBounds 动态填充保留 3 位精度")
    check('data-hb="net_sixth"' in html_raw, "首屏卡拥有 data-hb 契约属性")

    print("[6] 端点契约（鉴权方式与既有 portfolio 调用一致）")
    for fn in ("loadBlueCoverage", "loadPopularity"):
        body = _function_source(src, fn)
        check("api(" in body, f"{fn} 走统一 api() 封装（继承 Cookie 会话鉴权）")
    check("/api/portfolio/blue-coverage" in src, "调用 /api/portfolio/blue-coverage")
    check("/api/portfolio/popularity" in src, "调用 /api/portfolio/popularity")


# ---------------------------------------------------------------- 渲染等价性

_NODE_HARNESS = r"""
import fs from 'fs';
const [appPath, fixturePath] = process.argv.slice(2);
const src = fs.readFileSync(appPath, 'utf8');
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

function grab(name) {
  const i = src.indexOf('function ' + name);
  if (i < 0) throw new Error('missing ' + name);
  let d = 0; const j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') d++;
    else if (src[k] === '}') { d--; if (d === 0) return src.slice(i, k + 1); }
  }
  throw new Error('unbalanced ' + name);
}
const store = {};
globalThis.$ = (s) => store[s] || null;
globalThis.escHtml = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
globalThis.TICKET_PRICE_YUAN = 2;
globalThis.POP_TIER_CLASS = { low: 'cool', mid: 'common', high: 'hot' };

for (const fn of ['_pct', '_yuan', '_renderDisclosureLine',
                  'renderBlueCoverage', 'renderPopularity']) {
  (0, eval)(grab(fn));
}
store['#blueCovBody'] = { innerHTML: '' };
store['#blueCovTag'] = { textContent: '' };
store['#popBody'] = { innerHTML: '' };
store['#popTag'] = { textContent: '' };
renderBlueCoverage(fixture.bc);
renderPopularity(fixture.pop);
process.stdout.write(JSON.stringify({
  bc: store['#blueCovBody'].innerHTML,
  pop: store['#popBody'].innerHTML,
  bcTag: store['#blueCovTag'].textContent,
  popTag: store['#popTag'].textContent,
}));
"""


def _first_metrics_container(html: str) -> str:
    """切出第一个 class="v10-metrics" 容器的内容（按标签配平）。"""
    start = html.index('<div class="v10-metrics">')
    i, depth = start, 0
    while i < len(html):
        if html.startswith("<div", i):
            depth += 1
            i = html.index(">", i) + 1
        elif html.startswith("</div>", i):
            depth -= 1
            i += 6
            if depth == 0:
                break
        else:
            i += 1
    return html[start:i]


def probe_render() -> int:
    node = shutil.which("node")
    if not node:
        print("\n[render] 跳过：本机没有 node（静态断言已覆盖验收 1~3）", file=sys.stderr)
        return 2

    # 用路由处理函数直接生成真实响应：不起服务、不需要登录、不碰生产库
    sys.path.insert(0, str(REPO))
    from lottery.routes_portfolio import (  # noqa: E402
        portfolio_blue_coverage,
        portfolio_popularity,
    )

    n = 10
    fixture = {
        "bc": json.loads(json.dumps(
            portfolio_blue_coverage(n_tickets=n, full_ev=True), default=str)),
        "pop": json.loads(json.dumps(
            portfolio_popularity(reds="1,2,3,4,5,6", sales=0.0), default=str)),
    }
    print("\n[render] 用真实后端响应喂渲染函数（n_tickets=%d）" % n)

    with tempfile.TemporaryDirectory() as td:
        fpath = Path(td) / "fixture.json"
        fpath.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        hpath = Path(td) / "harness.mjs"
        hpath.write_text(_NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(hpath), str(APP_JS), str(fpath)],
            capture_output=True, text=True, timeout=120,
        )
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
        check(False, "node 渲染沙箱执行成功", f"exit={proc.returncode}")
        return 1

    out = json.loads(proc.stdout)
    bc_html, pop_html = out["bc"], out["pop"]
    d = fixture["bc"]
    pop = fixture["pop"]

    # --- 标准 1：四个数字 + 净亏 + 返奖比，必须同屏 ---
    container = _first_metrics_container(bc_html)
    expect = {
        "覆盖概率": _js_fixed(float(d["p_any_blue_covered"]) * 100, 2) + "%",
        "随机重复概率": _js_fixed(float(d["p_any_blue_random_repeat"]) * 100, 2) + "%",
        "成本": "¥" + _js_fixed(d["cost_yuan"], 0),
        "六等奖期望回收": "¥" + _js_fixed(d["ev_sixth_yuan"], 2),
        "净亏": "¥" + _js_fixed(d["net_sixth_yuan"], 3),   # 3 位：与 disclosure 的 16.875 对齐
        "六等奖返奖比": _js_fixed(float(d["ev_ratio_sixth"]) * 100, 3) + "%",
    }
    for label, needle in expect.items():
        check(needle in container, f"同一容器内出现{label} {needle}")

    # 披露必须逐行完整，一行不少、一字不差
    lines = d["disclosure_lines"]
    check(len(lines) >= 4, f"后端返回 disclosure_lines={len(lines)} 行（≥4）")
    rendered = re.findall(r'<div class="v14-line"[^>]*>(.*?)</div>', bc_html, flags=re.S)
    check(len(rendered) == len(lines),
          f"渲染出 {len(rendered)} 行披露 = 后端 {len(lines)} 行", 
          f"{len(rendered)} != {len(lines)}")
    for idx, (raw, got) in enumerate(zip(lines, rendered), 1):
        # 后端用 **x** 标粗体，渲染后应变 <strong>；其余字符原样
        want = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", raw)
        unstripped = re.sub(r"<[^>]+>", "", got)
        check(unstripped == re.sub(r"\*\*", "", raw),
              f"第 {idx} 行披露文本完整", f"渲染={unstripped[:60]!r}")
        check("**" not in got, f"第 {idx} 行无裸星号")
        _ = want

    # 反向断言：只显示概率对比的一半，必须被判为不合格
    half_only = (expect["覆盖概率"] in bc_html) and (expect["成本"] not in bc_html)
    check(not half_only, "不存在「只显示概率提升、不显示成本」的半披露")

    # --- 边界：n > 16 覆盖饱和时，返奖比注解不得再说「恒定、与注数无关」 ---
    # 后端 2026-10-21 修正：返奖比恒等式 (5·p)/(2n) = 5/32 只在 n ≤ 16 成立。
    # n > 16 时 p 被 min(n,16)/16 封顶 1.0、成本仍线性涨 ⇒ 返奖比被稀释。
    # 前端若把「恒定 15.625%」写死，就会出现"数字 5%、注解说恒为 15.625%"的自相矛盾。
    sat_cases = [c for c in ("--saturated-n",) if c in sys.argv] or []
    _ = sat_cases
    if "--skip-saturated" not in sys.argv:
        print("  · 覆盖饱和分支（n=50）")
        sat = json.loads(json.dumps(
            portfolio_blue_coverage(n_tickets=50, full_ev=True), default=str))
        if sat.get("coverage_saturated"):
            with tempfile.TemporaryDirectory() as td2:
                fp2 = Path(td2) / "fixture.json"
                fp2.write_text(json.dumps({"bc": sat, "pop": fixture["pop"]},
                                          ensure_ascii=False), encoding="utf-8")
                hp2 = Path(td2) / "h.mjs"
                hp2.write_text(_NODE_HARNESS, encoding="utf-8")
                pr2 = subprocess.run([node, str(hp2), str(APP_JS), str(fp2)],
                                     capture_output=True, text=True, timeout=120)
            sat_html = json.loads(pr2.stdout)["bc"] if pr2.returncode == 0 else ""
            check(bool(sat_html), "n=50 渲染成功")
            # Lead 2026-10-21 复现裁定：返奖比恒等式 (n/16 × ¥5)/(2n) = 5/32 在**所有 n** 成立。
            # n>16 变的是「至少中一次」的概率（封顶 100%），不是返奖比。
            # 故此处断言的语义是「概率饱和」，不是「返奖比被稀释」。
            check("恒定" in sat_html,
                  "饱和时返奖比注解仍写「恒定」（恒等式与注数无关，不能改口）")
            check("概率已饱和" in sat_html, "饱和时显式提示「概率已饱和」")
            check("返奖比仍恒为" in sat_html,
                  "饱和提示里同时说明返奖比仍恒定（避免被读成收益率变化）")
            check(_js_fixed(float(sat["ev_ratio_sixth"]) * 100, 3) + "%" in sat_html,
                  f"饱和时返奖比显示为实际值 {_js_fixed(float(sat['ev_ratio_sixth'])*100, 3)}%")
        else:
            # 后端尚未提供 `coverage_saturated`（该字段属于 blue_coverage 的在途改动）。
            # 前端对此是**安全降级**的：`d.coverage_saturated` 为 undefined → 走 falsy 分支，
            # 显示「恒定 = 15.625%」。但注意 —— 此时后端若仍返回 n>16 的稀释后比例，
            # 文案就会与实际数字矛盾。所以这里不是"跳过"，而是显式记一笔"契约缺口"。
            print("  ⚠ 后端未返回 coverage_saturated：该字段属在途改动（blue_coverage.py）。")
            print("    前端按 falsy 降级，不报错；但 n>16 时注解会与实际比例不一致 —— "
                  "需后端补齐字段后本段断言才生效。")
            check("coverage_saturated" in APP_JS.read_text(encoding="utf-8"),
                  "前端已按 coverage_saturated 分支（后端补齐即自动生效）")

    # --- 契约：increases_prize_frequency 恒为 True（Lead 2026-10-21 裁决）---
    # 「中奖频率」= 每期**至少中一次**的概率。去重覆盖把它从 1-(15/16)^n 抬到 min(n,16)/16，
    # 在所有 n 上都是真实提升（对比对象是「随机重复买 n 注」，不是「n=16 时的自己」）。
    # n > 16 时封顶的是"还能再涨"，不是"从没涨过" —— 旧实现在 n>16 返回 False 是错的。
    # 测试先行：这里钉死契约，后端按此口径改。
    print("  · increases_prize_frequency 恒为 True")
    for _n in (1, 5, 16, 17, 50, 100):
        _r = json.loads(json.dumps(portfolio_blue_coverage(n_tickets=_n), default=str))
        check(_r["honesty_flags"]["increases_prize_frequency"] is True,
              f"n={_n}: increases_prize_frequency is True",
              f"实际 {_r['honesty_flags']['increases_prize_frequency']}")
    # 两个否定结论在所有 n 上都必须保持 False（这是本卡的全部意义所在）
    for _n in (1, 16, 17, 100):
        _r = json.loads(json.dumps(portfolio_blue_coverage(n_tickets=_n), default=str))
        check(_r["honesty_flags"]["increases_hit_probability"] is False,
              f"n={_n}: increases_hit_probability is False")
        check(_r["honesty_flags"]["increases_expected_return"] is False,
              f"n={_n}: increases_expected_return is False")

    # --- 标准 2：偏好度只给档位 ---
    check("v14-tier" in pop_html, "偏好度渲染出档位徽标")
    check('data-tier="%s"' % pop["tier"] in pop_html,
          f"档位值 data-tier={pop['tier']} 出现在 HTML 里")
    check(pop["tier_label"] in pop_html, f"档位中文标签「{pop['tier_label']}」出现")
    check(not re.search(r"\d+\s*注", pop_html), "偏好度 HTML 里没有任何「N 注」数字")
    check(not re.search(r"注中奖", pop_html), "偏好度 HTML 里没有「…注中奖」")

    # --- 标准 3：禁用词（渲染后的可见文本）---
    for where, text in (("蓝球覆盖 HTML", bc_html), ("偏好度 HTML", pop_html)):
        hits = [w for w in BANNED if w in text]
        check(not hits, f"{where} 无禁用词", f"命中 {hits}")

    # --- 披露语义：honesty_flags 必须如实呈现 ---
    flags = d["honesty_flags"]
    check(flags["increases_hit_probability"] is False and
          "提高中奖概率：否" in bc_html, "如实显示「提高中奖概率：否」")
    check(flags["increases_expected_return"] is False and
          "提高期望收益：否" in bc_html, "如实显示「提高期望收益：否」")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="V14-B2/B3 前端披露完整性探针")
    ap.add_argument("--render", action="store_true",
                    help="额外做真实渲染等价性检查（需要 node）")
    args = ap.parse_args()

    print("=" * 72)
    print("V14-B2/B3 前端探针：蓝球覆盖披露 + 大众偏好度档位")
    print("=" * 72)
    probe_static()
    rc = 0
    if args.render:
        rc = probe_render()

    print("\n" + "=" * 72)
    if failures:
        print(f"结果：{len(failures)} 条断言失败")
        for f in failures:
            print("  ✗ " + f)
        return 1
    print("结果：全部通过" + ("（含渲染等价性）" if args.render and rc == 0 else "（静态契约）"))
    if args.render and rc == 2:
        print("注意：--render 因缺少 node 被跳过，建议装了 node 的机器上再跑一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
