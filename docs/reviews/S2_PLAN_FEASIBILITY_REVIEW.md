# S2 方案可实现性评审：V10 改进方案 v2.0 能不能被程序员一行行写出来

> 评审员：方案可实现性评审员（资深工程师视角）
> 评审日期：2026-10-17
> 评审对象：`docs/V10_IMPROVEMENT_PLAN.md`（v2.0，**1264 行，已逐行通读**）
> 评审方式：只读代码与文档，只写本文件；**所有可数值验证的论断均在 `/tmp` 独立复算**，所有"方案说能改"的地方均在真实源码上确认
> 基线实测：`.venv/bin/python -m pytest -q` → **485 passed / 77.28s**（方案写 480，见 P2-6）
> schema_version=2、`draws`=3515、`schema_version` 表 2 行、`llm_claims.json` `n_claims=0` —— 均现场复核

---

## 一、一句话结论

# 有条件通过

方案 v2.0 **在数据结构层面确实把"前置注册"接进了 `usable`**（这是首轮裁决 G4/G8 的成败点，且方案 §3.3.5 的三处改法在真实代码结构上**成立**），`var_ratio` 的 11/54 推导**完全正确**（我逐项用 Fraction 精确复算，边界值 5.0 / 0.1111 / 1.0 全对，偏差 0），AI 两岗位的"金标准"确实是**确定性、无网络、无随机**的。

**但不能按现状开工 V10-01**：方案有三处**会直接导致"白做"或"写不出来"的缺陷** ——

1. **前置注册只有"读"没有"写"**：全仓 `grep -rn prereg lottery/ web/ tests/` → **零命中**。方案规定了 `prereg_ok` 的**读者**（`patterns_runtime.py:110`），但**从未规定 `prereg_status` / `prereg_ok` 由谁计算、在哪调用、何时运行**。U1–U5 全部 monkeypatch `hypothesis.load_registry`，**即使实现者永远不产生一条 `prereg_ok=True` 的记录，U1–U5 依然全绿** —— 这正是方案自己警告的"只建登记簿不改 `usable` 判定 = 白做"，只是搬到了写侧。
2. **`var_ratio` 的"可为负"是错误结论**，且 §3.4.7 要求的"负值用例"**用真实票组无法构造**（我穷举 n=2..33 的最小可行 Ā：最小 ratio = **0.0**，永不 <0；方案举的 `n=10、Ā=0 → −3.000` 三重错误：算术应为 −1.000、n=10/Ā=0 需 60 个不重复号 > 33 不可能、−3.000 对应 n=19）。
3. **`resolve_channels()` 的 `source` 层在现有代码上无法判定**：`config.py:239`（`set_methods`）与 `config.py:267`（`_load_runtime_methods_config`）**都把生效值写回 `os.environ["LOTT_METHODS"]`**，运行时第 1 层（json）与第 2 层（.env）**永久不可区分**，T1/T3 无法诚实地写出来。

这三条都是**文档级修复**（补规格、改断言、加一个 origin 标记），不需要推翻设计。修完即可开工。**详见 §四 必须修复项（6 条，估 0.5 人日）。**

---

## 二、评分表

| 维度 | 分数 | 判据摘要 |
|---|---:|---|
| **前置注册接线**（§3.3） | **2 / 5** | 读侧接线成立 ✅；**写侧缺失**（无 producer/无调用点/无调度接入）❌；U1 不是"没白做"的有效反证 ❌；12 字段**不足以表达 R1/R2/R5** ❌ |
| **AI 两岗位**（§3.2） | **3 / 5** | 金标准确定性 ✅、四态 + 分母口径清晰 ✅；但 `golden_ok` **签名写错必抛 TypeError** ❌、复算口径与 `evaluate.aggregate` 实际返回**对不上** ❌、`LEVEL_NAMES` 不存在 ❌、`seed`/默认值 §2.1.1 与 T1 **互斥** ❌ |
| **`var_ratio` 11/54 推导**（§3.4） | **2 / 5** | 闭式与系数**完全正确**（精确有理数验证，偏差 0）✅；但"可为负 −3.000"**错误**、表格第 4 行 ΣA **自相矛盾**、要求写的负值测试**不可构造** ❌ |
| **契约冻结 V10-00a**（§2.1） | **3 / 5** | `result` 新增字段、别名表、DDL 冻结**写得足够实现** ✅；`source` 层不可判定 ❌、T1 与默认值自相矛盾 ❌、静态闸门**时序错**（00a 阶段必红）❌、fixture 名与已落地代码不符 ❌ |
| **任务表与估时**（§4） | **4 / 5** | 依赖图**无环** ✅、真实并行度 2 路判断正确 ✅；M0 中 1.5d 已花掉、里程碑天数与任务表**不自洽**、02 的文件域**漏 `api_app.py`**（同类错误正是首轮 F3） |
| **无残留**（§0.2 / 附录 C） | **4 / 5** | 人工复核：**无一处仍在指导实现已删除的设计** ✅；但方案自己的"附录 C grep 全过 / Gate-7 已完成"**按其自定判据为假** ❌ |

**合计 18 / 30。** 评分说明：3 分是"读得懂、能动手"，4 分是"照抄不歧义"，5 分是"带反例防护"。本方案的问题**不在方向、不在数值推导**，集中在**"规定的读者多于规定的写者"**和**"行号/常量/签名与仓库现状脱节"**。

---

## 三、问题清单

### P0（不修则实现者是"照着写但写出来是空转/写不出来"）

#### P0-1　前置注册**只有读者、没有写者**：方案复制了它自己警告的"白做"

**证据链（四步，全部实读）**

1. **全仓无 `prereg` 任何痕迹**：
   ```
   $ grep -rn "prereg" lottery/ web/ tests/ --include=*.py --include=*.js
   （无输出）
   ```
2. **`usable` 的唯一计算点已确认**：`lottery/patterns_runtime.py:110`
   ```python
   usable = (status in USABLE_STATUSES) and h.get("status") == "validated"
   ```
   （方案说 `:104`，实际 **:110**，行号偏移 6 行 —— 见 P2-5。）
   它读的 `h` 来自 `evolution._as_hyp_list(hypothesis.load_registry())`（`patterns_runtime.py:101`，`evolution.py:88-99`）—— **`_as_hyp_list` 只是归一成 `list[dict]` 并过滤非 dict，不做字段投影**，所以方案"给假说记录加 12 个字段 → 自动被 `usable` 读到"这一步**在代码结构上确实成立** ✅。这是方案 §3.3.1 判断正确的地方。
3. **方案只规定了读**：§3.3.5（:692-732）给了 `usable` 的改法、`rules.append` 的 `prereg` 子块、`result.prereg` 汇总 —— **三处都是读/展示**。
4. **没有任何一节规定 `prereg_status` 的推进**：`prereg_ok = (prereg_status=="passed") and ...`（方案 :663）中的 `prereg_status`，**既不在 `register_hypotheses`（`hypothesis.py:1257`）里被写入，也不在 `resolve_registry`（`:1443`）里被推进**。`resolve_registry` 推进的是另一套状态机 `status ∈ {proposed, validated, rejected, retired}`（`hypothesis.py:107` `HYPOTHESIS_STATUSES`），依据是 `score_hypothesis` 的 `verdict`（`:1348-1440`，来自 `battle.battle_report` 回放）。**两套状态机并行运行时，谁把 `prereg_status` 推到 `passed`？方案没说。**

**为什么这是 P0 而不是 P1**：这正是裁决 G4 的原话「只建登记簿不改 `usable` 判定 = 白做：用户看到的仍然是 0 条可用规律」。方案把它修成了「改了 `usable` 判定但没人写登记簿」—— **结果完全相同**：`prereg_ok` 恒为 `None`，`h.get("prereg_ok") is True` 恒假，`usable` **恒为 0**。

**U1–U5 为什么不是反证**（题目要求的构造反例，我构造出来了）

