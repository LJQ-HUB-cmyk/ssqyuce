# V12-01 + V12-21 落地验收报告（Lead 执行）

日期：2026-10-18 ｜ 基线 HEAD `46e5e38` ｜ 数据 3515 期

## 一、交付内容

| 任务 | 状态 | 改动 |
|---|---|---|
| **V12-01** 修 30 档 KeyError | ✅ | `lottery/discovery.py`：`ScanContext.__getitem__` 支持切片 + `_rows_as_draws()` |
| **V12-21** 补集反转 | ✅ | `lottery/discovery.py`：`complement_filter()` / `complement_of_filter()`；`lottery/hypothesis.py`：`_build_complement_hypothesis()` / `_upsert_complement()` + `resolve_registry` 分支 |
| 测试 | ✅ | 新增 `tests/test_v12_dimension_exec.py`（8 例）、`tests/test_v12_complement.py`（20 例，含参数化） |

## 二、V12-01 效果（最关键）

**修复前**：`n_tested=94`、`E[FP]=4.70`、30/124 档从未运行
**修复后**：`n_tested=124`、`E[FP]=6.20`、**124/124 全部可执行**

8 个曾经 100% 失败的族现已全部进入搜索：
`sum_range` 6 · `span_range` 4 · `parity_cfg` 4 · `big_small_cfg` 4 · `route012_cfg` 4 · `ac_value` 4 · `consecutive_hot` 2 · `tail_group` 2

**真实性数据（3515 期）**：修复后 `best_quick_p_adjusted = 0.0804` —— 与 P5/P6 预测的 BH=0.0804 **逐位吻合**，证明修复行为正确，且**裁决 R-1 成立**（仍 > 0.05，不许邀功）。

## 三、注入验收（裁决 R-1 要求的「注入必检出 + 纯随机不误报」）

每档 6 次重复，`discover()` 全 124 档：

| 场景 | BH 显著 | 最强候选符号 | 可转补集 |
|---|---|---|---|
| **H0 纯随机** | **0/6** ✅ | + 3 / − 3 | 3/6 |
| **注入 +10%/号** | **3/6** | 全为 + | 0/6 |
| **注入 +15%/号** | **6/6** ✅ | 全为 + | 0/6 |

### 关键读数

1. **纯随机不误报**：H0 下 6 次全部 `best_adj = 1.0`（一次 0.0708 但未过 0.05）→ **FWER 校准正确**。
2. **注入必检出**：+15% 偏置 **6/6 全部检出**；+10% 检出 3/6（符合 MDE +5.88% 的预期量级）。
3. **修复前对照（P2 基线）**：同样注入 +10% 时，系统虽 quick 层 8/8 能检出，但**走到 `usable` 是 0/8**。
4. **补集反转的意义被数据确认**：H0 下最强候选符号 **3 正 3 负**——正好一半是负号。修复前这一半会被判 `rejected` 直接销毁；现在其中 3/6 被正确转成补集候选。
   而**注入真实偏置时，最强候选全部为「+」**（6/6 + 6/6），所以补集不是把噪声抬上来，而是在 H0 下**对称保留**、在有信号时让正向通道自然胜出。

## 四、测试

- 新增 28 例，全部通过
- 全量回归：**886 passed, 19 skipped, 1 xfailed**（基线 858 → 新增 28，零失败）
- 两处既有测试需要同步更新，均为**接口契约变更**，已在测试内注明原因：
  - `test_v4_hypothesis.py::test_resolve_registry_handles_empty_and_missing`：返回体新增 3 字段
  - 我自己改写 `len(ctx)` 用法：`tests/test_v6_regime.py` 明确守护「`len(ctx)` 是 dict 键数量，不能当期数」，我**尊重该契约未覆盖 `__len__`**，改用自己的 `ctx.n`

## 五、尚未修复（留给后续任务）

| 项 | 说明 |
|---|---|
| `persistent` 断崖 | 裁决 R-4 暂缓；+10% 注入即使 quick 检出，仍可能停在 `persistent 0/8`，需单独立项 |
| `patterns_runtime.py:205` 跨层求和 | 裁决 R-2，属 V12-04 |
| 形态通过率 68.2992% 锁值测试 | 裁决 R-3，属 V12-12 |
| APP_VERSION 未升 | 当前仍 `3.1.0 / 2026-10-V10`，v11/v12 共 7 提交未升版 |

## 六、复现命令

```bash
cd /root/DSH/应用/ssqyuce
# 单测
LOTT_DB=/tmp/v12t.db .venv/bin/python -m pytest tests/test_v12_complement.py tests/test_v12_dimension_exec.py -q
# 全量
LOTT_DB=/tmp/v12full/t.db .venv/bin/python -m pytest -q
# 注入验收（约 37 分钟）
PYTHONPATH=. .venv/bin/python /root/DSH/分析/team/lead/inject_acceptance.py 6
```

