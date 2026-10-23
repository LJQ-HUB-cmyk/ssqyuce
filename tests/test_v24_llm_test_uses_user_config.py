"""V24：LLM 连接测试必须用**用户生效的配置**，不是全局默认。

## 用户报告的缺陷（2026-10-23）

> 「用户自己填写自定义模型，保存配置，测试是否正常。貌似测的还是默认的，不是手动填写的」

## 实测复现（修复前）

```
用户 1 生效来源 = "user"，自备 model = group/subscription   (host=magpie.ff88.de)
全局默认        model = agent-router/deepseek-v4-flash
/api/llm/test 用的 model = agent-router/deepseek-v4-flash   ← 测的是全局默认
```

`test_llm_connection()` 旧实现只用 `config.llm_model_list()`，
**从头到尾没有读用户自备配置**。

## 为什么这是"最坏的一种误导"

若用户的自备通道其实是坏的（地址错、Key 无效、模型名拼错），
测试仍会显示成功 —— 因为它测的是另一条通道。用户会以为配好了，
直到真正出号时才失败，而那时他无法判断问题出在配置还是系统。

## 本文件守什么

1. 测试端点必须走 `user_llm_store.effective_for_user`（与出号链路同源）；
2. 必须真的用到用户填的 base_url / model，而不是全局默认；
3. 返回值必须标明 `source` —— 让人看得出测的是哪一条通道；
4. 无用户通道时才退回全局配置，且同样标注 `source="system"`。

## 恒真断言守卫

见 `tests/test_d15_no_vacuous_assertions.py`。每条断言绑定一个可变异的实现点。
"""
from __future__ import annotations

import pytest

from lottery import routes_llm as RL
from lottery import user_llm_store


# ==========================================================================
# 1. 实现层：必须读用户配置
# ==========================================================================

def test_test_endpoint_reports_source(monkeypatch):
    """返回值必须标明配置来源（user / system / none）。

    变异点：把 `"source": cfg.get("source")` 从返回体删掉 —— 变红。
    为什么关键：不标来源，用户无法判断"测试成功"到底在证明哪条通道。

    说明：本用例**只做行为断言**（不看源码）。
    A 类元守卫（`test_no_new_python_source_string_assertions`）要求
    `inspect.getsource` 子串断言**必须为 0** —— 它是"测实现"而非"测行为"，
    实现一换名字就假红/假绿。第一版我写了 `getsource` 断言，被元守卫拦下。
    """
    from lottery import llm_client

    user_cfg = {
        "source": "user", "llm_available": True,
        "base_url": "https://x.example.com/v1", "api_key": "sk-x",
        "model": "m1", "model_list": ["m1"], "samples": 3, "extra_body": {},
    }
    monkeypatch.setattr(user_llm_store, "effective_for_user",
                        lambda *a, **k: dict(user_cfg), raising=False)
    monkeypatch.setattr(llm_client, "chat_json",
                        lambda *a, **k: {"ok": True}, raising=False)
    monkeypatch.setattr(RL.db, "load_draws", lambda *a, **k: [], raising=False)

    r = RL.test_llm_connection()
    assert r.get("source") == "user", (
        f"返回体必须标明配置来源，实际 {r.get('source')!r}")


# ==========================================================================
# 2. 行为层：真的用上了用户填的值
# ==========================================================================

