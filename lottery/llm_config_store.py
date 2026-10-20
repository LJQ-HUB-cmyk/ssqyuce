"""全局 LLM 默认配置的读写（P2 从 api_app 中搬出）。

## 为什么单独成模块

`update_llm_config()` 原本写在 `api_app.py` 里，而 `user_llm_api.py` 需要调用它
来保存**全局共享通道** —— 于是出现 `user_llm_api → api_app` 的反向依赖。
把一个纯配置函数搬到独立模块后，两条端点模块都依赖它，方向变成：

```
api_app.py     ──> llm_config_store.py <── user_llm_api.py
```

它**只操作 config 模块的常量与一个 JSON 文件**，不碰 FastAPI、不碰请求对象，
所以搬迁是零风险的。

## 这里管的是「全局共享通道」，不是「我的配置」

两者的边界必须分清（MU-2 的产品形态 = 方案 C）：

* 本模块 = `data/llm_config.json` = **管理员配置的共享通道**，
  对所有"没有配个人 Key"的用户生效；
* `user_llm_store.py` = `user_llm_config` 表 = **每个账号自己那一行**，
  用自备 Key 时优先生效。

把这两个混在一起，就会出现"我改了设置结果影响所有人"或反之的错乱。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from . import config


def read() -> Dict[str, Any]:
    """当前全局默认通道的公开视图。**绝不含 api_key 明文**。"""
    from . import llm_client
    learned = None
    if config.LLM_BASE_URL and config.LLM_MODEL:
        try:
            learned = llm_client.learned_max_tokens(config.LLM_BASE_URL, config.LLM_MODEL)
        except Exception:      # noqa: BLE001 —— 纯装饰信息
            learned = None
    return {
        "disabled": config.LLM_DISABLED,
        "base_url": config.LLM_BASE_URL,
        "model": config.LLM_MODEL,
        "model_list": list(config.LLM_MODEL_LIST),
        "samples": config.LLM_SAMPLES,
        "max_tokens": config.LLM_MAX_TOKENS,
        "learned_max_tokens": learned,
        "total_timeout": config.LLM_TOTAL_TIMEOUT,
        "verify": config.LLM_VERIFY_ENABLED,
        "configured": config.llm_configured(),
    }


def save(payload: Dict[str, Any]) -> Dict[str, Any]:
    """保存全局默认通道（管理员），落 `data/llm_config.json`。

    只更新 payload 里出现的键；文件里其它键（如手工写入的 `extra_body` /
    `total_timeout`）原样保留 —— 设置页保存不该抹掉手工配置。
    """
    if "base_url" in payload:
        config.LLM_BASE_URL = (payload.get("base_url") or "").strip().rstrip("/") or None
        os.environ["LOTT_LLM_BASE_URL"] = config.LLM_BASE_URL or ""
    if "api_key" in payload:
        config.LLM_API_KEY = (payload.get("api_key") or "").strip() or None
        os.environ["LOTT_LLM_API_KEY"] = config.LLM_API_KEY or ""
    if "model" in payload:
        config.LLM_MODEL = (payload.get("model") or "").strip() or "minimax-m3"
        os.environ["LOTT_LLM_MODEL"] = config.LLM_MODEL
        # 设置页保存的模型是**唯一事实来源：替换**（而非前插）模型列表。
        # 前插会把 .env 里遗留的旧模型留在观察轮轮转列表里 —— 新模型一旦失败，
        # 旧模型的 404 就成了最终报错，把真实原因（如推理型模型未产出正文）
        # 彻底掩盖掉。多模型容错请用 LOTT_LLM_EXTRA_MODELS / 附加通道。
        config.LLM_MODEL_LIST = [config.LLM_MODEL]
    if "samples" in payload:
        try:
            config.LLM_SAMPLES = max(1, min(20, int(payload["samples"])))
        except (TypeError, ValueError):
            config.LLM_SAMPLES = 3
        os.environ["LOTT_LLM_SAMPLES"] = str(config.LLM_SAMPLES)
    if "disabled" in payload:
        config.LLM_DISABLED = bool(payload["disabled"])
        os.environ["LOTT_LLM_DISABLED"] = "1" if config.LLM_DISABLED else "0"

    config.LLM_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    conf: Dict[str, Any] = {}
    try:
        if config.LLM_CONFIG_FILE.exists():
            existing = json.loads(config.LLM_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                conf.update(existing)
    except (ValueError, OSError):
        pass
    conf.update({
        "disabled": config.LLM_DISABLED,
        "base_url": config.LLM_BASE_URL or "",
        "api_key": config.LLM_API_KEY or "",
        "model": config.LLM_MODEL or "",
        "samples": config.LLM_SAMPLES,
    })
    try:
        config.LLM_CONFIG_FILE.write_text(
            json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"配置写入失败: {e}"}
    return {"ok": True, "configured": config.llm_configured(),
            "model_list": list(config.LLM_MODEL_LIST)}