---

# 第二批：V12-04 + V12-12 落地验收

## V12-04 拆掉跨层求和（裁决 R-2）

**发现比 P6 报告的更多**：跨层求和其实在**两处**，不止一处。

| 位置 | 原代码 | 处置 |
|---|---|---|
| `patterns_runtime.py:205` | `"usable": _n_long + _n_phase`（注释「与旧口径兼容」） | 改为 `_n_long`，跨层合计移入显式命名的 `all_tiers_sum` |
| `rule_lifecycle.py:466` | `counts["usable"] = long + phase`（降级重算路径） | 同上，并同步刷新 `by_layer` |

**新结构**：
```
counts.by_layer      = {usable_long_term, usable_phase, monitor, rejected}   # 权威分层
counts.usable        = usable_long_term        # 兼容字段，语义收紧，禁止跨层相加
counts.all_tiers_sum = long + phase            # 跨层合计，字段名自带警告
```

**真实数据快照验证**：`usable=0 == usable_long_term=0`，`by_layer={0,0,monitor:17,rejected:0}`。
**前端同步改 3 处**（`app.js`）：`长期可用 / 监控 / 否决` 三格展示；闭环 toast 只用长期数并把阶段可用单列标注「仅观察」；调试串改用 `by_layer`。
`node --check` 通过。

**测试**：`test_v11_phase_usable.py` 中原本**断言跨层相加成立**的用例已按裁决改写——现断言 `usable != long + phase`（反向锁），并新增 `by_layer` / `all_tiers_sum` 校验。

## V12-12 锁定形态通过率 + 消除双口径（裁决 R-3）

### 硬编码估计值全部替换为全枚举精确值

| 维度 | 原手写值 | 全枚举精确值 | 偏差 |
|---|---|---|---|
| sum | 0.90 | **0.906411** | +0.0064 |
| span | 0.95 | **0.999853** | **+0.0499（原值严重偏低）** |
| parity | 0.8257（20000 次抽样） | **0.825159** | 抽样误差 |
| zones | 0.90 | **0.799152** | −0.1008 |
| consec | 0.99 | **0.997042** | +0.0070 |

新增 `shape_rules.enumerated_rates()`：单次全枚举 C(33,6)=1107568，惰性缓存，返回 `sum/span/odd/consec/shape5`。

**parity 的口径不统一**被顺带修掉：`desc_zh` 与 `theoretical_rate` 原本都用 `_theoretical_reds()` 的 20000 次随机抽样（0.8257），与其它四维的全枚举口径不一致，现已统一。

### 锁值测试（`tests/test_v12_shape_lock.py`，6 例）

关键教训：**68.2992% 是与数据相关的数，不是常量**。
`constraint_ctx` 的 `odd_range`/sum 带/`top_zones` 全部由最近 500 期实算，换数据就变：
- 真实数据 `odd_range=[1,2,3,4,5]` → **68.2992%**
- 合成数据 `odd_range=[1,2,3,4,5,6]` → 68.6229%

因此采用**双锁**：
- **结构锁**（始终可跑）：锁 `top_zones==10`、`odd_range` 是频次≥2% 的动态子集且宽度≥5（退回理论带 [2,3,4] 会立刻失败）、span/sum 来自分位
- **真值锁**（`>3000` 期真实库时才跑）：锁 68.2992%，实测偏差 2.17e-07

**反向用例**（P6 要求）：把 `top_zones` 缩到 5 → 通过率偏离锁值 → 测试必须失败。已实现并通过。

## 全量回归

| 批次 | 结果 |
|---|---|
| 基线（HEAD 46e5e38） | 858 passed |
| V12-01 + V12-21 | 886 passed |
| **V12-04 + V12-12** | **891 passed, 20 skipped, 1 xfailed** |

## 本轮改动文件

```
lottery/patterns_runtime.py      V12-04 拆跨层求和 + by_layer
lottery/rule_lifecycle.py        V12-04 同上（降级重算路径）
lottery/shape_rules.py           V12-12 全枚举精确率 + 替换 5 处手写值
web/static/app.js                V12-04 前端 3 处改用分层计数
tests/test_v11_phase_usable.py   V12-04 断言从「必须相加」改为「禁止相加」
tests/test_v12_shape_lock.py     新增 6 例（结构锁 + 真值锁 + 反向用例）
```

## 仍未做

- `persistent` 断崖（裁决 R-4 暂缓，需单独立项）
- `APP_VERSION` 仍为 `3.1.0 / 2026-10-V10`（v11/v12 共 7 提交未升版）

---

# 第三批：persistent 断崖评估 → 裁决 C（不必做），升版本号