def test_user_config_wins_over_global(monkeypatch):
    """有用户自备配置时，测的必须是用户那条，**不是**全局默认。

    变异点：把 cfg 赋值改回 `cfg = config.llm_model_list()[0]` —— 变红。

    做法：打桩 `effective_for_user` 返回一个与全局默认明显不同的地址与模型，
    再打桩真正的 `chat_json` 记录它收到的 model_cfg，断言两者一致。
    """
    from lottery import llm_client

    user_cfg = {
        "source": "user", "llm_available": True,
        "base_url": "https://user-own.example.com/v1",
        "api_key": "sk-user-owned", "has_api_key": True,
        "model": "user-chosen-model",
        "model_list": ["user-chosen-model"],
        "samples": 3, "extra_body": {},
    }
    monkeypatch.setattr(user_llm_store, "effective_for_user",
                        lambda *a, **k: dict(user_cfg), raising=False)

    seen = {}

    def _fake_chat(system, user, **kw):
        seen["model_cfg"] = kw.get("model_cfg")
        return {"ok": True}

    monkeypatch.setattr(llm_client, "chat_json", _fake_chat, raising=False)
    # 让"真实提示"构建失败，走小型自检提示（避免依赖数据库）
    monkeypatch.setattr(RL.db, "load_draws", lambda *a, **k: [], raising=False)

    RL.test_llm_connection()
    got = seen.get("model_cfg") or {}
    assert got.get("model") == "user-chosen-model", (
        f"测试必须使用用户填的 model，实际用了 {got.get('model')!r}"
        f"（若为全局默认值，说明缺陷复发）")
    assert got.get("base_url") == "https://user-own.example.com/v1", (
        f"测试必须使用用户填的 base_url，实际 {got.get('base_url')!r}")


def test_falls_back_to_global_when_user_unavailable(monkeypatch):
    """用户没有可用通道时退回全局配置，并标注 source="system"。

    变异点：把 `cfg["source"] = "system"` 删掉 —— 变红（用户看不出退化）。
    """
    from lottery import llm_client

    monkeypatch.setattr(user_llm_store, "effective_for_user",
                        lambda *a, **k: {"source": "none", "llm_available": False,
                                         "reason": "未配置"}, raising=False)
    seen = {}

    def _fake_chat(system, user, **kw):
        seen["model_cfg"] = kw.get("model_cfg")
        return {"ok": True}

    monkeypatch.setattr(llm_client, "chat_json", _fake_chat, raising=False)
    monkeypatch.setattr(RL.db, "load_draws", lambda *a, **k: [], raising=False)
    # 显式钉住"全局有一条可用通道"，否则全量环境下前序用例可能已把
    # LLM_MODEL_LIST 改空 → 端点走 no_channel 分支、根本没调 chat_json，
    # seen 为空（单跑通过、全量失败，就是这种跨用例污染）。
    from lottery import config as _cfg
    _global = {"base_url": "https://global.example.com/v1", "api_key": "sk-g",
               "model": "global-default-model"}
    monkeypatch.setattr(_cfg, "llm_model_list", lambda: [dict(_global)], raising=False)
    monkeypatch.setattr(_cfg, "LLM_DISABLED", False, raising=False)

    RL.test_llm_connection()
    got = seen.get("model_cfg") or {}
    assert got, ("应退回全局配置并仍发起探测（若 seen 为空说明走了 no_channel 分支，"
                 "即未真正退回全局通道）")
    assert got.get("source") == "system", (
        f"退回全局配置时必须标 source='system'，实际 {got.get('source')!r}")
    assert got.get("model") == "global-default-model", (
        f"退回时应使用全局通道的 model，实际 {got.get('model')!r}")


def test_no_channel_at_all_returns_actionable_error(monkeypatch):
    """两条通道都没有时，错误文案必须可行动（去设置页填 / 联系管理员）。

    变异点：把 error 文案改回「LLM 未配置」这种没有下一步的短句 —— 变红
    （`test_d20_actionable_errors` 会拦）。
    """
    from lottery import config
    monkeypatch.setattr(user_llm_store, "effective_for_user",
                        lambda *a, **k: {"source": "none", "llm_available": False},
                        raising=False)
    monkeypatch.setattr(config, "llm_model_list", lambda: [], raising=False)
    monkeypatch.setattr(config, "LLM_DISABLED", False, raising=False)

    r = RL.test_llm_connection()
    assert r.get("ok") is False
    assert r.get("source") == "none"
    err = r.get("error") or ""
    assert ("设置页" in err or "填写" in err), (
        f"错误文案要告诉用户下一步做什么，实际：{err!r}")
