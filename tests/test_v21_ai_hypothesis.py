"""V21 / L1：AI 假设生成器——LLM 提可证伪假设，系统自动证伪。

## 背景（docs/analysis/V19_AI_REDESIGN.md §1.1）

旧 `llm_lab` 要求 LLM 输出 `filters:[{key,params}]`，输出空间 = 134 档排列组合，
是**搜索**不是推理（枚举一遍只要 12.8~17s，比问 LLM 快两个数量级）。
且 `llm_claims.json` 的 `claims`/`runs` 长期为 0 —— 从未产出过东西。

V21 改为 `question + test_spec + why + falsifier`，并用三关自动证伪。

## 本文件守什么（核心是「不许让 LLM 编出无法验证的东西」）

1. `test_spec.key` 必须是已实现的维度族（防幻觉维度）；
2. `params` 必须**完全匹配**已声明档位（防自造参数——自造参数没有实现，无从验证）；
3. 没有 `falsifier` 的假设必须被拒（不可证伪 = 不是科学假设）；
4. 三关必须真的跑：存在性 / 阶段 / 前向回测；
5. 失败项必须**保留**并带原因（它们记录了「问过什么、答案是没有」）；
6. 前向回测的 `wf_caveat_zh` 必须说明「回测对象是方法通道，不是维度本身」。

## 恒真断言守卫

见 `tests/test_d15_no_vacuous_assertions.py`。每条断言绑定一个可变异的实现点。
"""
from __future__ import annotations

import pytest

from lottery import ai_hypothesis as AH
from lottery import discovery


# --------------------------------------------------------------------------
# 合成数据（证伪三关用，不依赖真实库）
# --------------------------------------------------------------------------

def _draws(n: int = 700, seed: int = 11):
    import random
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        order = list(reds)
        rng.shuffle(order)
        out.append({"issue": f"{2003000 + i}", "date": None,
                    "reds": reds, "order": order, "blue": rng.randint(1, 16)})
    return out


def _valid(key: str, params: dict, q: str = "问题"):
    return {"question": q, "test_spec": {"key": key, "params": params},
            "why": "直觉", "falsifier": "前向回测 z <= 1 则放弃"}


# ==========================================================================
# 1. 校验器：三条防线
# ==========================================================================

def test_validate_accepts_declared_family_and_params():
    """合法族 + 合法档位 → ok，且返回归一化后的 spec。

    变异点：去掉 `matched` 的档位匹配、直接放行任意 params —— 变红（下一条也会红）。
    """
    fam = discovery.FAMILY_BY_KEY["order_pos"]
    params = dict(fam["params"][0])
    v = AH.validate_hypothesis(_valid("order_pos", params))
    assert v["ok"] is True, f"应通过校验，实际 errors={v['errors']}"
    assert v["normalized"]["test_spec"]["key"] == "order_pos"
    assert v["normalized"]["test_spec"]["params"] == params


def test_validate_rejects_hallucinated_family():
    """不存在的族（LLM 幻觉出来的维度）必须被拒。

    变异点：把 `if not fam:` 分支删掉/改成放行 —— 变红。
    为什么关键：幻觉维度没有任何实现，跑不出东西，"证伪"就成了空话。
    """
    v = AH.validate_hypothesis(_valid("i_made_this_up", {}))
    assert v["ok"] is False
    assert any("不是已实现的维度族" in e for e in v["errors"]), v["errors"]


def test_validate_rejects_invented_params():
    """自造参数（不在该族已声明档位里）必须被拒。

    变异点：把档位匹配改成「key 存在就放行」—— 变红。
    这是防"LLM 编个看起来像统计的参数"的核心。
    """
    v = AH.validate_hypothesis(_valid("order_pos", {"window": 999, "k": 7}))
    assert v["ok"] is False
    assert any("不匹配" in e and "档位" in e for e in v["errors"]), v["errors"]


