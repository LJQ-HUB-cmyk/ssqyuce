"""LLM 域路由（P2 模块化样板）。

## 为什么先切这个域

`api_app.py` 里按域分块共 10 个域。选 `llm` 打头是因为它边界最干净：
3 个端点、依赖只有 `config` / `db` / `llm_client` / `engine`，不碰中间件、
不碰租户状态、不碰静态文件。**先做一个样板并跑通全量测试，再批量套用**，
比一次性搬 10 个域安全得多。

## 切分后 api_app 还保留什么

`api_app.py` 之后只负责**组装**：建 app、装中间件、include 各域 router、
挂静态文件。端点实现全部下沉到 `routes_*.py`。

## 依赖纪律（有测试钉住）

域模块**不得 import api_app**（否则又回到倒置依赖）。需要的应用级能力
（如 `_bg_run`、`current_user_id`）从 `request_context` 或专门的
服务模块取；确实需要 `_bg_run` 这类短期耦合的，用**函数内局部 import**
并在注释里写明原因，而不是顶层 import。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, Query, Request

from . import config, db
from . import features as F

router = APIRouter()


@router.get("/api/config/llm")
def get_llm_config():
    """全局默认通道（管理员专用）。实现见 `llm_config_store.read()`。"""
    from . import llm_config_store
    return llm_config_store.read()


@router.post("/api/config/llm")
def update_llm_config(payload: dict):
    """保存全局默认通道（管理员专用）。实现见 `llm_config_store.save()`。

    保留这个名字的原因：`user_llm_api` 与既有测试都按它调用，
    **名字是契约，实现位置不是**。
    """
    from . import llm_config_store
    return llm_config_store.save(payload)


@router.post("/api/llm/test")
def test_llm_connection(request: Request = None):
    """用**当前用户生效的**配置做一次贴合真实预测的结构化出文探测。

    ## V24 修复的缺陷（2026-10-23 用户报告「测的还是默认的，不是手动填写的」）

    旧实现只用 `config.llm_model_list()`（**全局/管理员默认通道**），
    从头到尾没有读用户自备配置。于是：

    * 用户在设置页填了自己的 base_url / Key / model 并保存 → 保存成功；
    * 点「测试」→ 实际测的是**系统默认通道**；
    * 结果「测试通过」只证明默认通道能通，**与用户填的那份配置毫无关系**；
    * 若用户的自备通道其实是坏的（地址错、Key 无效、模型名拼错），
      测试仍会显示成功 —— 这是最坏的一种误导：它让人以为配好了。

    生产实证（修复前）：
        用户 1 生效来源 = "user"，自备 model = `deepseek-flash`
        而测试端点使用的 model = `agent-router/deepseek-v4-flash`（全局默认）

    ## 修法

    改走 `user_llm_store.effective_for_user(uid, with_secret=True)` ——
    这是全项目**唯一**的"该用户实际生效参数"判据（出号链路也用它）。
    测试与出号从此同源，不会再出现"测的是 A、用的是 B"。

    ## 向后兼容

    无用户上下文（如脚本直接调用 / 未登录）时退回全局配置，
    并在返回值里标明 `source`，让人看得出测的是哪一条通道。
    """
    from . import llm_client
    from .llm_client import LLMChannelError
    if config.LLM_DISABLED:
        return {"ok": False, "error": "LLM 已停用（在设置中启用后重试）"}

    # ---- 解析**当前用户实际生效**的配置（与出号链路同源）----
    _uid = None
    _eff = None
    try:
        from . import request_context
        _uid = request_context.current_user_id()
    except Exception:      # noqa: BLE001
        _uid = None
    if _uid is None:
        try:
            from . import auth_store
            _uid = auth_store.first_admin_id() or None
        except Exception:  # noqa: BLE001
            _uid = None
    try:
        from . import user_llm_store
        _eff = user_llm_store.effective_for_user(_uid, with_secret=True)
    except Exception as e:     # noqa: BLE001
        _eff = {"source": "none", "llm_available": False,
                "reason": f"读取用户配置失败（{type(e).__name__}: {e}）"}

    if _eff and _eff.get("llm_available") and _eff.get("base_url") \
            and _eff.get("api_key") and _eff.get("model"):
        cfg = {
            "base_url": _eff.get("base_url"),
            "api_key": _eff.get("api_key"),
            "model": _eff.get("model"),
            "samples": _eff.get("samples"),
            "extra_body": _eff.get("extra_body") or {},
            "source": _eff.get("source"),
        }
    else:
        # 没有用户级通道 → 退回全局配置（保留旧行为，但**如实标注**）
        cfgs = config.llm_model_list()
        if not cfgs:
            return {"ok": False,
                    "error": ("LLM 未配置：当前账号没有可用的 LLM 通道，系统也没有共享通道。"
                              "请先在设置页填写 API 地址 / Key / 模型并保存，然后再测试。"),
                    "source": "none",
                    "user_reason": (_eff or {}).get("reason")}
        cfg = cfgs[0]
        cfg["source"] = "system"
    # 尽量用真实观察轮提示（数据不足时退化为一个小型 JSON 自检）
    user_prompt = '请只输出 JSON：{"ok": true, "note": "结构化输出自检"}'
    realistic = False
    try:
        from . import engine as E
        draws = db.load_draws()
        if len(draws) >= 60:
            stats = F.compute_features(draws)
            ctx = E.build_context(draws, stats, db.load_patterns())
            user_prompt = llm_client.observations_prompt(
                llm_client.compact_stats(ctx["stats"]), ctx["recent"], ctx["patterns"])
            realistic = True
    except Exception as e:  # noqa: BLE001
        print(f"[llm] 测试探测构建真实提示失败，退化为自检提示: {e}")
    t0 = time.time()
    try:
        res = llm_client.chat_json(
            llm_client.SYSTEM_BASE, user_prompt, max_tokens=1600, temperature=0.0,
            timeout=config.LLM_TIMEOUT, model_cfg=cfg, strict=True,
            deadline=time.time() + max(60.0, config.LLM_TOTAL_TIMEOUT))
    except LLMChannelError as e:
        return {"ok": False, "model": cfg.get("model"), "realistic": realistic,
                "source": cfg.get("source"),
                "time_ms": int((time.time() - t0) * 1000), "error": str(e),
                "hint": ("若提示「只返回推理内容」，说明该模型把输出预算全花在 reasoning 上："
                         f"可调大 LOTT_LLM_MAX_TOKENS（当前 {config.LLM_MAX_TOKENS}），"
                         "或改用不这样做的模型（例如 discovery-api-intern-ai-org/deepseek-v4-flash-0731）")}
    dt_ms = int((time.time() - t0) * 1000)
    learned = llm_client.learned_max_tokens(cfg["base_url"], cfg["model"])
    if not res:
        return {"ok": False, "model": cfg.get("model"), "realistic": realistic,
                "source": cfg.get("source"), "time_ms": dt_ms,
                "error": "模型有返回但无法解析为 JSON（请检查模型是否遵循 JSON 输出要求）"}
    keys = list(res.keys())[:6] if isinstance(res, dict) else []
    return {"ok": True, "model": cfg.get("model"), "realistic": realistic,
            "source": cfg.get("source"),
            "base_url_host": (cfg.get("base_url") or "").split("//")[-1].split("/")[0],
            "time_ms": dt_ms, "max_tokens": learned or 1600,
            "reasoning_model": bool(learned and learned > 1600),
            "json_keys": keys,
            "reply": json.dumps(res, ensure_ascii=False)[:160]}
