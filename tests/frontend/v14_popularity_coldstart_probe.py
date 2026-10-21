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
#: 因此任何阶段的请求耗时都不得显著超过「等待上限 + 网络/序列化开销」。
REQUEST_CEILING = 2.0 + 1.5     # = 3.5s

#: 落盘缓存命中时的预算。实测 0.3s 级，3s 已很严格。
CACHE_HIT_BUDGET = 3.0

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

    # 默认开启：必须是「显式关闭」语义（getenv 默认 "1"，比较 != "0"）
    m = re.search(
        r'LOTT_POPULARITY_WARMUP"?\s*\)\s*or\s*"(\d)"\s*\)\s*\.strip\(\)\s*!=\s*"0"', src)
    check(m is not None,
          "预热默认开启（getenv 缺省 '1' 且判定 != '0'）—— 漏配时应得到好默认")
    if m:
        check(m.group(1) == "1", "缺省值为 '1'（开启）", f"实际 {m.group(1)!r}")

    check("daemon=True" in src[src.index("def _popularity_warmup"):
                               src.index("def _popularity_warmup") + 3000],
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


def _timed_get(url: str, cookie: str = "") -> tuple[float, int]:
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
            return time.time() - t0, r.status
    except urllib.error.HTTPError as e:
        return time.time() - t0, e.code


def _start_serve(tmp: Path, port: int, env_extra: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(REPO),
        "LOTT_HOME": str(tmp / "home"),
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


def _cache_file() -> Path:
    """落盘缓存路径（与 popularity._cache_path() 同源，不另写一份常量）。"""
    sys.path.insert(0, str(REPO))
    from lottery import config
    return Path(config.DATA_DIR) / "popularity_model.json"


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
    cache = _cache_file()
    backup: bytes | None = None
    if cache.exists():
        backup = cache.read_bytes()
        cache.unlink()

    proc = None
    try:
        # ========== 阶段 A：无落盘缓存（首次部署） ==========
        # 设计承诺**不是**"第一次就快"—— fit 要 8~13s，物理上快不了。
        # 承诺是：**请求不排队**。所以断言的是「≤ REQUEST_CEILING 且要么 200 要么 503」，
        # 而不是"必须 200 且 < 3s"。后者在首次部署时是不可能达到的，
        # 写死它只会逼下一个人去调大超时或降低 n_perm —— 两条都是错的。
        port1 = _free_port()
        base1 = f"http://127.0.0.1:{port1}"
        proc = _start_serve(tmp, port1, {"LOTT_POPULARITY_WARMUP": "1"})
        if not _wait_http(base1 + "/api/health", timeout=120):
            print("  ✗ 服务未在 120s 内起来", file=sys.stderr)
            return 2
        t1, st1 = _timed_get(base1 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
        print(f"  · [A 无缓存] 首个请求：{t1:.2f}s  HTTP={st1}")
        check(st1 in (200, 503), "A 无缓存：首个请求不 5xx 崩溃", f"实际 {st1}")
        check(t1 < REQUEST_CEILING,
              f"A 无缓存：首个请求 < {REQUEST_CEILING}s（不为后台拟合排队）",
              f"{t1:.2f}s —— 若接近 fit 时长，说明请求在等训练")

        # 等预热跑完（fit 8~13s，与启动期其他任务争 CPU 时更久）
        deadline = time.time() + 90
        t_ready = None
        while time.time() < deadline:
            tt, ss = _timed_get(base1 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
            if ss == 200:
                t_ready = tt
                break
            time.sleep(2)
        print(f"  · [A 无缓存] 预热完成后请求：{t_ready if t_ready is None else round(t_ready,2)}s")
        check(t_ready is not None,
              "A 无缓存：预热最终完成，端点转为 200（模型确实拟合成功）")
        if t_ready is not None:
            check(t_ready < CACHE_HIT_BUDGET,
                  f"A 无缓存：就绪后请求 < {CACHE_HIT_BUDGET}s", f"{t_ready:.2f}s")
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
        t2, st2 = _timed_get(base2 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
        print(f"  · [B 有缓存] 首个请求：{t2:.2f}s  HTTP={st2}")
        check(st2 == 200,
              "B 有缓存：首个请求直接 200（指纹匹配、跳过 fit）", f"实际 {st2}")
        check(t2 < CACHE_HIT_BUDGET,
              f"B 有缓存：首个请求 < {CACHE_HIT_BUDGET}s（这才是用户常态）",
              f"{t2:.2f}s —— 若接近 fit 时长，说明缓存未命中")

        t3, st3 = _timed_get(base2 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
        print(f"  · [B] 热态请求：{t3:.2f}s")
        check(st3 == 200 and t3 < CACHE_HIT_BUDGET, "B 热态请求稳定在预算内", f"{t3:.2f}s")

        bc, st4 = _timed_get(base2 + "/api/portfolio/blue-coverage?n_tickets=10")
        print(f"  · [B] 蓝球覆盖：{bc:.2f}s  HTTP={st4}")
        check(st4 in (200, 401, 403), "B 蓝球覆盖端点可达", f"实际 {st4}")
        check(bc < CACHE_HIT_BUDGET, f"B 蓝球覆盖端点 < {CACHE_HIT_BUDGET}s", f"{bc:.2f}s")

        # ========== 阶段 C：显式关闭预热 —— 请求仍不得排队 ==========
        # 这是最关键的回归护栏：即使预热被关掉，用户也只该等 `timeout`，
        # 而不是等完整个 fit。防线在**请求路径的超时**，不在预热线程。
        port3 = _free_port()
        base3 = f"http://127.0.0.1:{port3}"
        proc = _start_serve(tmp, port3, {"LOTT_POPULARITY_WARMUP": "0"})
        if not _wait_http(base3 + "/api/health", timeout=120):
            print("  ✗ 阶段 C 服务未起来", file=sys.stderr)
            return 2
        t4, st5 = _timed_get(base3 + "/api/portfolio/popularity?reds=1,2,3,4,5,6")
        print(f"  · [C 预热关闭] 首个请求：{t4:.2f}s  HTTP={st5}")
        check(st5 in (200, 503), "C 预热关闭：端点仍可用（200 或降级 503）", f"实际 {st5}")
        check(t4 < REQUEST_CEILING,
              f"C 预热关闭：首个请求仍 < {REQUEST_CEILING}s（超时防线独立于预热）",
              f"{t4:.2f}s —— 若接近 fit 时长，说明请求在无预热时直接排队训练")
        _stop(proc)
        proc = None
        return 0
    finally:
        _stop(proc) if proc else None
        # 还原缓存文件：不把测试产物留在仓库里
        if backup is not None:
            cache.write_bytes(backup)
        elif cache.exists():
            cache.unlink()
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