## P7 独立评估结论：**C — 不必做**

依据 `docs/analysis/P7_PERSISTENT_CLIFF.md`（458 行，Lead 已复核）。

### 决定性证据一：新档会在纯随机数据上误报（第一指标）

每次「运行」= 一次真实扫描 → 12 条候选 → 判定；「命中」= 该次运行至少 1 条通过。

| 方案 | H0 纯随机 (n=8) | +10%/号 | +15%/号 |
|---|---|---|---|
| **L0 现状 `persistent`** | **0/8（0%）** | 0/8 | 0/8 |
| N1 长期显著(BH m=12) | **2/8（25%）** | 8/8 | 8/8 |
| N2 = P2-S2 原建议（+近3段同向） | **2/8（25%）** | 8/8 | 8/8 |
| N1b 长期显著(Bonferroni m=124) | 0/8（0%） | 3/8 | 8/8 |

**逐次明细最有力**：H0 的第 3、6 次异常样本命中 **3 条、7 条**，而 +10% 注入下的命中是 3/5/7/9 条 —— **数值完全重叠**。即：用户在纯随机数据上看到 3 条「可用」，与在真实 +10% 偏置下看到 3 条，系统分辨不出。

### 决定性证据二：年化后从「基本不会」变成「几乎一定」

每期一次快照，约 150 期/年（Lead 已独立复算）：

| 方案 | 每期误报率 | **一年内至少一次假「可用」** |
|---|---|---|
| L0 现状 | 3.6e-4 | **5.26%** |
| N1 / N2 名义 | 0.0496 | 99.95% |
| N1 / N2 实测 | 0.25 | **100.00%** |

### 决定性证据三：真实数据上的唯一产出恰好违反刚立的纪律

新档在真实 3515 期上只放出 `sum_range{w30,[45,75)}`：`p_bonf=0.046`，但**权威口径 BH=0.0804 > 0.05**，且它正是 V12-01 刚解封、此前从未检验的 8 族之一。**发布它就直接违反裁决 R-1（不得邀功）。**

### 决定性证据四：断崖的瓶颈不在 persistent

真实最强候选效应 **+0.0542/期 = +5.0%**，而单阶段（150 期）locked MDE = **+26.0%**（Lead 独立复算，与 P7 修正后一致）。**差 5.2 倍。**

单阶段 MDE 表（Lead 独立复算，配对差口径）：

| α(单阶段) | z_crit | MDE(红球/期) | 相对 |
|---|---|---|---|
| 0.05/32（现状 locked） | 3.1628 | 0.2837 | **+26.0%** |
| 0.05/24（当前段数） | 3.0781 | 0.2777 | +25.5% |
| 0.05（完全不校正） | 1.9600 | 0.1985 | +18.2% |

⇒ 把分母 32→24 只值 **0.5pp**；完全不校正也只到 +18.2%。**调 `ERA_FAMILY_M` 或换 BH 是无效改动。**
真正的杠杆是阶段长度（150→600 可把 MDE 降到 +13%）或不用阶段——**两者都不在 persistent 判据里**。

### 关于 `ERA_FAMILY_M=32`

**不是 bug，是有意设计**。`regime.py:124-139` 注释明确：固定分母换来「历史段 p_adj 永久不变」；若用当前段数，加一期就会让所有历史段 p 值集体漂移——那正是 V10-07 花一个版本修掉的不可复现性。注释自己写了「这是已知且接受的代价」。**改它不是修 bug，是撤销一个显式设计决定。**

## 裁决

| 编号 | 决定 | 理由 |
|---|---|---|
| **R-4 维持** | `persistent_effect` **不做** | ① H0 实测点估计 25%（Wilson 下界 7.1%），远高于可接受 5%；② 年化 100%；③ 真实数据唯一产出违反 R-1；④ 瓶颈在阶段粒度不在判据 |
| **N1b 暂不推进** | 唯一未被否掉但证据不足的变体 | H0 0/8 的 Wilson 上界 **32.4%**，无法证明 ≤5% |

### 对 N1b 的一处修正（Lead 复核）

P7 建议「补 ≥40 次 H0 校准（约 2 小时机时）」。**这个次数不够**——Lead 复算 Wilson 上界：

| 若 n 次全 0 命中 | Wilson 95% 上界 | 能否证明 ≤5% |
|---|---|---|
| n=40 | 8.8% | ❌ |
| n=60 | 6.0% | ❌ |
| **n=80** | **4.6%** | ✅ |
| n=100 | 3.7% | ✅ |

⇒ 若要推进 N1b，需 **≥80 次**（约 2.7 小时机时，单次 discover ≈120s），且必须**先校准再写代码**。

## 版本号升级

