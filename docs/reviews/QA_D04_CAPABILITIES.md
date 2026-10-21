# D-04 验收报告：能力清单端点 `/api/capabilities`

> 验收人：严QA（独立验收）｜日期：2026-10-21
> 被验对象：`lottery/routes_capabilities.py`（376 行，未跟踪新文件）+ `tests/test_d04_capabilities_api.py`（257 行，21 条）
> 基准：工作区（HEAD `db634e3` + 未提交改动）
> 隔离副本：`/tmp/qa/ws_now`

---

## 结论：❌ **打回**

端点的**结构设计是对的**（constants 确实实时读取，五个值全部正确，失败会上报而非静默错）。
但**「constants 必须由代码实时读取」这条验收标准，测试只守住了一半**：

- ✅ `alpha` / `single` / `cross` / `ev_sixth`：4 个变异的捕获**已验证**（P2、P3 各 2 failed）
- ❌ **`ev_single` 的守卫是空的**：把它写死成任意值（含贴着区间上界的 0.6499）**21 条测试全绿**

`ev_single` 恰恰是**唯一依赖奖金假设、最容易漂移**的那个数字，也是用户在页面上最可能看到的一个。
**最该守的没守住。**

---

## 1. 已验证的部分（先肯定，这部分是真的）

### 1.1 端点真的能调通（红线 3：必须实际调用）

```python
from fastapi.testclient import TestClient
from lottery.api_app import app
TestClient(app).get("/api/capabilities")
```
```
HTTP 200
constants = {
  "alpha": 0.05,
  "single": 306,
  "cross": 72350,
  "ev_single": 0.531925,
  "ev_sixth": 0.15625
}
```

**五个值与红线表比对**：

| 项 | 红线表 | 端点实测 | 判定 |
|---|---|---|---|
| α | 0.05 | 0.05 | ✅ 一致 |
| single | 306 | 306 | ✅ 一致 |
| cross | 72350 | 72350 | ✅ 一致 |
| ev_sixth | 15.625% | 0.15625 | ✅ 一致 |
| **ev_single** | **52.3%** | **0.531925（53.19%）** | ⚠️ **差 0.89 个百分点，见 §3** |

### 1.2 `constants` 确实不是写死的（我读了源码）

```python
out["alpha"]   = float(EP.DEFAULT_ALPHA)                    # 符号引用
out["single"]  = int(CS.total_single())                     # 函数调用
out["cross"]   = int(CS.total_cross())                      # 函数调用
out["ev_single"] = round(float(ev["ev_ratio"]), 6)          # 函数调用
out["ev_sixth"] = round(float(BC.optimize(1)["ev_ratio_sixth"]), 6)
```
五个值**全部来自符号或函数调用，无字面量** —— 这一条**做得对**，符合齐交付的要求。
且每个值都带 `sources`（出处符号）、失败时 `errors` 如实上报、`None` 而非"看起来正常的错值"。

### 1.3 变异验证：4 个常量守得住

| # | 变异 | 结果 | 判定 |
|---|---|---|---|
| P2 | `alpha` 硬编码 `0.10`（放宽显著性门槛） | **2 failed**（`test_constants_match_redline_table`、`test_constants_are_read_live_not_hardcoded`） | ✅ 捕获 |
| P3 | `cross` 硬编码 `12000`（缩小搜索空间） | **2 failed**（`test_constants_match_redline_table`、`test_constants_failure_is_reported_not_silently_wrong`） | ✅ 捕获 |
| P1 | `ev_single` → `0.60` | **21 passed 全绿** | ❌ **存活** |
| P4 | `ev_single` → `0.6499`（贴上界） | **21 passed 全绿** | ❌ **存活** |

---

## 2. 打回理由：`ev_single` 的守卫是空的

### 2.1 断言实为区间，等于没锁

`tests/test_d04_capabilities_api.py:127-132`：

```python
# ev_single 依赖奖金假设，区间断言防止它被悄悄改成「好看」的值
assert consts["ev_single"] is not None
assert 0.40 <= consts["ev_single"] <= 0.65, (...)
```

**注释写着"防止被悄悄改成好看的值"，而这条断言允许的值域是 40%~65%。**
单注返奖比在这个区间内的任何值都能通过 —— 包括 `0.6499`（用户看的会是 64.99% 回收，接近"稳赚"的观感）。

**这是本项目记录过的「恒真断言」的同一类问题**（`x or True` 是极端形态，区间过宽是同一种病）：
**断言必须能区分"正确实现"与"错误实现"，区分不了的断言等于注释。**

### 2.2 「读实时值」守卫的盲区：`monkeypatch` 打不到 `ev_single` 的依赖

