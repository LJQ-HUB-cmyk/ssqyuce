# V14 架构与工程健康度独立审查

- **审查对象**：`/root/DSH/应用/ssqyuce`，git HEAD `432d0a7`（另有 19 项未提交改动）
- **审查方式**：只读代码 + 静态 AST 解析 + **变异测试** + 真实数据端到端复算
- **脚本位置**：`/tmp/arch/`（全部在仓库外，**未改动任何生产代码**）
- **立场**：默认怀疑。所有数字均为本机实测，命令与耗时见文末《复现清单》

---

## 结论摘要

**选择：重构（Refactor），但范围比 Lead 建议的更大 —— 判定层必须重做，不是修。**

1. **不重写**：`lottery/` 34 466 行里，有 ≈22 000 行是**不可替代资产**（数据管道、DB 迁移、多租户认证、LLM 记账、1292 条测试），重写成本 ≥ 46 人日，而当前缺陷全部集中在 ≈2 200 行的统计判定路径上。
2. **但"只修判定层 3 处"不够**：实测发现 **4 处缺陷**，Lead 的 3 处之外还有 1 处（`z_scores_from_scan` 用被空期稀释的 `k_mean` 定标 sigma）。其中 **2 处是 `test_p4_eprocess.py` 17 条测试全绿也抓不到的**（变异测试证实）。
3. **判定层可以抽离**：`rigor.py` / `eprocess.py` 的 **fanout = 0**，`idle_mining.py` 只依赖 4 个模块。抽成 `lottery/stats/` 独立包是**已有引力中心**的自然结果，不是强行分层。
4. **真正的结论**：`usable` 长期为 0 **不是缺陷，是数据现实**。3517 期实测：红球均匀性 p=0.065、蓝球 p=0.887、和值 KS p=0.059 —— 与均匀随机不可区分。重构判定层的价值是**让系统诚实地报告"没有发现"**，而不是"挖出规律"。
5. **工程风险不在统计层**：`engine.py` 3 044 行 / 149 KB 承担 8 个互不相关子系统，是**唯一真正难以维护的模块**；它的耦合（fanin=12 / fanout=13）比任何统计缺陷都更值得优先处理。

---

## Q1 代码规模与耦合

### 1.1 规模总账（实测）

```bash
cd /root/DSH/应用/ssqyuce && wc -l lottery/*.py | sort -n
find . -name '*.py' -not -path './.venv/*' | wc -l
```

| 指标 | 实测值 |
|---|---|
| `lottery/` 模块数 | **70** 个 `.py` |
| `lottery/` 总行数 | **34 466** 行 |
| 仓库 Python 文件总数（除 `.venv`） | **177** |
| `tests/` 测试文件 | **74** 个 `test_*.py`（另有 `conftest.py`/`calibration.py`） |
| `tests/` 总行数 | **23 474** 行 |
| 测试/源码比 | **0.68 : 1**（不含 `lottery/` 之外的产品代码，实际更低） |

**LOC Top-10**（`wc -l lottery/*.py | sort -n`）：

| 排名 | 文件 | LOC | 字节 | 顶层 def/class |
|---|---|---|---|---|
| 1 | `engine.py` | **3 044** | 149 014 | 75（3 class / 72 def） |
| 2 | `hypothesis.py` | **1 978** | 91 578 | 56（0 class） |
| 3 | `ml_model.py` | **1 672** | 77 254 | 45（0 class） |
| 4 | `discovery.py` | 1 501 | 65 628 | 55（1 class / 54 def） |
| 5 | `battle.py` | 1 416 | 70 050 | 23 |
| 6 | `rigor.py` | 1 354 | 64 058 | 23 |
| 7 | `rule_lifecycle.py` | 1 129 | 54 156 | 35 |
| 8 | `db.py` | 1 057 | 44 158 | — |
| 9 | `data_audit.py` | 1 033 | 48 880 | — |
| 10 | `evolution.py` | 930 | 44 333 | — |

Top-7 合计 **12 092 行 = `lottery/` 的 35%**。

### 1.2 职责是否单一：**大文件全部不单一，`engine.py` 最严重**

按代码内部的章节分隔符实测（`grep -n '^# ---\|^# ===' lottery/engine.py`），`engine.py` 3 044 行被切成 **11 个章节，主题互不相关**：

| 行区间 | 行数 | 章节主题 |
|---|---|---|
| L32–100 | 69 | 硬约束 |
| L101–145 | 45 | 统计采样 |
| L146–527 | **382** | 融合权重（walk-forward + softmax + 收缩） |
| L528–591 | 64 | 主流程 |
| L592–1017 | **426** | 规律 → 出号（运行时快照驱动） |
| L1018–1289 | 272 | **蓝球模式与覆盖** |
| L1290–1420 | 131 | 奖金期望模型 |
| L1429–1440 | 12 | 组合数学 |
| L1441–1940 | **500** | 概率边界与尾概率 |
| L1941–2670 | **730** | **组合覆盖优化** |
| L2671–3044 | 374 | 预算内最优组合求解器 |

**职责混杂的直接证据**：

- `engine.py` 一个文件同时装了：`numpy` 数组混音（`_BlendArray` 子类）、LLM 提示词拼装（`llm_tickets`）、超几何/泊松二项闭式概率（`_exact_red_tail`、`p_at_least_r_mc`）、双色球奖金表（`expected_prize`）、蒙特卡洛求解器（`_PmaxEstimator`）、组合覆盖优化器（`coverage_optimize`）。**这 6 类代码彼此没有调用关系，却共享同一个模块命名空间。**
- `ml_model.py` 1 672 行里有 **751 行（45%）** 属于单一章节 "零复制次数 B（V10-08 / D-2 修正）"——一个**统计校验实验**寄生在 ML 模型文件里。
- `hypothesis.py` 1 978 行里 **563 行**（L267–829）是单一巨型章节，且该文件 **0 个 class、56 个顶层函数**，全部靠模块级全局状态运转（`load_registry` / 全局 dict）。
- `battle.py` 1 416 行 **0 class / 23 个顶层函数**，平均**每函数 61 行**——函数粒度过粗。