- U1（方案 :740）的写法是："造一条 `regime.status="persistent"` + `status="validated"` + **`prereg_ok=True`** 的假说，`monkeypatch` 注入 `hypothesis.load_registry`，调**真实** `patterns_runtime.build_snapshot` → 断言 `usable is True`"。
- 反例实现者：**只加 12 个字段（全部置 `None`）+ 改 `usable` 三处 + 从不设置 `prereg_ok=True`**。此时：
  - U1：注入记录里**手工写了** `prereg_ok=True` → 绿 ✅
  - U2（删 `prereg_ok`）→ `usable is False` → 绿
  - U3/U4（`provisional` / `source=="llm"`）→ 绿
  - U5（`prereg` 子块 6 key）→ 字段全是 `None`，但"key 在"仍成立 → 绿
  - P1–P5（`exp_id` 幂等、时序拒绝、冻结期拒绝、批大小、批量上限）→ 可以全部在"字段校验层"写成绿
  - P6（10 批 × 10 条纯噪声 → 累计口径发现率 ≤ 5%）→ **一个"永不通过任何假说"的实现，发现率恒为 0 ≤ 5%**，**照样绿**
  - **全量 `pytest -q` → 绿**。→ **P0-2 白做，测试体系零告警。**
- 根因：**U1 把"证物"（`prereg_ok=True` 的记录）由测试伪造**，所以它证明的是"读者会读"，不是"写者会写"。方案 :752 已经意识到要 mock 数据来源而非 mock `build_snapshot` 内部，但**漏了最重要的一条：反例必须从真实入口（`register_hypotheses`）出发**。

**修法（必须写进 §3.3.5）**：
- 明确一个新入口，例如 `hypothesis.evaluate_prereg(draws, issue, registry_path=None) -> Dict`（**签名、入参、返回值、幂等性写死**），职责：按 `evaluate_after_issue` 到期、批满 `MIN_BATCH`、两套 p 都算，推进 `prereg_status`，写 `prereg_ok`。
- 明确**调用点与调度**：`api_app._scheduler_step()`（`api_app.py:~1142` 起）或 `patterns_runtime.refresh`（`api_app.py:1176` 调用）**二者选一写死**，并写进 §2.1.7 文件域表（现在是漏的，见 P1-4）。
- **新增 E2E 正例测试**（这一条才是真正的"没白做"反证）：
  `register_hypotheses(强信号假说) → evaluate_prereg(到期期号) → load_registry 里 prereg_ok is True → build_snapshot → rules[0]["usable"] is True`，**全程不 monkeypatch 任何 `lottery/` 内部**。
- **新增反向用例**：把 `evaluate_prereg` 的调用点注释掉 → **上述 E2E 必须变红**。缺这条，P0-2 就没有完成证明。

---

#### P0-2　`var_ratio`"可为负"是错误结论，且方案要求的负值测试**不可构造**

方案 §3.4.2 坑 1（:801）：

> 「**`var_ratio` 可以为负**（n=10、`Ā=0` → **−3.000**）。它不是"方差比"，而是协方差结构的线性泛函。」

**三重错误，逐条复算（脚本 `/tmp/vr.py`、`/tmp/minratio.py`，全部用 `fractions.Fraction` 精确算，无浮点误差）**

| 主张 | 我的复算 | 结论 |
|---|---|---|
| 系数 `11/54` 与闭式 `var_ratio = 1+(n−1)(11/54)(Ā−12/11)` | 由 `Var[T]=nσ₁²+2[(27/176)ΣA−(81/484)C(n,2)]` 代数展开，`(C(n,2)/(n·729/968))·(27/88)=(n−1)·11/54` ✅；常数项 `=(n−1)·2/9` ✅ | **推导正确** |
| n=10、Ā=0 → −3.000 | `1+9·(11/54)·(0−12/11) = 1−2 = **−1.000000**`；−3.000 需要 `1−(n−1)·2/9=−3` → `n=19` | **算术错** |
| n=10、Ā=0 可构造 | Ā=0 ⇔ 10 注两两不相交 ⇔ 需 60 个互不相同的红球号，**双色球只有 33 个** → **几何上不可能**（`n≥6` 就做不到 Ā=0：6 注已占 36 个号位） | **前提不可能** |
| "可为负"（一般命题） | 对**真实票组**穷举 n=2..33 的最小可行 Ā = `min Σ C(c_k,2)` s.t. `Σc_k=6n, c_k≤n`，再算 ratio：**全局最小 = 0.0**（n=11 每号恰 2 次、n=22 每号恰 4 次、n=33 每号恰 6 次）；其余 n 均 > 0（n=6 → 0.0926，n=10 → 0.1000，n=16 → 0.0347） | **命题为假**：真实票组下 `var_ratio ≥ 0`，**永不 < 0** |

**连带后果**：
- §3.4.7 验收（:872）明文要求"**必须有** … `var_ratio` 为负的用例" → **实现者写不出来**。他只有三条路：(a) 只测解析式、手工喂一个不可能的 `Ā`（那是测公式不是测票组，与方案"测试必须用构造性票组，禁止用随机票组"的要求同源冲突）；(b) 造假；(c) 删除该用例并违规。
- §3.4.7 还要求前端显示 `var_ratio` 并"**带'可为负'的说明**" → 会把一个**假命题**写进用户界面。
- 附带：方案 :801 说"`var_T` 可为 0，但非负"（:349）—— 这一条**是对的**（我的穷举中 n=11/22/33 最极端构造恰为 0）。

**表格第 4 行自相矛盾（:797）**：

| 方案写的票组 | 方案写的 ΣA | 方案写的 ratio | 我的复算 |
|---|---:|---:|---|
| 4 注不相交 + 1 注重叠 1 号 | **6** | **0.600** | **两种读法都到不了 6**：①"第 5 注与那 4 注**各**重叠 1 号"→ ΣA=**4**、Ā=0.4、`Var[T]=1.645661`、ratio=**0.437037**（实测构造 `{1,2,3,4,5,6},{7..12},{13..18},{19..24},{1,7,13,19,25,26}`）；②"第 5 注**只**与其中 1 注重叠 1 号"→ ΣA=**1**、Ā=0.1、ratio=**0.0648**。ΣA=6/Ā=0.6/`Var[T]=2.259298`/0.600 是**另一组**数字，与标签不匹配 |

（前三行我全部复算通过：5 注全同 ΣA=60 → `Var[T]=18.827479`、ratio=**5.000000**；5 注不相交 ΣA=0 → `Var[T]=0.418388`、ratio=**0.111111**；Ā=12/11 → ratio=**1.000000**，我用 n=11/ΣA=60 构造验证，且检查了 n=11 时 `C(11,2)·12/11=60` 为整数、构造可行。）

**修法**：把"可为负"整段换成正确命题 + 一句边界说明：
> `var_ratio ∈ [0, +∞)`；下界 0 在"每个红号恰被 n/… 次覆盖"的极端票组上取到（n=11 且每号恰 2 次时 `Var[T]=0`、ratio=0）；`Ā=12/11` 为中性点 ratio=1；`Ā>12/11` 才 >1。**并删掉 `n=10、Ā=0 → −3.000` 与"负值用例"**，改为"**下界用例**：n=11 每号恰 2 次 → ratio==0（±1e-12）+ `var_T==0`"。

---

#### P0-3　`resolve_channels()` 的 `source` 层在现有代码上**无法判定**（T1/T3 写不出来）

方案 §2.1.2 冻结了 `"source": "methods_config.json" | ".env" | "default"`，并在 T1/T3 要求断言来源。但真实代码把三层**揉进了同一个变量**：

```python
# lottery/config.py:266-267   _load_runtime_methods_config()（import 期执行，见 :413）
        METHODS_RAW = candidate
        os.environ["LOTT_METHODS"] = METHODS_RAW      # ← json 覆盖后写进 env
# lottery/config.py:239-243   set_methods()（设置页运行时更新）
        os.environ["LOTT_METHODS"] = METHODS_RAW      # ← 用户改设置也写进 env
```

