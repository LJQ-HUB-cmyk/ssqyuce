"""MU-2：按用户的 LLM 用量记账。

## 为什么记账挂在 `_bill_finish`

`chat()` 有 4 个出口（成功 / strict 抛错 / 降级 / 超时）。若在每个 return 前
各插一次记账，迟早漏掉某个分支 —— 表现为"某些调用不计入配额"，
而这种漏记账**不会报错**，只会让配额在用户不知情时被放行。

所以记账放在唯一交汇点 `_bill_finish`，并在这里用测试钉住：
成功要计、失败也要计（失败通常也消耗了 prompt token）。

## 归属来源

用 `api_app.current_user_id()`（contextvar）—— `llm_client` 是无法改签名的
深层库，正是"contextvar 只留给这类库"的适用场景（Lead 裁决 9）。
无上下文时**不记到任何人头上**（后台线程/调度器），绝不回退到 admin。
"""
from __future__ import annotations

import pytest

from lottery import config, db, llm_client, user_llm_store


def _resp(payload):
    return type("_R", (), {
        "status_code": 200, "text": "",
        "json": lambda self: payload,
    })()


def _ok_with_usage(tin=100, tout=50):
    return _resp({
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": tin, "completion_tokens": tout},
    })


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOTT_HOME", str(tmp_path))
    monkeypatch.setenv("LOTT_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("LOTT_MASTER_KEY", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "LLM_CONFIG_FILE", config.DATA_DIR / "llm_config.json")
    monkeypatch.setattr(config, "LLM_DISABLED", False)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://fake.example.com/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-fake-key-123456")
    monkeypatch.setattr(config, "LLM_MODEL", "fake-model")
    monkeypatch.setattr(config, "LLM_MODEL_LIST", ["fake-model"])
    monkeypatch.setattr(config, "LLM_EXTRA_MODELS", [])
    monkeypatch.setattr(config, "LLM_EXTRA_BODY_DEFAULT", {})
    monkeypatch.setattr(config, "LLM_EXTRA_BODY_BY_MODEL", {})
    monkeypatch.setattr(config, "LLM_MAX_TOKENS", 8000)
    db.close()
    db.get_conn()
    # 建一个真实用户（记账写 user_llm_config 行）
    from lottery import auth
    conn = db.get_conn()
    conn.execute("UPDATE users SET role='admin', status='active', is_primary_admin=1"
                 " WHERE status='bootstrap'")
    conn.commit()
    u = auth.create_user("dave", "DavePass1234", role="user")
    llm_client.reset_learned_budgets()
    yield u["user_id"]
    db.close()


def _run_chat(monkeypatch, user_id, resp):
    """跑一次 chat()，并把请求替换成固定响应（不发真网络）。"""
    from lottery import api_app
    monkeypatch.setattr(api_app, "current_user_id", lambda: user_id, raising=False)
    monkeypatch.setattr(llm_client.requests, "post", lambda *a, **k: resp)
    return llm_client.chat("sys", "user")


def test_successful_call_records_tokens(env, monkeypatch):
    uid = env
    user_llm_store.save(uid, {"daily_token_quota": 100000})
    assert user_llm_store.get_view(uid)["today_tokens_used"] == 0
    _run_chat(monkeypatch, uid, _ok_with_usage(120, 80))
    v = user_llm_store.get_view(uid)
    assert v["today_tokens_used"] == 200, f"应记 120+80，实际 {v['today_tokens_used']}"
    assert v["today_calls_used"] == 1


def test_failed_call_records_call_but_not_success(env, monkeypatch):
    """失败也消耗了 prompt token，但不算"成功调用次数"。"""
    uid = env
    user_llm_store.save(uid, {"daily_token_quota": 100000})
    bad = _resp({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    _run_chat(monkeypatch, uid, bad)
    v = user_llm_store.get_view(uid)
    assert v["today_calls_used"] == 0, "失败的调用不该计入成功次数"


def test_no_context_does_not_attribute_to_anyone(env, monkeypatch):
    """后台线程没有 contextvar → **不得**把消费记到某个具体用户头上。"""
    uid = env
    user_llm_store.save(uid, {"daily_token_quota": 100000})
    from lottery import api_app
    monkeypatch.setattr(api_app, "current_user_id", lambda: None, raising=False)
    monkeypatch.setattr(llm_client.requests, "post",
                        lambda *a, **k: _ok_with_usage(500, 500))
    llm_client.chat("sys", "user")
    assert user_llm_store.get_view(uid)["today_tokens_used"] == 0, (
        "无用户上下文时不得记账到具体用户（否则后台任务会吃掉某人配额）")


def test_usage_accumulates_across_calls(env, monkeypatch):
    uid = env
    user_llm_store.save(uid, {"daily_token_quota": 100000})
    for _ in range(3):
        _run_chat(monkeypatch, uid, _ok_with_usage(10, 5))
    v = user_llm_store.get_view(uid)
    assert v["today_tokens_used"] == 45
    assert v["today_calls_used"] == 3


def test_missing_usage_block_does_not_crash(env, monkeypatch):
    """上游没返回 usage 时不得崩、也不得凭空补 0 记成"免费"。"""
    uid = env
    user_llm_store.save(uid, {"daily_token_quota": 100000})
    no_usage = _resp({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})
    _run_chat(monkeypatch, uid, no_usage)
    v = user_llm_store.get_view(uid)
    assert v["today_tokens_used"] == 0     # 未知 ≠ 0 消费，但也不能崩
    assert v["today_calls_used"] == 1


def test_record_usage_works_for_user_without_config_row(env, monkeypatch):
    """**核心回归**：从未打开过设置页的用户，也必须能记账。

    第一版 `record_usage` 是 `UPDATE ... WHERE user_id=?`。对"从没配过个人配置"
    的用户（最常见的情况：管理员用共享通道）匹配 0 行，于是：

      * 真实 LLM 调用成功、成本日志 tokens 齐全
      * 但配额计数永远是 0，用户永远不会被拦

    实测就是这么发现的：carol 有配置行所以能记，admin 没有行所以记不上，
    而 admin 恰恰是唯一在真实出号的人。改成 UPSERT 才覆盖这种情况。
    """
    uid = env
    # 确保这个用户**没有** user_llm_config 行
    db.get_conn().execute("DELETE FROM user_llm_config WHERE user_id=?", (uid,))
    db.get_conn().commit()
    assert user_llm_store.get_raw(uid) is None

    user_llm_store.record_usage(uid, tokens=500, calls=2)

    raw = user_llm_store.get_raw(uid)
    assert raw is not None, "没有配置行时也必须建行记账（否则用量静默丢失）"
    assert raw["today_tokens_used"] == 500
    assert raw["today_calls_used"] == 2
    assert raw["last_reset_date"] == __import__("time").strftime("%Y-%m-%d")


def test_record_usage_accumulates_on_existing_row(env):
    uid = env
    user_llm_store.record_usage(uid, tokens=100, calls=1)
    user_llm_store.record_usage(uid, tokens=150, calls=1)
    v = user_llm_store.get_view(uid)
    assert v["today_tokens_used"] == 250
    assert v["today_calls_used"] == 2


def test_record_usage_resets_on_new_day(env):
    """跨天：新一天的第一次记账必须**覆盖**而不是累加昨天的。"""
    uid = env
    user_llm_store.record_usage(uid, tokens=1000, calls=5)
    # 把日期改成昨天
    db.get_conn().execute(
        "UPDATE user_llm_config SET last_reset_date='1970-01-01' WHERE user_id=?", (uid,))
    db.get_conn().commit()
    user_llm_store.record_usage(uid, tokens=30, calls=1)
    v = user_llm_store.get_view(uid)
    assert v["today_tokens_used"] == 30, "跨天后应重置为当天用量，而不是累加"
    assert v["today_calls_used"] == 1


def test_quota_blocks_after_usage_exceeds(env, monkeypatch):
    uid = env
    user_llm_store.save(uid, {"daily_token_quota": 100})
    _run_chat(monkeypatch, uid, _ok_with_usage(80, 40))     # 累计 120 > 100
    assert user_llm_store.quota_exceeded(uid) is not None