> **判断**：`engine.py` 已无法维护。任何人改动 `coverage_optimize` 都要在一个 149 KB 文件里定位，且 `import engine` 会连带加载 numpy/scipy/模型注册表。这是**唯一值得动用"重写级别"手段的模块**，但它属于产品层，不属于统计判定层。

### 1.3 依赖方向：**无循环依赖，但存在双上帝模块**

用 AST 静态解析 70 个模块、302 条内部依赖边（`/tmp/arch/depgraph.py`，修正了 `from . import a, b, c` 的多名字导入）：

```bash
cd /root/DSH/应用/ssqyuce && .venv/bin/python /tmp/arch/depgraph.py
```

**循环依赖：0 个**（含 2 节点环、3 节点环全扫描）。这一项**做得干净**，是 P2 模块化的真实成果。

**God 模块（fanin 与 fanout 双高）**：

| 模块 | fanin（被依赖） | fanout（依赖别人） | LOC | 判定 |
|---|---|---|---|---|
| **`engine.py`** | **12** | **13** | 3 044 | **双高 = 真正的上帝模块** |
| `config.py` | **41** | 1 | 676 | 纯配置枢纽，可接受 |
| `db.py` | **39** | 5 | 1 057 | 数据访问层，可接受 |
| `discovery.py` | 14 | 1 | 1 501 | 统计内核，healthy |
| `llm_client.py` | 13 | 3 | 813 | 基础设施，healthy |
| `evaluate.py` | 12 | 3 | 286 | healthy |
| `evolution.py` | 7 | 8 | 930 | 中度耦合 |

**`engine.py` 的 fanin=12 明细**（`grep` + AST 交叉验证）：`api_app`、`battle`、`cli`、`evaluate`、`evolution`、`l2_service`、`llm_eval`、`replay`、`routes_llm`、`routes_portfolio`、`routes_predict`、`routes_tasks` —— **产品层 12 个模块直接捅进集成引擎**，中间没有任何服务边界。

**关键健康信号**（决定重构可行性的核心证据）：

| 模块 | fanout | 含义 |
|---|---|---|
| `rigor.py` | **0** | 只依赖标准库+numpy，**零内部依赖** |
| `eprocess.py` | **0** | 同上，**零内部依赖** |
| `idle_mining.py` | 4 | 仅依赖 `candidate_space`/`config`/`db`/`discovery` |
| `features.py` | 1 | 准叶子节点 |

> **这是"把统计内核抽成独立包"可行的**全部**论据**：判定层的三个核心模块（`rigor`/`eprocess`/`candidate_space`）已经是**依赖图的叶子**。它们不依赖 `engine`、不依赖 `db` 的业务表结构，抽走不会引发任何级联改动。

### 1.4 `routes_*.py` 14 个域是否真的隔离：**"隔离"只做到了一半**

实测 15 个 `routes_*.py`（共 3 107 行）：

**做对的部分 —— 横向隔离 100% 达成**：
```
routes_* 之间互相 import 的数量 = 0
```
14 个路由域**没有任何一个 import 另一个路由域**（`/tmp/arch/edges.json` 逐条核验）。P2 声称的"模块化"在**横向**上是真实的，不是包装。

**没做到的部分 —— 纵向是"薄路由 + 深调用"**：

| 路由 | fanout | 直接依赖的业务模块数 |
|---|---|---|
| `routes_rules.py` | **14** | battle, bg_tasks, config, db, discovery, evolution, evolution_service, hypothesis, jsonutil, llm_lab, patterns_runtime, regime, request_context, rule_lifecycle |
| `routes_audit.py` | **13** | ai_audit, bg_tasks, config, data_audit, db, evolution, hypothesis, jsonutil, l2_service, llm_lab, ml_model, patterns_runtime, selfcheck |
| `routes_predict.py` | 10 | backtest, config, db, **engine**, llm_client, methods, patterns_runtime, request_context, shape_rules, user_llm_store |
| `routes_llm_ops.py` | 10 | battle, config, db, decision_skill, jsonutil, llm_client, methods, multiscale, replay, shape_rules |

**判断**：路由层确实是薄壳（`routes_ml.py` 仅 37 行、`routes_methods.py` 58 行），但它**不是领域边界，而是 URL 到 10~14 个业务函数的直通车**。真正的业务边界在 `engine`/`discovery`/`battle` 内部，而那里没有边界。

**`routes_*` 总计 3 107 行 / 15 个文件**，最大 `routes_rules.py` 559 行——**这一层不是问题所在**，260 行/文件的中位数对 Web 层完全合理。

---

## Q2 测试可信度

### 2.1 实测通过率

```bash
cd /root/DSH/应用/ssqyuce
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider --ignore=tests/frontend
```

```
1292 passed, 20 skipped, 1 xfailed, 4 warnings in 178.70s (0:02:58)
```

| 项 | 数值 |
|---|---|
| passed | **1 292** |
| failed | **0** |
| skipped | 20 |
| xfailed | 1 |
| 耗时 | **178.7 秒**（< 3 分钟，无需分层） |

> **注意口径差**：任务背景称"历史 858 passed"，而 commit 自报 1266→1276。实测 **1 292**。差异来自未提交的 `tests/test_db_guard.py` + `tests/frontend/` + `test_mu1_auth.py` 新增 277 行。**测试数量在增长，不是萎缩**，这是正面信号。

### 2.2 `conftest.py` 的 DB 隔离：**实测有效**

历史上"字母序巧合通过"的教训已被真正修复。三项主动验证：

**验证 1 — 反向字母序全跑**（正向顺序是历史假绿条件）：
```bash
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider $(ls tests/test_*.py | sort -r)
```
→ `1292 passed, 20 skipped, 1 xfailed in 184.17s` —— **与正向完全一致**。