后果（三条，都能在测试里立刻炸）：

1. **json 生效后，`.env` 的原始值被抹掉** → `os.environ["LOTT_METHODS"]` 的值**永远等于生效值**，`source` 无法由它反推。T3（json 写 `stat,ml` + env 写 `stat` → `source=="methods_config.json"`）**如果实现者用 `os.environ` 判断，必然误报 `".env"`**。
2. **T1 在真实生产目录上不成立**：`data/methods_config.json` 一旦被设置页写出（`config.py:278 _save_methods_config`，当前该文件**不存在**，`ls data/` 已确认），此后每次启动都会 import 期覆盖 → 永远拿不到 `source=="default"`。
3. **`os.environ.get("LOTT_METHODS", "")` 把"设为空串"与"未设置"合并**（`methods.py:57-58` 就是 `raw = os.environ.get(ENV_NAME, "")`）→ 设 `LOTT_METHODS=""`（= 全开）与不设，来源不同、取值相同，`source` 判不出来。

**修法（必须写进 §2.1）**：`resolve_channels()` **不得**从 `os.environ` 推断来源，改为：
- 读**文件**判第 1 层（`METHODS_CONFIG_FILE.exists()` 且含 `raw`/`mode` 键）；
- 用 `"LOTT_METHODS" in os.environ`（**不是** `.get(..., "")`）判第 2 层；
- 都不命中 → `"default"`；
- 并在 `config.py` 增加一个 `METHODS_SOURCE: str` 全局，由 `_load_runtime_methods_config`（→`"methods_config.json"`）、`set_methods`（→`"methods_config.json"`）、import 期 env 分支（→`".env"`）、兜底（→`"default"`）各自写入，`resolve_channels()` 直接读它。**这一条不写，三个测试写不诚实。**

---

### P1（不修则实现者必然写错，且测试可能假绿）

#### P1-1　`_spec_canonical()` 的 6 个组成项里，**3 个在假说记录中不存在**

方案 :653 定义 `exp_id = sha1(canonical(spec))`，`spec = {family_key, params, target, test, direction, min_samples}`。
实测一条真实假说记录的字段（`data/hypotheses.json` 第 1 条）：

```
['action','attempts','created_at','evidence','family','id','last_seen_at','n_samples',
 'params','refutes','regime','source','statement','status','supports']
```

- `family_key` ← `family` ✅；`params` ← `params`（或 `action.filter.params`）✅；`target` ← `discovery.FAMILY_BY_KEY[key]["target"]` ✅（20 个 family 全有，`target` 缺省 `"red"`）
- **`test` / `direction` / `min_samples`**：**三个都不存在**，仓内也无任何同义字段（`evidence` 里有 `p_value`/`baseline`/`lift`，但那是**观测结果**不是**检验设计**）。

→ 实现者必须自行编造默认值。三个人编三套 → `exp_id` 不稳定 → **P1（"改截止期号 → 同一 exp_id"）会时红时绿**。
**修法**：写明这三个字段的**取值来源与默认值**（例：`test="replay_delta"` 常量、`direction` 取 `action` 的白名单枚举、`min_samples=config.METHOD_RECOMMENDATION_MIN_ISSUES`），并把它们**同时**列为 §3.3.2 的落地字段。

#### P1-2　`exp_id` 去重键与既有 `_dedup_key` 的"兼容"**无解**：两种选择各破一头

方案 :653 写「`exp_id` 与 `_dedup_key` 兼容（同规律不得在两处各占一条、`attempts` 分裂）」。但现有去重键是 **statement 文本**：

```python
# lottery/hypothesis.py:1249-1255
def _dedup_key(hyp):
    st = hyp.get("statement")
    if isinstance(st, str) and st.strip():
        return f"statement:{st.strip()}"
    ...
```

两条路都被现有测试堵住（`tests/test_v4_hypothesis.py:472-497`，485 基线之一）：

```python
def _hyp(statement, family="f", action=None, baseline=1.09):
    return {"family": family, "statement": statement,
            "action": action or {"type": "prefer_numbers", "numbers": [1, 2]}, "params": {}, ...}
...
hyps = [_hyp("S1"), _hyp("S1"), _hyp("S2", family="g"), _hyp("S3", family="f"), ...]
r1 = H.register_hypotheses(hyps, reg_path)
assert r1["registered"] == 3 and r1["deduped"] == 1
```

- 选 **(a) 保留 statement 去重、另加 exp_id**：`_hyp("S1")` 与 `_hyp("S3", family="f")` 的 spec **完全相同** → 同 `exp_id`、两条记录 → **`attempts` 分裂**，违反方案自己的要求。
- 选 **(b) 改成 exp_id 去重**：上面那条测试的 `registered` 会从 3 变成 2 → **基线由绿转红**（方案 §5.4 第 1 条硬要求"0 failed"）。

**修法**：二选一并写死，同时写明**要改哪些存量断言**。推荐 (b) 并同步改 `test_v4_hypothesis.py:472-497`（把 S3 的 action 改掉以保持不同 spec），并在方案里显式登记"本条会改一条存量测试的期望值"——**绝不能让它默默红**。

#### P1-3　`register_hypotheses` 的去重分支**只回写 4 个字段**，新字段会被静默丢弃

```python
# lottery/hypothesis.py:1289-1293（命中已有 statement 时的分支）
        cur["attempts"] = _as_int(cur.get("attempts"), 0) + 1
        cur["last_seen_at"] = _now_iso()
        for field in ("evidence", "n_samples", "params", "source"):
            if norm.get(field) is not None:
                cur[field] = norm[field]
```

方案 :654 明确要求「改 `evaluate_after_issue` 只是'改预约'，**不产生新 exp_id**」。但按现有分支，**第二次注册时传入的新 `evaluate_after_issue` 根本不会被写入** → "改预约"静默无效，而 P1 测试只断言 `exp_id` 相同与 `attempts` 累加，**仍然会绿** → 假绿。
**修法**：在方案里明写去重分支的**回写白名单如何扩展**（`evaluate_after_issue`/`frozen_until_issue`/`batch_id` 可回写；`prereg_at`/`exp_id`/`n_tested_cumulative` **不可回写**），并加测试"改预约后 `load_registry` 里的 `evaluate_after_issue` 真的变了"。

（补一条**好消息**：新记录分支用 `rec = dict(norm)`（`:1305`）**全字段透传**，`_save_registry`（`:1233`）与 `annotate_hypotheses`（`evolution.py:470` 整库 load-modify-save）都不会丢新字段 —— 所以 §3.3.1 的"加字段零迁移成本"**成立** ✅。）

#### P1-4　12 个字段**不足以表达** R1/R2/R5 —— 至少缺 4 项

逐条对表（方案 :650-664 vs :668-675）：