`test_constants_are_read_live_not_hardcoded` 的机制是对的：
```python
monkeypatch.setattr(EP, "DEFAULT_ALPHA", 0.123)
monkeypatch.setattr(CS, "total_single", lambda: 999)
→ 断言端点跟着变
```

**但它只覆盖了 `alpha` 和 `single`**。
`ev_single` 的依赖是 **`_PRIZE_ASSUMPTION` 这个模块级字典**（`routes_capabilities.py:49`）+ `engine._single_ticket_ev`，
**没有任何测试篡改它们再验证端点跟着变**。

### 2.3 我构造的决定性实验（证明盲区真实存在）

**实验 A（P5）：把 `ev_single` 写死为 `0.531925`（与今日实时值一模一样）**
```
→ 21 passed 全绿
```
写死也能过 —— 因为写死的值恰好等于正确值，区间断言无从分辨。

**实验 B：在 P5 基础上，把一等奖奖金假设从 684 万改成 1500 万（模拟"奖金假设漂移"）**

真值（我独立算）：
```python
engine._single_ticket_ev({"first": 6840000,  "second": 205000}, sixth="published")["ev_ratio"]  → 0.531925
engine._single_ticket_ev({"first": 15000000, "second": 205000}, sixth="published")["ev_ratio"]  → 0.762159
```
端点实际返回：
```
改奖金假设后 ev_single = 0.531925   （写死了，所以不变）
```
**真值 0.7622，端点返回 0.5319，偏差 43% —— 而 21 条测试全绿。**

**这正是本项目最怕的失败形态**：文档写一个值、代码算一个值，两边漂移而测试看不出 ——
也就是 `routes_capabilities.py:13` 自己写在文件头的那句话：
> 「项目历史上多次出现「文档写一个值、代码算另一个值」—— 本端点的存在就是为了终结它。」

**它自己成了那个漂移源，而它自己的测试抓不住。**

---

## 3. 附带发现：`ev_single = 53.19%` 与红线表 `52.3%` 不一致

不是 bug，是**口径未声明**，但会直接造成 D-01 前端展示矛盾。

```python
engine._single_ticket_ev(PA, sixth="published")["ev_ratio"]  → 0.531925  (53.19%)
engine._single_ticket_ev(PA, sixth="strict")["ev_ratio"]     → 0.522907  (52.29%)
```

- `routes_capabilities.py:102` 用的是 **`sixth="published"`** → **53.19%**
- 红线表（`BOARD.md §0`）与 `popularity.py:42`、`blue_coverage.py:32` 写的是 **52.3%**（≈ strict 口径）

**影响**：D-01 要求前端同屏披露"52.3%"，而 `/api/capabilities` 会返回 53.19%。
若前端两处都用，用户会看到**同一系统两个返奖比**。这正是"口径一致性"要防的事。

**建议**（二选一，必须书面声明）：
1. 端点改用 `sixth="strict"`，与红线表统一为 52.29%（我推荐 —— 红线表是对外承诺）；
2. 端点保留 published，但在 `constants_meta` 里明确标注 `ev_single_convention: "published"`，
   并在 `cannot` 段同时给出 strict 值，前端**只展示 strict**。

---

## 4. 三态表

| 项 | 状态 | 证据 |
|---|---|---|
| 端点存在且返回 200 | **已验证** | `TestClient(app).get("/api/capabilities")` → HTTP 200 |
| `alpha/single/cross/ev_sixth` 实时读取 | **已验证** | P2、P3 变异各 2 failed；源码无字面量 |
| 失败时如实上报（`None` + `errors`） | **已验证** | P3 触发 `test_constants_failure_is_reported_not_silently_wrong` |
| **`ev_single` 实时读取** | ❌ **有风险（实证为假）** | P1/P4/P5 三变异全绿；奖金假设改 684万→1500万，真值 0.7622 而端点仍 0.5319 |
| `ev_single` 与红线表 52.3% 一致 | ❌ **不一致** | 端点 0.531925（published）vs 红线 52.3%（strict） |
| 路由已注册 | **已验证** | `api_app.py:271 app.include_router(routes_capabilities.router)` |
| 前端调用 `/api/capabilities` 且首屏可见 | **未验证** | 本轮未测，见 §5 |

---

## 5. 本轮未验的部分（明确标注，不判通过）

| 项 | 原因 |
|---|---|
| 前端是否调用 `/api/capabilities` 且**首屏可见**（验收标准 4） | 前端仍在高频改动中，需等 D-01/D-05/D-06 落地后一并验 |
| `cannot` 段必须包含四项（不可预测/期望为负/六等奖/蓝球覆盖只改频率）（验收标准 2） | 见 §5.1，**部分已核** |
| 端点不得出现禁用词、`test_v14_naming_discipline.py` 覆盖它（验收标准 5） | 命名守卫本身在返工中（见 `QA_D12_NAMING_GUARD.md`） |