**验证 2 — 主动探针**（写入临时探针测试，跑完即删）：
```python
def test_probe_db_is_isolated():
    assert config.DB_PATH != PROD_DB          # 通过
    assert db.count_draws() == 0              # 通过：隔离库为空
def test_probe_base_is_isolated():
    assert not str(config.DATA_DIR).startswith(PROD + "/data")   # 通过
```
→ **2 passed**。隔离库为空、路径在 `tmp_path`、`DATA_DIR` 不泄漏。

**验证 3 — 机制审查**：`tests/conftest.py:119` 的 `_auto_isolate_lottery_paths` 是 `autouse=True`，且显式修复了两个历史坑：
- `llm_lab.CLAIMS_FILE` / `patterns_runtime.SNAPSHOT_FILE` 在 **import 时固化成模块常量**，必须单独 monkeypatch（L105-107）——这是真正容易被漏掉的点，做对了。
- `auth._RATE` 是**进程级全局 dict**，跨用例累积导致"单跑绿、合跑红"；在 autouse 里 `reset_rate_limits()`（L127-133）——**做对了**。

> **结论：DB 隔离可信。** 这一项是这套测试体系里**质量最高的部分**。

### 2.3 「测了个寂寞」—— 5 个实测例子

判断标准不是"有没有 assert"，而是**把 bug 注入进去，测试能不能抓到**。为此做了变异测试（`/tmp/arch/mutate.py`，只在临时副本改，跑完立即还原并 `git diff` 验证）。

#### 例 1 ★最严重★ — `tests/test_p4_eprocess.py`（全文件 17 条测试）**完全不测 e-BH**

`eprocess.ebh_selected()` 是整个判定层的**最终闸门**（决定什么是"可用规律"），但：

```bash
grep -rln "ebh_selected" tests/
# → tests/test_p4_candidate_space.py   ← 只有这一个
#    test_p4_eprocess.py 里 0 处
```

**变异实验**（把 e-BH 改成多选一个，FDR 直接失控）：
```python
# lottery/eprocess.py:  return set(order[:k_star])
#            变异为:    return set(order[:k_star+1])
```
```bash
.venv/bin/python -m pytest tests/test_p4_eprocess.py -q
# → 17 passed in 0.70s          ← 全部通过
```

**为什么没测到**：`test_p4_eprocess.py` 测的全是 `log_e_value` / `threshold` / `verdict` / `sigma_hypergeom` / `z_scores_from_scan`，**唯一没测的就是 `ebh_selected`**——也就是这套系统最需要验证的那一个函数。

#### 例 2 — `tests/test_p4_candidate_space.py:54`：**逻辑恒真断言**

```python
a = CS.candidate_at(layer, idx)
b = CS.candidate_at(layer, idx)
assert a is None or b is not None, f"{layer}#{idx} 首轮合法但复算返回 None"
```

**真值表证明**：

| a | b | 断言结果 |
|---|---|---|
| `None` | `None` | **True** |
| `None` | 任意 | **True** |
| 合法 | 合法 | **True** |
| 合法 | `None` | False |

`a` 为 `None` 时**恒真**。且 `b` 与 `a` 用同一输入调用同一确定性函数，`b is None` 发生在 `a` 非 `None` 之后**在实现上不可能**。该断言等价于"没崩溃"。

**它的来历是"修复把断言改宽"**：`git log -L 54,54` 显示这行由 `37d6fce` 引入，原断言是 `assert a is not None`（能抓到"整层全被误杀"），改成了"可复现即可"。

#### 例 3 — `tests/test_p4_candidate_space.py:199`：**写了 `or True` 的自废断言**

```python
assert m - 1 not in eprocess.ebh_selected([need * 0.99], m_total=m) \
    or True   # 单元素情形下 k=1，阈值恰为 m/α
```

`X or True` 恒为 `True`。**这是死代码**——它左侧的真实检查 `m - 1 not in ...` 永远不会影响结果。凑数痕迹明显。

#### 例 4 — `tests/test_p4_idle_mining.py:267` `test_ebh_stays_local_to_its_layer`：**断言太弱，放过真实缺陷**

```python
got = idle_mining._recompute_usable(cands, n_checked=72656)
assert any(k.startswith('cross|0|hot') for k in got)      # 只要有 1 个就过
for k, v in got.items():
    assert v.get("ebh_threshold_k1", 1) < 20000           # 上限给了 2 万
```

**变异实验**（把 cross 组 m 硬编码改成 1 = 阈值彻底失效）：
```python
# idle_mining.py:  return max(n_in_group, 135)
#         变异为:  return 1
```
```bash
.venv/bin/python -m pytest tests/test_p4_idle_mining.py -q
# → 23 passed in 1.32s          ← 全部通过，**缺陷存活**
```

**为什么没测到**：断言是 `any(...)` 而非精确集合；阈值只设了 `< 20000` 的宽松上限。m 从 135 → 1 时阈值从 2 700 → 20，`any` 依然成立，`< 20000` 依然成立。**正确的断言应是 `assert got.keys() == {...精确期望...}`。**

> 对比：改 `single` 组的同一处（`return min(n_checked, 306)` → `return 1`）**被抓到**（1 failed）。说明测试对单因子层有保护、对交叉层（占空间的 99.6%）没有。

#### 例 5 — `tests/test_v10_calibration.py:286` `test_evaluate_signal_fpr_is_calibrated`：**样本量不足，断言区间宽到失效**

```python
sims = cal.n_sims(30)                        # 快速模式只有 30 次模拟
cal.assert_calibrated(fpr, alpha, sims, sigmas=3.5, label="evaluate_signal")
```

实测容差：
```
n_sims=30, alpha=0.05, sigmas=3.5 → tol = ±0.1393
接受区间 = [0.0, 0.1893]
```
即 **FPR 高达 0.189（名义 α 的 3.8 倍）仍判"校准通过"**。

