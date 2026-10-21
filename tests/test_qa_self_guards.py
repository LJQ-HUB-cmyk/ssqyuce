"""QA 自检守卫 —— 防「结构性灾难」与「守卫空转」，不测业务。

## 为什么单独开一个文件

`tests/` 下的**业务测试**归各模块负责人；本文件只放**QA 自检类守卫**
（环境哨兵、结构性灾难防护、反空转）。独立成文件是为了：
1. 避免与业务测试的写域并发冲突（本项目已因并发写同一文件差点互相覆盖）；
2. 这类守卫的失败含义不同 —— 它们红了说明**测试基础设施**出了问题，
   而不是某个业务行为错了，应当一眼能分辨。

## 判据

本文件的守卫都满足两个条件：
* **不 import 被测模块**（结构性守卫必须在「收集阶段」就能跑，
  否则一旦发生循环导入，守卫自己也跑不到 —— 见案例 #1）；
* **有超时上限**（不能用一个可能挂起的测试去防挂起）。
"""
from __future__ import annotations

import signal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


class _Timeout:
    """SIGALRM 兜底超时（项目未装 pytest-timeout）。

    为什么必须加：本文件里有一条直测涉及重试/抽样路径，
    若被测逻辑因缺陷退化为长循环，**测试本身会挂起**。
    绕起挂起的测试去测挂起，等于用同一个病治病。
    """

    def __init__(self, seconds: int = 30):
        self.seconds = seconds
        self._old = None

    def __enter__(self):
        if hasattr(signal, "SIGALRM"):          # POSIX
            self._old = signal.signal(signal.SIGALRM, self._raise)
            signal.alarm(self.seconds)
        return self

    def __exit__(self, *exc):
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
            if self._old is not None:
                signal.signal(signal.SIGALRM, self._old)
        return False

    @staticmethod
    def _raise(signum, frame):
        raise TimeoutError("QA 守卫超时（疑似退化为长循环/挂起）")


# ---------- 1. 结构性灾难防护 ----------

def test_engine_is_a_single_module_not_a_package():
    """`lottery/engine.py` 必须是**单文件模块**，不得变成 `lottery/engine/` 包。

    ## 为什么这条必须存在（案例 #1，真实 P0 事故）

    2026-10-21，有人把 `engine.py` 改名为 `engine/__init__.py`（内容原样 3107 行）。
    包内 `from . import backtest as BT` 的相对导入目标随之从
    `lottery.backtest` 变成 `lottery.engine.backtest` → **循环导入**
    → 10 个测试文件在**收集阶段**崩溃 → 一条测试都跑不了。

    ## 为什么用文件系统判断，而不是 `assert engine.BT is lottery.backtest`

    包转换后，`is` 断言确实会红 —— **但那时收集阶段已经崩了，测试根本跑不到这一行**。
    「能捕获收集崩溃的测试，本身必须在收集阶段之前就能跑」，
    所以这里只做 `Path.is_dir()`，不 import 任何模块。
    """
    assert (REPO / "lottery" / "engine.py").is_file(), (
        "lottery/engine.py 不存在 —— engine 模块丢失或被改名")
    assert not (REPO / "lottery" / "engine").is_dir(), (
        "lottery/engine/ 目录不应存在。engine 一旦变成包，"
        "`from . import backtest` 会解析到 lottery.engine.backtest 而非 lottery.backtest，"
        "引发循环导入并导致全套测试在收集阶段崩溃（案例 #1）。"
        "若确需拆分，必须同步改写为 `from .. import backtest` 并全跑验证。")


def test_no_stray_collect_error_probe_files():
    """不得残留 `test_zz_*` 探针文件（变异/故障注入时的临时产物）。

    残留的注入探针会让后续所有验收看到"莫名多了 N 条失败"。
    """
    stray = [p.name for p in (REPO / "tests").glob("test_zz*")]
    assert not stray, f"发现残留探针文件：{stray}"


# ---------- 2. pass_constraints 奇偶分支直测（补间接覆盖） ----------