| 项 | 原值 | 新值 |
|---|---|---|
| `APP_VERSION` | `3.1.0` | **`4.0.0`** |
| `APP_BUILD` | `2026-10-V10` | **`2026-10-V12`** |
| `APP_MILESTONES` | 无 V12 | 新增 **V12** 条目（含四项修复要点） |
| 前端 `?v=` 令牌 | `20261018.9`×2 + `20260817.7`×1（不一致） | **全部统一 `20261018.12`** |
| 页脚版本徽章 | 硬编码 `v3.1.0` | **`v4.0.0`**（并同步 V4~V12 文案） |
| `docs/PROJECT_CONTEXT.md` | `v3.0.0 / 2026-10-V10` | **`v4.0.0 / 2026-10-V12`** |

**说明**：此前 v11、v12 共 7 个提交从未升版，导致 `/api/info` 报的版本与代码状态不符——这正是 V12 §0 记录的不一致项①，本次一并修正。

---

# 第四批：部署 + 两项性能回归修复（4.0.1 / 4.0.2）

## 部署

| 项 | 结果 |
|---|---|
| 部署前备份 | `/backup/ssq-backup-20261018-132017-pre-v12.tar.gz`（43M，含 ssq.db，tar 校验 30 项） |
| 镜像重建 | `docker compose build` 成功 |
| 容器 | `ssq-predictor` healthy |
| 本地 `/api/info` | **4.0.2 / 2026-10-V12** |
| 公网 `https://ssq.ff88.de` | **4.0.0 / 2026-10-V12**（部署当时版本） |
| 数据 | 3516 期（部署期间新开 2026119 一期） |

## 线上验证：V12 四项改动全部生效

`/api/rules/runtime` 实测返回：

```
by_layer      : {usable_long_term: 0, usable_phase: 0, monitor: 21, rejected: 0}
usable        : 0  | usable_long_term: 0 | all_tiers_sum: 0
禁止跨层相加   : usable == usable_long_term -> True     ← V12-04 生效
scan.n_tested : 124  | E[FP]: 6.2                       ← V12-01 生效（原 94 / 4.70）
scan.best_adj : 0.08111095360626415                     ← 仍 >0.05，R-1「修好≠找到」成立
n_draws       : 3516 | base_issue: 2026119 | stale: False
```

## 性能回归一（4.0.1）：切片不得整条历史重建

**V12-01 修复自身引入的回归**：初版 `ScanContext.__getitem__` 一次性把所有期还原成
dict 并缓存，但 `advance()` 每期都让缓存失效 ⇒ 30 个形态档每期各重建 3315 条 dict
（0.23ms/次 × 30 档 × 3315 期 ≈ 23 秒）。

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `sum_range` 6 档 | 28.4s | **1.4s**（快 20×） |
| `discover` 全 124 档 | 106s | **13.9s**（快 7.6×） |
| `/api/rules/runtime` | **3 分 45 秒** | 见下 |
| 结果一致性 | — | `n_tested=124`、`E[FP]=6.2`、`best_adj=0.0811` **完全不变** |
| 全量测试耗时 | 182s | 159s |

**修法**：按 `slice.indices()` 只构造被请求的窗口，不再整条历史重建。

## 性能回归二（4.0.2）：`_zones_top10_rate` 缺缓存

V12-12 新增的该函数每次全枚举 `C(33,6)=1107568` 注（实测 0.646s），
但它的返回值**不依赖任何数据**（枚举全空间、取前 10 个最常见三区形状）⇒ 是常量。
前端轮询 `/api/shape/rules` 时把容器 CPU 打满。

**修法**：加模块级缓存，首次 0.646s → 后续 0s。

**修复后端点延迟**：3 分 45 秒 → **4~10 秒**。

## 关于容器 CPU ~100% 的核查（结论：既有现象，非本次引入）

发现容器 CPU 长期 100%，做了 A/B 对照：

| 镜像 | CPU |
|---|---|
| 当前 4.0.2 | 100.88% / 99.84% / 100.57% |
| **7 小时前的旧镜像 c0c703d** | **98.76%** |

⇒ 高 CPU 在本次改动之前就存在，**不是 V12 引入的**。已确认后台线程只有两个
（scheduler `sleep(1800)`、boot-refresh `sleep(20)`），无忙等待循环；
heat 线程 tid=17/19 属既有负载。**建议单独立项排查**（项目日志里
`joblib` 多进程后端曾导致段错误，见 api_app.py:2965 注释）。

## 性能护栏

新增 2 条回归测试（`tests/test_v12_dimension_exec.py` ⑥）：
- 小窗口切片不得随历史长度线性变慢
- `discover` 耗时不得随历史长度平方增长（400→800 期应 <3.6 倍）

