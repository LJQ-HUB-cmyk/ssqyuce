"""兼容薄壳：实现已迁至 `lottery.stats.eprocess`（V14-A，2026-10-21）。

## 为什么用「模块级代理」而不是镜像复制

镜像复制会产生**两份状态**：`monkeypatch.setattr(eprocess, "X", fake)` 只改薄壳、
不改真源，被测代码若从另一入口取 `X` 就会不一致。全量跑测试时才复现，
依赖测试顺序，极难排查（本轮真实踩到，见 `lottery/rigor.py` 的说明）。

代理方案：读一律问真源（单一真源），写同时写两边（打桩两个入口都生效），
私有符号（`_xxx`）通过 `__getattr__` 自动可用。

## 推荐用法

    from . import eprocess          # 旧写法，仍然可用（走本模块代理）
    from .stats import eprocess     # 新写法，推荐
"""
from __future__ import annotations

import sys as _sys
import types as _types

from .stats import eprocess as _src

_OWN = {"__name__", "__doc__", "__file__", "__package__", "__loader__",
        "__spec__", "__builtins__", "__all__"}


class _ProxyModule(_types.ModuleType):
    """把属性读写转发给真源模块的代理模块。"""

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            return getattr(_src, name)
        except AttributeError:
            raise AttributeError(
                f"module 'lottery.eprocess' has no attribute {name!r}"
            ) from None

    def __setattr__(self, name, value):
        if name in _OWN:
            super().__setattr__(name, value)
            return
        setattr(_src, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _OWN:
            super().__delattr__(name)
            return
        if hasattr(_src, name):
            delattr(_src, name)
        if hasattr(type(self), name) or name in self.__dict__:
            super().__delattr__(name)


_sys.modules[__name__].__class__ = _ProxyModule

__all__ = [n for n in dir(_src) if not n.startswith("_")]