def test_pass_constraints_parity_branch_directly():
    """**直测** `pass_constraints` 的奇偶分支。

    ## 为什么需要直测

    该分支此前只有「间接覆盖」（通过 `sample_stat_ticket` 的抽样间接触到）。
    变异实验（把 `odd not in range` 反转成 `odd in range`）证明：
    反转后会被 `test_v12_shape_rules.py:264` 抓到 —— 但那是**恰好**，
    不是设计。直测让这一分支的语义**显式钉死**：

        odd ∈ odd_range  → 放行
        odd ∉ odd_range  → 拦截

    ## 为什么带超时

    若某天 `pass_constraints` 或其后代逻辑退化为长循环，
    本用例必须**失败而非挂起**。绕起挂起的测试去测挂起，是同一个病治两次。

    ## 为什么必须「隔离」到只有奇偶起作用

    第一版守卫的样本**选错了**，做变异验证时暴露：删掉整个奇偶检查，
    守卫仍全绿 —— 因为那两个样本先被**三区约束**拦住了，
    奇偶有没有都不影响结果。**这就是"假测试"的典型形态。**

    修法：把 `top_zones` 放宽成**所有可能的 (z1,z2,z3) 组合**（28 种），
    把 sum/span/连号也全部放开 —— 此时唯一还能改变结果的维度就是**奇偶**。
    这样删掉奇偶检查，样本会立刻从 False 变 True，守卫必红。
    """
    from lottery import engine

    # 所有可能的三区组合 (z1,z2,z3)，z1+z2+z3=6
    all_zones = [(a, b, 6 - a - b)
                 for a in range(7) for b in range(7 - a)]
    ctx = {
        "sum_min": 0, "sum_max": 300,
        "top_zones": all_zones,
        "odd_range": [3],
        "span_min": 0, "span_max": 32,
        "max_consecutive": 6,
    }
    with _Timeout(30):
        # ── 样本构成自检：确认三区/和值/跨度都不构成拦截理由 ──
        # 奇数 3 个 → 落在 odd_range=[3] 内 → 必须放行
        odd3 = [1, 2, 7, 12, 15, 20]
        # 奇数 0 个 → 不在 odd_range 内 → 必须拦截
        odd0 = [2, 4, 8, 12, 20, 24]
        # 奇数 6 个 → 不在 odd_range 内 → 必须拦截
        odd6 = [1, 3, 5, 7, 9, 11]
        for cand, want_odd in ((odd3, 3), (odd0, 0), (odd6, 6)):
            actual_odd = sum(1 for r in cand if r % 2 == 1)
            assert actual_odd == want_odd, \
                f"样本构造错误：{cand} 实际奇数 {actual_odd}，期望 {want_odd}"
            z = (sum(1 for r in cand if 1 <= r <= 11),
                 sum(1 for r in cand if 12 <= r <= 22), 0)
            z = (z[0], z[1], 6 - z[0] - z[1])
            assert z in all_zones, f"样本三区 {z} 应被放宽集合包含"

        assert engine.pass_constraints(odd3, 1, ctx) is True, \
            "奇数 3 个在 odd_range=[3] 内应放行 —— 奇偶分支被反转/删除？"
        assert engine.pass_constraints(odd0, 1, ctx) is False, \
            "奇数 0 个不在 odd_range=[3] 内应拦截 —— 奇偶分支被反转/删除？"
        assert engine.pass_constraints(odd6, 1, ctx) is False, \
            "奇数 6 个不在 odd_range=[3] 内应拦截 —— 奇偶分支被反转/删除？"


def test_pass_constraints_parity_is_isolated_from_other_dims():
    """**反空转**：确认奇偶分支确实单独起作用，不被其他维度掩盖。

    第一版守卫栽在这里：删掉奇偶检查后仍全绿，因为样本先被三区拦住。
    本条直接检验「在同一样本上，只改 `odd_range` 就能翻转结果」——
    若奇偶分支失效，翻转不会发生。
    """
    from lottery import engine

    all_zones = [(a, b, 6 - a - b)
                 for a in range(7) for b in range(7 - a)]
    base = {
        "sum_min": 0, "sum_max": 300,
        "top_zones": all_zones,
        "span_min": 0, "span_max": 32,
        "max_consecutive": 6,
    }
    cand = [1, 2, 7, 12, 15, 20]          # 奇数 3 个
    with _Timeout(30):
        allow = engine.pass_constraints(cand, 1, dict(base, odd_range=[3]))
        deny = engine.pass_constraints(cand, 1, dict(base, odd_range=[2, 4]))
    assert allow is True, "odd_range 含 3 时应放行"
    assert deny is False, (
        "同一样本仅把 odd_range 从 [3] 换成 [2,4] 后结果未翻转 —— "
        "奇偶分支未真正起作用（可能被删除，或被其他维度掩盖）")


# ---------- 3. 变异探针残留守卫（严QA，2026-10-21 事故后补） ----------

def test_no_mutation_probe_functions_in_tests():
    """不得残留**变异探针函数**（`test_probe_*` / `test_mutation_*`）。

    ## 为什么上面那条（`test_zz*`）不够 —— 真实事故

    上面的守卫只查**文件名**为 `test_zz*` 的临时**文件**。
    而 2026-10-21 我做的变异是**往已有文件追加一个临时测试函数**（文件名不变）：

        # 加到 tests/test_qa_self_guards.py 末尾
        def test_probe_b():
            blob = (WEB/"index.html").read_text()
            assert "生成预测" in blob

    随后我删掉了它，但**在删除与重跑之间，别人跑全量时看到了它** ——
    而且它不是「无害噪声」，它是**方向反了的守卫**：

        它断言页面**必须有**「生成预测」，
        而本轮工作正是**把「生成预测」改成「生成候选注单」**。

    **即：它会把正确改动判成错误。** 这是第 4 类守卫病（反向误报）。

    ## 教训

    **变异探针只能「加进去、立刻删」，且删完必须 `grep` 确认。**
    更稳妥的做法：**不要往共享文件追加探针**，改用
    - 独立的 `test_zz_*.py` 文件（上面的守卫会盯着），或
    - 直接命令行注入（`python -c`），不经文件。

    本守卫是最后一道网：扫**函数名**，不扫文件名。
    """
    import re as _re

    offenders = []
    pat = _re.compile(r"^def (test_(?:probe|mutation)\w*)\(", _re.M)
    for p in sorted((REPO / "tests").glob("test_*.py")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for name in pat.findall(text):
            offenders.append(f"{p.name}::{name}")

    assert not offenders, (
        "发现残留的变异探针函数（它们可能断言过时文案，把正确改动判成错）：\n  "
        + "\n  ".join(offenders)
        + "\n删掉它们，并用 grep 确认；或改用 test_zz_*.py 独立文件。")