| 约束 | 需要什么 | 12 字段里有吗 | 结论 |
|---|---|---|---|
| **R1** 累计台账唯一分母、双口径、`divergence` | `n_tested_cumulative` ✅ `n_tested_batch` ✅ `p_batch` ✅ `p_cumulative` ✅ + **`divergence`** | **无 `divergence` 字段**（只在 §3.3.5 的 `rules.prereg` 里被读，没有人在假说记录上写它） | 缺 1 个字段，且**谁比较两套结论、谁写 `divergence`** 未定义 |
| **R1** "永久累计"的权威计数 | 一个**库级**单调计数器（否则每条记录各自累计 N，互不可信） | **无**（`_empty_registry()` 只有 `{version, updated_at, hypotheses}`，`hypothesis.py:1206-1208`） | 需在库根加 `prereg: {n_tested_cumulative: N, batches: [...]}` 结构，方案未提 |
| **R2** 批 `M_min≥10`、凑不满**并入下一批** | 跨调用的"待决批"缓冲（9 条暂存 → 下次 +1 条成批） | `batch_id` / `n_tested_batch` **表达不了"未成批暂存"** | 需明确用"同 `batch_id` 且 `prereg_status=="registered"` 的记录集合"作为缓冲，且**批号生成规则**要写死 |
| **R5** 注册速率 ≤10 条/**期** | **`registered_issue`**（哪一期注册的） | `prereg_at` 是 float **时间戳**，无法映射到期号 | **缺 1 个字段** |
| **R5** 超限"必须填 `reason`" | `reason` | **无** | **缺 1 个字段**（且 `reason` 该是 `prereg_reason`，避免与 `regime.reason_zh` 混淆） |
| **R3** 截止期独立字段 | `evaluate_after_issue` ✅ | 有 | ✅ |
| **R4** 冻结期 ≥150 期、禁止回退 | `frozen_until_issue` ✅ + 状态机断言 | 有 | ✅ |
| **R6** `provisional` 不进 `usable` | `prereg_status` ✅ | 有 | ✅ |
| `prereg_ok` 公式里的 `alpha` | 字段或配置常量 | **无** | 需写明取哪个 alpha（仓内已有 `config.METHOD_RECOMMENDATION_ALPHA`，但方案说的是"检验 α"，两者口径可能不同） |

**修法**：字段表从 12 → **16**（+ `divergence`、`registered_issue`、`prereg_reason`、`alpha` 或在库根加计数结构并说明）。

#### P1-5　AI 岗位 A 的"复算口径"与 `evaluate.aggregate()` 的**实际返回对不上**

方案 :543 硬性要求「复算统一走 `evaluate.aggregate()` 的口径（`evaluate.py:57-`），**不得**在 `ai_audit.py` 里另写一套均值逻辑」。但 `aggregate` 的真实返回（`evaluate.py:56-79`，`def aggregate` 在 **:56**，不是 :57）：

```python
return {"issues", "tickets",
        "red_hits_mean": float(np.mean(all_red)),      # ← 名字不是 red_mean，且不 round
        "red_hits_dist", "levels_dist",
        "blue_hit_rate": float(np.mean(all_blue)),     # ← 名字不是 blue_rate
        "prize_rate_ge5", "prize_rate_ge4",
        "best_level_run": int(np.max(all_lvl)),        # ← 是 max(奖级)，不是众数
        "reward_total", "reward_mean_per_issue", "cost_total", "roi"}
```

方案要的三项**没一项能直接取到**：

| 方案要的字段（:526-529） | `aggregate` 给的是 | 差距 |
|---|---|---|
| `red_mean`（`round(...,4)`） | `red_hits_mean`（**未 round**） | 名字 + 规整都要另写 |
| `blue_rate`（`round(...,4)`） | `blue_hit_rate`（**未 round**） | 同上 |
| `best_level`：**"出现次数最多的奖级（并列取最高）、无中奖则'无'"** | `best_level_run = max(levels)`（**最高奖级**，int） | **语义不同**（众数 vs 最大）；需从 `levels_dist` 自己取 argmax + tie-break + 中文名 |
| 中文奖级名"见 `engine` 的 `LEVEL_NAMES`" | **`engine` 里没有 `LEVEL_NAMES`**（`grep -rn "LEVEL_NAMES" lottery/` 零命中） | 真实常量是 `evaluate.py:12` `PRIZE_NAME = {1:"一等奖(≈*)", 2:"二等奖(≈*)", 3:"三等奖", …, 6:"六等奖"}`，**一二等奖带 `(≈*)` 后缀** |

后果：**方案禁止另写逻辑，但它要的字段 `aggregate` 里没有** → 实现者只能"偷偷另写"，而方案第 2 个"金标准来源"就失效（判定标准被架空）。同时测试 6「桩把 `best_level` 写成"二等奖"（实际六等奖）→ fail」如果金标准取 `PRIZE_NAME[2]="二等奖(≈*)"`，字符串永远不等 → **误判方向都可能反**。
**修法**：给出**唯一**的复算函数签名，例如 `ai_audit.golden_narrator(rows) -> dict`，内部：按 method 分组 → 用 `evaluate.tickets_result` 造 `runs` → 调 `evaluate.aggregate` → 再做 `round(...,4)` 与 `levels_dist` argmax + **明确的中文名映射表（照抄 `PRIZE_NAME` 并写清是否带 `(≈*)`）** → 输出与岗位 A schema **同构**的 dict。方案里把"不得另写均值逻辑"改成"**只允许一层薄适配**：均值必须来自 `aggregate`，中文名与众数规则在此函数内实现"。

#### P1-6　`golden_ok` 的规格**签名写错，照抄必抛 TypeError**

方案 :594-601：

```python
def golden_ok(key, params) -> tuple[bool, list[str], str]:
    """金标准 = 三项既有校验的合取（与生产同源）：
         a) llm_lab._params_ok(key, params)               ✅ 签名是 (key, params)，llm_lab.py:325
         b) evolution.discovery_params_ok(key, params)    ❌ 真实签名是 (f: Dict)，evolution.py:545
         c) key ∈ discovery.FAMILY_BY_KEY
    """
```

`evolution.discovery_params_ok(f: Dict)` 收的是**整个 filter** `{"key":..., "params":...}`，不是 `(key, params)` 两个位置参数 → 照抄即 `TypeError: discovery_params_ok() missing 1 required positional argument`。
**修法**：`golden_ok` 内部写成 `evolution.discovery_params_ok({"key": key, "params": params})`。

**附带发现（设计层，非打字错）**：两个函数**逻辑等价**，都是"`params` 是否精确等于 `FAMILY_BY_KEY[key]["params"]` 里某一档"（`llm_lab.py:325-332` 与 `evolution.py:545-553` 唯一差别是 `fam["params"]` vs `fam.get("params")` 与 `dict()` 包装）。因此：
- **"两来源一致性"是假的**（同一张表的两次查表），`reason="gold_disagreement"` **在生产上永不发生**，只有测试 7 用 monkeypatch 才能造出来 → 该分支实际是死代码；
- 好消息：**金标准确实是确定性的**（纯 dict 查表，无 rng、无网络、无 I/O，`FAMILY_BY_KEY` 是 import 期常量），**可以当金标准** ✅ —— 这一点方案问的问题答案是"可以"。

**修法建议**：把 b) 换成**真正独立**的第二来源（例如"`discovery.make_filter(key, params)` 不抛异常"），或直接说明"a/b 同源，`gold_disagreement` 仅为防御性分支（永不触发的死代码，接受）"。后者更省事，但必须写清楚，否则实现者会以为要造一个"两个独立校验"。

#### P1-7　岗位 B 的"reason 码"与输出 schema 不一致，测试 4 无法实现

方案 :610 要求「未知 key → 金标准 False，`reason` 必须是 `"unknown_key"`」；测试 4（:617）要求「桩说 false 但 **reason 缺失** → `invalid`」。
但岗位 B 的**输出 schema**（:583-588）里，`ok=false` 的条目只有 `offending`（字段名数组）与 `reason_zh`（**自由中文**），**没有机器可读的 reason 码**。判定器无法判断"这个 false 的理由是不是 unknown_key"，只能做中文子串匹配（脆弱且方案未授权）。
**修法**：在输出 schema 增 `"reason_code": str`（枚举 `{"out_of_grid","unknown_key","missing_param"}`），或把规则改成"未知 key → `offending` 必须含 `"key"`"（复用已有的可审计性规则，与 :608 一致）。

#### P1-8　`edit`/默认值：T1 与 §2.1.1 的默认值**互斥**（照写必红一条）

- 方案 §2.1.1（:186）：「代码默认 `METHODS_RAW` 缺省 **""（全开）改为 `stat,ml,uniform`**（allow-list）」。
- 方案 T1（:230）：「什么都不设 → `source=="default"`、**`families=={stat, blend, ml, uniform: True, llm: False}`**」。

我用真实函数验证（`methods.implement_spec` + `is_enabled`）：

```
spec = methods.implement_spec('stat,ml,uniform')
stat True
blend False      ← 与 T1 的 blend:True 冲突
ml True
uniform True
llm False
```

`blend` **不在** allow-list 里 → `families["blend"] is False`。T1 与 §2.1.1 **必有一条要改**：要么默认改 `stat,blend,ml,uniform`（则 §2.1.1 随之改），要么 T1 断言 `blend:False`。**必须二选一写死**，否则实现者会顺手"让 blend=True"从而**悄悄改变生产出号行为**（这正是 00a "不改变行为"承诺的红线）。

#### P1-9　静态闸门（T9 / 纪律 1）在 **00a 阶段必然红**，且会被注释命中

方案 :220 的闸门：
```bash
grep -rn "LOTT_METHODS\|LLM_ONLY_OUTPUT" lottery/ | grep -v "methods.py" | grep -v "^lottery/config.py"
```
实测现在（内容命中，`__pycache__` 除外）：

```
lottery/api_app.py:778:    methods: LOTT_METHODS 字符串（...）        ← docstring
lottery/engine.py:1589:  reason = "LLM 方法通道已被方法开关关闭（LOTT_METHODS / 设置页方法开关）"  ← 字符串
lottery/engine.py:1711:  # LLM 实际生成注数模式（默认开，LOTT_LLM_ONLY_OUTPUT=0 关闭）  ← 注释
lottery/engine.py:1717:        config.LLM_ONLY_OUTPUT                   ← 真实代码（04b 才删）
```
- `engine.py:1717` 是**真代码**，归 **04b**（方案自己在 §2.1.7 就是这么排的：04b 改 `:1712-1725`）。但闸门被写在 **00a** 的"硬性纪律 1"里、又被 Gate-2 引用 → **00a 完成时 Gate-2 必然红**。
- 另三处是注释/docstring，**任何 grep 式测试都会被误伤**，方案 T9 只说"用 `pathlib` 扫源码 + 白名单"，没写"**必须跳过注释与字符串字面量**"。

**修法**：闸门**分两阶段**（00a 阶段白名单 `engine.py`，04b 完成后转严格）+ 写清"扫描前剥掉 `#` 注释与三引号字符串，或直接用 `ast` 遍历 `ast.Name/ast.Attribute`"。后者才是可实现的。

#### P1-10　§2.2 的 fixture 名与**已落地**的 `tests/conftest.py` 不符（新测试会 ImportError/挂）

方案 :432-440 规定新测试用 `isolated_db` fixture；§5.4 第 5 条（:1124）再次要求"必须用 `isolated_db` fixture 造库"。但返工已落地的 `tests/conftest.py`（实测存在，`git log 8955d37`）提供的是**另外三个名字**：

```
tests/conftest.py:68   def isolated_env(tmp_path)
tests/conftest.py:113  def make_draws()
tests/conftest.py:135  def seeded_db(isolated_env, make_draws)
tests/conftest.py:107  @pytest.fixture(autouse=True) def _auto_isolate_lottery_paths(isolated_env)
```
且它是 **autouse 全局隔离**（连 `llm_lab.CLAIMS_FILE` / `patterns_runtime.SNAPSHOT_FILE` 都重定向），方案里 19 处裸 `DB_PATH` 的设定也已过时（实测**真代码 0 处**，3 处命中全是注释）。

**修法**：§2.2 与 §5.4 改为照抄已落地的 fixture 名（`isolated_env` / `seeded_db` / `make_draws`），并把 §2.2 标注为 **"已落地（V10-00b）"**，只保留**未落地项**：`LOTT_SLOW_TESTS`（返工自述"未实现"，见 `V10_REWORK_VERIFICATION.md` §四 第 2 项）。

#### P1-11　`replay_results` 的 `PRIMARY KEY (method, issue)` **与 `mode` 冲突，会互毁数据**

方案 §2.1.5 DDL（:286-298）：`PRIMARY KEY (method, issue)`，`mode ∈ {replay, research_arm}`；§2.1.5 写语义表（:319）规定 `INSERT OR REPLACE`。
而 §3.5.2 第 4 条（:889）与 §3.6（:900）要求**同一 `method`、同一 `issue`** 分别出现在：
- `mode='replay'`（离线回放，`run_replay(draws, methods, mode="replay")`）
- `mode='research_arm'`（每期离线跑一次 `stat` 池 + `llm` 探针，写 `mode='research_arm'`）

例如 `stat:freq` / `2026119` 在两个模式都会出现 → **同一主键 `(stat:freq, 2026119)`** → `INSERT OR REPLACE` 让后写的**覆盖**先写的（研究臂数据被回放覆盖，或反之）；用 `OR IGNORE` 则静默丢弃。**两种写语义方案都规定了，但都会丢数据**，且方案 §3.6 验收第 3 条"幂等（同 `(method,issue)` 重跑不增行）"还会因为主键撞车而**假绿**。
**修法**：主键改 `(method, mode, issue)`（或 `(method, mode, issue, channels_fp)`），并同步改 §3.6/§3.9 的幂等断言为 `(method, mode, issue)`。

（**兼容性检查通过的部分**：新 `migrations.apply` 已改为"整批一个事务 + autocommit 下 `BEGIN IMMEDIATE` + 备份 fail-closed"（`migrations.py:_run_batch` / `apply`），`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX` / `INSERT OR REPLACE` / `INSERT OR IGNORE` 全部在单事务内，**语义兼容** ✅；`tests/test_v10_migrations.py:29` 与 `tests/test_m4_5_engineering.py:11` 都已用 `MIGRATIONS[-1][0]` 而非写死 `2`，**新增第 3 号迁移不会破这两处** ✅ —— 方案 §2.1.5 要求改 `test_m4_5_engineering.py:11` 的那条**已经做过了**，属于过期指令，见 P2-6。）

> **迁移加固的测试覆盖我已做变异验证**（评审期间观察到工作树里出现一处把 `_run_batch` 的 `BEGIN IMMEDIATE` 换成 `pass` 的临时变异，随后被还原 —— 应为其他评审/测试同事的突变测试）：该变异让 `tests/test_v10_migrations.py` **2 failed**（`test_failed_migration_rolls_back_whole_batch_and_keeps_backup`、`test_backup_failure_aborts_migration`），还原后 5 passed、全量 **485 passed**。结论：**V10-00d 的加固有真实测试守卫**，方案 §2.4 想防的"半迁移"风险已被覆盖，V10-02 可以放心在其上加第 3 号迁移。

#### P1-12　§2.1.7 文件域表**漏了 02 对 `api_app.py` 的改动**（首轮 F3 同类错误再现）

§2.1.7（:378-401）把 `api_app.py` 的被改方列为 **02 / 10 / 11 三方**（:383）—— 写的是"`/api/prereg/*`"路由。这没错，但真正的问题是：**让 `prereg_status` 推进的那段逻辑必须在调度链路上被调用**，而调度链路在 `api_app.py`（`_scheduler_step`，`:1142`；`:1176` 调 `patterns_runtime.refresh`；`:1261` 调 `annotate_hypotheses`）与 `llm_lab.run_closed_loop`（`llm_lab.py:540`）里。方案既没指定调用点（P0-1），也没把 `llm_lab.py` 列为 02 的文件（`llm_lab.py` 在表里只归 **01**，:386）。
→ 实现者一定会动 `llm_lab.py` 或 `api_app.py`，**与 01 抢 `llm_lab.py`、与 10/11 抢 `api_app.py`**。这正是首轮 F3 被判定"并行度虚报"的同一类错误。

**修法**：P0-1 定下调用点后，把该文件明确划给 02，并在 §4.2 冲突表里加一行。

---

### P2（不修不阻塞，但会误导实现者或让评审结论失真）

**P2-1　行号漂移（`usable` 的 :104 → 实际 :110）**
方案 6 处引用 `patterns_runtime.py:104`（:168, :644, :694, :1027, :1240 等），实测该行是 `usable = (...)` 在 **:110**（`:104` 是 `spec = evolution.hypo_structure_filter(h)` 附近）。建议改成 `patterns_runtime.py:110` 或直接写"`build_snapshot` 里的 `usable = ...` 那一行"。同类：`evaluate.aggregate` 在 **:56**（方案写 :57），影响很小。

**P2-2　基线数字再次过时：480 → 485**
方案 20+ 处写 `480 passed`（:8, :56, :163, :411, :458, :1110, :1115, :1199 等），实测 **485 passed / 77.28s**（返工新增 `tests/test_v10_migrations.py` 5 例）。同时 §5.4 第 3 条"新增 ≤100（与 480 合并后 ≤580）"应为 **≤585**。虽然 Gate-3 写的是"≥480"（仍成立），但方案以"基线数字必须精确"为立身之本（F2），自己又错一次，建议全量替换为"**当前基线 485（以实跑为准，写文档时重跑）**"。

**P2-3　里程碑天数与任务表不自洽**
§4.1 总量 19.0 人日（各行 1+0.5+0.5+0.5+2+2+2+1.5+1.5+1+1.5+1+2+1+1，我核过 = **19.0** ✅）。
§4.4 里程碑：M0 **2.5**（= 00a..00d 之和 2.5 ✅）、M1 **4.5**（实际 04a 0.5 + 01 2 + 02 2 + 03 2 = **6.5** ❌）、M2 **5**（04b 1 + 05 1.5 + 07 1 + 08 1.5 = 5 ✅）、M3 **3**（09 1 + 10 2 + 11 1 = **4** ❌）、M4 **1** ✅。合计标注 16 人日 vs 任务表 19。
**修法**：明确里程碑用"人日"还是"日历日（2 路）"，并让两栏相加一致。另外"19 人日 ÷ 2 ≈ 10 工作日"是按**人力上限**估的（我算关键路径只有约 6.5～7 个工作日），两句话要标注口径，否则用户会以为要 10 个日历日。

**P2-4　M0 的 1.5 人日已花掉、`migrations.py`/`conftest.py`/备份已落地，方案 §2.3/§2.4 仍写成"待做"**
`git log` 与 `docs/reviews/V10_REWORK_VERIFICATION.md` 证实 V10-00b/c/d 已完成（conftest、备份 cron + 恢复演练 3515、迁移事务 + `schema_version` 进 health），只有 **00a 未做**。
方案 §2.0 "约 2.5 人日"、§4.1 的 00b/00c/00d 三行、M0 里程碑都应改成"**已落地（剩余：`LOTT_SLOW_TESTS`）**"，否则排期虚增 1.5～2 人日。同时 §2.4 的"规格"（:476-486）描述的**已经是实现后的行为**，应改为验收清单（他的 `backup_path_for` 命名是 `pre-migrate-v<cur>`，方案写的是 `pre-v{latest}.bak`，两处不一致，以代码为准）。

**P2-5　`COVERAGE_OBJECTIVE` 别名表：现有取值只有 2 个，且 `p_any_blue` 在本仓**根本不存在**
`config.py:210-212`：
```python
COVERAGE_OBJECTIVE = (os.environ.get("LOTT_COVERAGE_OBJECTIVE") or "expected_value").strip().lower()
if COVERAGE_OBJECTIVE not in ("coverage", "expected_value"):
    COVERAGE_OBJECTIVE = "expected_value"
```
方案 §2.1.4 别名表（:254-260）覆盖 `expected_value`/`coverage` ✅、新增 `pmax_high`/`custom_weight`（属值域扩展，可接受）。但 `p_any_blue` 在 `lottery/` 里 **grep 零命中**（只有 `_ticket_objective` 的注释与 `blue_coverage_plan` 的闭式解），所以 §2.1.4「`p_any_blue` 不作为可选值」与 §3.4.3「**删除 `p_any_blue`**…**降级为报告项**」（:812）是对**不存在的东西**下指令；"降级为报告项"更像"新增一个报告字段"，容易被实现者当成要做的事。建议改成一句"`p_any_blue` 从未是代码里的可选项（v1.0 文本臆造），**无需任何代码改动**；若前端要把 `blue_hit_prob` 展示为报告项，那是 V10-10 的展示项"。
另：`engine.py:1734` 把 `config.COVERAGE_OBJECTIVE` 传给 `coverage_optimize(objective=...)`，而 `_ticket_objective` 只识别 `"coverage"` 与"其它"（`engine.py:1400-1409`）。别名表把 `coverage` **映射走**之后，`_ticket_objective` 里 `coverage` 分支将成为死分支 —— 方案 §3.4.3 说"objective 收敛为 3 个"却**没写 `_ticket_objective` 的 3 分支怎么落**（这是 03 的核心改动之一）。建议补一句映射：`ev_conserving → 只输出结构指标（不做优化，直接返回）`、`pmax_high → 以 P(≥1注≥4红) 为目标（需枚举/近似，见 §3.4.4）`、`custom_weight → 权重来自 P2-3`。

**P2-6　§2.1.5 的"改 `test_m4_5_engineering.py:11`"是过期指令**（已改成 `MIGRATIONS[-1][0]`，实测 `tests/test_m4_5_engineering.py:9-19`）——保留无害，但会浪费实现者时间，建议标注"已落地"。

**P2-7　`var_ratio` 表格第 4 行（见 P0-2）与"三处 `?v=`"的数字**：`?v=` 实测仍是 `20261018.3 ×2 + 20260817.7 ×1`（与方案一致 ✅，`doctor` 仍 WARN ✅）。

**P2-8　`eval_details` 的 method 数**：方案附录 B（:1209）写"5 期 / 30 行 / **9 个 method**"，实测 `select count(distinct method) from eval_details` = **7**（30 行 / 5 期对 ✅）。不影响设计，但这是"数字来源台账"里的一条错数，建议改 7 或注明口径（如把 `llm:` 多模型算多）。

---

## 四、必须修复项（6 条，否则不能开工 V10-01）

> 全部是**文档级**修复，估 **0.5 人日**；建议由方案作者一次改完，再开 02/01/03。

1. **【P0-1】补"写者"规格**：新增 `hypothesis.evaluate_prereg(...)` 的**完整签名 + 幂等语义 + 调用点（`_scheduler_step` 或 `patterns_runtime.refresh` 二选一写死）+ 库根累计计数结构**；并新增**两条测试**：① E2E 正例（真实 `register_hypotheses` → `evaluate_prereg` → `build_snapshot` → `usable is True`，**不 monkeypatch 任何 `lottery/` 内部**）；② 反向用例（注释掉调用点 → 该 E2E 必须红）。**没有这两条，U1–U5 不构成"没白做"的证明。**
2. **【P0-2】改 `var_ratio` 的"可为负"**：删 `n=10、Ā=0 → −3.000` 与 §3.4.7 的"负值用例/可为负说明"；改为"`var_ratio ≥ 0`，下界 0 在 n=11 每号恰 2 次时取到（`var_T=0`）"；并修正 §3.4.2 表格第 4 行（该构造 ΣA=**4**/Ā=0.4/`Var[T]=1.645661`/ratio=**0.437037**）与 §3.4.7 的断言值。
3. **【P0-3】补 `source` 层的判定机制**：`resolve_channels()` 读**文件 + `"LOTT_METHODS" in os.environ`**（不是 `.get`），并由 `config.METHODS_SOURCE` 记录 origin；T1/T3 的前提从"生产环境真实 repo"改为"临时 `LOTT_HOME`"（否则 T1 在已有设置页写过的机器上必红）。
4. **【P1-8 + P1-6 + P1-5 + P1-7】修四处"照抄会错"的细节**：默认值 `stat,ml,uniform` 与 T1 的 `blend` 二选一；`golden_ok` 里 `discovery_params_ok({"key":key,"params":params})`；岗位 A 复算改成"薄适配层 + 冻结的中文名映射表"（`LEVEL_NAMES` 不存在，引用 `evaluate.PRIZE_NAME` 并写清 `(≈*)`）；岗位 B 输出 schema 加 `reason_code`（或改用 `offending` 规则）。
5. **【P1-4 + P1-1 + P1-2 + P1-3】**：字段表 12 → 16（`divergence`/`registered_issue`/`prereg_reason`/`alpha`）+ 库根计数器；写死 `test`/`direction`/`min_samples` 的取值来源；**明确选 (a) 还是 (b) 去重键**并登记"要改的存量测试"（`test_v4_hypothesis.py:472-497`）；写明去重分支的可回写白名单。
6. **【P1-11 + P1-9 + P1-10 + P2-4/P2-2】**：`replay_results` 主键改 `(method, mode, issue)`；静态闸门拆两阶段 + 用 `ast` 而不是裸 grep；§2.2 fixture 名改成已落地的 `isolated_env`/`seeded_db`/`make_draws` 并标注 00b 已落地；基线 480→**485**；§2.3/§2.4 标"已落地"。