对比同文件 `test_verdict_fpr_follows_the_p_value_source` 用 `n_sims=400` + `sigmas=3.0` → 区间 `[0.017, 0.083]`，这才是有效校准测试。**同一个文件里两种标准并存**，说明有人已经知道怎么测对，却对新加的判据放宽了。

### 2.4 变异测试总表（`/tmp/arch/mutate.py`）

| 注入的缺陷 | 抓到? | 原因 |
|---|---|---|
| e-BH 多选一个（`k_star` → `k_star+1`） | **存活** ❌ | `test_p4_eprocess.py` 不测 `ebh_selected` |
| 回退 432d0a7 的空期修复（`z=0` → `continue`） | 抓到 ✅ | 有专门回归测试 |
| 回退 38d44ff 的固定 sigma（`sqrt(var)` → `0.8678`） | 抓到 ✅（6 failed） | 有 scipy 逐位对照 |
| cross 组 m 硬编码 → `1` | **存活** ❌ | 断言是 `any(...)` + 宽松上限 |
| single 组 m → `1` | 抓到 ✅ | — |
| 回退 37d6fce 的蓝球退化判据 | 抓到 ✅ | — |
| 回退 432d0a7 的 `k>=1` → `k<1` | 抓到 ✅ | — |

**变异得分 5/7 = 71%**。对一套 1 292 条测试的体系，这个分数**偏高**（正常应在 85%+），且**两个存活点恰好都在判定层的命门上**。

> 跑完变异后已 `git diff --name-only lottery/eprocess.py lottery/idle_mining.py lottery/candidate_space.py` 验证**输出为空**（完全还原），并复跑三个测试文件确认 `57 passed`。

---

## Q3 四个 commit 复核

### 3.1 逐个结论

| commit | 声称 | 实际改动 | 判定 |
|---|---|---|---|
| `a0a116b` | 全史窗口 + 分层 e-BH + CPU 限流 | `idle_mining.py` +175 行，引入 `_recompute_usable` / `_group_checked` / `_throttle` | **部分正确**，但引入了下文 Q3.3 的核心缺陷 |
| `37d6fce` | 剔除交叉层退化候选 | `candidate_space.py` +88 行，新增 `_cross_is_degenerate`（k<1 或 k>12） | **方向正确**，判据过松（见 432d0a7） |
| `9e34a33` | 修正 CPU 限流口径 | `_throttle` 判据从"占空比"改为"整机余量 ≥ 一个核" | **正确**，逻辑自洽 |
| `38d44ff` | 修正方差口径 | 新增 `sigma_hypergeom`，按 k 定标 | **正确但半途**：只按 `k_mean` 定标，不按逐期 k |
| `432d0a7` | 空期不得跳过 | `z_scores_from_scan` 空期 `z=0`；退化判据加严到 `3≤k≤12` | **方向正确**，但关联断言被放宽 |

### 3.2 修复是否让测试变成"永不失败"

**是，找到 2 处**：

1. **`37d6fce` 把 `test_candidate_at_is_deterministic` 从 `assert a is not None` 改成 `assert a is None or b is not None`**（恒真，见 Q2 例 2）。改宽的理由写在注释里："交叉层有退化组合会被剔除"——这个理由本身合理，但**正确的写法是 `assert (a is None) == (b is None)`**，而不是恒真。

2. **`432d0a7` 的 commit message 明说**："`test_degenerate_filter_does_not_overkill` 改为横跨全层采样……（原来只扫前 400 索引，那里全是空集，会得到"全部剔除"的假象）"。**这是把"测试失败"通过改测试样本分布来解决，而不是确认判据正确。** 虽然新判据 `3≤k≤12` 有实测支撑，但**过程上是"让测试通过"而非"证明正确"**。

### 3.3 ★最关键：Lead 的 3 处诊断 —— 2 处确认，1 处不成立，另有第 4 处

#### ① e-BH 被喂预筛列表 —— **确认，且比 Lead 说的更严重**

`idle_mining.py:359-372`：候选先经 `judge_candidate` 单测，`decision == "usable"` 才放进 `candidates`；`_recompute_usable` 再对 `candidates` 做 e-BH。

**实测**（`/tmp/arch/` 直接调用）：
```
假设 1000 个检验里只有 3 个进 candidates:
  m=1000 (正确): set()          ← 一个都不选
  m=3   (预筛后): {0, 1, 2}     ← 全部通过
全量 m=1000 真实调用: set()
```
**同样的 3 个"强"候选，正确口径返回 ∅，预筛口径全部通过。**

#### ② m 记账用硬编码常数 —— **确认，且低估 198 倍**

`idle_mining.py:482-497` 的 `_group_checked`：
```python
if gkey == "single":
    return min(n_checked, 306) if n_checked else n_in_group   # 硬编码 306
return max(n_in_group, 135)                                    # 硬编码 135
```

**实测**（真实候选空间）：
```
cross 族对数量: 268   模式数: 2
cross_total = 72 350，按模式分布: {'1'(union): 26 793, '0'(intersection): 11 079}
代码实际分组数: 2   ← 应为 268 × 2 = 536 组
_group_checked 对 cross 返回（硬编码）: 135
真实每组检验数（26793 / 536 平均）: ~50，但 union 组实为 26 793
```

**更致命的第二层缺陷（Lead 未提及）**：分组键塌陷。代码用 `key = "cross|" + parts[1]`（只有模式），而 docstring 写的是"按「族对+模式」各一组"。实测：

```
代码实际分组数: 2      （cross|1: 26793 个候选，cross|0: 11079 个）
文档声称的分组数: 403  （族对×模式去重后）
阈值对比: m=135 → 2 700      vs   m=26 793 → 535 860
低估倍数: 198 倍
```

**即：交叉层 99.6% 的候选被塞进 2 个组，而每组 m 只记 135（一个硬编码常数）。**这比 Lead 说的"硬编码常数"更糟——常数之外，**分组本身也塌了**。

