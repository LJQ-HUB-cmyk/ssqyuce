"""全局测试隔离与公共 fixture（V10-00b）。

为什么需要这个文件（缺陷背景）
------------------------------
`docs/reviews/R4_RELEASE_REVIEW.md` P0-B 实测：本目录此前**没有 conftest.py**，
19 处用例用裸赋值 ``config.DB_PATH = tmp_path / ...`` 把全局配置改到临时库且
不还原；而 ``lottery/db.py`` 还有模块级连接缓存 ``_CONN``。结果是：

* 某用例把库换到临时目录后，**排在它后面的用例**都在这个（可能是空的）临时库上跑；
* 全量 ``pytest -q`` 当时能过 **纯属文件字母序巧合**（污染源都在 ``test_v4_*`` 之前，
  唯一受害用例是 ``test_v4_honest_ui.py``）。新增的 ``test_v10_*`` 排在 ``test_v4_*``
  **之后**，正好落在污染源下游，会以 ``count_draws() == 0`` 直接假红；
* 反过来，按字符序以外的顺序（例如 ``pytest tests/test_m4_eval.py tests/x.py``）
  也能立刻复现。

本文件提供的机制
----------------
1. ``isolated_env``（经 autouse 的 ``_auto_isolate_lottery_paths`` **自动应用**）：
   每个测试**天然**拿到独立的临时 ``BASE / DATA_DIR / RAW_DIR / DB_PATH`` 以及
   ``config`` 上由 DATA_DIR 派生的配置文件路径（``llm_budget.json`` /
   ``methods_config.json`` / ``adaptive_state.json`` / ``llm_config.json``），
   另外同步重定向两个把 DATA_DIR 在 import 时固化下来的模块级常量
   （``llm_lab.CLAIMS_FILE``、``patterns_runtime.SNAPSHOT_FILE``）。
   测试前后都会 ``db.close()`` 重置连接缓存，所以既不会串库，也**不会写入生产
   ``data/``**；结束时按原值恢复，**泄漏为零**（即使用例自己又直接改了 ``config``，
   ``monkeypatch.undo()`` + 显式还原也会把它拉回原值）。
2. ``seeded_db``：在当前临时库里写入合成开奖数据，替代「依赖真实库恰好有数据」。
   新用例**不得**直接赋值 ``config.DB_PATH``，也不得假设生产库内容。
3. ``make_draws``：合成开奖数据工厂（结构同 ``db.load_draws()`` 的元素），供用例自己造库。

约定：``config.DB_PATH = ...`` 这类裸赋值在本目录已全部清理；需要换库时用
``monkeypatch.setattr(config, "DB_PATH", ...)`` + ``db.close()``，或直接用本文件夹具。
"""
from __future__ import annotations

import datetime as _dt
import random
from types import SimpleNamespace

import pytest

# 需要隔离的 config 属性（全部与"数据落在哪里"有关）
_ISOLATED_CONFIG_ATTRS = (
    "BASE",
    "DATA_DIR",
    "RAW_DIR",
    "DB_PATH",
    "LLM_BUDGET_FILE",
    "METHODS_CONFIG_FILE",
    "ADAPTIVE_STATE_FILE",
    "LLM_CONFIG_FILE",
)

_MISSING = object()


def _real_config_snapshot():
    """在收集阶段（任何 fixture 生效前）快照 config 的真实取值。"""
    from lottery import config

    return {name: getattr(config, name, _MISSING) for name in _ISOLATED_CONFIG_ATTRS}


_REAL_CONFIG = _real_config_snapshot()


@pytest.fixture
def isolated_env(tmp_path):
    """当前测试专属的临时数据根目录；自动应用到所有测试（见下方 autouse fixture）。"""
    from lottery import config, db
    from lottery import llm_lab, patterns_runtime

    base = tmp_path / "isolated"
    data = base / "data"
    raw = data / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    env = SimpleNamespace(tmp=tmp_path, base=base, data=data, raw=raw,
                          db_path=data / "ssq.db")

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(config, "BASE", base)
        mp.setattr(config, "DATA_DIR", data)
        mp.setattr(config, "RAW_DIR", raw)
        mp.setattr(config, "DB_PATH", env.db_path)
        mp.setattr(config, "LLM_BUDGET_FILE", data / "llm_budget.json")
        mp.setattr(config, "METHODS_CONFIG_FILE", data / "methods_config.json")
        mp.setattr(config, "ADAPTIVE_STATE_FILE", data / "adaptive_state.json")
        mp.setattr(config, "LLM_CONFIG_FILE", data / "llm_config.json")
        # 这两个模块在 import 时就把 DATA_DIR 固化成了模块常量，必须一起重定向，
        # 否则测试会读写真实的 data/llm_claims.json 与 data/patterns_runtime.json。
        mp.setattr(llm_lab, "CLAIMS_FILE", data / "llm_claims.json")
        mp.setattr(patterns_runtime, "SNAPSHOT_FILE", data / "patterns_runtime.json")
        # 丢弃指向旧（可能是生产）库的连接缓存，保证后续 get_conn() 连到临时库
        db.close()
        yield env
    finally:
        # 先断开临时库连接，再撤销补丁、并显式把真实值写回（双保险：泄漏为零）
        db.close()
        mp.undo()
        for name, value in _REAL_CONFIG.items():
            if value is not _MISSING:
                setattr(config, name, value)


@pytest.fixture(autouse=True)
def _auto_isolate_lottery_paths(isolated_env):
    """所有用例自动获得独立临时库/数据目录，不依赖测试文件字母序。"""
    yield isolated_env


@pytest.fixture
def make_draws():
    """合成开奖数据工厂：``make_draws(600)`` → 600 期升序、结构同 ``db.load_draws()``。"""

    def _factory(n: int = 600, seed: int = 42, start_year: int = 2013):
        rng = random.Random(seed)
        start = _dt.date(start_year, 1, 1)
        draws = []
        for i in range(n):
            reds = sorted(rng.sample(range(1, 34), 6))
            draws.append({
                "issue": f"{start_year + i // 150}{i % 150 + 1:03d}",
                "date": (start + _dt.timedelta(days=3 * i)).isoformat(),
                "reds": reds,
                "order": list(reds),
                "blue": rng.randint(1, 16),
            })
        return draws

    return _factory


@pytest.fixture
def seeded_db(isolated_env, make_draws):
    """把合成开奖数据写进当前临时库；返回写入的 draw 列表。"""
    from lottery import db

    def _seed(n: int = 600, seed: int = 42):
        draws = make_draws(n=n, seed=seed)
        db.upsert_draws(draws)
        return draws

    return _seed