> 附加建议（不阻塞）：把 §4.4 里程碑的天数改成与 §4.1 相加一致，并把"10 个工作日（人力上限）"与"关键路径 ≈7 个工作日"分开写。

---

## 五、我认为"实现者最可能写错的三处"及防护测试

### 第一处：**`patterns_runtime` 读 `prereg_ok` 了，但没人写它 → 上线后仍显示"0 条可用规律"**

为什么最容易错：方案 §3.3.5 **只给了读者**，而且给了 5 条看起来很像样的测试（U1–U5）。实现者照着 U1 写完，`monkeypatch` 注入一条"完美假说"，**测试全绿、`usable=True` 一次**，于是宣布 P0-2 完成。而生产上 `prereg_ok` 恒 `None`，`usable` 恒 0 —— 与修复前**一模一样**。这是本项目已发生过一次的病（V6→V9 四轮"闭环空转"），也是 R1 原话警告的那件事。

**防护测试（必须新增，写进 §3.3.5）**：
```python
def test_usable_is_reachable_from_real_pipeline(seeded_db, tmp_path):
    """反例守卫：从真实入口一路走到 usable==True，全程不 mock lottery/ 内部。
    若有人把 evaluate_prereg 的调用点删掉/没接线，此例必须变红。"""
    reg = tmp_path / "hypotheses.json"
    # 1) 造一条"必然通过"的假说（强注入信号），走真实 register
    hypothesis.register_hypotheses([strong_hyp], registry_path=reg)
    # 2) 走真实 prereg 判定（到期期号由 FROZEN_ISSUES 决定，构造 draws 覆盖它）
    out = hypothesis.evaluate_prereg(draws, issue=target_issue, registry_path=reg)
    rec = {h["id"]: h for h in hypothesis.load_registry(reg)["hypotheses"]}[strong_hyp["id"]]
    assert rec["prereg_ok"] is True and rec["prereg_status"] == "passed"
    # 3) 走真实快照（只 mock 数据来源，不 mock build_snapshot）
    snap = patterns_runtime.build_snapshot(draws)
    assert any(r["usable"] for r in snap["rules"]), "prereg 通过了但 usable 仍为 0 → 接线断了"

def test_usable_pipeline_breaks_if_producer_removed(monkeypatch):
    """反向用例：让 evaluate_prereg 变成 no-op，上一条必须红（用 pytest.raises 或直接断言 False）。"""
    monkeypatch.setattr(hypothesis, "evaluate_prereg", lambda *a, **k: {"ok": True})
    ...
    assert not any(r["usable"] for r in snap["rules"])   # 证明"有写者"是必要的
```
> 这两条的价值：**U1 证明"会读"，它们证明"会写"**。缺写者时第一条红 —— 这才是 Gate-8 的真正判据。

