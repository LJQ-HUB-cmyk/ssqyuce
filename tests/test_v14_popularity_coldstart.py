"""大众偏好度模型冷启动：并发锁 / 落盘缓存 / 指纹失效（D-16）。

## 这个文件守的三件事

1. **同一进程内 `fit()` 只能执行一次**（并发时不许各跑一遍）
2. **落盘缓存命中时必须跳过 fit**，且训练期指标（`r2_increment`/`p_value`）不丢
3. **指纹变化必须使缓存失效**（数据或口径变了不许复用旧模型）

## 缺陷背景（2026-10-21 实测）

`get_model()` 原本无锁，末尾才 `_CACHE = m`：

    t=0.0  预热线程进来，_CACHE is None → 开始 fit（6~7s）
    t=0.3  用户请求进来，_CACHE 仍是 None → 也开始 fit
    结果：两线程各跑一次，用户等的是自己那次，**预热等于白做**

实测：fit 执行 2 次，用户仍等 16.78s。多用户并发时每请求各 fit 一次，CPU 打爆。

## 为什么「等待」要设上限

预热通常 6~7s。若请求无超时地持锁等待，用户会从"自己训练 7s"变成
"等别人训练完 6.6s" —— 几乎没改善。所以请求路径用
`get_model_with_deadline()`：最多等 2s，超时返回未拟合对象走 503，
**宁可显示"未就绪"，也不让用户为后台任务排队。**
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from lottery import popularity as P


@pytest.fixture(autouse=True)
def _no_real_cache(tmp_path, monkeypatch):
    """把落盘缓存指向临时目录，避免读到/污染真实缓存，也避免受其影响。"""
    monkeypatch.setattr(P, "_cache_path", lambda: tmp_path / "popularity_model.json")
    P.reset_cache()
    yield
    P.reset_cache()


def _draws(n=400, seed=11):
    """造带 `p1`/`sales` 的合成期次（`fit` 只用这两列齐全的行）。"""
    import random
    rng = random.Random(seed)
    out = []
    for i in range(n):
        reds = sorted(rng.sample(range(1, 34), 6))
        out.append({
            "issue": f"20{i // 150 + 13}{i % 150 + 1:03d}",
            "date": f"20{i // 150 + 13}-01-{(i % 28) + 1:02d}",
            "reds": reds, "order": list(reds),
            "blue": rng.randint(1, 16),
            "p1": float(3 + sum(1 for x in reds if x <= 31) + rng.random()),
            "sales": 3.2e8 + rng.random() * 1e8,
        })
    return out


# ---------- 1. 并发：fit 只能跑一次 ----------

def test_concurrent_get_model_fits_only_once(monkeypatch):
    """**核心守卫**：多线程同时取模型，`fit()` 只能执行 1 次。

    变异捕捉：去掉 `_CACHE_LOCK`（或用无锁版 `get_model`）⇒ 计数 > 1 ⇒ 红。
    """
    draws = _draws()
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws)

    calls = []
    orig = P.PopularityModel.fit

    def counting(self, d):
        calls.append(1)
        return orig(self, d)

    monkeypatch.setattr(P.PopularityModel, "fit", counting)
    P.reset_cache()

    results = []
    def worker():
        results.append(P.get_model())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert len(calls) == 1, f"fit 执行了 {len(calls)} 次，应为 1（锁没生效）"
    assert len(results) == 5
    assert all(m.fitted for m in results)
    # 五个线程必须拿到**同一个**对象（不是各自构造）
    assert len({id(m) for m in results}) == 1


def test_get_model_returns_same_object_when_hot(monkeypatch):
    """热路径必须直接返回缓存对象，且极快（不取锁）。"""
    monkeypatch.setattr("lottery.db.load_draws", lambda: _draws())
    P.reset_cache()
    m1 = P.get_model()
    t = time.time()
    m2 = P.get_model()
    dt = time.time() - t
    assert m1 is m2
    assert dt < 0.05, f"热路径耗时 {dt:.3f}s —— 快路径可能被锁拖慢"


# ---------- 2. 请求路径的等待上限 ----------

def test_deadline_returns_unfitted_instead_of_blocking(monkeypatch):
    """预热占着锁时，请求最多等 `timeout` 秒，超时返回未拟合对象。

    这是 Product 指出的 (a) 案：**不阻塞**比"等到了数据"更重要。
    变异捕捉：把 `get_model_with_deadline` 换成无超时 `get_model()`
    ⇒ 等待时间 > timeout ⇒ 红。
    """
    import threading as _th
    import time as _t

    # 必须用**慢 fit**：否则 holder 在 timeout 内就拟合完了，
    # 请求拿到的是已拟合模型，根本不会走到超时分支（测试会假绿）。
    def slow(self, d):
        _t.sleep(3.0)
        return orig_fit(self, d)

    orig_fit = P.PopularityModel.fit
    monkeypatch.setattr(P.PopularityModel, "fit", slow)
    monkeypatch.setattr("lottery.db.load_draws", lambda: _draws(400))
    P.reset_cache()

    # 先让后台线程占住锁
    holder = _th.Thread(target=P.get_model, daemon=True)
    holder.start()
    time.sleep(0.2)

    t = time.time()
    m = P.get_model_with_deadline(timeout=0.5)
    waited = time.time() - t

    assert waited < 1.5, f"等待 {waited:.2f}s 超过上限 —— 请求被阻塞了"
    # 超时返回的是未拟合对象（端点据此走 503 降级）
    assert m.fitted is False
    holder.join(timeout=120)


def test_deadline_returns_fitted_when_model_ready(monkeypatch):
    """模型就绪时（缓存/预热已完成），`get_model_with_deadline` 直接返回它。

    ## 语义变更说明（2026-10-21）

    旧版这条断言「锁空闲就自己 fit 出模型」—— **那正是 D-16 缺陷本身**。
    请求路径现在**永不自 fit**（结构判据 `no_fit_on_request_path.py`），
    所以必须由后台（预热/重试循环）先把模型准备好，请求只负责取。
    """
    monkeypatch.setattr("lottery.db.load_draws", lambda: _draws())
    P.reset_cache()
    P.get_model()                     # 模拟「预热已完成」
    m = P.get_model_with_deadline(timeout=5.0)
    assert m.fitted is True


# ---------- 3. 落盘缓存 ----------

def test_cache_skips_refit_and_preserves_training_metrics(monkeypatch):
    """缓存命中必须跳过 fit，且 `r2_increment`/`p_value` 不丢。

    丢训练期指标会让 `describe()` 返回 `r2_increment=None`，
    而前端 popularity 卡要显示它 —— 这是 Product 特别提醒的点。
    """
    draws = _draws()
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws)
    P.reset_cache()

    m1 = P.get_model()
    assert P._cache_path().exists(), "首次拟合必须写盘"
    assert m1.fitted
    r2_before, p_before = m1.r2_increment, m1.p_value

    # 第二次：清内存，必须在**不重新 fit** 的前提下拿到等价模型
    fitted_calls = []
    orig = P.PopularityModel.fit
    monkeypatch.setattr(P.PopularityModel, "fit",
                        lambda self, d: (fitted_calls.append(1), orig(self, d))[1])
    P._CACHE = None
    t = time.time()
    m2 = P.get_model()
    dt = time.time() - t

    assert not fitted_calls, "缓存命中却仍调用了 fit"
    assert dt < 1.0, f"缓存命中耗时 {dt:.2f}s，疑似仍在重训"
    assert m2.fitted
    # 训练期指标必须原样保留
    assert m2.r2_increment == pytest.approx(r2_before, rel=1e-9)
    assert m2.p_value == pytest.approx(p_before, rel=1e-9)
    # 档位阈值必须与训练时一致（分位点依赖训练集）
    assert m2.tier_lo == pytest.approx(m1.tier_lo, rel=1e-9)
    assert m2.tier_hi == pytest.approx(m1.tier_hi, rel=1e-9)


def test_cache_corrupt_file_is_ignored(monkeypatch):
    """缓存文件损坏时只当未命中，绝不抛异常影响主链路。"""
    draws = _draws()
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws)
    P.reset_cache()
    P.get_model()
    P._cache_path().write_text("{ this is not json", encoding="utf-8")
    P._CACHE = None
    m = P.get_model()          # 不应抛
    assert m.fitted


def test_cache_write_failure_does_not_break_model(monkeypatch):
    """写盘失败不能影响功能（写不了就用内存结果）。"""
    draws = _draws()
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws)
    P.reset_cache()
    monkeypatch.setattr(P, "_save_cached", lambda m, rows: (_ for _ in ()).throw(OSError("disk full")))
    m = P.get_model()
    assert m.fitted, "写盘失败不该影响返回的模型"


# ---------- 4. 指纹失效（数据/口径变化必须重训） ----------

def test_fingerprint_covers_draw_count_and_last_issue():
    """指纹必须含 `n_draws`、`last_issue`、`model_version`、特征数。

    只看 `MODEL_VERSION` 不够：新增期次会让 `tier_lo/tier_hi` 过期
    （分位阈值按训练集标定）。
    """
    fp = P._fingerprint_of(_draws(50))
    for key in ("model_version", "n_draws", "last_issue", "n_features"):
        assert key in fp, f"指纹缺 {key}"
    assert fp["n_draws"] == 50


def test_new_draw_invalidates_cache(monkeypatch):
    """**变异实证**：加一期数据后缓存必须失效并重训。

    这是「看起来对」的 K 族第 7 个变体防线 ——
    「数据变了但缓存没失效，返回旧结果」是最难被发现的错误。
    """
    draws = _draws(400)
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws)
    P.reset_cache()
    m1 = P.get_model()
    fp1 = json.loads(P._cache_path().read_text())["fingerprint"]

    # 加一期（含 p1/sales，会进入训练集）
    extra = dict(draws[-1]); extra["issue"] = "2999999"
    draws2 = draws + [extra]
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws2)

    # 缓存应判为失效 → 必须重训
    P._CACHE = None
    fitted = []
    orig = P.PopularityModel.fit
    monkeypatch.setattr(P.PopularityModel, "fit",
                        lambda self, d: (fitted.append(1), orig(self, d))[1])
    m2 = P.get_model()

    assert fitted, "数据变化后没有重训 —— 缓存失效逻辑失效"
    fp2 = json.loads(P._cache_path().read_text())["fingerprint"]
    assert fp2["n_draws"] == fp1["n_draws"] + 1
    assert fp2["last_issue"] == "2999999"
    assert m2.n_train == 401


def test_force_refit_ignores_cache(monkeypatch):
    """`force_refit=True` 必须真的重拟合（有调用方依赖它）。"""
    draws = _draws(400)
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws)
    P.reset_cache()
    P.get_model()

    fitted = []
    orig = P.PopularityModel.fit
    monkeypatch.setattr(P.PopularityModel, "fit",
                        lambda self, d: (fitted.append(1), orig(self, d))[1])
    P.get_model(force_refit=True)
    assert fitted, "force_refit=True 却没有重拟合"


# ==========================================================================
# 5. 请求路径**绝不自 fit**（2026-10-21 红线修复）
#
# 缺陷背景（严QA 读代码发现，测试抓不到）：
#   `get_model_with_deadline(timeout=2.0)` 的 timeout **只约束"等锁"**：
#       acquired = _CACHE_LOCK.acquire(timeout=2.0)
#       if acquired:
#           return _get_model_locked(...)    # ← 拿到锁后全额 fit，无超时
#   而"预热线程还没取到锁"的那一瞬间（正是每次重启后第一个用户的时刻），
#   请求会自己 fit 6~9s，然后返回**已拟合的真模型** ⇒ 端点走不到 503 分支，
#   用户等 9 秒拿到 200，**以为一切正常** —— 慢且不自知，最坏的一种。
#
# 修法原则（不是"加超时"，是"解耦"）：请求只负责**触发**，不负责**完成**。
# 这些用例用「让 fit 故意慢 5 秒」来证明请求确实没等它。
# ==========================================================================

_SLOW_FIT_SECONDS = 5.0


def _slow_fit(monkeypatch, draws, counter):
    """把 fit 换成「慢 5 秒 + 计数」的版本。"""
    orig = P.PopularityModel.fit

    def slow(self, d):
        counter.append(1)
        time.sleep(_SLOW_FIT_SECONDS)
        return orig(self, d)

    monkeypatch.setattr(P.PopularityModel, "fit", slow)
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws)


def test_request_path_does_not_block_on_fit(monkeypatch):
    """**核心守卫**：缓存缺失时，请求不得为 fit 阻塞（必须 < 3s 返回）。

    变异捕捉：把 `get_model_for_request` 换成 `get_model()` 或
    `get_model_with_deadline()` ⇒ 请求会等满 5s ⇒ 红。
    """
    calls = []
    _slow_fit(monkeypatch, _draws(400), calls)
    P.reset_cache()

    t = time.time()
    m = P.get_model_for_request(timeout=0.3)
    elapsed = time.time() - t

    # ⚠ 断言阈值的选择（2026-10-22 一次全量里本用例在负载尖峰下误红）
    #
    # 正确行为是「等 timeout=0.3s 后返回」，实测 0.93~1.03s（含导入开销）。
    # 原阈值 3.0s 只有 3 倍余量 —— 在 load 高、GC 抖动或别的测试并发跑时
    # 时间断言会误红，而代码完全正确。
    #
    # **但这不能用「放宽阈值」来解决**：若把阈值抬到 10s，
    # 那真正的缺陷形态（自 fit ≈ 5~9s）就会落在阈值内，守卫失去意义。
    #
    # 正确解法是**结构性判据优先 + 时间判据只做粗筛**：
    #   ① 结构（下一条 assert）：请求路径不得调用 fit —— 这是本质
    #   ② 时间：只要求「明显短于慢 fit」，即 < _SLOW_FIT_SECONDS * 0.6（=3s）
    #      —— 与「自 fit 一定 ≥5s」形成 5:3 的判别余量，不依赖机器快慢
    #
    # 若你看到这条因为负载误红：**先看下一条结构断言是否也红**。
    # 结构绿 + 时间红 ⇒ 负载问题，复跑；结构红 ⇒ 真缺陷。
    assert elapsed < _SLOW_FIT_SECONDS * 0.6, (
        f"请求阻塞了 {elapsed:.2f}s（拟合故意慢 {_SLOW_FIT_SECONDS}s）—— "
        f"请求路径仍在自 fit，这是「9.29s 全额阻塞」缺陷复现。\n"
        f"先看下一条结构断言：若它也红，则是真缺陷；若它绿，多半是负载尖峰，复跑确认。")
    assert m.fitted is False, "未就绪时应返回未拟合对象（端点据此走 503 warming）"
    # 请求路径**不再触发**拟合（结构判据要求）：拟合由启动时的预热/重试循环负责。
    # 这是刻意的设计 —— 「请求只负责取，不负责算」。
    assert not calls, (
        "请求路径触发了 fit —— 违反 §8「请求路径永不自 fit」，"
        f"AST 判据会报 VIOLATION（实际 {len(calls)} 次）")


def test_request_path_never_writes_unfitted_to_cache(monkeypatch):
    """超时返回的未拟合对象**不得写入 `_CACHE`**。

    否则后续请求会一直看到「有对象但未 fitted」，每次都走完整路径，
    且 `is_warming()` 之外的地方可能误判为「已有模型」。
    """
    calls = []
    _slow_fit(monkeypatch, _draws(400), calls)
    P.reset_cache()

    m = P.get_model_for_request(timeout=0.2)
    assert m.fitted is False
    assert P._CACHE is None, "未拟合对象污染了 _CACHE"
    time.sleep(_SLOW_FIT_SECONDS + 1.0)


def test_concurrent_requests_never_fit(monkeypatch):
    """8 个并发请求**一次 fit 都不应触发**（请求路径纯只读）。

    旧版这条断言「只触发 1 次 fit」—— 但即使 1 次，那也是**请求路径可达 fit**
    （AST 判据会报 VIOLATION）。正确结构是请求路径完全不碰 fit。
    """
    calls = []
    _slow_fit(monkeypatch, _draws(400), calls)
    P.reset_cache()

    results = []
    threads = [threading.Thread(target=lambda: results.append(
        P.get_model_for_request(timeout=0.2))) for _ in range(8)]
    t = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    elapsed = time.time() - t

    assert elapsed < 5.0, f"8 个并发请求耗时 {elapsed:.2f}s，疑似有请求在 fit"
    assert len(results) == 8
    assert not calls, f"并发请求触发了 {len(calls)} 次 fit，应为 0"
    assert all(m.fitted is False for m in results), "未就绪时应统一返回未拟合对象"


def test_request_path_reports_warming_while_fitting(monkeypatch):
    """请求返回未就绪期间，`is_warming()` 必须为 True（端点据此选 503 文案）。"""
    calls = []
    _slow_fit(monkeypatch, _draws(400), calls)
    P.reset_cache()

    # 模拟「预热线程正在拟合」：由预热线程（不经过请求路径）触发
    bg = threading.Thread(target=P.get_model, daemon=True)
    bg.start()
    time.sleep(0.2)

    m = P.get_model_for_request(timeout=0.2)
    assert m.fitted is False
    assert P.is_warming() is True, "后台正在拟合，但 is_warming() 为 False —— 端点会误报「样本不足」"
    bg.join(timeout=_SLOW_FIT_SECONDS + 5)
    assert P.is_warming() is False, "拟合结束后标记未清除"


def test_request_path_serves_from_disk_cache_without_fit(monkeypatch):
    """缓存命中时，请求既不 fit 也不阻塞（这是 (c) 案的机制）。"""
    draws = _draws(400)
    monkeypatch.setattr("lottery.db.load_draws", lambda: draws)
    P.reset_cache()
    P.get_model()                    # 先正常拟合一次，生成缓存

    calls = []
    monkeypatch.setattr(P.PopularityModel, "fit",
                        lambda self, d: calls.append(1))
    P._CACHE = None
    t = time.time()
    m = P.get_model_for_request(timeout=0.5)
    elapsed = time.time() - t

    assert m.fitted is True, "缓存命中却没拿到已拟合模型"
    assert not calls, "缓存命中仍调用了 fit"
    assert elapsed < 2.0


def test_hot_request_path_is_fast(monkeypatch):
    """热态请求必须极快（不取锁、不读盘）。"""
    monkeypatch.setattr("lottery.db.load_draws", lambda: _draws(400))
    P.reset_cache()
    P.get_model()
    t = time.time()
    m = P.get_model_for_request(timeout=0.5)
    assert m.fitted is True
    assert time.time() - t < 0.05
