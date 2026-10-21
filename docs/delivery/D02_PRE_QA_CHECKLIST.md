# D-02 `engine.py` 拆分 —— QA 预验收清单（动手前必读）

> 出方：严QA（独立验收） · 2026-10-21
> 定位：**在高架构动手之前**把"哪些动作会被当场判死"写清楚。
> 目的：把"事后打回"换成"事前立规"。
>
> 依据：`docs/delivery/D02_ENGINE_SPLIT_PLAN.md`（1219 行）+ 本文件全部判据均为**实测定量**，
> 每条命令可复跑。**动手前请先确认这些数字与你的方案一致。**

---

## §0 一句话总纲

**拆分允许"代码位置变化"，不允许"行为、身份、打桩能力任何一项变化"。**

三条判死红线：
1. `lottery.engine` 不得变成包（导入语义改变 → 循环导入 → 全套收集崩）
2. 拆分后 `predict_next` 输出必须与拆分前**逐字节等价**（除 `generated_at`）
3. `monkeypatch.setattr(engine, "X")` 必须仍然打到真源（防镜像双源）

---

## §1 强制判据（每条都必须有"注入缺陷 → 变红"的实证）

> **红线级纪律**：未做变异实证的守卫，**视为未验证**。
> 依据：`ev_single` 那条 `0.40 <= x <= 0.65` 宽区间断言，连"注入缺陷"这关都过不了 ——
> 它本来就什么都放行，却制造了"有守卫"的假象。

### ① `is` 恒等（防第二真源）

**要求**：拆分后，所有通过薄壳访问的对象，必须与真源**同一对象**（`is`），不是"值相等"。

**判据**：
```python
from lottery import engine
from lottery.engine import <子模块>

assert engine.predict_next is <真源>.predict_next, "薄壳与真源不是同一函数对象"
assert engine.PRIZE_CASH_FIXED is <真源>.PRIZE_CASH_FIXED
```

**必须有变异实证**：
```
注入：在薄壳里重新定义 def predict_next(...): return <真源>.predict_next(...)
期望：is 断言变红（因为不是同一对象）
```
⚠️ **仅 `==` 相等的守卫无效** —— 一个包装函数 `==` 不了，但"重新实现一遍"可能碰巧相等。

### ② 30 个未覆盖私有函数

**实测依据**：方案 §3.4 提到拆分涉及 53.6% 的代码没有监工。

**要求**：拆这 30 个私有函数之前，必须**二选一**：
- (a) 先补覆盖（哪怕是一条 smoke 断言：函数可调用、返回类型正确）；
- (b) 在交付报告里**逐个签字**："此函数无覆盖，拆了也没人守，接受风险"。

**判据**：
```bash
# 交付报告必须附这张表，缺一行即打回
grep -c "^def _\|^    def _" lottery/engine.py    # 私有函数总数
```
**不接受**："影响不大"、"这只是搬迁"。**要么有覆盖，要么有签字。**

### ③ `predict_next` 逐字节等价

**判据**：
```bash
# 拆分前后各跑一次，比对
.venv/bin/python docs/delivery/repro/d02_equivalence.py > /tmp/before.json
# ... 拆分 ...
.venv/bin/python docs/delivery/repro/d02_equivalence.py > /tmp/after.json
diff <(jq 'del(.generated_at)' /tmp/before.json) <(jq 'del(.generated_at)' /tmp/after.json)
```
**期望：无差异。**

⚠️ **已知坑（方案 §5.2 已记录）**：`generated_at` 每次都变，必须剔除后再比。
若剔除后仍有差异，**先确认不是随机种子问题**，再判缺陷。

### ④ 首改即全跑（**不许"全改完再测"**）

**要求**：**第一期改完立刻跑全量**，必须看到 `passed ≥ 1521`（当前地板）。
每期一次，不许攒。

**判据**（每一期都要贴输出）：
```bash
date -u +"观察时刻 %H:%M:%S"
set -o pipefail
.venv/bin/python -m pytest tests/ -q --ignore=tests/frontend
```
**地板：`1521 passed, 24 skipped, 1 xfailed`（2026-10-21 13:38 实测）**

⚠️ 若 `passed` 少于 1521、或 `skipped` > 24、或 `xfailed` > 1 → **立刻停手回滚**。
⚠️ **不许只看 exit code**（`| tail` 会吞掉它，见闸门 §5.2）。

### ⑤ 导入语义（**这是上次炸的根因**）

**实测现状**（`lottery/engine.py:11-12`）：
```python
from . import backtest as BT
from . import config, db, features as F, llm_client, methods as METH, ml_model, models as M
```

