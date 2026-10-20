"""请求级上下文（P2 模块化第一步）。

## 这个模块解决的倒置

改造前，这条依赖是**倒的**：

```
db.py         ──(懒加载)──> api_app.py   ← 底层数据层依赖顶层应用
llm_client.py ──(懒加载)──> api_app.py   ← 底层 LLM 层依赖顶层应用
```

`api_app.py` 是 3500 行的 FastAPI 应用模块，导入它会连带拉起 FastAPI、全部路由、
启动钩子。底层库为了读一个 contextvar 去依赖它，后果是：

* **无法独立测试**：单独测 `db.resolve_tenant()` 要先拉起整个 Web 应用；
* **循环依赖的诱因**：`api_app` → `db` → `api_app`，靠"把 import 写进函数体里"
  延迟到运行期才不炸（M1 §1.2 统计有 70+ 处这种懒加载）。懒加载是补丁，不是分层；
* **切分 api_app 时处处受阻**：任何搬迁都要考虑"谁会反过来 import 我"。

把 contextvar 这个**纯状态**搬到独立模块后，依赖方向变为单向：

```
api_app.py ──> request_context.py <── db.py
                                   <── llm_client.py
```

## 为什么 contextvar 还能用来传用户（裁决 9 的边界）

Lead 裁决 9 规定：**tenant 传参用显式参数，contextvar 只留给无法改签名的深层库**。
这里正是那个被允许的场景 —— `llm_client.chat()` 的签名被整条调用链使用，
为了记账去改签名会波及 engine / evaluate / llm_eval 全部调用点。

但**必须记住 contextvar 的两个陷阱**（本项目都实测踩过）：

1. **裸 `threading.Thread` 不继承** —— 后台任务要显式
   `threading.Thread(target=contextvars.copy_context().run, args=(fn,))`；
2. **`ThreadPoolExecutor` 也不继承** —— 提交处要 `copy_context()`，
   线程内用 `ctx.copy().run(fn)`（同一个 Context 不能并发进入）。

两处漏掉的表现都一样：**静默**拿不到用户 → 记账落空 → 配额永远显示 0，
而且不会报错。见 `tests/test_mu2_contextvar.py`。
"""
from __future__ import annotations

import contextvars

# 当前请求的用户 id。None = 无上下文（后台线程 / 调度器 / 未登录）。
#
# **绝不回退到 admin 或任何默认用户**：回退等于提权后门，而且会让"后台任务
# 写的票"悄悄挂到某个真实人头下，表现为数据串号。
_current_user_id: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "current_user_id", default=None)


def current_user_id():
    """供深层库读取当前用户；无上下文时返回 None（绝不回退 admin）。"""
    return _current_user_id.get()


def set_current_user_id(user_id):
    """设置当前用户（仅中间件与测试用；返回 token 以便 reset）。

    中间件在 `finally` 里必须 `reset(token)` 而不是 `set(None)`：
    AnyIO 会复用工作线程，用 set(None) 在嵌套场景下会误清外层身份。
    """
    return _current_user_id.set(user_id)


def reset_current_user_id(token) -> None:
    """恢复设置前的值（与 `set_current_user_id` 配对使用）。"""
    try:
        _current_user_id.reset(token)
    except (ValueError, RuntimeError):
        # token 来自别的上下文（例如跨线程重置）：退化为直接置 None。
        # 宁可清空也不要留下一个"看起来有效"的旧身份。
        _current_user_id.set(None)