#### ③ H0 过阈率偏离名义值 25 倍 —— **不成立（方向反了）**

Lead 说"过阈率偏离名义值 25 倍"，暗示**假阳性过多**。实测相反：

**单候选层面**（`/tmp/arch/` 模拟 2 000 次纯 H0）：
```
n_obs= 500 : 过阈率=0.0010   名义=0.050  比值=0.02x
n_obs=3217 : 过阈率=0.0005   名义=0.050  比值=0.01x
```
**过阈率比名义 α 低 27~50 倍——系统是过度保守的，不是失控的。**

**放大到候选规模**：
```
H0 下 P(e ≥ 20) = 0.001850
在 26 793 个候选下期望假发现数 = 49.57 个「候选」
P(e ≥ 2700) = 0.000000
```
即：**"单候选过阈"这一级在 H0 下会冒出 ~50 个，但这一级本来就不叫"可用"**（代码注释说得对：`usable` 需要 e-BH）。真正的 `usable` 需要 e ≥ 2700，而 H0 下该尾概率实测为 **0.000000**（20 000 次采样未出现一次）。

> **所以"25 倍"的真实含义**：`1/α = 20` 的阈值，在 3217 期数据上实际给出的是 **0.05%~0.1%** 的假阳性率——比名义值**严 27 倍**。这是**过度保守**，方向与 Lead 判断相反。它解释了为什么 `usable` 长期为 0：**不是判据坏了，是判据太严 + 数据没有信号**。

#### ④ **Lead 未发现的第 4 处缺陷**：`k_mean` 被空期稀释导致 sigma 失真

`eprocess.py` `z_scores_from_scan`：
```python
k_mean = float(scan.get("k_mean") or 0.0)      # ← 全段平均，含 k=0 的空期
sigma = sigma_hypergeom(k_mean, target)         # ← 用稀释后的 k 定标
for o, e in zip(obs, exp):
    if e <= 0.0:
        out.append(0.0)      # 空期计入 z=0
    else:
        out.append((o - e) / sigma)   # 活跃期用同一个稀释 sigma
```

**实测后果**：
```
逐期 k: 2576 期 k=0 + 641 期 k=6  →  k_mean = 1.1955
sigma(k_mean=1.1955) = 0.4204     ← 代码实际用这个
sigma(k=6 真实)      = 0.8678
z 放大 2.1 倍  ⇒  e 放大 ≈ 4 倍

k_mean=0.219 的情形：
sigma(0.219) = 0.1827 vs sigma(6) = 0.8678  ⇒ z 放大 4.8 倍, e 放大 23 倍
```

commit `38d44ff` 声称"**逐候选按 k 定标**"，`432d0a7` 又改成"按 k 定标、空期计入"。但实现始终是"**按全段平均 k（含空期）定标**"——文档与实现的语义差了一整个量级。**这正是"名义 H0 过阈率"真正会失真的地方**：非空期的 z 被系统性放大，而放大倍数取决于该候选的空期比例（不可控变量）。

> **为什么真实数据上没爆**：实测 21 个真实存续候选，`k_mean == k_active`（到小数点后 3 位），比值 e_used/e_true **中位数 1.00x、最大 1.00x**。原因：`432d0a7` 把退化判据加严到 `3≤k≤12` 后，**k_mean < 3 的候选在生成阶段就被剔除了**。所以这个缺陷目前被**上游过滤掩蔽**了——但缺陷本身仍在，一旦有人放宽 `_cross_is_degenerate`（比如为了"多挖点规律"），立即复现。

### 3.4 修复累积的净效果

| 指标 | 修复前 | 修复后（实测） |
|---|---|---|
| 有效候选空间 | 72 656 | **72 350**（命中统计）/ ≈38 868（`432d0a7` 声称剔除 53%） |
| 交叉层最大 e | 0.77（`37d6fce` 报告） | 实测 21 样本**最大 2.94** |
| 过阈候选数 | 84 → 31 假"可用" | 0 |
| 单候选 e 尾概率 | 假阳性 31 条 | 0 |

**净效果：把 e 值从"假阳性"压到了"零发现"。这是正确的，但方向是**关闭**而不是**打开**。Lead 说"e 值回落几个数量级"——准确，实测回落 100~6490 倍（各 commit 自报 + 本审查独立复算一致）。

**但要警惕**：连续 4 个 commit 都在**同一方向（把判据调严）**上打补丁，且每次都放宽了测试。这是"**防御性开发螺旋**"：`37d6fce` 严了 → 又冒出新假阳性 → `38d44ff` 再严 → 又冒出 → `432d0a7` 再严。**每轮都以"调严"收场，没有人问过"这个判据的统计语义是否自洽"。** 第 4 处缺陷（k_mean 稀释）就是这个螺旋留下的遗漏。

---

## Q4 三方案对比：修 / 重构 / 重写

### 4.1 方案对比表

| 选项 | 依据（实测） | 人日估算 | 主要风险 |
|---|---|---|---|
| **A. 只修判定层**（Lead 方案） | 4 处缺陷全在 `idle_mining.py`(564) + `eprocess.py`(318)；`rigor.py`/`eprocess.py` **fanout=0**；**完全不碰 `engine.py`** | **3~5** | ① 第 4 处（k_mean 稀释）属"设计级"，修它要改 `z_scores_from_scan` 签名并**逐期传 k**，牵动 `discovery.asof_scan` 返回结构；② 修完仍是"零发现"——**用户感知不到任何改善**，会被当成"又白干一轮" |
| **B. 重构（抽 `lottery/stats/` 独立包）** | `rigor`/`eprocess` fanout=0、无循环依赖、`idle_mining` 仅 4 依赖 —— **抽象已存在，只是没落地为包** | **12~18** | ① 需要给 `ebh_selected` 补 15+ 条边界测试（当前 0 条）；② `candidate_space` 依赖 `discovery`，抽包时要把"筛选器生成"与"统计检验"切开 |
| **C. 推倒重写** | 不可替代资产 ≈22 000 行（见 4.3），实测重造成本 ≥46 人日 | **46~70** | ① 重写 `db.py`(1057) + `migrations.py`(731) 要重放 3517 期历史 + 19 张表的迁移链；② 1292 条测试中约 60% 会作废；③ **最致命**：重写出来的东西面对同样的数据，结论仍然是"没有规律"——**花 60 人日得到和今天一样的结论** |

