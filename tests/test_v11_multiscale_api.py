"""V11 多尺度扫描 API + 前端卡测试。

规格：``lottery/multiscale.py`` 的算法经 ``tests/test_v11_multiscale.py`` 验证；
本文件只验证 **API 层与前端接线**，不重复验证统计语义。

核心要守住的：
1. 两个端点可用、返回体**无 numpy 对象**（前端直接消费）；
2. ``mde-table`` 必须覆盖使用中的档位，且相对 MDE 量级正确；
3. ``scan`` 的 ``only_interesting`` 参数生效；
4. 前端卡片存在、``?v=`` 已升、**MDE 与"显著但不可信"的展示不可缺失**
   （这是本卡存在的意义：防止用户误读短窗口的"显著"）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from lottery.api_app import app
    return TestClient(app)


@pytest.fixture()
def client_with_data(seeded_db):
    """带合成开奖数据的 client。

    ``scan`` 端点需要历史数据才能算各档效应（conftest 的隔离 fixture 默认给空库，
    直接调用会得到 400「本地暂无开奖数据」）。
    用 600 期合成数据足以覆盖 40/60/150/300 四个窗口。
    """
    from lottery.api_app import app
    seeded_db(n=600, seed=42)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. MDE 对照表端点
# ---------------------------------------------------------------------------

def test_mde_table_ok_and_shape(client):
    r = client.get("/api/multiscale/mde-table")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert isinstance(d["rows"], list) and len(d["rows"]) >= 5
    assert d["alpha"] == 0.05 and d["power"] == 0.8


def test_mde_table_is_monotone_in_window(client):
    """窗口越长 MDE 越小 —— 这是「短窗口不等于更容易找规律」的量化依据。"""
    d = client.get("/api/multiscale/mde-table").json()
    rows = {r["n"]: r["mde_rel"] for r in d["rows"]}
    for short, long_ in ((30, 40), (40, 60), (60, 100), (100, 150), (150, 300)):
        if short in rows and long_ in rows:
            assert rows[short] > rows[long_], (short, long_, rows)


def test_mde_table_flags_scales_in_use(client):
    """使用中的档位必须被标记（前端据此高亮）。"""
    d = client.get("/api/multiscale/mde-table").json()
    in_use = [r["n"] for r in d["rows"] if r["in_use"]]
    assert in_use, "没有任何档位被标记为 in_use"
    # 全部期数档在 scale 定义里 window=None，故 3515 不necessarily 在 in_use
    assert set(in_use) & {40, 60, 150, 300}


def test_mde_table_carries_scale_legend(client):
    """必须带档位说明，否则前端无法渲染"这一档是干什么的"。"""
    d = client.get("/api/multiscale/mde-table").json()
    assert len(d["scales"]) == 5
    for s in d["scales"]:
        assert s["label_zh"] and s["role_zh"]


def test_mde_table_has_honest_note(client):
    d = client.get("/api/multiscale/mde-table").json()
    assert "MDE" in d["note_zh"]
    assert "短" in d["note_zh"] or "窗口越短" in d["note_zh"]


def test_mde_table_no_numpy_objects(client):
    """返回体必须能 json.dumps（TestClient 已序列化，再确认一次无 numpy 痕迹）。"""
    body = client.get("/api/multiscale/mde-table").text
    for bad in ("np.float64", "np.int64", "ndarray"):
        assert bad not in body, bad


# ---------------------------------------------------------------------------
# 2. 扫描端点
# ---------------------------------------------------------------------------

def test_scan_ok_and_no_numpy(client_with_data):
    r = client_with_data.get("/api/multiscale/scan?only_interesting=false")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["n_scanned"] >= 5
    json.dumps(d)
    assert "np.float64" not in r.text


def test_scan_only_interesting_filters_none(client_with_data):
    """默认 only_interesting=True 时不应包含 recommendation=none 的条目。"""
    d = client_with_data.get("/api/multiscale/scan").json()
    assert all(x.get("recommendation") != "none" for x in d["results"])


def test_scan_all_includes_none_entries(client_with_data):
    d = client_with_data.get("/api/multiscale/scan?only_interesting=false").json()
    # 真实数据上绝大多数维度应是 none；这里只要求"能返回结果"
    assert d["results"]
    assert all("recommendation" in x for x in d["results"])


def test_scan_accepts_scale_subset(client_with_data):
    """scales 参数应能裁剪档位。"""
    d = client_with_data.get("/api/multiscale/scan?scales=all,long&only_interesting=false").json()
    assert isinstance(d["results"], list)
    for item in d["results"]:
        if item.get("error"):
            continue
        assert set(item["scales"]).issubset({"all", "long"}), item["scales"]


def test_scan_invalid_scale_falls_back_to_all(client_with_data):
    """非法 scales 应回退到全部档位，而不是报错或返回空。"""
    d = client_with_data.get("/api/multiscale/scan?scales=not_a_scale").json()
    assert d["ok"] is True


def test_scan_result_entries_carry_verdict_and_mde(client_with_data):
    """每个非不足档位都必须带 verdict 与 mde_abs —— 前端展示依赖这两个字段。"""
    d = client_with_data.get("/api/multiscale/scan?only_interesting=false").json()
    checked = 0
    for item in d["results"]:
        for k, v in (item.get("scales") or {}).items():
            if v.get("verdict") == "insufficient":
                continue
            assert "verdict" in v and "mde_abs" in v, (item.get("key"), k, v)
            assert v["verdict"] in ("credible", "underpowered", "not-significant")
            checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------
# 3. 前端接线（防止"后端做了、前端没展示"）
# ---------------------------------------------------------------------------

def _read(path: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")


def test_frontend_has_multiscale_card():
    html = _read("web/index.html")
    assert 'id="multiScaleCard"' in html
    assert 'id="multiScaleBody"' in html


def test_frontend_discloses_mde_reason():
    """卡片必须写明"短窗口检出下限更高"—— 这是本卡存在的意义，不能省。"""
    html = _read("web/index.html")
    assert "MDE" in html or "检出下限" in html
    assert "35%" in html or "+35" in html


def test_frontend_renders_verdict_states():
    """前端必须区分 credible / underpowered 三种判定（否则用户看不出"不可信"）。"""
    js = _read("web/static/app.js")
    assert "credible" in js
    assert "underpowered" in js
    assert "功效不足" in js


def test_frontend_mde_table_is_fetched():
    js = _read("web/static/app.js")
    assert "/api/multiscale/mde-table" in js
    assert "/api/multiscale/scan" in js


def test_frontend_version_bumped():
    """?v= 必须升到 20261018.7（否则用户拿到的是缓存里的旧前端）。"""
    html = _read("web/index.html")
    assert "20261018.7" in html
    assert "20261018.6" not in html