### 5.1 `cannot` 段部分核对（顺手做的）

源码里能看到四项都在（`routes_capabilities.py:209-234`）：不可预测、期望为负（指向 `constants.ev_single`）、
六等奖 15.625%、蓝球覆盖只改频率不改回报率。**但** `:234` 有一句：

```
"evidence": "10 注：47.55% → 62.50%，成本 20 元 / 期望回收 3.125 元 / 返奖比 15.625%"
```

`期望回收 3.125 元` 与 `成本 20 元 / 返奖比 15.625%` 自洽（20 × 0.15625 = 3.125）✅ **正确**。
（对比：看板 D-01 的 `16.73 元` 是错的，已单独打回 —— **这条端点的数字反而是对的**。）

---

## 6. 修复建议（可直接交给李工）

```python
# 1) 收紧 ev_single 断言 —— 与红线表口径对齐后锁死
def test_ev_single_matches_redline(monkeypatch):
    """ev_single 必须精确等于当前奖金假设下的计算值（不是区间）。"""
    from lottery import engine, routes_capabilities as C
    expect = engine._single_ticket_ev(C._PRIZE_ASSUMPTION, sixth="strict")["ev_ratio"]
    assert C.capabilities()["constants"]["ev_single"] == pytest.approx(round(expect, 6), abs=1e-9)

# 2) 补「奖金假设漂移」守卫（P5 实验的捕捉点）
def test_ev_single_tracks_prize_assumption(monkeypatch):
    from lottery import routes_capabilities as C
    monkeypatch.setitem(C._PRIZE_ASSUMPTION, "first", 15_000_000)
    v = C.capabilities()["constants"]["ev_single"]
    assert v == pytest.approx(0.762159, abs=1e-6), (
        "改了一等奖奖金假设但端点 ev_single 没变 —— 说明它是写死的字面量")
```

第 2 条是关键：**它用一个"改依赖 → 输出必须变"的正向断言，把"写死"变成可捕获的缺陷。**
现有 `test_constants_are_read_live_not_hardcoded` 的思路完全正确，只是**漏了 `ev_single` 这一个依赖**。

---

## 7. 复现命令

```bash
# 建副本
SRC=/root/DSH/应用/ssqyuce; NOW=/tmp/qa/ws_now
rm -rf $NOW && mkdir -p $NOW && cd $SRC
tar -c --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.serena --exclude=data/archive --exclude=data/backups . | tar -x -C $NOW
ln -s $SRC/.venv $NOW/.venv

# 复现 P5：写死 ev_single，测试仍全绿
cd $NOW && python - <<'EOF'
import pathlib
p = pathlib.Path("lottery/routes_capabilities.py"); t = p.read_text(encoding="utf-8")
old = '        out["ev_single"] = round(float(ev["ev_ratio"]), 6)'
assert t.count(old) == 1
p.write_text(t.replace(old, '        out["ev_single"] = 0.531925   # 写死', 1), encoding="utf-8")
EOF
.venv/bin/python -m pytest tests/test_d04_capabilities_api.py -q     # → 21 passed

# 决定性实验：改奖金假设，真值 0.762159 而端点不变
python - <<'EOF'
import pathlib
p = pathlib.Path("lottery/routes_capabilities.py"); t = p.read_text(encoding="utf-8")
p.write_text(t.replace('"first": 6840000', '"first": 15000000', 1), encoding="utf-8")
EOF
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from lottery import routes_capabilities as C
print('端点返回:', C.capabilities()['constants']['ev_single'])     # → 0.531925（不变）
"
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from lottery import engine
print('真值应为:', engine._single_ticket_ev({'first':15000000,'second':205000}, sixth='published')['ev_ratio'])  # → 0.762159
"
```

---

## 8. 我发现的、报告里没提到的

1. **`ev_single` 是唯一没被"读实时值"守卫覆盖的常量**，而它恰恰是唯一依赖外部假设、最容易漂移的。
   守卫的**设计思路是对的，只是漏了一个依赖** —— 补 10 行测试即可，不是架构问题。
2. **`routes_capabilities.py:13` 自称"终结文档与代码漂移"，但它自己对 `ev_single` 的漂移毫无防护。**
   文件头那句话需要兑现。
3. **`ev_single` 的 53.19% 与红线表 52.3% 口径不一致**（published vs strict），
   会直接造成 D-01 前端两个返奖比并存。**这是口径问题，不是数字错**，但必须在交付前定口径。
4. `:234` 的 `期望回收 3.125 元 / 成本 20 元 / 返奖比 15.625%` **算对了**（20×0.15625=3.125）——
   比看板 D-01 的 `16.73 元` 靠谱。**端点的数字质量高于验收文档的数字质量**，这个反差值得注意。