### 第二处：**`var_ratio` / `var_T` 用"解析式直接吃票组"时口径错位（`σ₁²` 与 `C(n,2)` 的 2 倍）**

为什么最容易错：闭式里三个数字都在（`n·σ₁²`、`(27/88)ΣA`、`(81/242)C(n,2)`），而 `R2` 原文把系数印成 `11/27`（2 倍手误）、方案又要求"照抄不要自行改系数"，同时 `Cov = (27/176)A − 81/484` 与 `Var[T]` 里的 `27/88`、`81/242` 差一个 2 倍（是 `2×Cov` 的结果）。实现者极可能：① 直接把 `11/27` 抄进去（→ 5 注全同得 10.0 而不是 5.0）；② 把 `ΣA` 当成 `Ā` 传给闭式（少除 `C(n,2)`）；③ 用 `n·σ₁²` 但 `σ₁²` 取了 `discovery.SD_HITS**2`（对 ✅）或取了 `HITS_VAR` 的别的值。

**防护测试**（取值必须**精确**，不许近似）：
```python
@pytest.mark.parametrize("tickets,sumA,varr,ratio", [
    ((["01-06","same"]*5),        60, 18.827479, 5.0),      # 5 注全同
    (five_disjoint_tickets(),      0,  0.418388, 1/9),      # 5 注不相交
    (eleven_tickets_each_number_twice(), 33, 0.0, 0.0),     # 下界 0（n=11，每号恰 2 次）
])
def test_var_ratio_analytic_exact(tickets, sumA, varr, ratio):
    td = engine.ticket_distribution(tickets)
    assert td["sum_A"] == sumA
    assert abs(td["var_T"] - varr) < 1e-9
    assert abs(td["var_ratio"] - ratio) < 1e-12
    # 交叉验证：直接逐对求和 Cov，不得依赖闭式（防"闭式自证"）
    direct = sum(2*((27/176)*A - 81/484) for A in pairwise_A(tickets)) + len(tickets)*729/968
    assert abs(direct - td["var_T"]) < 1e-9
def test_abars_neutral_point():
    """Ā=12/11 → ratio==1；且 ratio 随 Ā 单调递增（方案自己保留的那条断言）。"""
def test_var_ratio_is_never_negative():   # 覆盖 n=2..20 的极端构造
    ...
```
> 关键：**交叉验证一例必须"逐对算 Cov 再加"**，否则闭式抄错会自证成功（方案已经踩过一次：R2 的 11/27 与它的边界值同时"正确"）。