### 4.2 「只修判定层」的真实改动面

**要改的文件**（实测 LOC）：

| 文件 | LOC | 需改动 |
|---|---|---|
| `lottery/eprocess.py` | **318** | `z_scores_from_scan`（逐期 k）、`ebh_selected`（保持）、`sigma_hypergeom`（保持） |
| `lottery/idle_mining.py` | **564** | `run_once`（把全量 e 值传给 e-BH）、`_recompute_usable`（分组键加族对）、`_group_checked`（删掉硬编码） |
| `lottery/discovery.py` | 1 501 | **可能**需要在 `asof_scan` 返回里加逐期 k 序列（当前只给 `k_mean`） |
| `tests/test_p4_*.py` | ~700 | 补 e-BH 边界测试、修正恒真断言 |

**不会碰到 `engine.py`（149 KB）——这是确定的**：

```bash
grep -rn "engine" lottery/eprocess.py lottery/rigor.py lottery/idle_mining.py
# → 无输出：判定层三件套完全不 import engine
```

依赖方向是 `idle_mining → discovery / candidate_space / eprocess / config / db`，`engine` 在另一条分支（`engine → discovery`，反向不成立）。**判定层与 149 KB 的 `engine.py` 之间没有任何代码路径。**

**但改动面比"3~5 人日"大**，因为第 4 处缺陷需要**逐期 k**：

```python
# 当前 discovery.asof_scan 只返回均值
"k_mean": float(np.mean(ks)) if ks else 0.0,
```
要修 k_mean 稀释，必须让 `asof_scan` 额外返回 `k_series`，并在 `z_scores_from_scan` 里 `zip(obs, exp, ks)`。这**修改了 `discovery` 的公共返回契约**，而 `discovery` 有 **14 个 fanin**——需要逐个验证下游是否受影响。这是"3 人日"变"5~8 人日"的原因。

**修正后的人日：5~8 人日**（Lead 的 3~5 偏乐观）。

### 4.3 不可替代资产清单（重写要花多久）

| 资产 | 实测规模 | 重造成本 | 为什么不可替代 |
|---|---|---|---|
| **数据管道**（`data_fetcher`/`data_check`/`data_audit`/`migrations`） | 1 033 + 731 + 辅助 ≈ 2 400 行 | **6~8 人日** | 3517 期真实数据、19 张表、schema_version 迁移链、对账逻辑（`reconcile_runs`）。重写要重新对接数据源并逐期核对 |
| **DB 迁移链**（`migrations.py`） | **731 行** | **4~6 人日** | 历史迁移必须**按序重放**才能得到今天的 schema；漏一步则 19 张表数据错位 |
| **多租户 + 认证**（`auth.py` 697 + `auth_api.py` 589 + `user_llm_store` 401） | **1 687 行** | **5~7 人日** | session/CSRF/rate-limit/多租户隔离，且刚在未提交改动里被加固（+171 行） |
| **LLM 记账**（`llm_client` 812 + `llm_lab` 640 + `llm_eval` 310 + 预算/claims） | **1 762 行** | **4~6 人日** | 成本账本、预算封顶、claims 去重、失败回退——这些是**运维血泪积累**，不是可以"重新设计"的东西 |
| **备份门禁 / 自检**（`selfcheck` 685 + `db_guard`） | **685 行** | **2~3 人日** | 零备份保护、真值锁、完整性检查 |
| **测试体系**（`tests/`） | **23 474 行 / 1 292 条** | **8~12 人日** | 60% 会随重写作废，但保留下来的（conftest 隔离、校准 harness、DB guard）**质量很高**，重写必须重建等价物 |
| **ML / 进化 / 战斗 / 规律生命周期** | `ml_model` 1672 + `evolution` 930 + `battle` 1416 + `rule_lifecycle` 1129 ≈ **5 100 行** | **6~9 人日** | 大量实验性代码，含可复用的校准/置换检验框架 |
| **前端 + API 路由** | `web/static/app.js` + 15 个 `routes_*`（3 107 行） | **4~6 人日** | — |
| **合计** | ≈ **22 000 行**（`lottery/` 的 64%） | **39~57 人日** | 不含重写后重新调参、重新跑 3517 期回放、重新挖矿的时间 |

**加上统计内核本身（约 2 200 行，6~10 人日）+ 联调/回归（5~10 人日）→ 重写总成本 50~77 人日。**

**决定性论证**：重写需要 **46~77 人日**，而修判定层需要 **5~8 人日**。重写成本是修补的 **7~10 倍**。而**重写完成后，面对同一份 3517 期数据，结论仍然是 `usable = 0`**（红球均匀性 p=0.065）。

> **重写唯一正当的理由不是"代码乱"，而是"某个模块已无法维护"。** 按这个标准，唯一够格的是 `engine.py`（3 044 行 / 8 个不相关子系统），但它**不在判定层的改动路径上**——可以单独、增量地拆，不需要整体重写。

### 4.4 第三条路：把统计内核抽成 `lottery/stats/` —— **可行，且是已存在的引力中心**

**可行性证据（全部来自 Q1 的实测依赖图）**：

| 条件 | 实测 | 判定 |
|---|---|---|
| 无循环依赖 | **0 个环** | ✅ |
| 内核 fanout = 0 | `rigor.py` = 0，`eprocess.py` = 0 | ✅ 已在叶子 |
| 内核不依赖 DB 业务结构 | `eprocess.py` 只依赖 `math`/`typing` | ✅ |
| 内核被产品层直接依赖 | `rigor` fanin=5（battle/replay/regime/routes_rigor/selfcheck） | ⚠️ 抽包需改 5 处 import |
| 判定调度与统计分离 | `idle_mining` 依赖 `candidate_space`/`discovery` | ⚠️ 需切"筛选器生成"与"检验" |