**阈值修正留痕**：初版设 3.2 倍，一次全量跑实测 3.25 被误报——
正常实现落在 2.3~3.3 倍之间（固定开销 + 常数抖动），真正的 O(N²) 是 4 倍以上。
放宽到 3.6 并在注释里写明实测区间与理由：**护栏设在抖动带边缘只会变成噪音**。

## 提交记录

| 版本 | commit | 内容 |
|---|---|---|
| 4.0.0 | `8ce3e98` | V12-01/04/12/21 + P7 裁决 C + 版本升级（891 passed） |
| 4.0.1 | `b4cae07` | 性能回归一：切片窗口化 |
| — | `c7fb463` | 版本号 4.0.0 → 4.0.1 |
| 4.0.2 | `fc2088f` | 性能回归二：zones 缓存 + 护栏阈值修正（893 passed） |

作者身份已修正为项目惯例 `ssq-dev <dev@local>`（首次提交曾被 git 自动写成
模板占位邮箱，已 `--amend --reset-author` 修正）。

---

# 第五批：L2 结构层 + L3 决策层 + V12-04 收尾（Lead 执行，2026-10-19）

承接 `V12_PATTERN_REDEFINITION_PLAN.md` 的阶段 B / C。本批补齐方案里 **L2 三项（10/11/12 的收尾）+ L3 四项（13/14/15/16）+ V12-04 的分层强制项**。

> **口径总纲**：L2 不是「从历史学到的规律」，而是**组合数学恒等式**（可全枚举验证到 1e-9，
> 不需要样本量/p 值/功效）；L3 的每一条都必须能被确定性代码打分，**弃权不得计入做对**。

## 一、交付清单

| 任务 | 层 | 状态 | 主要落点 |
|---|---|---|---|
| **V12-10** L2 精确值服务 | L2 | ✅ | `lottery/l2_service.py`（新）+ `GET /api/l2/distribution` |
| **V12-11** 预算内最优组合求解器 | L2 | ✅ | `lottery/engine.py::optimal_portfolio` + `tests/test_v12_portfolio.py` |
| **V12-12** 形态约束加固（收尾） | L2 | ✅ | 已在第四批前完成；本批补「L2 不得叫可用规律」口径 |
| **V12-13** AI 两岗位 + 打分卡 | L3 | ✅ | `lottery/ai_audit.py`（新）+ migration **#4 `llm_audit`** + `GET /api/llm/audit/latest` + `POST /api/llm/audit/run` |
| **V12-14** 数据异常期检测器 | L3 | ✅ | `lottery/data_audit.py`（新）+ `GET /api/audit/anomaly/latest` |
| **V12-15** 边际读数端点 | L3 | ✅ | `GET /api/portfolio/marginal` |
| **V12-16** 用户可自证实验 | L3 | ✅ | `lottery/selfcheck.py`（新）+ `POST /api/experiments/randomness-selfcheck` |
| **V12-04**（收尾）`layer` 必填 + 跨层求和强制测试 | 横向 | ✅ | `patterns_runtime.py`（`layer` + `by_evidence_layer`）、`shape_rules.py`、`api_app.py` 严格模式门控、`tests/test_v12_layer_isolation.py`（新） |

## 二、L2 验收（方案写死的数字，逐项实测）

| 验收项 | 方案要求 | 实测 | 结论 |
|---|---|---|---|
| V12-10 `n=5 preset=spread` 的 `p_at_least_r.4` | `0.024504`（±1e-6） | **0.02450413879779842** | ✅ |
| `p_at_least_r_method` | 必须 `exact_enum` | `exact_enum` | ✅ |
| `union_coverage`（5 注互不相交） | 30 | **30** | ✅ |
| `var_ratio`（`Ā=0`） | 0.1111… | **0.1111111111111111** | ✅ |
| `ev_ratio_full` | ≈ 0.45673 | **0.45673239701760976** | ✅ |
| `prize_assumption` 必填 | 缺即失败 | 存在（V4 假设表 A 全文） | ✅ |
| V12-11 `n=5 min_var` 的 `var_ratio` | ≤ 0.1112 | **0.111111** | ✅ |
| **A5 反向用例**：`ev_ratio_full` 与注单无关 | 逐位相同 | 三种 objective 下均 == 单注口径 | ✅ |
| V12-15 禁建议文案 | `划算\|推荐` 计数 = 0 | **0** | ✅ |
| V12-15 边际读数 | 中性、不含建议 | `delta_pp=6.6046`、`expected_return_yuan=-1.0865` | ✅ |

**L2 的真实收益必须说清**：把票单从「高重叠」修到「互不相交」`P(≥1注≥4红)` 从 `0.019517` 提到 `0.024504`，而 **iid 近似是 `0.024265`** —— 即低重叠只比 iid 高 **+0.98%**，**iid 就是天花板**。**EV 恒为 `0.45673`，不受任何选号影响。**

