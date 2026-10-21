"""开放候选空间（P4 生成式挖掘）。

## 它要解决的问题

原先的闲时挖掘遍历 `discovery.FAMILY_BY_KEY` 里**写死的 124 个**「族 × 参数档位」
组合。跑完这 124 个之后，每一轮都是 `processed=0` —— 那不是挖掘，是**重复验证**。
真正的挖掘必须能持续产出「此前没检验过」的候选。

本模块把候选空间从「封闭的 124 元集合」改成**可无限推进的开放空间**，
由三层来源构成，按确定性顺序枚举：

1. **单因子参数网格**（`single`）：把每族的参数域从手写档位**程序化扩展**到更细的
   网格（如 `window` 在 10/20/30/50/100/200 之外再插入插值档）。
   这一层是「同一族换档位会不会有效」，属于对既有族的高分辨率重扫。
2. **双因子交叉**（`cross`）：用 `discovery.combine()` 把任意两个单因子做
   交集/并集。现有 29 族**全部是单因子**，两两交叉是**从未检验过的搜索空间**，
   规模约 M²/2×2（M 为单因子总数），是"新规律"的主战场。
3. **LLM 新假说**：由 `llm_lab` 提出人类没写过的新族，落地时注册进本模块
   （不在本模块职责内，见 `llm_lab.run_closed_loop`）。

## 为什么不需要 `_params_ok` 约束

`llm_lab._params_ok` 要求参数必须落在维度表声明的档位里，理由是
「否则等价于私自新开搜索维度，不能算**已验证**」——那条判据管的是
**LLM 规律能否作为已验证结论**，不是「搜索能不能探索新档位」。

搜索当然要探索新档位，否则永远只有 124 个。管住多重检验的不是档位白名单，
而是 `eprocess` 的 **anytime-valid** 性质：e-process 在任意停止规则下错误率受控，
所以「边挖边看、随时可能停」不会像固定样本量检验那样把 α 撑爆。
这也正是当初选 e-process 而不是 Bonferroni 的理由（见 `eprocess.py` 模块注释）。

## 三个必须保证的工程性质

### 1. 确定性（可续跑的前提）

`candidate_at(layer, idx)` 是**纯函数**：给定 (层, 索引) 必然返回同一个候选。
断点只需记「跑到哪个索引」，不必物化整个空间（交叉层可能有几十万项）。

### 2. 指纹精确（去重的唯一依据）

候选指纹必须能区分**不同参数**的同族组合。这里踩过一个坑：
`discovery.combine()` 返回的 flt 里 `key` 是 `"hot+cold"`、`params` 只有
`{"mode": ...}`，**不含各成分自己的参数** —— 于是 `hot(30,6)∩cold(100,8)` 与
`hot(30,6)∩cold(30,5)` 会算出**同一个** `key:params`，指纹撞车、去重误判。
因此指纹在**生成时**构造（含成分完整参数），不从 flt 反推。

### 3. 规模可控

单因子网格每族截断到 `MAX_PER_FAMILY` 项（确定性取前 N 个合法组合），
避免某族参数域爆炸拖垮整层；非法组合（如 `lo >= hi`）在构造 selector 时跳过。
"""
from __future__ import annotations

import itertools
import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import discovery

#: 每族参数网格最多保留多少个组合（确定性截断，控制单层规模）
MAX_PER_FAMILY = 48
#: 数值型参数的取值上限（防止插值出荒谬的窗口跨度）
MAX_NUMERIC = 2000
#: 退化判断用的探测窗口（最近多少期构造 ScanContext）
PROBE_WINDOW = 500

#: 层名
LAYER_SINGLE = "single"
LAYER_CROSS = "cross"

#: 交叉层的两种合并方式（顺序固定，保证索引确定性）
CROSS_MODES: Tuple[str, ...] = ("intersection", "union")


# --------------------------------------------------------------------------
# 参数域扩展
# --------------------------------------------------------------------------

def _slots(fam: Dict) -> List[str]:
    """该族参数涉及的所有键（按首次出现顺序，保证确定性）。"""
    seen: List[str] = []
    for p in fam.get("params") or []:
        for k in p.keys():
            if k not in seen:
                seen.append(k)
    return seen


