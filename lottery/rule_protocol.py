"""规律统一抽象协议（P3 规律三段分离核心）。

依据 `docs/reviews/M2_STATISTICS.md` §2。
让"扩大规律定义"（用户第 3 点）变成可插拔注册，而不是失控扩大多重检验。

## 三条不可违背的设计原则（M2 §2.1）

1. **P1 规律只能声明「我要被怎么检验」，不能声明「我通过」**：
   显著性阈值由全局误差预算统一裁决，RuleFamily 不得自定阈值。
2. **P2 所有规律的证据汇流到同一个误差预算**：
   禁止并行存在多套独立 α 导致 FWER 膨胀。
3. **P3 每条规律必须可回放**：
   给定任意历史切片，能确定性重算其打分器。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Protocol, Sequence

Draw = Dict[str, Any]
Layer = Literal["L1_long", "L1_phase", "L2_shape", "L3_hypothesis", "L4_lifecycle", "L5_ab"]


@dataclass(frozen=True)
class HypothesisSpec:
    """一条规律假设 = 假设空间里的一个确定参数点。"""
    family_key: str
    params: Dict[str, Any]
    target: Literal["red", "blue"]
    direction: Literal["positive", "negative", "two-sided"] = "two-sided"

    def exp_id(self) -> str:
        """确定性指纹：sha1(family_key + sorted(params) + target + direction)。
        
        绝不含截止期号/当前期号/运行时窗口，保证跨期指纹稳定。
        """
        canonical_params = json.dumps(self.params, sort_keys=True, ensure_ascii=False)
        raw = f"{self.family_key}:{canonical_params}:{self.target}:{self.direction}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class Evidence:
    """一次检验的产物：全部是可观测量，不含最终裁决（P1 原则）。"""
    n_obs: int
    effect: float
    se: float
    z: float
    p_two_sided: float
    mde_abs: float
    e_value: Optional[float] = None
    window: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    """唯一裁决结构，由统一预算判定器产生。"""
    state: Literal["usable", "monitor", "rejected", "underpowered", "invalidated"]
    layer: Layer
    reason_zh: str
    alpha_spent: float
    alpha_remaining: float
    evidence: Evidence


class RuleFamily(Protocol):
    """一族规律的可插拔契约。新增规律族 = 实现此接口 + register()。"""
    KEY: str
    LAYER: Layer
    TARGET: Literal["red", "blue"]

    def enumerate_hypotheses(self) -> Sequence[HypothesisSpec]:
        """假设空间：必须有限、先于数据可知。"""
        ...

    def applicable(self, draws: Sequence[Draw]) -> bool:
        """适用条件。数据不满足返回 False（记 underpowered 而非静默跳过）。"""
        ...

    def score(self, hyp: HypothesisSpec, draws: Sequence[Draw],
              window: Optional[str] = None) -> Evidence:
        """打分器：纯函数，只读 as-of 切片。禁止 RNG / 未来信息。"""
        ...

    def null_model(self, hyp: HypothesisSpec, draws: Sequence[Draw]) -> Dict[str, float]:
        """显著性基线：返回 {'mean', 'sd'}，不返回阈值。"""
        ...

    def invalidation_criteria(self) -> Sequence[str]:
        """失效判据代码列表。"""
        ...

    def apply(self, hyp: HypothesisSpec, draws: Sequence[Draw]) -> Optional[Dict[str, Any]]:
        """使用方式：返回候选池重排偏置 {'numbers': set[int], 'weight': float}。"""
        ...


RULE_FAMILIES: Dict[str, RuleFamily] = {}


def register_family(fam: RuleFamily) -> RuleFamily:
    """注册新的规律族。"""
    assert fam.KEY not in RULE_FAMILIES, f"重复注册规律族: {fam.KEY}"
    RULE_FAMILIES[fam.KEY] = fam
    return fam


def get_family(key: str) -> Optional[RuleFamily]:
    return RULE_FAMILIES.get(key)