## 三、L3 验收（A6 / A7）

### V12-13 打分卡（A6：`skipped` 单列、不进分母）
- 测试 **35 例**全绿，含：`degraded` 不进分母、`n_wrong>0 ⇒ score<1`、`n= "12"`（字符串）判 `invalid`、method 集合多/少判 `invalid`、`red_mean` 小数第 4 位 +0.0001 必 `fail`、并列奖级取**最高**。
- **补上 V10-01 从未落地的 `llm_audit` 表**（migration #4，字段与 V10 §2.1.5 冻结 DDL 逐字一致）。
- `skill_stats` 默认基线由写死的 `0.5` 改为**哑判官基线**（最常见金标准占比）——原 `0.5` 会让闭眼全答 `keep` 的假判官（实测准确率 99.19%）轻松「达标」。

### V12-16 自证实验（A7：同 seed 逐字节一致）
- 真实 3516 期：4/4 检查通过，`response_sha256` 同 seed 两次相同，换 seed 不同。
- **发现并修复一个真实性能缺陷**：首次计算零分布（`n_perm=200` × `randomness_report`）实测 **5.6~6.7s**，**超出方案要求的 3s**。
  - **不能靠调小 `n_perm` 过关**：实测零分布 5% 分位随 `n_perm` 单调变化（200→0.0264，100→0.0325，60→0.0537，40→0.0863），调小等于**偷偷放宽判据**。
  - 改为「确定性值两级缓存（进程内 + 磁盘）+ 启动期后台预热」：预热后用户请求 **0.069s**，且 `q05` 仍为 **0.0264**（判据未放宽）。
  - 新增 2 条护栏测试（磁盘缓存命中不重算、缓存键必须区分参数）。

### V12-14 异常期检测器（A6 的判定口径）
| 场景 | 判定 | p_adj | 结论 |
|---|---|---|---|
| 纯随机（`num_freq`/`sum_mean`/`odd_count`） | `noise` | 0.287 / 1.0 / 1.0 | ✅ 不误报 |
| 只在末 100 期注入偏置 | `regime_shift` | 0.0125 | ✅ 正确区分「只在一段」 |
| **全程** +30% 号频偏置 | `real_defect` | 0.0125 | ✅ 两段复现才升级 |
| 纯随机对照 | `noise` | 1.0 | ✅ |

> `regime_shift` 与 `real_defect` 的区分正是方案要的「不许把单段显著当成结构缺陷」：只有**独立复现段**也成立才判 `real_defect`。

## 四、V12-04 收尾（裁决 R-2 的强制项）

| 项 | 落地 |
|---|---|
| `layer` 字段**必填** | `patterns_runtime.build_snapshot` 每条规则带 `layer="L1"` |
| 拆掉跨层求和 | `counts["usable"] == counts["usable_long_term"]`（禁止 `long+phase`） |
| 按层计数 | 新增 `counts["by_evidence_layer"] = {L1:{tier:…}, L2:{…}, L3:{…}}`；`by_layer`（V11 起的 tier 计数）**原样保留**避免契约漂移 |
| 强制测试 | `tests/test_v12_layer_isolation.py` **6 例**，含两个反向用例：注入 `layer="L4"` 必失败、缺 `layer` 必失败、注入跨层求和字段必被点名 |
| 严格模式门控 | 改为 `layer=="L2"` 且 `violation_count==0`，文案明示「L2 可用 ≠ 存在可预测规律」 |
| 命名 | `shape_rules.py` 去掉「可用规律」自称，保留**否定式**说明（「**不是**可用规律」）作为口径教育 |

## 五、全量回归

| 阶段 | 结果 |
|---|---|
| 基线（HEAD `b283b12` / 4.0.2） | 892 passed, 20 skipped, 1 xfailed（含 1 例性能护栏在机器负载下偶发） |
| 本批新增测试 | **+90 例**（l2_exact 16 / portfolio 11 / selfcheck 9 / data_audit 13 / ai_audit 35 / layer_isolation 6） |
| 修复的既有测试 | `tests/test_v10_replay.py::test_migration_3_creates_frozen_ddl`——原断言 `MIGRATIONS[-1][0]==3` 与「新增第 4 号」冲突；改为按版本号定位并额外锁定 #4，**冻结语义不变** |

## 六、如实记录的未做项与遗留

