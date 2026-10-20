"""JSON 归一化工具（P2 从 api_app 中提取的共享函数）。

## 为什么单独成模块

`_jsonable()` 在 `api_app.py` 的 V4 域里被 **10 个端点**共用。
把 V4 拆成多个域模块时，它就成了唯一的真枢纽：若留在 `api_app`，
每个新域都得反向 import api_app（切分等于白做）；若复制到每个域，
又会有 N 份逐渐漂移的实现。

所以先把它提到独立模块，各个域共同依赖它。

## 它解决的问题

FastAPI 的 JSON 序列化遇到 numpy 标量会直接报错。本项目的统计函数
（scipy/numpy 重度使用）返回的字典里到处是 `np.float64` / `np.int64` /
`np.ndarray`，不统一转换就会出现「算法算对了、接口 500」的故障。
"""
from __future__ import annotations

from typing import Any


def jsonable(obj: Any, _depth: int = 0) -> Any:
    """把返回体递归归一成 JSON 原生类型（numpy 标量 / dict 键 / None）。

    - ``np.generic``（float64/int64/bool_ …）→ ``.item()`` 转 Python 原生；
    - ``np.ndarray`` → ``.tolist()`` 后继续递归；
    - dict / list / tuple 递归处理，dict 的键统一转 ``str``；
    - 其它不可序列化对象 → ``repr()``（宁可显示成字符串，也不让端点 500）。
    """
    if _depth > 12:                                   # pragma: no cover - 纯防御
        return repr(obj)
    try:
        import numpy as _np
    except Exception:                                 # pragma: no cover - numpy 是硬依赖
        _np = None
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if _np is not None:
        if isinstance(obj, _np.generic):
            return obj.item()
        if isinstance(obj, _np.ndarray):
            return jsonable(obj.tolist(), _depth + 1)
    if isinstance(obj, dict):
        return {str(k): jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v, _depth + 1) for v in obj]
    return repr(obj)
