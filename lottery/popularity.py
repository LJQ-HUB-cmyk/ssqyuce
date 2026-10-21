"""大众偏好度模型（B-2）：预测「这一注号码会有多少人跟着中奖」。

## 它要回答的问题，以及它**不**回答的问题

本系统已反复证明：**号码本身不可预测**。红球频次 χ²=44.84/df=32（p=0.065）、
蓝球 χ²=8.81（p=0.887）、单球边际熵 5.0429 vs 理论上限 5.0444（差 0.0015 bit，噪声内）、
条件熵「给定上期是否出现」只降 0.0076 bit、最强候选在最大统计量零分布下 p=0.2995。
**任何声称"预测下期开什么号"的模型都是假的。**

但这个模型预测的不是号码，而是**人群行为**：`p1`（一等奖中奖注数）。
号码是随机的，而选号的人不是 —— 大量彩民按生日、同尾、连号选号，
所以「哪些号码组合会被很多人选」是**可学习的**。

    ❌ 不回答：下期会开什么号（做不到）
    ✅ 回答：这一注号码"大众程度"有多高，从而在真中奖时预计会被摊薄多少

## 实测效应量（务必如实告知用户）

| 检验 | 值 |
|---|---|
| 形态对 `p1` 的增量 R²（控制销量后，扩展窗口时序 CV） | **+0.0077**（0.77%） |
| 不同折数（3/5/8/10）下 | **恒为 +0.0077**（不随切分漂移） |
| 剔除上 1% 异常值后 | +0.0186 |
| 置换检验（200 次） | **p = 0.0050** |
| 销量本身的 R² | +0.0205（`p1` 主要由销量驱动） |

**0.77% 的解释力意味着：它能给出粗略排序，不能给出可用数值。**
所以对外只输出「档位」（低/中/高），**不输出精确预测注数** —— 后者会让人误以为精确。

## 钱的角度（这是唯一能兑现的价值，也是唯一必须同屏披露的限制）

头奖单注金额 = 当期一等奖总奖金 ÷ 中奖注数。历史实测（3266 期）：

    极端热门  p1≥21 : 平均 5,212,219 元
    常态      4≤p1≤10: 平均 7,636,006 元

但**不能**把这个差额当成"避开热门的收益"：奖池规模才是金额主因
（`rho(pool,amt)=+0.50`），而金额对 p1 的弹性实测只有 **−0.088**
（纯恒等式 `amt=pool/p1` 应为 −1；两者几乎抵消）。
换算成期望增益只有 **0.005 ~ 0.18 元/注**（占 2 元成本的 0.3%~9%）。

**而中奖概率完全不变，长期期望仍为负（单注 EV≈1.0458 元 / 2 元 = 52.3%）。**
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 模型版本（口径变更时必须升版，断点/缓存据此失效）
MODEL_VERSION = "pop-1"

#: 档位划分的**分位点**（在「预测值自己的历史分布」上标定，不是固定注数）。
#:
#: 为什么不能写死注数（2026-10-21 实测踩到）：预测值的中位是 8.8、真实 `p1` 中位是 6
#: —— 模型系统性高估（因为目标是"每亿销量的注数"，而销量分布右偏）。
#: 若阈值写死 `≤4 / ≥11`，落在 low 档的只有 17%、high 档只有 19%，
#: 且**与真实的冷热程度不对应**（`[1,2,3,4,5,6]` 被判成"常见"）。
#: 改用分位后，三档各占约 1/3，且排序与真实冷热一致（这是排序模型，不是回归模型）。
TIER_LOW_Q = 0.33         # 预测值下三分位以下 → 偏冷门
TIER_HIGH_Q = 0.67        # 上三分位以上 → 偏热门

#: 形态特征名（顺序即特征顺序，不可随意调整）
FEATURE_NAMES: Tuple[str, ...] = (
    "birthday_count",     # ≤31 的号码个数（生日号效应，最强的单项）
    "same_tail",          # 同尾号重复数（6 - 不同个位数）
    "consecutive",        # 连号个数
    "sum_reds",           # 红球和值
    "spread",             # 极差（max - min）
    "even_count",         # 偶数个数
    "zone_count",         # 三区覆盖数（1-11/12-22/23-33）
)


def features_of(reds: Sequence[int]) -> Dict[str, float]:
    """从 6 个红球算形态特征（纯函数，可单测）。

    这些特征全部来自**号码的外形**，与开奖概率无关 —— 它们影响的是
    「多少人会选这种号」，不是「这种号会不会开」。
    """
    r = sorted(int(x) for x in reds)
    if len(r) != 6:
        raise ValueError(f"需要 6 个红球，收到 {len(r)} 个")
    return {
        "birthday_count": float(sum(1 for v in r if v <= 31)),
        "same_tail": float(6 - len({v % 10 for v in r})),
        "consecutive": float(sum(1 for i in range(1, 6) if r[i] - r[i - 1] == 1)),
        "sum_reds": float(sum(r)),
        "spread": float(r[-1] - r[0]),
        "even_count": float(sum(1 for v in r if v % 2 == 0)),
        "zone_count": float(len({(v - 1) // 11 for v in r})),
    }


def feature_vector(reds: Sequence[int]) -> List[float]:
    f = features_of(reds)
    return [f[k] for k in FEATURE_NAMES]


def tier_of(predicted_p1: float, lo: float, hi: float) -> str:
    """按**分位阈值**把预测值映射成档位（对外只给档位，不给数字）。

    `lo`/`hi` 由 `PopularityModel.fit()` 在训练集预测值上算出并保存，
    这样档位始终是「相对同期彩民习惯」的三分位，而不是绝对注数。
    """
    if predicted_p1 <= lo:
        return "low"          # 偏冷门：该形态历史上较少人覆盖
    if predicted_p1 >= hi:
        return "high"         # 偏热门：可能被较多彩民覆盖
    return "mid"


TIER_LABELS: Dict[str, str] = {
    "low": "偏冷门",
    "mid": "常见",
    "high": "偏热门",
}

#: 档位对应的诚实说明（前端必须原样展示，不得删改）
TIER_CAVEATS: Dict[str, str] = {
    "low": "该号码形态在历史上被较少彩民选中。**这不提高中奖概率**；"
           "只在中奖时略微减少分奖人数。",
    "mid": "该号码形态的大众程度接近历史中位。",
    "high": "该号码形态历史上被较多彩民选中（如偏小号、同尾集中）。"
            "同样中奖时可能分给更多人。**这不降低中奖概率**。",
}


class PopularityModel:
    """线性最小二乘（岭回归）—— 刻意用最简单、最可解释的模型。

    ## 为什么不用 GBDT / 神经网络

    实测对照（时序切分，样本外 R²）：

        Ridge               : +0.0232
        HistGB              : +0.0099
        HistGB(仅形态)       : −0.0544   ← 过拟合，比均值基线还差

    形态的真实增量只有 **0.77%**，在这个量级上复杂模型只会把噪声学进去。
    更重要的理由：**可解释**。线性系数能直接告诉用户"小号越多越热门"，
    这正是本功能要传达的信息；一个 R²=0.01 的黑箱毫无产品价值。

    ## 为什么先做残差化

    `p1` 主要由销量驱动（R² +0.0205），而销量与号码形态无关。
    若不先扣除销量，模型会把"这期销量高"误学成"这种形态热门"。
    因此训练目标是 ``p1 / (sales / 1e8)`` —— 每亿元销量的中奖注数。
    """

    def __init__(self) -> None:
        self.coef_: Optional[List[float]] = None
        self.intercept_: float = 0.0
        self.n_train: int = 0
        self.r2_increment: float = 0.0
        self.p_value: float = 1.0
        self.fitted: bool = False
        #: 档位分位阈值（在训练集预测值上标定）
        self.tier_lo: float = 0.0
        self.tier_hi: float = 0.0

    # ---------- 训练 ----------

    def fit(self, draws: Sequence[Dict]) -> Dict[str, Any]:
        """用历史开奖拟合。**样本外验证与训练在同一函数里完成并留档**。

        只使用 ``p1`` 与 ``sales`` 都存在的期次（实测 3428 期）。
        """
        rows = [d for d in draws
                if d.get("p1") is not None and d.get("sales")
                and d.get("reds") and len(d["reds"]) == 6]
        if len(rows) < 300:
            return {"ok": False, "reason": "insufficient_data",
                    "note_zh": f"样本不足（{len(rows)} 期，需 ≥300）"}

        # 标准化目标：每亿元销量的中奖注数
        y = [float(d["p1"]) / (float(d["sales"]) / 1e8) for d in rows]
        X = [feature_vector(d["reds"]) for d in rows]

        n = len(rows)
        split = int(n * 0.7)
        self.r2_increment, self.p_value = _out_of_sample_check(X, y, split)

        # 全量重拟合（部署用）
        self.coef_, self.intercept_ = _ols(X, y)
        self.n_train = n
        self.fitted = True
        # 档位分位阈值：在训练集自己的预测值上标定
        preds = sorted(self._raw_predict(x) for x in X)
        self.tier_lo = _quantile(preds, TIER_LOW_Q)
        self.tier_hi = _quantile(preds, TIER_HIGH_Q)
        return {
            "ok": True,
            "n_train": n,
            "model_version": MODEL_VERSION,
            "r2_increment": round(self.r2_increment, 5),
            "p_value": round(self.p_value, 4),
            "coefficients": dict(zip(FEATURE_NAMES, [round(c, 4) for c in self.coef_])),
            "note_zh": (
                f"形态对头奖注数的增量解释力 R²={self.r2_increment:.4f}"
                f"（置换 p={self.p_value:.4f}）。**效应真实但极小**："
                f"可给出粗略排序，不能给出可用数值；不提高中奖概率。"
            ),
        }

    # ---------- 预测 ----------

    def _raw_predict(self, x: Sequence[float]) -> float:
        """标准化空间的预测（每亿元销量的中奖注数）。"""
        if self.coef_ is None:
            return 0.0
        return max(self.intercept_ + sum(c * v for c, v in zip(self.coef_, x)), 0.0)

    def predict_p1(self, reds: Sequence[int], sales: Optional[float] = None) -> Optional[float]:
        """预测该号码形态的"每亿元销量中奖注数"；传 sales 时换算成注数。"""
        if not self.fitted or self.coef_ is None:
            return None
        z = self._raw_predict(feature_vector(reds))
        if sales:
            return z * (float(sales) / 1e8)
        return z

    def describe(self, reds: Sequence[int], sales: Optional[float] = None) -> Dict[str, Any]:
        """对外输出：档位 + 依据 + 强制披露。**不返回精确注数。**

        ## 口径必须一致（2026-10-21 实测踩到）

        `tier_lo` / `tier_hi` 是在**标准化空间**（每亿元销量的注数）上标定的，
        所以档位也必须用同一口径的预测值来比较。

        第一版写成 ``pred = self.predict_p1(reds, sales)``，传了 `sales` 时预测值
        被乘上 `sales/1e8`（实测约 3.3 倍），再拿去和按未乘销量标定的阈值比 ——
        结果 3056/3428 条历史期次全被判成 "high"，档位完全失效。
        现在档位一律用未乘销量的标准化预测值，`sales` 只影响（已停用的）注数换算。
        """
        if not self.fitted or self.coef_ is None:
            return {"ok": False, "reason": "model_not_fitted"}

        z = self._raw_predict(feature_vector(reds))       # 标准化空间，与阈值同口径
        tier = tier_of(z, self.tier_lo, self.tier_hi)
        f = features_of(reds)

        reasons: List[str] = []
        if self.coef_:
            order = sorted(zip(FEATURE_NAMES, self.coef_),
                           key=lambda kv: -abs(kv[1] * f[kv[0]]))
            for name, coef in order[:2]:
                contribution = coef * f[name]
                if abs(contribution) < 1e-9:
                    continue
                word = "偏热门" if contribution > 0 else "偏冷门"
                reasons.append(f"{_FEATURE_ZH[name]}（{f[name]:g}）使其{word}")

        return {
            "ok": True,
            "model_version": MODEL_VERSION,
            "tier": tier,
            "tier_label": TIER_LABELS[tier],
            "reasons": reasons,
            "caveat": TIER_CAVEATS[tier],
            "honesty": (
                "本项只反映「多少人可能选同样的号」；"
                "中头奖概率恒为 1/17,721,088，不受选号影响。"
                "期望收益仍为负（约 52.3% 回收）。"
            ),
            "r2_increment": round(self.r2_increment, 5),
        }


_FEATURE_ZH = {
    "birthday_count": "生日号个数",
    "same_tail": "同尾号",
    "consecutive": "连号",
    "sum_reds": "和值",
    "spread": "极差",
    "even_count": "偶数个数",
    "zone_count": "三区覆盖",
}


# ---------- 数值内核（不依赖 numpy，便于单测与低依赖运行） ----------

def _ols(X: Sequence[Sequence[float]], y: Sequence[float],
         ridge: float = 1e-6) -> Tuple[List[float], float]:
    """最小二乘（带极小岭项保证可解）。返回 ``(coef, intercept)``。

    用正规方程 + 高斯消元，特征数只有 7，规模极小，无需 numpy。
    """
    n = len(X)
    p = len(X[0]) if n else 0
    if not n or not p:
        return [], 0.0

    # 中心化，避免截距与特征共线
    mx = [sum(row[j] for row in X) / n for j in range(p)]
    my = sum(y) / n
    Xc = [[row[j] - mx[j] for j in range(p)] for row in X]
    yc = [v - my for v in y]

    # A = Xc^T Xc + ridge*I, b = Xc^T yc
    A = [[sum(Xc[i][a] * Xc[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    for j in range(p):
        A[j][j] += ridge * n
    b = [sum(Xc[i][j] * yc[i] for i in range(n)) for j in range(p)]

    coef = _solve(A, b)
    intercept = my - sum(c * m for c, m in zip(coef, mx))
    return coef, intercept


def _solve(A: List[List[float]], b: List[float]) -> List[float]:
    """高斯消元（列主元）。p 很小，纯 Python 足够。"""
    p = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            continue
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        M[col] = [v / pv for v in M[col]]
        for r in range(p):
            if r == col:
                continue
            factor = M[r][col]
            if factor:
                M[r] = [a - factor * bc for a, bc in zip(M[r], M[col])]
    return [M[i][p] for i in range(p)]


def _r2(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    n = len(y_true)
    if not n:
        return 0.0
    my = sum(y_true) / n
    ss_tot = sum((v - my) ** 2 for v in y_true)
    ss_res = sum((a - b) ** 2 for a, b in zip(y_true, y_pred))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0


def _ts_predict(X: Sequence[Sequence[float]], y: Sequence[float],
                folds: int = 5) -> List[float]:
    """扩展窗口（expanding window）预测：第 k 折用前 k 段训练、预测第 k+1 段。

    **不用随机 KFold**：时间序列上随机切分会让"用未来预测过去"，
    这是本项目历史上反复踩过的坑（如 500 期滑窗造成的 e 值高估）。
    """
    n = len(y)
    size = n // (folds + 1)
    pred = [float("nan")] * n
    for k in range(1, folds + 1):
        tr_end = size * k
        te_end = size * (k + 1)
        coef, icpt = _ols(X[:tr_end], y[:tr_end])
        for i in range(tr_end, te_end):
            pred[i] = icpt + sum(c * v for c, v in zip(coef, X[i]))
    return pred


def _out_of_sample_check(X: Sequence[Sequence[float]], y: Sequence[float],
                         split: int, n_perm: int = 200) -> Tuple[float, float]:
    """返回 ``(形态增量 R², 置换检验 p 值)``。

    增量定义为「含形态的模型 R² − 仅截距（均值）基线 R²」，
    在**扩展窗口时序预测**上计算 —— 与训练集不同分布的部分才算数。

    p 值用置换：打乱 y 后重做同一流程 200 次，看真实增量在零分布中的位置。
    **置换会破坏形态与 y 的联系**，因此零分布正确刻画了"没有信号"时的期望。
    """
    pr_full = _ts_predict(X, y)
    ok = [i for i, v in enumerate(pr_full) if not math.isnan(v)]
    if not ok:
        return 0.0, 1.0
    yt = [y[i] for i in ok]
    pf = [pr_full[i] for i in ok]
    # 基线：训练段均值（用同一批预测点的前序均值近似）
    base = [sum(y[:i]) / i if i else yt[0] for i in ok]
    inc = _r2(yt, pf) - _r2(yt, base)

    # 置换（次数少：纯 Python，200 次 × 1000 点 × 7 特征已约 3~5 秒）
    rng_state = 12345
    null: List[float] = []
    for _ in range(n_perm):
        yp = _shuffle(y, rng_state)
        rng_state += 7919
        pr2 = _ts_predict(X, yp)
        pf2 = [pr2[i] for i in ok]
        null.append(_r2(yt, pf2) - _r2(yt, base))
    ge = sum(1 for v in null if v >= inc)
    return inc, (ge + 1) / (len(null) + 1)


def _shuffle(seq: Sequence[float], seed: int) -> List[float]:
    """确定性洗牌（用 LCG，避免依赖 random 全局状态影响其它模块）。"""
    out = list(seq)
    state = seed
    for i in range(len(out) - 1, 0, -1):
        state = (1103515245 * state + 12345) % (2 ** 31)
        j = state % (i + 1)
        out[i], out[j] = out[j], out[i]
    return out


# ---------- 进程级单例（避免每次请求都重训） ----------

_CACHE: Optional[PopularityModel] = None


def get_model(force_refit: bool = False) -> PopularityModel:
    """取全局模型；未训练/强制刷新时用 `db.load_draws()` 重新拟合。"""
    global _CACHE
    if _CACHE is not None and not force_refit:
        return _CACHE
    from . import db
    m = PopularityModel()
    try:
        m.fit(db.load_draws())
    except Exception:        # noqa: BLE001 —— 模型失败绝不影响主链路
        pass
    _CACHE = m
    return m


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    """线性插值分位数（输入必须已升序）。"""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_vals[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)