def test_validate_rejects_partial_params():
    """参数档必须**完全一致**：少一个键也不算匹配（不许"部分匹配"。）

    变异点：把 `set(cand) == set(params)` 改成 `cand.keys() <= params.keys()`
    —— 变红（会放行缺参数的规格，实现时用的是默认值，等于偷偷换了假设）。
    """
    fam = discovery.FAMILY_BY_KEY["order_pos"]
    full = dict(fam["params"][0])
    partial = {k: v for k, v in list(full.items())[:1]}
    if len(full) > 1:
        v = AH.validate_hypothesis(_valid("order_pos", partial))
        assert v["ok"] is False, f"残缺参数不应通过：{partial}"
    else:
        pytest.skip("该族参数档只有一个键，无法构造残缺场景")


def test_validate_rejects_missing_falsifier():
    """没有 falsifier 的假设必须被拒 —— 不可证伪的不是科学假设。

    变异点：删掉 `if not fal:` 检查 —— 变红。
    """
    h = _valid("hot", {"window": 30, "k": 6})
    h["falsifier"] = ""
    v = AH.validate_hypothesis(h)
    assert v["ok"] is False
    assert any("falsifier" in e for e in v["errors"]), v["errors"]


def test_validate_rejects_empty_question():
    """question 为空必须被拒（否则不知道要检验什么）。"""
    h = _valid("hot", {"window": 30, "k": 6})
    h["question"] = "   "
    v = AH.validate_hypothesis(h)
    assert v["ok"] is False
    assert any("question" in e for e in v["errors"])


# ==========================================================================
# 2. 提示词：必须要求"没有机理就直说是巧合"
# ==========================================================================

def test_prompt_asks_for_hypotheses_not_filters():
    """提示词必须要求 question/test_spec/falsifier，**不再**是 filters 挑选。

    变异点：把提示词改回旧的 `filters:[{key,params}]` 清单 —— 变红。
    这条守的是"重设计真的换了职责"，而不是换了个函数名继续做搜索。
    """
    p = AH.build_hypothesis_prompt()
    for k in ("question", "test_spec", "falsifier"):
        assert k in p, f"提示词缺少 {k}"
    assert "不是要检验" in p or "你不是在选号" in p, "必须明确说明不是选号"
    # 关键诚实要求：允许 LLM 说"这是巧合"
    assert "巧合" in p, "必须允许/要求 LLM 在无机理时直说巧合"


def test_prompt_lists_only_implemented_families():
    """提示词里列的族必须来自 FAMILIES（不许列不存在的）。

    变异点：把族清单换成手写列表 —— 变红（一旦 FAMILIES 增删就对不上）。
    """
    p = AH.build_hypothesis_prompt()
    keys = [f["key"] for f in discovery.FAMILIES]
    missing = [k for k in keys[:5] if k not in p]
    assert not missing, f"提示词缺少已实现的族：{missing}"


# ==========================================================================
# 3. 证伪三关
# ==========================================================================

def test_falsify_runs_all_three_gates():
    """三关都必须被**真的执行**（不是只填了键名）。

    变异点：删掉任意一关的赋值 —— 变红；
    把前向回测起点写死（2917）—— 也会变红（短样本上 n=0、z=None，
    这条断言会因 `wf["n"] <= 0` 失败）。第一版正是栽在这里：
    测试只断言了"键存在"，而前向回测其实一步没跑（n=0），假绿。
    """
    d = _draws(700)
    fam = discovery.FAMILY_BY_KEY["order_pos"]
    r = AH.falsify_hypothesis(d, {"question": "q", "test_spec": {
        "key": "order_pos", "params": dict(fam["params"][0])}})
    for g in ("exists", "regime", "walk_forward"):
        assert g in r["gates"], f"缺少第 {g} 关"
    assert r["gates"]["exists"] is True, "合成数据上 order_pos 应可执行"
    assert r["gates"]["regime"] is not None, "阶段关必须有结果"

    # ---- 证明第三关**真的执行了**（不是占位）----
    wf = r["gates"]["walk_forward"]
    assert wf.get("ok") is True, f"前向回测未真正执行：{wf.get('reason_zh')}"
    assert (wf.get("n") or 0) > 0, (
        f"前向回测评估样本为 0 —— 说明起点设置让它在短样本上退化了（{wf}）")
    assert wf.get("z") is not None, (
        "前向回测必须给出 z 值；为 None 说明样本不足，等于第三关没生效")
    assert "z_by_seed" in wf and len(wf["z_by_seed"]) >= 2, (
        "必须跑多种子稳健性（V20 的要求），否则单次 z 会被噪声愚弄")


