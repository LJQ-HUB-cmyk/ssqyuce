"""护栏自身的回归测试：证明它真的会拦。"""
import os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(env_extra):
    env = dict(os.environ)
    env.pop("LOTT_TEST_REAL_DB", None)
    # 关键：必须清掉 LOTT_DB / LOTT_HOME。
    # 若在 isolated_test.sh 里跑，LOTT_DB 已指向临时库，子进程天然不会碰生产库，
    # 护栏不触发是**正确**的 —— 那样就测不到「事故场景」。
    # 本用例要复现的正是「变量名设错 → 静默回落生产库」，所以必须让默认值生效。
    env.pop("LOTT_DB", None)
    env.pop("LOTT_HOME", None)
    env.update(env_extra)
    code = "from lottery import db\ntry:\n db.get_conn()\n print('NO_RAISE')\nexcept RuntimeError:\n print('RAISED')\n"
    r = subprocess.run([str(ROOT / ".venv/bin/python"), "-c", code],
                       cwd=str(ROOT), env=env, capture_output=True, text=True)
    return r.stdout.strip()


def test_guard_blocks_prod_db_in_test_context():
    """设了不存在的 LOTT_DATA_DIR（真实事故场景）→ 护栏必须拦住。"""
    out = _run({"PYTEST_VERSION": "x", "LOTT_DATA_DIR": "/tmp/nope"})
    assert "RAISED" in out, f"护栏未拦截生产库: {out}"


def test_guard_allows_real_db_channel():
    """LOTT_TEST_REAL_DB=1 专用通道必须放行（真值锁用例依赖它）。"""
    out = _run({"PYTEST_VERSION": "x", "LOTT_TEST_REAL_DB": "1"})
    assert "NO_RAISE" in out, f"真实库通道被误拦: {out}"


def test_guard_silent_outside_test_context():
    """生产运行（无 pytest 痕迹）不得被拦，否则线上起不来。"""
    env_clean = {"PYTEST_VERSION": "", "PYTEST_CURRENT_TEST": "", "PYTEST_ADDOPTS": "",
                 "LOTT_REQUIRE_ISOLATION": ""}
    out = _run(env_clean)
    assert "NO_RAISE" in out, f"生产运行被误拦: {out}"