**建议结构**：

```
lottery/stats/
├── __init__.py          # 统一导出
├── eprocess.py          # e-process + e-BH（零内部依赖，直接搬）
├── sigma.py             # sigma_hypergeom（从 eprocess 拆出，纯数学）
├── zscore.py            # z_scores_from_scan（逐期 k 版本）
├── rigor.py             # 显著性判据（零内部依赖，直接搬）
└── multilayer.py        # 分层 e-BH 分组与 m 记账（新写，修缺陷②③）
```

**关键收益**：
1. `stats/` 的**输入是纯数组**（`z_scores: List[float]`、`k_series: List[int]`、`obs/exp`），**输出是判定字典**。可以用 30 行纯 numpy 脚本**独立复算**——目前 `idle_mining` 与 `discovery` 拧在一起，任何独立复核都要先起一个完整 DB。
2. **可替换性**：将来换统计方法（比如从 e-process 换成 confidence sequences），只改这一个包，不动产品层。
3. **测试可完备**：纯函数无副作用，`ebh_selected` 的边界测试可以从 0 条补到 20 条，无需 DB。

**风险**：`candidate_space.py`(405 行) 依赖 `discovery`，而 `discovery` 依赖 `db`。抽包时 `candidate_space` **不能进 `stats/`**（它会拖进 DB 依赖），必须留在 `lottery/` 作为"空间生成"。这需要在设计时明确切分。

### 4.5 最终建议

**选择「重构」，但按两阶段做**：

**阶段 1（5~8 人日）—— 修判定层 4 处缺陷，不抽包**
- 修 ① e-BH 喂预筛列表：`run_once` 收集**全部** e 值（含未过阈的），整轮送 `ebh_selected`。
- 修 ② m 记账：删 `_group_checked` 的两个硬编码；分组键改成 `cross|<mode>|<族A>|<族B>`（403 组）。
- 修 ③（澄清，非修改）：H0 过阈率**过度保守**，记录在案；`1/α` 阈值在 3217 期上实际是 0.05%。**不要为了"挖出东西"而放宽它**。
- 修 ④ k_mean 稀释：`z_scores_from_scan` 改为逐期 k 定标（需 `discovery.asof_scan` 返回 `k_series`）。
- 补 `ebh_selected` 的 15~20 条边界测试；修正 Q2 的例 2/3/4/5 四处失效断言。

**阶段 2（7~10 人日）—— 抽 `lottery/stats/`**
- 搬 `eprocess` / `rigor`（fanout=0，零阻力）。
- 新写 `multilayer.py` 承担分层 e-BH，把"分组/m 记账"从 `idle_mining` 里剥离。
- 保持 `lottery/eprocess.py` 作为 re-export 垫片，避免改 5 处 fanin。

**明确不做的**：
- ❌ 不重写（46~77 人日，收益为"同样的零发现"）。
- ❌ 不在阶段 1 里碰 `engine.py`（它不在依赖路径上）。
- ❌ 不为了"让 `usable` 不为 0"而放宽 alpha 或缩小 m——`V14_ALGO_REVIEW.md` 已独立证实数据与随机不可区分。

**`engine.py` 单独处理**（另立任务，8~12 人日）：按 Q1.2 的 11 个章节拆成 `engine/{weights,rules,blue,prize,coverage,solver,math}.py`。这是**纯搬家**，`import engine` 保持兼容。**优先级低于阶段 1**，因为它不阻止任何统计正确性。

---

## Q5 仓库卫生

### 5.1 未提交改动（19 项）

```bash
git status --short
```

**已修改（tracked，11 个文件，+694/−25）**：

| 文件 | 改动 | 判断 |
|---|---|---|
| `lottery/auth.py` | **+68** | 认证加固（CSRF/rate-limit），**应尽快提交** |
| `lottery/config.py` | **+65** | 新增配置项，与 auth 配套 |
| `lottery/auth_api.py` | +28 | 配套 |
| `lottery/api_app.py` | +11 | 配套 |
| `lottery/db.py` | +6 | 配套 |
| `web/static/app.js` | +117 | 前端 CSRF 改造 |
| `tests/test_mu1_auth.py` | **+277** | 新增测试，质量较高 |
| `tests/test_w08_frontend_auth.py` | +99 | 配套 |
| 其余 3 个测试 | +16 each | 配套 |

**判断**：这是一整套**未提交的认证/CSRF 加固**，跨 5 个生产文件 + 5 个测试 + 1 个前端文件，**自洽且带测试**。它是**好的改动**，但**悬在工作区里没有 commit**——这是本次审查最大的**工程风险**（不是统计意义上的）：

> 任何人 `git checkout .` 或 `git stash` 都会**瞬间丢失 694 行安全加固**。

**建议**：立即 commit（`feat(auth): CSRF 双 cookie + 前端配合`），不要等"全部做完"。

**未跟踪（8 项）**：

| 路径 | 大小 | 判断 |
|---|---|---|
| `qa-tools/` | **24 KB** | **应提交**。README 写得清楚：不是产品代码、与产品无关、长期存在；`delay_harness.py` 是可复现的时序缺陷工具 |
| `tests/frontend/` | **40 KB** | **应提交**。含 `mutation_check.py` 等真实的变异检查工具 |
| `scripts/isolated_test.sh` | 8 KB | **应提交**。解决了"测试写进生产库"的实际事故（见脚本头部注释） |
| `tests/test_db_guard.py` | — | **应提交**。守卫测试 |
| `docs/analysis/V14_ROOT_CAUSE.md` | 20 KB | 应提交（与 `docs/reviews/` 同级） |
| `docs/reviews/V14_ALGO_REVIEW.md`、`V14_PRODUCT_REVIEW.md` | — | 本类文档，应提交 |
| **`data/ssq.db.bak-probe-cleanup-20261021-072443`** | **1.5 MB** | **应清理** ❌ |
| **`data/ssq.db.bak-…-shm` / `-wal`** | 32 KB / 0 | **应清理** ❌ |

