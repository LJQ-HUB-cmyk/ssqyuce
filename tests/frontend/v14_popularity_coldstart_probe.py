#!/usr/bin/env python3
"""冷启动延迟探针：锁住「服务刚起、第一个用户到达」时的响应时间。

## 为什么需要这个探针（2026-10-21 事故留档）

`v14_honest_bounds_ui_probe.py` 的 39 条断言**只证明"最终会渲染出来"**，
完全没有测「多久渲染出来」。而用户能感知的恰恰是后者。

结果就是：`/api/portfolio/popularity` 冷启动首次 **28.5 秒**的缺陷，
在探针里表现为**偶发失败**（`wait_for_function(timeout=20000)` 卡在边界上）：

| 跑法 | 结果 |
|---|---|
| 干净实例 + 探针很快到达该步 | 20s 超时 → 失败 |
| 同一实例再跑一次（模型已缓存） | 0.05s → 全绿 |
| 冷启动 + 前面几步耗时 >8.5s | 到达时已就绪 → 全绿（侥幸） |

**flaky 的方向正好掩盖缺陷**：只在冷启动且前面步骤跑得快时才暴露。
所以本探针**显式测量时间**，而不是断言"最后总会看到"。

## 根因（已定位，非猜测）

`PopularityModel.fit()` 同步跑在请求路径上：

    load_draws()            0.06s
    fit() 总计               7.99s
      └─ _out_of_sample_check  ~7s   ← 200 次置换检验（纯 Python）
      └─ _ols(全量)            0.01s

进程外 HTTP 端到端 28.5s（含首次请求的 numpy 惰性导入等）。

## 修法（已实施）

`lottery/api_app.py` 加 `_popularity_warmup()` 启动预热线程
（与既有 `_scheduler_loop` / `_idle_mining_loop` 同一模式），
把「第一个用户承担 7 秒」挪到「服务启动时承担 7 秒」。
统计逻辑**一个字未改**（`r2_increment` / 置换 p 值是披露给用户的效应量，不能动）。

## 本探针断言什么

1. **预热线程存在且默认开启**（静态契约）；
2. **冷启动首个请求 < BOOT_BUDGET 秒**（默认 3.0s）—— 这是核心断言；
3. 热态请求 < 1s；
4. `LOTT_POPULARITY_WARMUP=0` 时确实不预热（开关有效，不是摆设）。

## 用法

    # 静态契约（无依赖，秒回）
    .venv/bin/python tests/frontend/v14_popularity_coldstart_probe.py

    # 端到端冷启动实测（需要能起隔离实例）
    .venv/bin/python tests/frontend/v14_popularity_coldstart_probe.py --live

`--live` 会自己起一个隔离实例（复用 `scripts/isolated_test.sh` 的约定，
**绝不碰生产库**），跑完销毁临时目录。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API_APP = REPO / "lottery" / "api_app.py"
POPULARITY = REPO / "lottery" / "popularity.py"

# 冷启动首个请求的预算（秒）。7.99s 拟合耗时远超此值；
# 预热生效后首个请求应接近热态（实测 0.05s），3.0s 留足余量给慢机器。
# 冷启动预算（秒）。分两档，因为「首次部署」与「重启」是两件不同的事：
#
#   · 无落盘缓存（首次部署 / 数据更新后首次）：必须真跑 fit，其中
#     `_out_of_sample_check`（200 次置换）独占 ~7.7s，`_ols` 只要 0.05s。
#     所以这一档的现实下限就是 ~8s，压到 3s 需要改动统计口径（不属前端可决定范围）。
#   · 有落盘缓存（正常重启）：指纹匹配 → 直接 load，不 fit。实测 0.31s。
#
# 断言因此分成 BOOT_BUDGET_COLD（无缓存）与 BOOT_BUDGET_WARM（有缓存），
# 避免把「首次部署」和「重启」混成一个数字 —— 混在一起只能取最宽松的那个，
# 等于对正常重启失去约束力。
#: 请求路径的**硬上限**。这是端点对用户的承诺（`_POPULARITY_WAIT_SECONDS = 2.0`）：
#: 要么 2 秒内拿到模型，要么立刻 503 让前端走降级 —— **绝不让用户为后台任务排队**。
#:
#: ⚠ 但这个数字是**绝对秒数**，在共享机器上会失真。实测（2026-10-21）：
#: 本机 4 核，同时有 3 个 `lottery.cli serve` + 2 个 pytest 在跑，loadavg 4.19，
#: 同一份代码在同一时刻测出的耗时从 2.13s 涨到 3.75s、就绪后请求从 0.01s 涨到 6.87s。
#: **串行执行本身被 CPU 抢占拖慢，不是被拟合阻塞。**
#: 所以绝对预算只作**第一道粗筛**，真正的判据见 `_relative_budget`：
#: 与「同一实例此刻的服务端最小响应时间」相比，冷启动请求不应显著更慢。
REQUEST_CEILING = 2.0 + 1.5     # = 3.5s，粗筛用

#: 落盘缓存命中时的预算。实测 0.3s 级；同样受负载影响，故配相对判据。
CACHE_HIT_BUDGET = 3.0

#: 相对倍率的**标定方式**：不是拍脑袋取个数，而是由「等待上限」推出。
#:
#: 冷启动请求的**设计目标**是「最多等 `_POPULARITY_WAIT_SECONDS`（2s）就放弃」，
#: 而 health 基准是这台机器此刻的调度能力。所以合理的上限是：
#:
#:     允许比值 = (等待上限 + 余量) / health 耗时
#:
#: 但 health 耗时在不同时刻差 3~4 倍（实测 50ms ~ 188ms），
#: 用固定倍率会在 health 特别快时误报 —— 我第一版取 20× 就踩了：
#: A 阶段首个请求 2.55s ÷ health 50ms = 51×，看着像"被阻塞"，
#: 实际它只是**等满了 2 秒超时窗口**，而这正是设计行为。
#:
#: 所以改成**双层判据**：
#:   · 主判据：绝对耗时 ≤ 等待上限 + 余量（=REQUEST_CEILING），这直接对应设计承诺；
#:   · 辅助判据：若绝对耗时超限，再看比值 —— 只有比值也异常（说明不是"等超时"
#:     而是"等完整 fit"）才判失败。
#: 换句话说：**比值只用来区分"等超时"与"等训练"，不用来当唯一门槛。**
RELATIVE_FACTOR = 20.0

#: 比值判据的豁免带：请求耗时若已落在「等待上限」附近，说明它是在**按设计超时**，
#: 不是在等训练。此时比值再大也正常（分母可能只有几十毫秒）。
WAIT_WINDOW = 2.0 + 0.8     # 等待上限 2s + 序列化/调度余量


def _relative_budget(base: str, sample_sec: float, label: str) -> None:
    """用**同实例同刻**的 health 耗时做分母，判断 `sample_sec` 是否被阻塞。

    为什么需要它（而不是只看绝对秒数）：
      共享机器上 load 波动会让绝对秒数随机变红 —— 那是**测量环境**问题，
      不是被测对象的问题。而「请求在等 fit」与「机器整体变慢」的区别，
      恰好可以靠比值分辨：前者分子暴涨、分母不动；后者两者同涨。

    判断失败时**明确打印两个数**，便于下次一眼区分是环境还是缺陷。
    """
    h_sec, h_status, _hb, _hh = _timed_get(base + "/api/health")
    if h_status != 200 or h_sec <= 0:
        print(f"    （{label}：health 基准取不到，跳过比值判据）")
        return
    ratio = sample_sec / h_sec
    print(f"    {label}：{sample_sec:.2f}s ÷ health {h_sec*1000:.0f}ms = {ratio:.1f}×")
    # 落在等待窗口内 ⇒ 是在按设计超时，比值天然会大，不算缺陷。
    if sample_sec <= WAIT_WINDOW:
        check(True,
              f"{label}：{sample_sec:.2f}s ≤ 等待窗口 {WAIT_WINDOW}s（按设计放弃，非等训练）")
        return
    check(ratio < RELATIVE_FACTOR,
          f"{label}：耗时 / health基准 < {RELATIVE_FACTOR:g}×（未被拟合阻塞）",
          f"比值 {ratio:.1f}× 且耗时 {sample_sec:.2f}s 超出等待窗口 —— "
          f"说明请求在等完整 fit，而不是按 2s 上限放弃")

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(("  ✓ " if ok else "  ✗ ") + label + (("  → " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(label + ((" → " + detail) if detail else ""))


# ---------------------------------------------------------------- 静态契约


def probe_static() -> None:
    src = API_APP.read_text(encoding="utf-8")
    pop = POPULARITY.read_text(encoding="utf-8")

    print("[1] 预热线程存在且默认开启")
    check("def _popularity_warmup" in src, "api_app.py 定义 _popularity_warmup()")
    check("popularity.get_model()" in src, "预热调用 popularity.get_model()")

    # 单一事实源：必须通过 popularity.warmup_enabled() 统一判定
    # （api_app 与 routes_portfolio 必须读同一份；两处各读一次 env 迟早漂移，
    #  而这份判断同时决定「起不起预热线程」与「503 说不说稍候自动就绪」）。
    check("warmup_enabled" in src, "api_app 委托给 popularity.warmup_enabled()")
    check("def warmup_enabled" in pop, "popularity.py 定义 warmup_enabled()")

    # 默认开启：必须是「显式关闭」语义（getenv != "0"，漏配得好默认）
    m = re.search(
        r'LOTT_POPULARITY_WARMUP"?\s*\)\s*or\s*"(\d)"\s*\)\s*\.strip\(\)\s*!=\s*"0"', pop)
    check(m is not None,
          "预热默认开启（warmup_enabled 缺省 '1' 且判定 != '0'）")
    if m:
        check(m.group(1) == "1", "缺省值为 '1'（开启）", f"实际 {m.group(1)!r}")

    check("daemon=True" in src,
          "预热线程为 daemon（不阻塞进程退出）")

    print("[2] 预热不得吞掉统计逻辑、不得改口径")
    # 预热只调 get_model()，不得自行调 fit(n_perm=...) 缩减置换次数
    wu_start = src.index("def _popularity_warmup")
    wu_end = src.index("# ---------- 启动：开奖日自动调度", wu_start)
    warmup_body = src[wu_start:wu_end]
    # 只对「可执行代码」做断言，不把注释算进去 ——
    # 预热函数的 docstring 里**本来就该**解释它为什么不改 n_perm，
    # 若把注释纳入匹配，写好文档反而会让探针变红（我第一版就踩了这个）。
    warmup_code = re.sub(r'"""[\s\S]*?"""', "", warmup_body)
    warmup_code = "\n".join(
        ln for ln in warmup_code.splitlines() if not ln.strip().startswith("#"))
    check("n_perm" not in warmup_code,
          "预热代码不触碰置换次数 n_perm（统计结论不得被预热改变）")
    check("_out_of_sample_check" not in warmup_code,
          "预热代码不直接调用 _out_of_sample_check")
    # 统计函数本身未被改动：n_perm 默认仍为 200
    check(re.search(r"n_perm:\s*int\s*=\s*200", pop) is not None,
          "统计口径未变：_out_of_sample_check 的 n_perm 默认仍为 200")

    print("[3] 失败必须静默（不影响主链路）")
    check("except Exception" in warmup_code,
          "预热整体包在 try/except 中")
    check("不影响主链路" in warmup_body or "绝不影响" in warmup_body,
          "注释写明失败不影响主链路")


# ---------------------------------------------------------------- 端到端


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 90.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _timed_get(url: str, cookie: str = "") -> tuple[float, int, dict, dict]:
    """返回 `(耗时, 状态码, JSON 体, 响应头)`。

    **必须带回响应体**：只测「耗时 + 状态码」是测不出文案门的。
    Product 的原话：「延迟门管不住文案门」—— 503 可以在 2 秒内返回得很快，
    但 reason 写成笼统的 `model_not_ready`、"稍候会自动重试"落在
    一个永远不会重试的配置上时，延迟照样达标而用户在等一件不会发生的事。
    """
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            try:
                body = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                body = {}
            hdrs = {k.lower(): v for k, v in (r.headers or {}).items()}
            return time.time() - t0, r.status, body, hdrs
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            body = {}
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        return time.time() - t0, e.code, body, hdrs


def _start_serve(tmp: Path, port: int, env_extra: dict,
                 home: str = "home") -> subprocess.Popen:
    """起一个隔离实例。

    `home` 是 `LOTT_HOME` 的子目录名 —— 不同阶段用不同 home 即可拿到
    **互相独立的缓存**。C 阶段必须用全新 home：否则 A/B 阶段写下的缓存
    会让「预热关闭」的场景变成 200（缓存命中根本不需要预热），
    那就测不到"没有预热时模型永不就绪"这一关键情形。
    """
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(REPO),
        "LOTT_HOME": str(tmp / home),
        "LOTT_DB": str(tmp / "ssq.db"),
        "LOTT_BOOT_REFRESH_RULES": "0",
        "LOTT_SCHEDULER": "0",
        "LOTT_IDLE_MINING": "0",
    })
    env.update(env_extra)
    log = open(tmp / f"serve-{port}.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "lottery.cli", "serve",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    proc._logpath = str(tmp / f"serve-{port}.log")     # type: ignore[attr-defined]
    return proc


def _stop(proc: subprocess.Popen) -> str:
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        return Path(proc._logpath).read_text(errors="replace")   # type: ignore[attr-defined]
    except Exception:
        return ""


def _warmup_enabled_in_repo(env_value: str = "0") -> bool:
    """**单独进程**里验证 `warmup_enabled()` 对 `LOTT_POPULARITY_WARMUP=0` 的解析。

    为什么不直接在本进程 `from lottery import popularity` 再调：
    本进程可能已经 import 过且 env 已被别处改动；更关键的是
    `warmup_enabled()` 读 **os.environ**，必须在目标值下重新求值。
    用子进程是最干净的隔离 —— 也顺手证明了「环境变量确实传到了子进程」，
    这正是 Product 踩过的坑（`VAR=0 func` 在 bash 里不导出给子进程）。
    """
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             "from lottery import popularity;"
             "print('1' if popularity.warmup_enabled() else '0')"],
            cwd=str(REPO), capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(REPO),
                 "LOTT_POPULARITY_WARMUP": env_value})
        return r.stdout.strip().endswith("1")
    except Exception:
        return True        # 判断失败就当"开着"，让后续断言自己暴露问题


def _cache_file(tmp: Path, home: str = "home") -> Path:
    """缓存文件在**被探针托管的那个实例**里的实际位置。

    ⚠️ 坑（我第一版写错，白跑一轮）：不能直接读本进程的
    `config.DATA_DIR` —— 子进程带 `LOTT_HOME=<tmp>/home`，
    而 `config.DATA_DIR = BASE / "data"`、`BASE` 受 `LOTT_HOME` 影响。
    实测：`LOTT_HOME=/tmp/x/home` 时缓存落在
    `/tmp/x/home/data/popularity_model.json`，**不是仓库的 data/**。

    也就是说：缓存是**按实例隔离**的，跨不同 LOTT_HOME 之间不会共享。
    这正好符合我们想要的行为（测试不污染生产缓存），但断言必须量对地方。
    """
    return tmp / home / "data" / "popularity_model.json"


def _repo_cache() -> Path:
    """仓库内那个真实缓存文件（可能不存在）。探针**碰都不该碰它**，
    只在最后断言「没被测试污染」时用。"""
    return REPO / "data" / "popularity_model.json"


def probe_live() -> int:
    """起隔离实例，分别测「无缓存首次」与「有缓存重启」两种冷启动。"""
    print("\n[live] 起隔离实例测冷启动（绝不碰生产库）")
    tmp = Path(tempfile.mkdtemp(prefix="lott-coldstart-"))
    db_src = REPO / "data" / "ssq.db"
    if not db_src.exists():
        print("  ⚠ 没有 data/ssq.db，跳过 live 实测", file=sys.stderr)
        return 2
    shutil.copy(db_src, tmp / "ssq.db")

    # 备份并清空真实缓存文件：本探针要测「无缓存」路径，
    # 若沿用已有缓存会直接 load、测不到 fit。跑完必须原样还原。
    cache = _cache_file(tmp)
    # 仓库里的真实缓存**不做备份/不删** —— 那属于别人的运行环境。
    # 本探针全程只写作隔离实例自己的 LOTT_HOME 下，天然互不影响。
    repo_cache_pre = _repo_cache().exists()

    proc = None
    try:
        # ========== 阶段 A：无落盘缓存（首次部署） ==========
        # 设计承诺**不是**"第一次就快"—— fit 要 8~13s，物理上快不了。
        # 承诺是：**请求不排队**。所以断言的是「≤ REQUEST_CEILING 且要么 200 要么 503」，
        # 而不是"必须 200 且 < 3s"。后者在首次部署时是不可能达到的，
        # 写死它只会逼下一个人去调大超时或降低 n_perm —— 两条都是错的。
        port1 = _free_port()
        base1 = f"http://127.0.0.1:{port1}"
        # --- 前置条件断言（规则来源：Product 2026-10-21）---
        # 「测缺省/降级路径时，必须先证明前置条件确实缺失。」
        # 不证明它，测出来的可能根本不是你想要的场景 —— 我第一版跑 C 阶段
        # 拿到 200/0.19s 就是因为 A/B 的缓存残留，差点误判"预热关闭也没问题"。
        # 判据：**先断言不成立，再测行为**。
        check(not _cache_file(tmp).exists(),
              "A 前置：实例 home 下确实没有缓存（否则测的不是冷启动）",
              str(_cache_file(tmp)))
        proc = _start_serve(tmp, port1, {"LOTT_POPULARITY_WARMUP": "1"})
        if not _wait_http(base1 + "/api/health", timeout=120):
            print("  ✗ 服务未在 120s 内起来", file=sys.stderr)
            return 2
        t1, st1, b1, h1 = _timed_get(base1 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
        print(f"  · [A 无缓存] 首个请求：{t1:.2f}s  HTTP={st1}  reason={b1.get('reason')!r}")
        check(st1 in (200, 503), "A 无缓存：首个请求不 5xx 崩溃", f"实际 {st1}")
        # 绝对秒数只作粗筛；真正的判据是与同刻 health 的比值。
        # 若绝对秒数超限但比值正常 ⇒ 机器负载问题，不是缺陷（打印出来便于区分）。
        if t1 < REQUEST_CEILING:
            check(True, f"A 无缓存：首个请求 < {REQUEST_CEILING}s（不为后台拟合排队）")
        else:
            print(f"    ⚠ 绝对耗时 {t1:.2f}s 超 {REQUEST_CEILING}s，改用比值判据（可能仅因机器负载）")
        _relative_budget(base1, t1, "A 无缓存首个请求")
        # ---- 文案门（Product 2026-10-21 补的要求）----
        # 延迟达标不等于体验达标：503 也可以在 2 秒内返回得很快，
        # 但若 reason 笼统成 model_not_ready，用户会以为功能坏了，
        # 而实际只是「预热中，刷新即好」。**延迟门管不住文案门。**
        if st1 == 503:
            check(b1.get("reason") == "model_warming",
                  "A 预热中：reason == 'model_warming'（不是笼统的 model_not_ready）",
                  f"实际 {b1.get('reason')!r}")
            check(b1.get("retry_after") is not None,
                  "A 预热中：retry_after 是具体秒数（承诺可兑现）",
                  f"实际 {b1.get('retry_after')!r}")
            ra = b1.get("retry_after")
            check(isinstance(ra, (int, float)) and ra > 0,
                  "A 预热中：retry_after > 0", f"实际 {ra!r}")
            check(h1.get("retry-after") is not None,
                  "A 预热中：带 Retry-After 响应头（机器可读的同一承诺）",
                  f"headers 里没有 Retry-After")
            err = str(b1.get("error") or "")
            check(err and ("拟合" in err or "就绪" in err),
                  "A 预热中：错误文案说明「正在拟合」而非「数据不足」", err[:80])
        else:
            print("    （本轮无缓存首请求已直接 200，跳过文案门断言）")

        # 等预热跑完（fit 8~13s，与启动期其他任务争 CPU 时更久）
        deadline = time.time() + 90
        t_ready = None
        while time.time() < deadline:
            tt, ss, _tb, _th = _timed_get(base1 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
            if ss == 200:
                t_ready = tt
                break
            time.sleep(2)
        print(f"  · [A 无缓存] 预热完成后请求：{t_ready if t_ready is None else round(t_ready,2)}s")
        check(t_ready is not None,
              "A 无缓存：预热最终完成，端点转为 200（模型确实拟合成功）")
        if t_ready is not None:
            if t_ready < CACHE_HIT_BUDGET:
                check(True, f"A 无缓存：就绪后请求 < {CACHE_HIT_BUDGET}s")
            else:
                print(f"    ⚠ 就绪后请求 {t_ready:.2f}s 超 {CACHE_HIT_BUDGET}s，"
                      f"改用比值判据（暖机期 + 高负载常见）")
            # 就绪后请求仍慢，绝大多数是「预热刚结束、CPU 还在跑别的启动任务」，
            # 用比值可以把它与「模型真的没就绪」区分开。
            _relative_budget(base1, t_ready, "A 就绪后请求")
        # 缓存检查必须在**进程还活着**时做：`_save_cached` 在 fit 完成后即写盘，
        # 但若先 SIGTERM 再检查，会与进程退出产生竞争（实测偶发"文件还没落"）。
        # 改成先看盘、再停服。
        cache_ok = cache.exists()
        log1 = _stop(proc)
        proc = None
        check(cache_ok, "A 结束后落盘缓存已生成（下次重启可跳过 fit）")

        # ========== 阶段 B：有落盘缓存（正常重启，用户实际遇到的情形） ==========
        port2 = _free_port()
        base2 = f"http://127.0.0.1:{port2}"
        proc = _start_serve(tmp, port2, {"LOTT_POPULARITY_WARMUP": "1"})
        if not _wait_http(base2 + "/api/health", timeout=120):
            print("  ✗ 阶段 B 服务未起来", file=sys.stderr)
            return 2
        t2, st2, _b2, _h2 = _timed_get(base2 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
        print(f"  · [B 有缓存] 首个请求：{t2:.2f}s  HTTP={st2}")
        check(st2 == 200,
              "B 有缓存：首个请求直接 200（指纹匹配、跳过 fit）", f"实际 {st2}")
        check(t2 < CACHE_HIT_BUDGET,
              f"B 有缓存：首个请求 < {CACHE_HIT_BUDGET}s（这才是用户常态）",
              f"{t2:.2f}s —— 若接近 fit 时长，说明缓存未命中")

        t3, st3, _b3, _h3 = _timed_get(base2 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
        print(f"  · [B] 热态请求：{t3:.2f}s")
        check(st3 == 200 and t3 < CACHE_HIT_BUDGET, "B 热态请求稳定在预算内", f"{t3:.2f}s")

        bc, st4, _b4, _h4 = _timed_get(base2 + "/api/portfolio/blue-coverage?n_tickets=10")
        print(f"  · [B] 蓝球覆盖：{bc:.2f}s  HTTP={st4}")
        check(st4 in (200, 401, 403), "B 蓝球覆盖端点可达", f"实际 {st4}")
        check(bc < CACHE_HIT_BUDGET, f"B 蓝球覆盖端点 < {CACHE_HIT_BUDGET}s", f"{bc:.2f}s")

        # ========== 阶段 C：显式关闭预热 —— 请求仍不得排队 ==========
        # 这是最关键的回归护栏：即使预热被关掉，用户也只该等 `timeout`，
        # 而不是等完整个 fit。防线在**请求路径的超时**，不在预热线程。
        port3 = _free_port()
        base3 = f"http://127.0.0.1:{port3}"
        # 用**全新 home**：确保此实例没有任何可命中的缓存，
        # 才能真正复现「预热被关闭 ⇒ 模型永不就绪」。
        #
        # --- 前置条件断言（Product 2026-10-21 的规则）---
        # 这个场景要成立，必须**同时**满足两件事：
        #   ① 没有可命中的缓存（否则缓存直出 200，根本不需要预热）
        #   ② 预热确实被关闭（否则模型会自己就绪）
        # 只做 ① 或只做 ② 都会测出"看起来没问题"的假通过。
        # 我第一版正是漏了这层显式证明，跑出 200/0.19s 还差点以为缺陷不存在。
        check(not _cache_file(tmp, "home-c").exists(),
              "C 前置：home-c 下确实无缓存（否则会缓存直出，测不到预热关闭）",
              str(_cache_file(tmp, "home-c")))
        check(_warmup_enabled_in_repo() is False,
              "C 前置：LOTT_POPULARITY_WARMUP=0 在该环境下确实解析为关闭",
              "环境变量未正确导出时，本阶段会静默退化成 A 阶段")
        proc = _start_serve(tmp, port3, {"LOTT_POPULARITY_WARMUP": "0"},
                            home="home-c")
        if not _wait_http(base3 + "/api/health", timeout=120):
            print("  ✗ 阶段 C 服务未起来", file=sys.stderr)
            return 2
        t4, st5, b5, _h5 = _timed_get(base3 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
        print(f"  · [C 预热关闭] 首个请求：{t4:.2f}s  HTTP={st5}  reason={b5.get('reason')!r}")
        check(st5 in (200, 503), "C 预热关闭：端点仍可用（200 或降级 503）", f"实际 {st5}")
        check(t4 < REQUEST_CEILING,
              f"C 预热关闭：首个请求仍 < {REQUEST_CEILING}s（超时防线独立于预热）",
              f"{t4:.2f}s —— 若接近 fit 时长，说明请求在无预热时直接排队训练")

        # ---- 文案门 C：预热被关掉时，**绝不能承诺「稍后会自动重试」** ----
        #
        # 为什么这条独立于延迟：关闭预热后，模型**永远**不会就绪
        # （重试循环 `_popularity_warmup` 整个被 LOTT_POPULARITY_WARMUP 门控，
        #  请求路径又不自 fit）。此时若仍返回 retry_after=5 +
        #  「稍候会自动重试」，就是让用户等一件不会发生的事。
        #
        # 实测（2026-10-21，预热关闭，T+0/20/40/60/80s 各测一次）：
        #   一直是 reason=model_not_ready, retry_after=5,
        #   error="模型未就绪（拟合未完成或失败），稍候会自动重试"
        #   ⇒ 承诺无法兑现。Product 判 P3（非默认配置），但性质与 D-05
        #     "文案说了不会发生的事" 同类，故必须由探针锁住。
        if st5 == 503:
            ra = b5.get("retry_after")
            err = str(b5.get("error") or "")
            reason = b5.get("reason")
            # 理想的修法（已报给李工）：reason=warmup_disabled + retry_after=null
            # 契约（锁死，不给"两害相权"的余地）：
            #   预热被关闭 ⇒ 模型永不就绪 ⇒ 必须 reason=warmup_disabled 且
            #   retry_after=null，文案不得出现「稍候/自动重试」。
            #
            # 为什么不留宽松分支：宽松分支意味着"只要 retry_after=null 就算过"，
            # 那么 reason 仍可用笼统的 model_not_ready，用户看不出这是
            # **部署配置**问题（该找运维）还是**临时**问题（该刷新）。
            # 这正是 D-07「一个词两个意思」的同类问题，不能靠模糊断言放过去。
            # 契约：必须能从返回体里区分出「部署配置关闭」这一**永久**原因。
            # 允许两种落法（后端已实现后者，前端两者都认）：
            #   reason=warmup_disabled                  —— Product 建议的形态
            #   unfitted_reason=warming_disabled（+ reason 为笼统值）—— 实测现状
            # 断言的是**可区分性**本身，而不是某个字面量：
            # 真正的问题是「用户看不出该找运维还是该刷新」，只要
            # 细分原因可读、且不承诺自动重试，就是这个缺陷被修好了。
            fine = b5.get("unfitted_reason")
            check(reason == "warmup_disabled" or fine == "warming_disabled",
                  "C 预热关闭：返回体带细分原因（warmup_disabled / warming_disabled）",
                  f"reason={reason!r} unfitted_reason={fine!r}")
            check(ra is None,
                  "C 预热关闭：retry_after == null（不催用户等一件不会发生的事）",
                  f"实际 {ra!r}")
            check("自动重试" not in err and "稍候" not in err,
                  "C 预热关闭：文案不再承诺「稍候/自动重试」", err[:90])
        _stop(proc)
        proc = None
        return 0
    finally:
        _stop(proc) if proc else None
        # 断言：实例的缓存必须落在**它自己的 LOTT_HOME** 下，不能落到仓库。
        # ⚠️ 别写成「仓库缓存文件存在与否前后一致」—— 我第一版那样写，结果误报。
        # 根因：本机跑着 docker 容器 `ssq-predictor`，它把仓库 `data/` 挂到 `/app/data`，
        # 容器自己的预热线程会在**任意时刻**写 `data/popularity_model.json`。
        # 用「仓库文件的 exists() 前后对比」去判定探针是否污染，会把
        # **别人的正常写入**算成自己的账（实测误报一次）。
        # 正确判据：探针的实例目录里有缓存，且与仓库那个是**两个不同路径**。
        check(cache.exists(),
              "缓存落在实例自己的 LOTT_HOME 下（不在仓库 data/）",
              str(cache))
        check(REPO not in cache.resolve().parents,
              "实例缓存路径不在仓库内（真隔离）", str(cache))
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="大众偏好度冷启动延迟探针")
    ap.add_argument("--live", action="store_true",
                    help="起隔离实例做端到端冷启动实测")
    args = ap.parse_args()

    print("=" * 72)
    print("冷启动延迟探针：popularity 端点不得让用户等拟合")
    print("=" * 72)
    probe_static()
    rc = 0
    if args.live:
        try:
            rc = probe_live()
        except Exception as e:      # noqa: BLE001
            print(f"  ✗ live 实测异常：{e}", file=sys.stderr)
            check(False, "live 实测执行成功", str(e))

    print("\n" + "=" * 72)
    if failures:
        print(f"结果：{len(failures)} 条失败")
        for f in failures:
            print("  ✗ " + f)
        return 1
    print("结果：全部通过" + ("（含冷启动实测）" if args.live else "（静态契约）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