def test_falsify_marks_wf_caveat():
    """必须明说「前向回测对象是方法通道，不是该维度本身」。

    变异点：删掉 `wf_caveat_zh` —— 变红。
    为什么关键：维度是"检验规格"，不产出号码，无法直接做前向回测；
    不写清楚就会被误读成"这个维度通过了前向回测"。
    """
    d = _draws(700)
    fam = discovery.FAMILY_BY_KEY["sum_dist"]
    r = AH.falsify_hypothesis(d, {"question": "q", "test_spec": {
        "key": "sum_dist", "params": dict(fam["params"][0])}})
    if r["ok"]:
        assert "wf_caveat_zh" in r, "必须给出前向回测的口径说明"
        assert "不是维度" in r["wf_caveat_zh"] or "不是该维度" in r["wf_caveat_zh"]
        assert r.get("wf_target"), "必须标出回测作用在哪个方法通道"


def test_falsify_rejects_unknown_family_without_crash():
    """未知族必须优雅返回 not_found/unverifiable，不许抛异常。

    变异点：把 `if not key or key not in FAMILY_BY_KEY` 去掉 —— 变红（KeyError）。
    """
    r = AH.falsify_hypothesis(_draws(200), {"question": "q",
                                            "test_spec": {"key": "nope", "params": {}}})
    assert r["ok"] is False
    assert r["gates"]["exists"] is False
    assert r["verdict"] == "unverifiable"


# ==========================================================================
# 4. 批量：失败项必须保留
# ==========================================================================

def test_run_hypotheses_keeps_failed_items():
    """失败的假设必须**保留**并带原因，不能只返回成功的。

    变异点：`items` 只 append 通过项 —— 变红。
    为什么关键：失败项记录了「我们问过什么、答案是没有」，这本身就是产出。
    """
    d = _draws(700)
    fam = discovery.FAMILY_BY_KEY["sum_dist"]
    hs = [
        _valid("sum_dist", dict(fam["params"][0]), q="合法1"),
        {"question": "幻觉", "test_spec": {"key": "ghost", "params": {}},
         "why": "w", "falsifier": "f"},
        {"question": "无证伪", "test_spec": {"key": "sum_dist",
                                            "params": dict(fam["params"][0])},
         "why": "w", "falsifier": ""},
    ]
    r = AH.run_hypotheses(d, hs)
    assert r["n_hypotheses"] == 3, "三条都要保留（含非法项）"
    verdicts = [it["verdict"] for it in r["items"]]
    assert any(v == "invalid_spec" for v in verdicts), (
        f"非法假设应标 invalid_spec 并保留，实际 {verdicts}")
    for it in r["items"]:
        assert it.get("reason_zh"), "每一项都必须带原因"


def test_run_hypotheses_never_produces_numbers():
    """返回值里不得出现任何"号码"字段 —— 假设生成器不产出票。

    变异点：在返回里塞 tickets/reds 之类 —— 变红。
    """
    d = _draws(700)
    fam = discovery.FAMILY_BY_KEY["sum_dist"]
    r = AH.run_hypotheses(d, [_valid("sum_dist", dict(fam["params"][0]))])
    blob = str(r)
    for bad in ("tickets", "ticket_list"):
        assert bad not in blob, f"假设器不应产出 {bad}"
    assert "不产出号码" in r["note_zh"], "必须声明不产出号码"