### 5.2 清理项：`data/` 备份文件

**.gitignore 有漏洞**：

```bash
grep -n "data" .gitignore
# 2:data/*.db          ← 只匹配 .db 结尾
# 5:data/*.pre-migrate-*
# 6:data/*.db.tmp
# 10:data/*.db-wal
# 11:data/*.db-shm
```

`data/ssq.db.bak-probe-cleanup-20261021-072443` **不匹配以上任何一条**（它在 `.db` 后面还有 `.bak-...` 后缀），因此**直接暴露在 `git status` 里**，随时可能被误 `git add`。

**`data/` 总大小 294 MB**，其中 `ssq.db` 1.5 MB，其余主要是 `archive/`、`backups/`、`raw/`、`ml_models/`（均已 gitignore）。**运行库本身大小正常**。

**建议**：
1. `.gitignore` 加一行 `data/*.bak*`（覆盖 `ssq.db.bak-*` 全部三种后缀）。
2. 删除 3 个 `bak-probe-cleanup` 文件（它们是 `9e34a33` 时代清理探针数据的临时副本，`docs` 里已有对应记录）。
3. `data/idle_mining_state.json`（23 KB，08:17 更新）、`patterns_runtime.json`（22 KB，08:20）、`rule_lifecycle.json`（08:30）—— **这些是运行状态，正在被写入**，说明**有后台挖掘进程仍在跑**。审查期间读到的最新写入时间是 `08:30`。

### 5.3 「项目是否还在受控」

**结论：受控，但处于"高活跃 + 低提交频度"的危险区**。

| 维度 | 状态 | 证据 |
|---|---|---|
| 版本控制 | ⚠️ **危险** | 694 行安全加固悬在工作区；1.5 MB 二进制备份裸露 |
| 测试基线 | ✅ **良好** | 1292 passed / 0 failed，正向反向一致，178 秒可跑 |
| 生产代码纯净度 | ✅ **良好** | 本审查全程未改 `lottery/`（已 `git diff` 验证） |
| 依赖循环 | ✅ **优秀** | 70 模块 0 循环 |
| 构建/运行隔离 | ✅ **良好** | `conftest` autouse 隔离 + `isolated_test.sh` |
| 文档 | ⚠️ **膨胀** | `docs/reviews/` 已 37 个文件，多轮审查叠加 |
| 死代码/孤儿 | ⚠️ 中 | 见 Q2 例 3 的 `or True`；`data/mining_artifact.json`（129 KB，10-05 后未更新） |

---

## 复现清单

| 编号 | 脚本 / 命令 | 用途 | 实测耗时 |
|---|---|---|---|
| R1 | `cd /root/DSH/应用/ssqyuce && wc -l lottery/*.py \| sort -n` | LOC 表 | <1s |
| R2 | `.venv/bin/python /tmp/arch/depgraph.py` | 依赖图 + 循环检测 + fanin/fanout | 0.8s |
| R3 | `.venv/bin/python /tmp/arch/mutate.py` | **变异测试**（7 个变体，逐条跑测试并还原） | **约 4 分钟** |
| R4 | `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider --ignore=tests/frontend` | 全量测试基线 | **178.7s** |
| R5 | `.venv/bin/python -m pytest tests/ ... $(ls tests/test_*.py \| sort -r)` | 反向字母序验证隔离 | **184.2s** |
| R6 | `.venv/bin/python /tmp/arch/real_h0.py` | 真实 data/ssq.db 上 21 个候选的 e_used/e_true 对比 | **约 3 分钟** |
| R7 | `.venv/bin/python /tmp/arch/h0_layer.py` | 分层 e-BH 的 H0 行为 + 尾概率 | 约 2 分钟 |
| R8 | `.venv/bin/python /tmp/arch/space.py` | 候选空间实测（72 350 / 分组塌陷 / 198 倍） | 约 1 分钟 |
| R9 | `.venv/bin/python /tmp/arch/isolation.py` | 主动写入隔离探针并跑、跑完删除 | 1.2s |

**环境**：Python 3.13（`.venv`），有状态为 `/tmp/arch/`；**所有脚本在仓库外，未修改 `lottery/` 或 `web/`**。

**数据一致性核对**：

| 检查 | 命令 | 结果 |
|---|---|---|
| 真实库期数 | `sqlite3 data/ssq.db "select count(*) from draws"` | **3 517** |
| 红球均匀性 | chi-square over 33 numbers | χ²=44.84, df=32, **p=0.0654** |
| 蓝球均匀性 | chi-square over 16 numbers | χ²=8.81, df=15, **p=0.8873** |
| 和值分布 | KS vs 理论 C(33,6) 分布 | KS=0.0224, **p=0.0585** |

> 三条独立性检验一致指向同一结论：**这份数据与均匀随机不可区分**。这决定了任何判定层修复的**上限**——它能让系统"正确地报告没有规律"，但不能让它"挖出规律"。

---

## 附：本次审查改动声明

- **未修改任何生产代码**：`git diff --name-only lottery/eprocess.py lottery/idle_mining.py lottery/candidate_space.py` → 空输出（变异测试后已复原并验证）。
- **写入的文件**：仅本报告 `docs/reviews/V14_ARCH_REVIEW.md`。
- **临时文件**：`/tmp/arch/depgraph.py`、`mutate.py`、`real_h0.py`、`h0_layer.py`、`space.py`、`isolation.py`。
- **删除的临时测试**：`tests/test_zz_isolation_probe.py`（R9 生成，运行后已 `unlink` 并验证不存在）。