| 项 | 说明 |
|---|---|
| **V12-17 前端三卡重排** | 未做。后端契约（`layer`/`by_evidence_layer`/`ai_audit`/L2 精确值/自证实验）已就绪，前端可直接消费 |
| **V12-19 版本号** | 未升（仍 `4.0.2 / 2026-10-V12`）。本批未发布，升版与部署需单独一批 |
| **V12-20 冻结开关** | 未做（`LOTT_SEARCH_FROZEN` 与 `/api/discovery/scan` 的 `frozen=true`） |
| **容器 CPU ~100%** | 第四批已核实为**既有现象、非 V12 引入**，本批未处理，仍建议单独立项 |
| **性能护栏偶发失败** | `test_discover_does_not_scale_with_history_squared` 在宿主 load≈2.8 时实测 2.96 倍 > 阈值 2.77 倍。属**机器负载敏感**而非实现回归；是否放宽阈值需单独裁决（避免把真 O(N²) 放过去） |

## 七、工作区与运行时状态说明

- `data/selfcheck_null_cache.json`（V12-16 零分布缓存）已加入 `.gitignore`——它是
  **确定性派生数据**（可由 seed 重算），与 `hypotheses.json` / `llm_claims.json` /
  `patterns_runtime.json` 同属运行时状态，不入库。
- 工作区曾出现 `data/llm_cost_log.jsonl` 新增 2 行（`purpose=hypothesis`，
  时间 15:06 / 15:56，落在并行子任务运行期间）。已核实：**不是测试写出的**——
  `llm_client.cost_log_path()` 走 `config.DATA_DIR`，而 `tests/conftest.py` 已隔离该路径
  （实测跑完 35 例后该文件无变化）。那两行是子任务在测试隔离之外直接跑脚本时按设计
  记录的真实 LLM 成本。已 `git checkout` 还原，未纳入本批改动。

## 八、复现命令

```bash
cd /root/DSH/应用/ssqyuce

# 本批新增测试（90 例）
LOTT_DB=/tmp/v12/all.db .venv/bin/python -m pytest \
  tests/test_v12_l2_exact.py tests/test_v12_portfolio.py \
  tests/test_v12_selfcheck.py tests/test_v12_data_audit.py \
  tests/test_v12_ai_audit.py tests/test_v12_layer_isolation.py -q

# 全量回归
LOTT_DB=/tmp/v13full/t.db .venv/bin/python -m pytest -q

# L2 真值（方案写死的验收数字）
PYTHONPATH=. .venv/bin/python -c "
from lottery import l2_service as L, engine as E
p=L.l2_distribution_payload(n=5, preset='spread')
print('p4 =', p['distribution']['p_at_least_r']['4'])          # 0.02450413879779842
r=E.optimal_portfolio(5,'min_var'); print('var_ratio =', r['var_ratio'])  # 0.1111…
"

# L3 自证实验（需先预热或接受首跑 ~5.6s）
PYTHONPATH=. .venv/bin/python -c "
from lottery import db, selfcheck as S
d=db.load_draws(); S.warm_null_quantile(n_issues=len(d))
import time; t=time.time(); a=S.selfcheck(d,seed=42); b=S.selfcheck(d,seed=42)
print('all_pass', a['all_pass'], '| sha equal', a['response_sha256']==b['response_sha256'],
      '|', round(time.time()-t,2),'s')
"
```

---

# 第九批：容器 CPU ~100% 的根因核查与治理（2026-10-19）

第四批留下的疑点「容器 CPU 长期 100%，建议单独立项排查」本次核查完毕。**结论：不是死循环，也不是 V12 引入，而是两条可测的重复计算。**

## 一、先纠正前提：当前基线并不高

| 观测 | 值 |
|---|---|
| `docker stats` 连续 12 次采样（1 分钟） | **0.20%~0.23%**（两次瞬时 1.03%/0.75%） |
| 宿主 load average | 0.62 / 0.30 / 0.22 |
| 容器累计 CPU / 运行时长 | 4082s / ≈17h ≈ **平均 6.7%** |

所以「长期 100%」这个描述**不准确**；真实现象是**周期性满核**。第四批当时看到的 100% 很可能正好落在满载窗口内。

## 二、真实根因：每 30 分钟跑一轮「23 分钟的重活」

`_scheduler_loop` 每 `sleep(1800)` 调用 `_scheduler_step()`；开奖日（周二/四/日）过 21:35 后每轮都会走完整链路。日志实测 2026-10-18（周日）三轮：

| 轮次 | 起 | 止 | 耗时 |
|---|---|---|---|
| 1 | 14:13:52 | 14:36:09 | **22 分 17 秒** |
| 2 | 15:06:38 | 15:25:33 | **18 分 55 秒** |
| 3 | 15:56:01 | 16:18:29 | **22 分 28 秒** |

⇒ 每 30 分钟里有 19~22 分钟单核跑满 = 持续占用 **57%~73%**。这才是「CPU ~100%」的观测来源。

## 三、根因一：`_rebuild_probs` 算 4 个模型只用 1 个（已修）

**profiler 定位**（`verify_hypothesis_ab` 单次 41.6s）：