### 第三处：**AI 岗位 A 的"误差 0"实现成"近似比对"**（以及把 `degraded` 计进分母）

为什么最容易错：`允许误差 0` 的字面写法 `abs(got-exp) <= 0` 看起来奇怪，实现者会"顺手"改成 `<= 1e-6`（或 `abs(a-b) < 1e-9`）以求测试好过；一旦放水，方案的核心卖点（"AI 的输出有唯一答案"）就变成"近似算对也算对"。第二类错：把 `degraded`（没做）混进分母 → 正确率虚高，正是方案 §3.2.1 明确禁止的"把'经常不做'伪装成'很少做错'"。

**防护测试**：
```python
def test_judge_is_exact_not_approximate():
    """把任一数字改动 1e-9 → 必须 fail（而不是 pass）。"""
    out = perfect(); out["per_method"][1]["red_mean"] += 1e-9
    assert judge_narrator(inp, out)["verdict"] == "fail"

def test_degraded_never_enters_denominator():
    before = audit_denominator()                 # pass+fail+invalid
    res = judge_narrator(inp, "抱歉我无法完成")   # 非 JSON
    assert res["verdict"] == "degraded"
    assert audit_denominator() == before         # 分母不动
    # 且 UI 文案必须是"未取得"，不得是"已核对"
    assert "未取得" in res["ui_text"] and "已核对" not in res["ui_text"]

def test_rounding_uses_same_round_as_golden():
    """LLM 输出 1.0833 vs 金标准 1.0833 必须 pass；输出 1.083 必须 fail（少一位小数）。"""
```
> 另外建议：方案应写明"**pass 率预计会很低**（LLM 算 4 位小数极易错）"以及**预注册的退役判据**（例如"前 30 次判定 pass+invalid ≥ 90% 且 pass < 20% → 岗位转为只报 degraded/停用"）。否则会出现"永远红、无退出条件"的岗位，成本会无限增长。

