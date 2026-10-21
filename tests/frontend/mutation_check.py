#!/usr/bin/env python3
"""变异验证：证明「断言锚在行为上」，而不只是「能被某一种破坏抓到」。

为什么需要它（严QA 2026-10-21 的对等补充）：
  单次变异只能证明「这一种破坏会被抓」。**换一种等价破坏仍能抓，才说明断言锚在行为上、
  而不是记住了某个具体写法。** 所以每个行为至少准备两种破坏方式。

判据：每个变异必须让**指定的测试变红**；还原后必须回到全绿，且文件 sha256 与原件一致
（还原不彻底 = 污染工作区，比变异本身更危险）。

用法：
  python3 tests/frontend/mutation_check.py
  # 只跑某组：python3 tests/frontend/mutation_check.py -k csrf
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]          # ssqyuce/
APP = REPO / "web/static/app.js"
TEST = "tests/test_w08_frontend_auth.py"
PY = REPO / ".venv/bin/python"

# (说明, 原文, 改成, 期望变红的测试名)
MUTATIONS = [
    # --- CSRF 恢复：两种等价破坏 ---
    ("删掉恢复调用（保留函数定义）",
     "      recoverCsrfAndHint(path);\n",
     "",
     "test_csrf_failure_is_not_treated_as_session_expiry"),
    ("把 CSRF 判定条件改成恒假",
     'if (msg.indexOf("CSRF") >= 0) {',
     "if (false) {",
     "test_csrf_failure_is_not_treated_as_session_expiry"),
    # --- 401 三形态：两种等价破坏 ---
    ("不再读取 WWW-Authenticate 头",
     'const wwa = (r.headers.get("www-authenticate") || "").toLowerCase();',
     'const wwa = "";',
     "test_api_handles_401_by_showing_login"),
    ("Cookie 分支永不命中",
     'if (wwa.indexOf("cookie") >= 0) {',
     "if (false) {",
     "test_api_handles_401_by_showing_login"),
    # --- 423/429 ---
    ("423/429 分支恒假",
     "if (status === 423 || status === 429) {",
     "if (false) {",
     "test_locked_account_is_not_auto_retried"),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_tests() -> tuple[int, str]:
    r = subprocess.run([str(PY), "-m", "pytest", TEST, "-q", "--no-header", "-x"],
                       cwd=REPO, capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": str(REPO)})
    return r.returncode, (r.stdout + r.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="只跑说明里包含该子串的变异")
    args = ap.parse_args()

    original = APP.read_bytes()
    orig_sha = sha(APP)
    fails: list[str] = []
    print(f"{'结果':<6} {'变异':<32} {'期望变红':<52}")
    print("-" * 96)

    try:
        for desc, old, new, expect in MUTATIONS:
            if args.k and args.k not in desc:
                continue
            src = original.decode()
            if old not in src:
                print(f"{'SKIP':<6} {desc:<32} 锚点未命中（代码已改，请更新变异清单）")
                fails.append(f"{desc}: 锚点未命中")
                continue
            APP.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, out = run_tests()
            ok = (rc != 0) and (expect in out)
            print(f"{'PASS' if ok else 'FAIL':<6} {desc:<32} {expect:<52}")
            if not ok:
                fails.append(f"{desc}: 期望 {expect} 变红，实际 rc={rc}")
            APP.write_bytes(original)          # 每条都还原
    finally:
        APP.write_bytes(original)

    # 还原完整性 + 基线全绿
    restored_ok = sha(APP) == orig_sha
    rc, out = run_tests()
    baseline_ok = rc == 0 and "passed" in out
    print("-" * 96)
    print(f"还原完整性: {'OK' if restored_ok else 'MISMATCH'} "
          f"(sha256 {sha(APP)[:12]} vs {orig_sha[:12]})")
    print(f"还原后基线: {'全绿' if baseline_ok else '有失败'}")
    if not restored_ok or not baseline_ok:
        fails.append("还原或基线异常")
    print(f"\n结论: {'全部通过' if not fails else '有问题 → ' + '; '.join(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
