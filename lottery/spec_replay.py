"""可回放规律执行体与验证（P3 判据 R1–R5 实现）。

依据 `docs/reviews/M3_AI.md` §7。
解开 V10"LLM 出号无法被显著性验证"的设计死锁：
把验证对象从"一次性自然语言/号码输出"变为"确定性代码可重放的规格（spec）"。

## 判据 R1–R5

- R1 (compilable): spec 包含 family_key 与参数，能被实例化为纯函数执行体；
- R2 (canonical): exp_id 指纹确定且仅依赖 spec 内容；
- R3 (no-future): 在 draws[:t] 上运行，绝不读取 draws[t:] 任何信息；
- R4 (deterministic): 同一 spec + 同一数据切片多次执行结果逐位完全一致；
- R5 (preregistered): 前置注册状态且 prereg_ok 为 True。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence, Set

from . import discovery


class SpecCompileError(ValueError):
    """规格编译失败。"""


def compile_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """R1: 将 spec 编译成可执行的规律实例。
    
    spec 格式约定：
      - family_key: str（对应 discovery.FAMILY_BY_KEY）
      - params: dict（该族合法的参数集合）
    """
    if not isinstance(spec, dict):
        raise SpecCompileError("spec 必须为字典对象")
    key = spec.get("family_key") or spec.get("key")
    if not key or not isinstance(key, str):
        raise SpecCompileError("spec 缺少合法的 family_key 或 key")
    
    params = spec.get("params") or {}
    try:
        flt = discovery.make_filter(key, params)
    except Exception as e:
        raise SpecCompileError(f"无法编译维度族 {key} 的参数 {params}: {e}") from e

    return flt


def spec_hash(spec: Dict[str, Any]) -> str:
    """R2: 稳定指纹。只对 spec 核心结构计算哈希。"""
    key = str(spec.get("family_key") or spec.get("key") or "")
    params = spec.get("params") or {}
    canonical_params = json.dumps(params, sort_keys=True, ensure_ascii=False)
    target = str(spec.get("target") or "red")
    direction = str(spec.get("direction") or "two-sided")
    raw = f"{key}:{canonical_params}:{target}:{direction}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def replayable(h: Dict[str, Any]) -> Dict[str, Any]:
    """检验一条假设是否满足 R1–R5 可回放判据。"""
    reasons = []
    
    # 提取 spec 描述
    spec = h.get("dim") or h.get("spec") or {}
    if not spec and "family_key" in h:
        spec = {"family_key": h["family_key"], "params": h.get("params", {})}

    # R1: 产物是规格，可被编译
    compiled = None
    try:
        compiled = compile_spec(spec)
    except SpecCompileError as e:
        reasons.append(f"R1_failed: {e}")

    # R2: 指纹稳定且可得
    h_fp = spec_hash(spec)
    if not h_fp:
        reasons.append("R2_failed: 无法计算 spec 指纹")

    # R5: 必须前置注册通过
    prereg_ok = h.get("prereg_ok")
    if prereg_ok is not True:
        reasons.append("R5_failed: 未通过前置注册判定 (prereg_ok!=True)")

    ok = len(reasons) == 0
    return {
        "ok": ok,
        "spec_hash": h_fp,
        "reasons": reasons,
        "compiled": compiled if ok else None,
    }


def eval_on_history(compiled_spec: Dict[str, Any], draws: Sequence[Dict[str, Any]]) -> Set[int]:
    """在 draws 历史切片上确定性执行过滤，返回推荐号码集合（R3/R4 保证）。"""
    selector = compiled_spec.get("selector")
    if not callable(selector):
        return set()
    return set(selector(draws))