def _domain(values: List[Any]) -> List[Any]:
    """把一个键出现过的值扩展成更细的取值域 —— **只在已声明区间内加密**。

    这一条是被真实数据打回来的教训：最初这里用 0.5 / 1.5 / 2.0 倍做外推，
    于是 `window` 从原始最小档 10 被下探到 5。结果 `hot{window:5, k:16}`
    这类**语义上无意义**的档位跑出 e=159 的「可用规律」——
    5 期内几乎每个号都出现过，「近 5 期热号」筛选出的 16 个号接近红球半数，
    它测的不是规律，是筛选集大小带来的假信号。

    所以现在只做**相邻原值的中点插值**：
    `(10,20,30,50,100,200)` → `(10,15,20,25,30,40,50,75,100,150,200)`。
    分辨率提高了，但没有任何档位跑出原始研究声明的范围 ——
    与 `llm_lab._params_ok`「参数必须在声明档位里」的精神一致：加密可以，
    私自越界开新维度不行。
    """
    nums = sorted({int(v) for v in values if isinstance(v, (int, float))
                   and not isinstance(v, bool)})
    if not nums:
        # 纯枚举键（如 mode/feature）：保持原值顺序（去重）
        out: List[Any] = []
        for v in values:
            if v not in out:
                out.append(v)
        return out
    ext = set(nums)
    for a, b in zip(nums, nums[1:]):
        mid = (a + b) // 2
        if a < mid < b:
            ext.add(mid)
    return sorted(ext)


def _family_grid(fam: Dict) -> List[Dict]:
    """把一族展开成参数组合列表（确定性、已过滤非法项）。"""
    keys = _slots(fam)
    if not keys:
        return [{}] if fam.get("params") else []
    # 每个键的取值域：把该键在所有 params 里出现过的值汇总后扩展
    domains: Dict[str, List[Any]] = {}
    for k in keys:
        vals = [p.get(k) for p in (fam.get("params") or []) if k in p]
        domains[k] = _domain(vals) if vals else []

    out: List[Dict] = []
    for combo in itertools.product(*(domains[k] for k in keys)):
        params = dict(zip(keys, combo))
        # lo/hi 成对约束：区间端点必须严格递增，否则是非法维度
        if "lo" in params and "hi" in params and params["lo"] >= params["hi"]:
            continue
        out.append(params)
        if len(out) >= MAX_PER_FAMILY:
            break
    return out


# --------------------------------------------------------------------------
# 单因子层
# --------------------------------------------------------------------------

_SINGLE_CACHE: Optional[List[Tuple[str, Dict]]] = None


def _single_index() -> List[Tuple[str, Dict]]:
    """单因子全序表：``[(family_key, params), ...]``（模块内缓存，确定性）。"""
    global _SINGLE_CACHE
    if _SINGLE_CACHE is None:
        items: List[Tuple[str, Dict]] = []
        for key, fam in discovery.FAMILY_BY_KEY.items():
            for params in _family_grid(fam):
                items.append((key, params))
        _SINGLE_CACHE = items
    return _SINGLE_CACHE


def total_single() -> int:
    """单因子层的候选总数。"""
    return len(_single_index())


def _make_single(idx: int) -> Optional[Tuple[Dict, str]]:
    items = _single_index()
    if not (0 <= idx < len(items)):
        return None
    key, params = items[idx]
    try:
        flt = discovery.make_filter(key, params)
    except Exception:       # noqa: BLE001 —— 非法参数直接跳过，不中断枚举
        return None
    fp = f"single|{key}|{json.dumps(params, sort_keys=True, ensure_ascii=False)}"
    return flt, fp


# --------------------------------------------------------------------------
# 交叉层
# --------------------------------------------------------------------------

def total_cross() -> int:
    """交叉层的候选总数 = 跨族两两组合 × 两种合并方式。

    **只算跨族对**：同族内部交叉（如 ``hot(k=2) ∩ hot(k=3)``）会退化成
    该族的一个更小/更大的取值集合，本质仍是「同一维度换档位」，
    不是新维度 —— 把它算作「新规律」是自欺。
    """
    pairs, _ = _cross_plan()
    return sum(cnt for _, _, cnt in pairs) * len(CROSS_MODES)


