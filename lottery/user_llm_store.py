"""每用户 LLM 配置的存取层（MU-2）。

本模块只做**存取与派生**，不碰 HTTP（那是 `user_llm_api`）、不碰加密细节
（那是 `secretbox`）。三层分开的理由：加密规则要能被单独测，HTTP 契约要能被
单独测，而"配置怎么合并"是第三件事。

## 三种配置来源，优先级固定

1. **用户自备**（`use_custom_key=1` 且 `base_url`/`model`/key 齐备）→ 用它；
2. **系统共享**（`data/llm_config.json`，即 MU-1 之前的全局配置）→ 管理员配的那份；
3. **无**（系统共享也没配）→ 纯统计通道，**照常出号**。

第 3 条是硬要求（U2 §5.3）：未配置 LLM 绝不能阻断出号。任何"取配置"的失败
都必须退化成"没有 LLM"，而不是抛异常。

## 为什么 `effective_for_user()` 返回的是"合并后的纯 dict"而不是 config 对象

`llm_client` 读的是模块级常量（`config.LLM_BASE_URL` 等），改签名会波及整条
调用链。这一步先提供确定性的"这个用户该用哪套参数"，真正接线（按用户替换
请求参数）在 `llm_client` 里用显式参数完成 —— 与 Lead 裁决 9 一致：
tenant 传参用显式参数，contextvar 只留给无法改签名的深层库。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from . import config, db, secretbox

# 默认配额（U2 §3.4 的冻结默认值）。管理员可后续在管理端调整。
DEFAULT_DAILY_TOKEN_QUOTA = 50000
DEFAULT_QPS_LIMIT = 2
DEFAULT_SAMPLES = 3


def _table_ready() -> bool:
    """迁移 6 是否已落地。

    刻意用"查表存在"而不是 try/except 吞异常：如果表不存在就静默返回"未配置"，
    那么**迁移没跑**这个真实故障会被伪装成"用户没配 LLM"，排查时彻底误导。
    这里只在表确实不存在（老库未升级）时降级，并把原因打出来。
    """
    try:
        row = db.get_conn().execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_llm_config'"
        ).fetchone()
        return row is not None
    except Exception:      # noqa: BLE001
        return False


def get_raw(user_id: int) -> Optional[Dict[str, Any]]:
    """读原始行（含密文），无行返回 None。**不含解密后的明文 Key。**"""
    if not _table_ready():
        print("[user_llm] user_llm_config 表不存在（迁移 6 未跑），按未配置处理")
        return None
    row = db.get_conn().execute(
        "SELECT * FROM user_llm_config WHERE user_id=?", (int(user_id),)).fetchone()
    return dict(row) if row else None


def get_api_key(user_id: int) -> Optional[str]:
    """取该用户的**明文** API Key（仅用于发请求）。解密失败返回 None。"""
    raw = get_raw(user_id)
    if not raw:
        return None
    return secretbox.decrypt(raw.get("api_key_encrypted"))


def _row_view(user_id: int) -> Dict[str, Any]:
    """缺省视图（完全没有行时）。全部按最保守的默认值。"""
    return {
        "user_id": int(user_id),
        "use_custom_key": 0,
        "disabled": 0,
        "base_url": "",
        "model": "",
        "model_list": [],
        "samples": DEFAULT_SAMPLES,
        "extra_body": {},
        "daily_token_quota": DEFAULT_DAILY_TOKEN_QUOTA,
        "today_tokens_used": 0,
        "today_calls_used": 0,
        "last_reset_date": "",
        "qps_limit": DEFAULT_QPS_LIMIT,
        "updated_at": None,
        "api_key_masked": "",
        "has_api_key": False,
    }


def get_view(user_id: int) -> Dict[str, Any]:
    """对外视图：**永不含明文 Key**，只含 `api_key_masked` 与 `has_api_key`。

    `has_api_key` 与 `api_key_masked` 分开是有意的：解密失败时 `masked` 会是空，
    但"库里确实存过 Key"这件事仍然可见，便于用户判断"是我记错了、还是库坏了"。
    """
    raw = get_raw(user_id)
    if not raw:
        return _row_view(user_id)
    view = _row_view(user_id)
    def _int(key: str, default: int) -> int:
        """取整数值。**不能用 `raw.get(k) or default`** —— 合法的 0 会被 or 吞掉。

        这正是本项目踩过的坑：`daily_token_quota=0` 表示"不限"，用 `or` 读出来
        变成 50000（默认值），于是"不限"静默变成"每天 5 万"。
        """
        v = raw.get(key)
        if v is None or v == "":
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    view.update({
        "use_custom_key": _int("use_custom_key", 0),
        "disabled": _int("disabled", 0),
        "base_url": str(raw.get("base_url") or ""),
        "model": str(raw.get("model") or ""),
        "samples": _int("samples", DEFAULT_SAMPLES),
        # 0 = 不限，必须原样保留
        "daily_token_quota": _int("daily_token_quota", DEFAULT_DAILY_TOKEN_QUOTA),
        "today_tokens_used": _int("today_tokens_used", 0),
        "today_calls_used": _int("today_calls_used", 0),
        "last_reset_date": str(raw.get("last_reset_date") or ""),
        "qps_limit": _int("qps_limit", DEFAULT_QPS_LIMIT),
        "updated_at": raw.get("updated_at"),
        "api_key_masked": str(raw.get("api_key_masked") or ""),
        "has_api_key": bool(raw.get("api_key_encrypted")),
    })
    try:
        view["model_list"] = json.loads(raw.get("model_list_json") or "[]") or []
    except (ValueError, TypeError):
        view["model_list"] = []
    try:
        view["extra_body"] = json.loads(raw.get("extra_body_json") or "{}") or {}
    except (ValueError, TypeError):
        view["extra_body"] = {}
    return view


# 允许用户写入的字段白名单。**不在表里的字段一律忽略** ——
# 直接 `payload.update` 会让用户能改 `today_tokens_used`（配额绕过的经典写法）。
_WRITABLE = {
    "use_custom_key", "disabled", "base_url", "model", "model_list",
    "samples", "extra_body", "daily_token_quota", "qps_limit",
}


def save(user_id: int, payload: Dict[str, Any], *,
         clear_api_key: bool = False) -> Dict[str, Any]:
    """写配置。返回保存后的视图。

    `api_key` 的处理（最容易写错的地方）：
    * 字段**不出现** → 保留原 Key（用户只是改了模型名，不该丢 Key）；
    * 字段出现且为空串 → 也**保留原 Key**。理由：前端输入框回显的是脱敏占位，
      用户没动它就会提交空串；把空串当"清空"会导致"改个注数就丢了 API Key"。
      真正清空要显式用 `clear_api_key=True`。
    * 字段出现且非空 → 加密后覆盖，同时刷新 masked。

    幂等：同一份 payload 重复保存结果一致（除 `updated_at`）。
    """
    uid = int(user_id)
    if not _table_ready():
        raise RuntimeError("user_llm_config 表不存在（迁移 6 未跑）")

    cur = get_raw(uid) or {}
    view = get_view(uid)

    merged: Dict[str, Any] = {
        "use_custom_key": int(view["use_custom_key"]),
        "disabled": int(view["disabled"]),
        "base_url": view["base_url"],
        "model": view["model"],
        "model_list": view["model_list"],
        "samples": int(view["samples"]),
        "extra_body": view["extra_body"],
        "daily_token_quota": int(view["daily_token_quota"]),
        "qps_limit": int(view["qps_limit"]),
    }
    for k, v in (payload or {}).items():
        if k in _WRITABLE:
            merged[k] = v

    # 归一化（越界值不报错，钳到合法范围：配置项不该因为一个数字让整个保存失败）
    try:
        merged["samples"] = max(1, min(10, int(merged["samples"])))
    except (TypeError, ValueError):
        merged["samples"] = DEFAULT_SAMPLES
    try:
        merged["daily_token_quota"] = max(0, int(merged["daily_token_quota"]))
    except (TypeError, ValueError):
        merged["daily_token_quota"] = DEFAULT_DAILY_TOKEN_QUOTA
    try:
        merged["qps_limit"] = max(1, min(50, int(merged["qps_limit"])))
    except (TypeError, ValueError):
        merged["qps_limit"] = DEFAULT_QPS_LIMIT
    merged["use_custom_key"] = 1 if merged.get("use_custom_key") else 0
    merged["disabled"] = 1 if merged.get("disabled") else 0
    merged["base_url"] = str(merged.get("base_url") or "").strip().rstrip("/")
    merged["model"] = str(merged.get("model") or "").strip()
    if not isinstance(merged.get("model_list"), list):
        merged["model_list"] = []
    if not isinstance(merged.get("extra_body"), dict):
        merged["extra_body"] = {}

    # ---- API Key ----
    enc = str(cur.get("api_key_encrypted") or "")
    masked = str(cur.get("api_key_masked") or "")
    if clear_api_key:
        enc, masked = "", ""
    else:
        typed = (payload or {}).get("api_key")
        if isinstance(typed, str) and typed.strip():
            plain = typed.strip()
            enc = secretbox.encrypt(plain)      # 主密钥不可用时这里会抛，不落明文
            masked = secretbox.mask(plain)

    db.get_conn().execute(
        """INSERT INTO user_llm_config
           (user_id, use_custom_key, disabled, base_url, api_key_encrypted, api_key_masked,
            model, model_list_json, samples, extra_body_json, daily_token_quota,
            today_tokens_used, today_calls_used, last_reset_date, qps_limit, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             use_custom_key=excluded.use_custom_key,
             disabled=excluded.disabled,
             base_url=excluded.base_url,
             api_key_encrypted=excluded.api_key_encrypted,
             api_key_masked=excluded.api_key_masked,
             model=excluded.model,
             model_list_json=excluded.model_list_json,
             samples=excluded.samples,
             extra_body_json=excluded.extra_body_json,
             daily_token_quota=excluded.daily_token_quota,
             qps_limit=excluded.qps_limit,
             updated_at=excluded.updated_at""",
        (uid, merged["use_custom_key"], merged["disabled"], merged["base_url"],
         enc, masked, merged["model"],
         json.dumps(merged["model_list"], ensure_ascii=False),
         merged["samples"], json.dumps(merged["extra_body"], ensure_ascii=False),
         merged["daily_token_quota"],
         int(cur.get("today_tokens_used") or 0),
         int(cur.get("today_calls_used") or 0),
         str(cur.get("last_reset_date") or ""),
         merged["qps_limit"], time.time()),
    )
    db.get_conn().commit()
    return get_view(uid)


def effective_for_user(user_id: Optional[int], *,
                       with_secret: bool = False) -> Dict[str, Any]:
    """该用户**实际生效**的 LLM 参数 + 来源标记。

    返回值里的 `source` 有三态：
      * ``"user"``     —— 用自己的 Key；
      * ``"system"``   —— 用管理员配置的共享通道；
      * ``"none"``     —— 没有可用通道 → 调用方必须走纯统计，**不许报错**。

    `llm_available=False` 是给上层的唯一判据：所有"要不要调 LLM"的分支都看它，
    而不是各自去拼 `base_url and api_key and model` 三个条件（那种拼法迟早漏一处）。

    ## 为什么明文 Key 默认**不**返回（`with_secret=False`）

    早先的实现无条件把明文塞进返回 dict。风险很实在：这个 dict 会四处流动，
    任何一处 `print(res)` / `json.dumps(res)` / 异常日志都会把 Key 打进日志。
    「安全默认」比「记得别打印」可靠 —— 需要真发请求的地方显式传
    `with_secret=True`，让"我要用明文"成为一个**显式的、可 grep 的**决定。
    """
    out: Dict[str, Any] = {
        "source": "none",
        "llm_available": False,
        "base_url": "",
        "api_key": None,
        "model": "",
        "model_list": [],
        "samples": DEFAULT_SAMPLES,
        "extra_body": {},
        "reason": "",
    }
    if user_id is None:
        out["reason"] = "无用户上下文"
        return out

    view = get_view(int(user_id))
    if view.get("disabled"):
        out["reason"] = "该用户已关闭 LLM"
        return out

    # 1) 自备配置
    if view.get("use_custom_key"):
        key = get_api_key(int(user_id))
        if view["base_url"] and view["model"] and key:
            out.update({
                "source": "user", "llm_available": True,
                "base_url": view["base_url"],
                "api_key": (key if with_secret else None),
                "has_api_key": True,
                "model": view["model"],
                "model_list": view["model_list"] or [view["model"]],
                "samples": int(view["samples"]),
                "extra_body": view["extra_body"],
            })
            return out
        # 勾了"用自备"但没配齐 → 明确退化到共享通道，并如实标注原因
        out["reason"] = "已勾选自备 Key 但未配置完整，改用共享通道"

    # 2) 系统共享通道（MU-1 之前的全局配置）
    if config.llm_configured():
        out.update({
            "source": "system", "llm_available": True,
            "base_url": config.LLM_BASE_URL or "",
            "api_key": (config.LLM_API_KEY if with_secret else None),
            "has_api_key": bool(config.LLM_API_KEY),
            "model": config.LLM_MODEL or "",
            "model_list": list(config.LLM_MODEL_LIST),
            "samples": int(config.LLM_SAMPLES),
            "extra_body": dict(getattr(config, "LLM_EXTRA_BODY_DEFAULT", {}) or {}),
        })
        return out

    out["reason"] = out["reason"] or "系统未配置共享通道"
    return out


def reset_daily_counters_if_needed(user_id: int) -> None:
    """跨天重置当日用量（U2 §3.4 的 `last_reset_date`）。

    为什么按"日期字符串"而不是时间戳取模：用户看到的"今天"是本地日历日
    （TZ=Asia/Shanghai），用 Unix 时间整除会得到 UTC 边界，与用户直觉不符。
    """
    raw = get_raw(user_id)
    if not raw:
        return
    today = time.strftime("%Y-%m-%d", time.localtime())
    if str(raw.get("last_reset_date") or "") == today:
        return
    db.get_conn().execute(
        "UPDATE user_llm_config SET today_tokens_used=0, today_calls_used=0,"
        " last_reset_date=? WHERE user_id=?", (today, int(user_id)))
    db.get_conn().commit()


def quota_exceeded(user_id: int) -> Optional[str]:
    """超配额时返回提示语，否则 None。

    只对**共享通道**有意义（用自己的 Key 花的是自己的钱，不该被系统配额卡住）——
    这条判断放在调用方，本函数只回答"这个用户今天用超了吗"。
    """
    reset_daily_counters_if_needed(user_id)
    view = get_view(user_id)
    quota = int(view.get("daily_token_quota") or 0)
    if quota <= 0:
        return None                      # 0 = 不限
    used = int(view.get("today_tokens_used") or 0)
    if used < quota:
        return None
    return (f"今日共享通道额度已用尽（{used}/{quota} tokens）。"
            f"可改用纯统计通道出号，或在设置页配置自己的 API Key。")


def record_usage(user_id: int, tokens: int = 0, calls: int = 1) -> None:
    """累加当日用量。失败不影响主流程（用量统计不该阻断出号）。

    ## 为什么必须是 UPSERT 而不是 UPDATE

    第一版写成 `UPDATE ... WHERE user_id=?`，在**从未进过设置页**的用户上
    匹配 0 行 —— 而那正是最常见的情况（管理员用共享通道、从没配过个人配置）。
    结果是：真实 LLM 调用成功、成本日志里 tokens 齐全，但配额计数永远是 0。
    这是本轮实测抓到的第二个"静默落空"（第一个是 contextvar 没穿过线程池）。

    失败方向很糟：用户以为有 5 万额度，实际从来没被计数，也就永远不会被拦。
    """
    if user_id is None:
        return
    try:
        tokens = max(0, int(tokens))
        calls = max(0, int(calls))
        today = time.strftime("%Y-%m-%d", time.localtime())
        db.get_conn().execute(
            """INSERT INTO user_llm_config
               (user_id, today_tokens_used, today_calls_used, last_reset_date, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 today_tokens_used = CASE
                   WHEN user_llm_config.last_reset_date = excluded.last_reset_date
                   THEN user_llm_config.today_tokens_used + excluded.today_tokens_used
                   ELSE excluded.today_tokens_used END,
                 today_calls_used = CASE
                   WHEN user_llm_config.last_reset_date = excluded.last_reset_date
                   THEN user_llm_config.today_calls_used + excluded.today_calls_used
                   ELSE excluded.today_calls_used END,
                 last_reset_date = excluded.last_reset_date,
                 updated_at = excluded.updated_at""",
            (int(user_id), tokens, calls, today, time.time()))
        db.get_conn().commit()
    except Exception:      # noqa: BLE001
        pass