---

## 六、复核通过项（给实现者的"可以信任"清单）

以下论断我逐一在真实代码/数值上验证**成立**，实现者**可以照方案写**：

1. **`usable` 加 `prereg_ok` 的改法在代码结构上成立**：`h` 来自 `evolution._as_hyp_list(hypothesis.load_registry())`（`patterns_runtime.py:101`），只归一类型、**不投影字段**，因此假说记录的新字段原样可达 → `h.get("prereg_ok")` 有效 ✅。`_save_registry`/`annotate_hypotheses`/`resolve_registry` 都是**整库 load-modify-save**，不会丢新字段（`hypothesis.py:1233`、`evolution.py:470`）✅。
2. **"加字段零迁移成本"成立**：`_empty_registry()`（`hypothesis.py:1206-1208`）只有 `{version, updated_at, hypotheses}`，JSON 文件无 schema 约束，新字段直接写 ✅。
3. **`source=="llm"` 与 `status` 的现有语义**：`HYPOTHESIS_STATUSES = ("proposed","validated","rejected","retired")`（`hypothesis.py:107`）；`source="llm"` 确由 `hypothesis.py:980` 与 `evolution.py:539` 写入 → 方案 U4（llm 永不 usable）有真实依据 ✅。
4. **死锁诊断成立**：`battle.py:77-84` `NON_REPLAYABLE_METHODS` 明确排除 `ml`/`llm`；`hypothesis.py:1501-1507` 只有"支持"才 `validated`；`patterns_runtime.py` `USABLE_STATUSES=("persistent",)` → "llm 且 usable"恒空 ✅（方案 G1 后门封死设计正确）。
5. **`Cov` 闭式与 `σ₁²`**：`A=6` 时 `(27/176)·6 − 81/484 = 729/968 = 0.753099173553719` 精确等于超几何 `Var(K)` ✅；`discovery.SD_HITS=0.8678128678198537` 与之相符 ✅。
6. **`var_ratio` 闭式与三个边界值**：精确有理数验证，`5 注全同=5.0`、`5 注不相交=1/9=0.1111`、`Ā=12/11→1.0`，偏差 **0**（不是 <1e-15，是精确相等）✅。系数 **11/54 正确**，R2 的 11/27 确为 2 倍手误 ✅。
7. **`p₄` 与 iid 上界**：`(C(6,4)C(27,2)+C(6,5)C(27,1)+1)/C(33,6) = 5428/1107568 = 0.0049008277596…` → 方案 `0.004900828` ✅；`1−(1−p₄)^5=0.0242651`、`^10=0.0479415` ✅（"覆盖铺满 n=10 → −2.6%"与 `0.046887/0.048150` 自洽）。
8. **随机 10 注 `Ā` 的分布**：我独立 MC（5000 次）：`Ā = 1.0899 ± 0.128`、5–95% `[0.889,1.311]`、`var_ratio 0.998 ± 0.235`、`P[ratio>1]=0.439`（方案：`1.0935±0.1306`、`[0.889,1.333]`、`1.005±0.239`、`0.447`）—— 在 MC 噪声内 ✅。因此"禁止用随机票组比较两票组"的告诫**正确且重要**。
9. **AI 金标准确定性**：`llm_lab._params_ok`（:325-332）与 `evolution.discovery_params_ok`（:545-553）都是对 `discovery.FAMILY_BY_KEY` 的**纯查表**（20 个 family 全部有 `params` 列表且无缺失键，实测），**无 rng、无网络、无 I/O** → **可作金标准** ✅。
10. **`engine.py` 的三处缺陷定位准确**：`:1409` 确为死代码 `return float(ev) + cover_bonus * n / max(1, n)`（`cover_bonus*n/max(1,n) ≡ cover_bonus`）✅；`:1493-1496` 确为 `for t, b in zip(out, plan): t["blue"] = int(b)` 强制改写蓝球 ✅；`_ticket_objective` 在 `:1386` ✅。
11. **`evaluate.py:146-147` 的 `int(np.mean(...))` 截断**确在（`:146`）✅，方案诊断"把 1.5 抹成 1"成立。
12. **`regime.build_adaptive_eras` 从后往前切**确有：`regime.py:140` `while end - block >= start_at: bounds.append((end-block, end)); end -= block`，最后 `bounds.reverse()` → 每加一期边界整体平移，历史段 p 值会变 ✅（V10-07 的核心诊断**成立**）。
13. **`ml_model._cache_key` 不含特征版本**：`ml_model.py:384-387` `return f"{len(draws)}:{draws[-1]['issue']}"` ✅ → 方案 V10-08 的 265MB 旧 joblib 复用风险**是真实的**。
14. **迁移 #3 与现有 `migrations.apply` 兼容**：批次事务（`_run_batch`，autocommit + `BEGIN IMMEDIATE`，已有事务时 SAVEPOINT）+ 备份 fail-closed；`INSERT OR REPLACE`/`INSERT OR IGNORE`/`CREATE TABLE IF NOT EXISTS` 全部可放在同一事务内 ✅；`test_v10_migrations.py:29`、`test_m4_5_engineering.py:11` 已用 `MIGRATIONS[-1][0]`，加第 3 号迁移**不会**破基线 ✅（唯一要求见 P1-11：主键要含 `mode`）。
15. **`docker-compose.yml` 未透传 `LOTT_METHODS`/`LOTT_LLM_ONLY_OUTPUT`**：实测 env 列表（`:19-62`）确无这两项 ✅ → 方案 §2.1.3 坑 2 **成立**。
16. **前端 `cfgLlm` 默认 checked + `localStorage` 吃掉后端默认**：`web/index.html:38` `<input type="checkbox" id="cfgLlm" checked …>`、`web/static/app.js:724`（写）/ `:728-730`（读）✅ → 方案 §2.1.3 坑 1 **成立**。（补充：方案漏了 `app.js:2627` `$("#cfgLlm").checked = !payload.disabled` —— 设置页刷新时也会**重新勾上**，04b 需一并处理。）
17. **`data["usage"]` 完全没读**：`llm_client.py:241-251` 只统计 `_usage["calls"]/prompt_chars/completion_chars`，**未读 `usage.prompt_tokens/completion_tokens`** ✅。
18. **无残留**：§0.2 五个删除项在正文中的命中**全部**落在"台账 / 为什么不做 / 任务表删除行 / 附录自查清单"四种语境，**没有一处是可执行的实现步骤** ✅（唯一要改的是方案自称的 Gate-7/附录 C 结论为假，见下）。
19. **依赖图无环**：00a→{01,02(经00d),03,04a,07}、00c→00d→02→05→09、03+04a→04b、01→11、{01,02,03,07}→10、全部→12 —— 我按 §4.1 的依赖列逐条走了一遍，**无环**，且 §4.3 的串行序列与依赖一致 ✅。
20. **估时量级合理**（个别偏紧）：02（12→16 字段 + 6 约束 + 状态机 + 16 测试）与 03（目标函数改造 + 票单分布 + holdout + 20 测试 + 1.1M 枚举缓存）各 2 人日**偏紧**，但不算离谱；01 的 2 人日（两岗位 + 20 测试 + 桩）合理。**建议给 02、03 各 +0.5 人日缓冲**（→ 20 人日），并在 M1 里程碑里体现。

---

## 七、给 Lead 的一句话

**方向与数值都站得住（`var_ratio` 我精确复算全对，金标准确实是确定性的，`usable` 的读侧接线也确实能改）——问题只有一类：方案规定了读者、没规定写者，还顺手写错了一个"可否为负"的断言和四处会直接抛错/对不上的接口细节。这 6 条全是文字活，0.5 人日能修完；修完即可开工 V10-01，不必再返工设计。**