_CROSS_CACHE: Optional[Tuple[List[Tuple[List[int], List[int], int]],
                             List[str]]] = None


def _cross_plan() -> Tuple[List[Tuple[List[int], List[int], int]], List[str]]:
    """跨族对计划：``[(A 的索引表, B 的索引表, 组合数), ...]``（确定性）。

    同 target 才配对 —— 红蓝独立开奖，跨 target 组合没有意义。
    """
    global _CROSS_CACHE
    if _CROSS_CACHE is not None:
        return _CROSS_CACHE
    singles = _single_index()
    groups: Dict[str, List[int]] = {}
    order: List[str] = []
    for idx, (key, _params) in enumerate(singles):
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(idx)

    pairs: List[Tuple[List[int], List[int], int]] = []
    for gi in range(len(order)):
        for gj in range(gi + 1, len(order)):
            ka, kb = order[gi], order[gj]
            fa = discovery.FAMILY_BY_KEY[ka]
            fb = discovery.FAMILY_BY_KEY[kb]
            if fa.get("target") != fb.get("target"):
                continue
            ia, ib = groups[ka], groups[kb]
            pairs.append((ia, ib, len(ia) * len(ib)))
    _CROSS_CACHE = (pairs, order)
    return _CROSS_CACHE


def _make_cross(idx: int) -> Optional[Tuple[Dict, str]]:
    """把交叉层序号实例化为 (flt, fingerprint)；退化组合返回 None。

    ## 退化组合必须被剔除（2026-10-21 实测发现）

    交叉层由「族A ∩/∪ 族B」构成，而两个筛选集的交集**可能为空或极小**。
    实测采样 300 个交叉候选：

        k_mean = 0（空集）      5 个 / 2%
        k_mean < 0.5（几乎空） 84 个 / 28%
        0.5 ≤ k_mean ≤ 6 正常 215 个 / 72%

    最典型的是 ``hot ∩ cold``（热点号与冷号取交集）——**必然为空**：
    一个号不可能既是近 W 期出现最多的，又是出现最少的。

    这类候选的伤害不是「算错了」，而是**纯浪费 + 稀释 m**：
      * obs 与 exp 都是 0，z 序列全零，e 值恒 ≈1（实测整层 max 只有 0.77）；
      * 它们照样被计入分层 e-BH 的分母，把真正有机会的候选的阈值抬高。

    因此这里**在生成阶段**剔除：并集覆盖过多（近全号）或交集过小的组合直接返回 None，
    由调用方跳过且不消耗预算。
    """
    pairs, _order = _cross_plan()
    if not pairs or idx < 0:
        return None
    per_mode = sum(cnt for _, _, cnt in pairs)
    if per_mode <= 0:
        return None
    total = per_mode * len(CROSS_MODES)
    if idx >= total:
        return None
    mode = CROSS_MODES[idx // per_mode]
    rank = idx % per_mode

    # 在跨族对序列上定位（族数 ≤ 数十，线性扫描足够且确定性最强）
    for ia, ib, cnt in pairs:
        if rank < cnt:
            ai, bi = divmod(rank, len(ib))
            i, j = ia[ai], ib[bi]
            break
        rank -= cnt
    else:
        return None

    a = _make_single(i)
    b = _make_single(j)
    if not a or not b:
        return None
    fa, fb = a[0], b[0]
    try:
        flt = discovery.combine([fa, fb], mode)
    except Exception:       # noqa: BLE001
        return None
    # **指纹必须含成分完整参数** —— combine() 返回的 flt 不含，
    # 直接用它算指纹会让不同参数组合撞车（见模块注释「指纹精确」）。
    fp = (f"cross|{CROSS_MODES.index(mode)}|"
          f"{fa['key']}:{json.dumps(fa.get('params') or {}, sort_keys=True)}|"
          f"{fb['key']}:{json.dumps(fb.get('params') or {}, sort_keys=True)}")
    return flt, fp


def _cross_is_degenerate(flt: Dict, target: str) -> bool:
    """判断交叉候选是否语义退化（筛选集为空 / 近乎全选）。

    用成分筛选器在当前历史上下文上取一次选号集，看大小。
    单次调用成本很低（筛选器基于预计算计数）。

    判据（红球 33 选 6、蓝球 16 选 1 的背景下）：
      * 选出 0 个号 —— obs/exp 恒为 0，z 序列全零，测不出任何东西
      * 选出 > 12 个红号 —— 覆盖超过 1/3，z 长期负偏，无判别力

    **判不了就放行**（返回 False）：不能因为探测失败就把合法候选误杀。
    但这种情况会打一次日志 —— 静默放行会让「退化过滤失效」藏起来，
    那正是本缺陷最初没被发现的原因。
    """
    try:
        hist = _probe_history()
        chosen = flt["selector"](hist) or set()
        k = len(chosen)
    except Exception as e:      # noqa: BLE001 —— 判不了就放行，不误杀
        global _DEGRADE_PROBE_FAILED
        if not _DEGRADE_PROBE_FAILED:
            _DEGRADE_PROBE_FAILED = True
            print(f"[candidate_space] 退化判断不可用（将放行全部交叉候选）: {e}")
        return False
    if k < 1:
        return True
    if target == "red" and k > 12:
        return True
    if target == "blue" and k > 8:
        return True
    return False


_PROBE_HIST: Optional[Any] = None
#: 退化判断是否已因异常而失效（只告警一次，避免刷屏）
_DEGRADE_PROBE_FAILED = False


def _probe_history() -> Any:
    """给退化判断用的历史上下文（只建一次，避免每候选重建）。

    用库内最近 `PROBE_WINDOW` 期构造 ScanContext —— 筛选器只需要「最近的
    热号/遗漏」这类量，用尾部窗口即可，不必全史。
    """
    global _PROBE_HIST
    if _PROBE_HIST is None:
        from . import db
        draws = db.load_draws()
        tail = draws[-PROBE_WINDOW:] if len(draws) > PROBE_WINDOW else draws
        _PROBE_HIST = discovery.ScanContext.new(tail)
    return _PROBE_HIST


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def candidate_at(layer: str, idx: int) -> Optional[Tuple[Dict, str]]:
    """取第 ``idx`` 个候选，返回 ``(flt, fingerprint)``；越界/非法/退化返回 None。

    纯函数：同样的 (layer, idx) 永远给出同一个候选（可续跑的前提）。

    交叉层会做**退化过滤**（见 `_cross_is_degenerate`）：交集为空、
    或选出号码过多的组合直接返回 None，由调用方跳过且不消耗预算。
    这一步很关键 —— 实测 28% 的交叉候选筛选集 < 0.5 个号，
    它们 e 值恒 ≈1，却照样被计入 e-BH 的分母。
    """
    if layer == LAYER_SINGLE:
        return _make_single(idx)
    if layer == LAYER_CROSS:
        got = _make_cross(idx)
        if got and _cross_is_degenerate(got[0], got[0].get("target") or "red"):
            return None
        return got
    return None


def layer_size(layer: str) -> int:
    if layer == LAYER_SINGLE:
        return total_single()
    if layer == LAYER_CROSS:
        return total_cross()
    return 0


def iter_candidates(layer: str, start: int = 0,
                    limit: Optional[int] = None) -> Iterator[Tuple[int, Dict, str]]:
    """从 ``start`` 起迭代候选，产出 ``(idx, flt, fingerprint)``。"""
    end = layer_size(layer) if limit is None else min(
        layer_size(layer), start + int(limit))
    for idx in range(max(0, start), end):
        got = candidate_at(layer, idx)
        if got:
            yield idx, got[0], got[1]


def stats() -> Dict[str, Any]:
    """空间规模快照（供 health / 日志展示「还有多少没挖」）。"""
    return {
        "single_total": total_single(),
        "cross_total": total_cross(),
        "families": len(discovery.FAMILY_BY_KEY),
        "max_per_family": MAX_PER_FAMILY,
        "cross_modes": list(CROSS_MODES),
    }


#: 供测试直接遍历原始族定义（避免测试去碰 `discovery.FAMILIES` 的私有约定，
#: 也避免将来 discovery 内部结构调整时测试跟着碎）。
FAMILIES_FOR_TEST: List[Dict] = list(discovery.FAMILIES)