**判据**：`lottery/engine.py` **必须保持单文件模块**。

```python
# 这条守卫已在 tests/test_qa_self_guards.py:test_engine_is_a_single_module_not_a_package
# 每期拆分后跑：必须 passed
.venv/bin/python -m pytest tests/test_qa_self_guards.py -q
```

**为什么用 `Path.is_dir()` 而不是 import 断言**：
包转换后收集阶段就崩了，**import 断言根本跑不到**。
能捕获"收集崩溃"的测试，必须在收集阶段之前就能跑。

### ⑥ 延迟导入位置（7 处，必须原样留在函数体内）

**实测清单**（`grep -n "from \." lottery/engine.py`，当前 3107 行版本）：

| 行号 | 语句 | 所在函数 |
|---|---|---|
| `60` | `from . import shape_rules as _SR` | 函数体内（`constraint_ctx`，4 空格缩进，已实测确认） |
| `559` | `from . import patterns_runtime` | 函数体内 |
| `578` | `from . import evaluate as EV` | 函数体内 |
| `597` | `from . import discovery` | 函数体内 |
| `657` | `from . import patterns_runtime` | 函数体内 |
| `1045` | `from . import ai_audit` | 函数体内 |
| `2528` | `from . import patterns_runtime` | 函数体内 |

**判据**：这 7 处**必须原样留在函数体内**，提到顶层**立刻成环**。

**必须有变异实证**：
```
注入：把 :597 的 from . import discovery 提到模块顶层
期望：import lottery.engine 失败（循环导入）
```

### ⑦ 打桩穿透（**拆分最容易漏的一条**）

**风险（方案 §⑥ 自报）**：`predict_next` 若早期绑定，`test_v10_cost.py:438` 的
`monkeypatch.setattr` 会失效，表现为**单跑绿、全量红**。

**这是本项目记录过的镜像双源病**（历史案例：`monkeypatch` 只改薄壳、真源未变）。

**判据**：
```python
# 在拆分后的结构上实测
from lottery import engine
monkeypatch.setattr(engine, "llm_tickets", fake)
assert <被拆出的模块>.llm_tickets is fake, "打桩只改了薄壳，真源未变"
```

**必须有变异实证**：
```
注入：让被拆出的子模块在 import 时 from .engine import llm_tickets（早期绑定）
期望：上述断言变红
```

⚠️ **必须"单跑 + 全量"两次都验**：
```bash
.venv/bin/python -m pytest tests/test_v10_cost.py -q          # 单跑
.venv/bin/python -m pytest tests/ -q --ignore=tests/frontend  # 全量
```
**单跑绿、全量红 = 典型的打桩穿透失败**，直接打回。

---

## §2 逐期验收表（每期填一次，缺项打回）

| 期次 | commit | ① is恒等 | ② 未覆盖签字 | ③ 等价 | ④ 全跑数字 | ⑤ 非包 | ⑥ 延迟导入 | ⑦ 打桩 | 判定 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | | | | | | | | | |
| 2 | | | | | | | | | |
| … | | | | | | | | | |

**任一期任一项未填 = 该期未通过。**

---

## §3 我承诺做的验证（你不用自证，我来打）

| 项 | 我的做法 |
|---|---|
| 逐字节等价 | **我自己跑** `d02_equivalence.py`，不用你的输出 |
| 打桩穿透 | 我注入早期绑定，看是否变红 |
| 导入语义 | 我把 `engine.py` 复制成包，看守卫是否变红 |
| 全跑 | 我 `set -o pipefail` 自己跑，解析 `passed` 计数 |
| 覆盖缺失 | 我 `--collect-only` 数测试数，对比拆分前后 |

---

## §4 我已经知道会踩的坑（提前告诉你，省一轮往返）

1. **`generated_at` 让"逐字节"不可执行**（方案 §5.2 已记录）→ 用 `del(.generated_at)` 后再 diff
2. **`n_tickets=50` 实际返回 10 注**（方案 §5.3 已记录）→ 等价性脚本不要假设 n 就是返回注数
3. **worktree 缺 `.venv` / `data/`**（闸门 §4.4）→ 若你在 worktree 里验证，先 `ln -s`
4. **`| tail` 吞退出码**（闸门 §5.2）→ 用 `set -o pipefail`
5. **并发期工作区快照不可信**（闸门 §6.5）→ 报告里所有状态注明**观察时刻**

---

## §5 一句话

> **拆分的成功标准不是"文件变小了"，而是"改动能被单测锁住 + 行为一个字节没变"。**
> 前者是手段，后者才是验收对象。

同意以上判据再动手；有异议**先改这份清单**，不要动手后再解释。