```
1200 calls  models.py:49  markov_model     20.6s  (63%)
1200 calls  models.py:37  omission_model    8.1s
```

原因：`engine._rebuild_probs(name, hist)` 无论要哪个方法，都调 `models.build_models(hist)`——**一次算 freq / omission / markov / bayes 四个**。而所有调用方都只取一个：

- `evolution._ab_structural_delta` 只要 `"freq"`（`freq_model` 是 O(30) 的轻量计算）
- `battle._method_probs` 每个方法也只取一个

**修法**：改为按需只算被请求的那一个（查 `_MODEL_BUILDERS` 表）。

**验收（三项全过）**：

| 验收项 | 结果 |
|---|---|
| 逐位对等：单模型 == `build_models` 里的那一个 | 4 个模型全部 `np.array_equal` 为真 |
| 结果一致性：优化前后 `delta_red_hits`/`delta_blue_rate` | **逐位相同**（已用 `git stash` 前后对照实测） |
| 提速 | 单次 verify 41.6s → **10.2s（4×）**；3 条假说 95s → **9.6s（10×）** |

> **一次被实测否定的假设（记录教训）**：我最初推测瓶颈是「32 条假说重复计算同一个对照组回放」，加了对照组缓存后发现**零收益**（112.3s vs 113.1s），说明对照组只占小头。该改动已**完整撤回**（`git checkout lottery/evolution.py`），没有把无效复杂度留在代码里。真正的线索来自 profiler，不是直觉。

## 四、根因二：同一期数据被反复进化（已修）

数据抓取每轮返回 `inserted_new: 0`（当天数据早已到齐），但 `_evolution_step` 每轮仍对全部 32 条假说重跑 A/B——**没有「同一份数据只进化一次」的约束**。

**修法**：幂等闸门。把「已进化到的数据指纹（期数 + 末期号）」写入 `data/evolution_state.json`；指纹没变就跳过。

| 场景 | 实测 |
|---|---|
| 第一次（真跑） | 102.7s |
| 第二次（同一期数据） | **0.038s，`skipped=True`** |
| `force=True`（`/api/evolution/run` 手动触发） | 97.4s，**不跳过** |

手动触发必须绕过闸门，否则「用户点了却没反应」会变成静默缺陷——该行为由 `test_evolution_step_force_bypasses_idempotency` 与 `test_api_run_endpoint_uses_same_step_as_scheduler` 双重锁定。

## 五、故意**不**修的：`constraint_ctx` 的逐期重建

剩余热点是 `constraint_ctx`（1200 次，6.8s）。但 `battle._ctx_for_t` 的 docstring 明确记录了一次**历史上的错误缓存**：`constraint_ctx` 用滑动窗口 `draws[-500:]`，t=500 与 t=504 的窗口不同，缓存会把 ctx 错误复用进而**改变出号结果**。

> 这是项目里一个**已显式裁决过的设计决定**（正确性优先于性能）。本次不推翻它，只在此记录：若将来仍要压这部分开销，正确方向是「让 ctx 计算可证明与窗口位置无关」，而不是加缓存。

## 六、新增测试（15 例，`tests/test_v12_cpu_governance.py`）

| 断言 | 反向验证（故意退回实现→测试变红） |
|---|---|
| `_MODEL_BUILDERS` 与 `build_models` 成员集合一致 | — |
| 单模型构建与 `build_models` 逐位相同（4 例参数化） | — |
| **请求 freq 时不得构建 markov/omission/bayes** | ✅ 退回全量构建 → 立即失败 |
| `force=True` 不跳过；指纹含末期号 | ✅ 禁用闸门 → 立即失败 |

两条关键测试都做了「故意退回实现、确认测试会红」的鉴别力验证，避免留下永远通过的空测试。

## 七、回归

| 阶段 | 结果 |
|---|---|
| 治理前基线 | 1033 passed, 20 skipped, 1 xfailed（158s） |
| 治理后 | **1036 passed, 20 skipped, 1 xfailed（111s）** |
| 全量耗时变化 | 158s → **约 115s**（本次优化同样加速了测试里的回放） |

**契约变更**：`_evolution_step` 新增 `force` 参数；`tests/test_v4_evolution_step.py` 的桩同步接收并断言 `force=True`（原桩不接该参数会导致 503）。

## 八、对第四批结论的更正

| 第四批结论 | 本次更正 |
|---|---|
| 「容器 CPU 长期 100%」 | **不准确**。当前基线 0.2%，真实现象是开奖日每 30 分钟一轮 19~22 分钟的满载 |
| 「不是 V12 引入」 | **成立**（7 小时前旧镜像同样如此），但根因是**既有**的重复计算，具有明确修复路径 |
| 「建议单独立项排查」 | **已完成**，两条根因均已修复并有回归测试 |
