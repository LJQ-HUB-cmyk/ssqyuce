# 引擎等价指纹偏离（`b3a2a3e6…` → `3165f786…`）诊断报告

> 日期：2026-10-23 ｜ 执行：Lead（Claude Code 子代理未能完成，遗留结论已由 Lead 独立复核推翻）
> 关联用例：`tests/test_v14_engine_split.py::test_predict_next_output_is_byte_identical_to_pre_split_baseline`
> 关联背景：`docs/analysis/V15_ASSUMPTION_SPACE.md` §5

---

## 0. 结论先行

| 问题 | 结论 |
|---|---|
| 是 V15 引入的吗？ | **不是**。V15 之前的 `8952c04` 上实测同样是 `3165f786…`。 |
| 是 D-02 拆分系列引入的吗？ | **不是**。`97220d0`（D-02 起点）与 `a2ce0b2`、`72e59d0`、`8952c04`、HEAD 五点实测**全部**为 `3165f786…`。 |
| 是数据期数变化吗？ | **不是**。截断最后 0/1/2/3/…/12 期，指纹逐个变化（`f769cfdb`、`d2141b9a`、`a29ed47c`…），无一命中 `b3a2a3e6`。 |
| 是 `hypotheses.json`（后台挖掘）漂移吗？ | **不是**。清空该文件前后指纹完全一致（`3165f786…`），正是 `a2ce0b2` 当初修的成果。 |
| 是剥离集（`_VOLATILE`）变化吗？ | **不是**。63 种剥离组合穷举枚举，无一能得到 `b3a2a3e6`。 |
| **真正原因** | **`PREDICT_FINGERPRINT` 常量自 `a2ce0b2` 起就是错的。** 那次提交记录了「第二次漂移 51226704 → 5f45e262 → b3a2a3e6」并声称验证过（清空 `hypotheses.json` 前后都是 `b3a2a3e6`），但**在 `a2ce0b2` 这个 commit 本身**上实测，实际值就是 `3165f786…`。 |
| 严重度 | **低**。`3165f786…` 跨进程稳定可复现（三次独立进程完全一致），说明引擎行为本身是确定的；坏的是**测试的期望常量**，不是代码。 |
| 当前状态 | 代码无回归。用例之所以红，是常量写错，不是引擎变了。 |

**一句话**：这不是「引擎行为漂移」，而是「**D-03 那次提交把常量写成了一个从未被复现过的值**」。
所以此前的「D-02 拆分偷偷改了行为」这个怀疑，**可以正式排除**。

---

## 1. 二分/排查过程（每个数字都是实跑）

统一指纹脚本（`/tmp/fp.py`）：固定 `rng=random.Random(42)`、`use_llm=False`、`use_ml=False`、
`n_tickets=5`、`persist=False`；剥离 `_VOLATILE` 六字段后 `json.dumps(sort_keys=True)` 取 sha256 前 32 位。

### 1.1 逐提交实测

| 提交 | 日期 | 实测指纹 | 说明 |
|---|---|---|---|
| `97220d0` | D-02 起点 | （常量当时是 `d5196ca3`，且**当时还没有** `_VOLATILE`） | — |
| `a2ce0b2` | 2026-10-22 04:28 | **`3165f786e1f6710450bec49a2fbb5e6b`** | 常量在此被设为 `b3a2a3e6`，但实测值不是它 |
| `72e59d0` | 2026-10-22 09:33 | `3165f786…` | 与上一点相同 |
| `8952c04` | V15 前 | `3165f786…` | 与上一点相同 |
| `443eb2f`（HEAD） | V15 后 | `3165f786…` | 与上一点相同 |

**关键**：从 `a2ce0b2` 到 HEAD，**五个点的实际指纹一个字都没变**。
所谓「D-02 之后某个提交引入了偏差」不成立 —— 偏差在 `a2ce0b2` 写下常量的那一刻就已经存在。

### 1.2 排除「数据期数变化」

在 `a2ce0b2` 的 worktree 上，按不同期数截断后实测：

| 截断期数 | 使用期数 | 最后一期 | 指纹 |
|---|---|---|---|
| 0 | 3518 | 2026121 | `3165f786…` |
| 1 | 3517 | 2026120 | `f769cfdb…` |
| 2 | 3516 | 2026119 | `d2141b9a…` |
| 3 | 3515 | 2026118 | `a29ed47c…` |
| 4 | 3514 | 2026117 | `c2e2fce0…` |
| 5 | 3513 | 2026116 | `f035783e…` |
| 6 | 3512 | 2026115 | `df433e21…` |
| 7 | 3511 | 2026114 | `41ca7f99…` |
| 8 | 3510 | 2026113 | `3a0d9e65…` |
| 10 | 3508 | 2026111 | `4836ed7f…` |
| 12 | 3506 | 2026109 | `99acd2fe…` |

期数确实是敏感输入（每变一期指纹就变），但**没有任何期数能产出 `b3a2a3e6`**。
`a2ce0b2` 提交时库里已经是 3515 期左右，而 3515/3516 两档的值都不匹配。

### 1.3 排除「`hypotheses.json` 运行时漂移」

该文件在 **2026-10-22 13:37** 被后台挖掘改写（晚于 `a2ce0b2` 的 04:28）。
实测：把它替换为空的合法结构后重跑，指纹**完全不变**（`3165f786…`），随后已原样还原（22773 字节，`cmp` 校验一致）。
说明 `_VOLATILE` 已把该文件的全部影响面剥离干净 —— 这正是 `a2ce0b2` 当初修的成果，**修得是对的**。

### 1.4 排除「剥离集变化」

穷举 `{generated_at, elapsed_ms, snapshot, note_zh, updated_at, patterns_summary}` 的全部 2⁶ = 64 种组合
（含空集），在 `a2ce0b2` 上逐个计算，**无一命中 `b3a2a3e6`**。
参考值：全剥离 → `3165f786…`；仅剥 `generated_at` → `613de552…`。

### 1.5 确定性确认

在 HEAD 上连跑三个独立进程，指纹三次完全相同（`3165f786…`）⇒ 引擎输出是确定的，
失败不是随机抖动。

---

## 2. 结论与建议

### 2.1 定性

`b3a2a3e6` 是一个**从未被真实复现过的常量**。`a2ce0b2` 的提交说明里写着
「清空 `hypotheses.json` → `b3a2a3e6` ✅ 完全一致」，但该结论在它自己的 commit 上就复现不出来
（实测 `3165f786…`）。最可能的情形是：那次验证遇到的输入状态（期数、或某个当时的路由/配置）
与最终提交时的代码状态不一致，验证结论与提交内容脱节。

**因此**：
- 「D-02 拆分改变了引擎行为」—— **排除**。五个点的实际值完全一致。
- 「引擎有 bug」—— **无证据**。`3165f786…` 稳定、可复现、且五个历史点一致。

### 2.2 建议（三步，按优先级）

1. **把常量更正为 `3165f786e1f6710450bec49a2fbb5e6b`**，并在 commit message 里写明：
   这是 `a2ce0b2` 记录错误的常量，不是"让测试变绿"；
   证据是本报告 §1.1 的五点实测 + §1.5 的三进程确定性。
2. **给该用例加一条防退化措施**：把指纹口径（剥离集、`n_tickets`、`rng` 种子、期数）写进
   失败信息，避免下次再出现"不知道常量对应什么状态"。
3. **让指纹对数据期数不敏感**（可选）：目前每多开一期，指纹就变，等于**每期开奖都会让这条用例变红**。
   建议在 fixture 里把 `draws` 截断到固定期数（例如 3500 期），使指纹只反映「代码」。
   这才是 `a2ce0b2` 原意（「指纹只应反映代码 + 历史开奖」）的完整实现 ——
   当前实现只做了「+ 全部历史开奖」，而"全部"会随时间增长。

> 第 3 条是关键设计缺陷：**一条每次开奖都会红的真值锁，等于没有真值锁**。
> 它会训练出「红了就改常量」的习惯，而这正是该用例 docstring 明令禁止的。

### 2.3 处置结果（2026-10-23 已实施）

维护者批准后，第 2、3 条已落地（commit 见 `git log --grep=指纹口径`）：

1. **口径：截断到固定 `FINGERPRINT_DRAWS = 3500` 期**（取最早 3500 期 `draws[:3500]`，
   永不受后续开奖影响）。fixture 里加断言：真实库不足 3500 期时**显式失败**，不静默缩小样本。
2. **常量更正为 `09cef0f617b096c6a111ff5ad614e8f3`** —— 这是**新口径下实跑得到**的值
   （不是旧口径的 `3165f786`，口径变了常量必然跟着变）。用例注释里写清了来历与判据。

**抗漂移验证（决定性）**：向库中插入一期假开奖（`9999001`）后重跑该用例 → **仍然 PASS**；
旧口径下同样操作必红。验证后已精确删除该行，并与生产备份
（`ssq-backup-20261023-002112.tar.gz`，3518 期 / 末期 2026121）独立核对一致。

**全量结果**：`pytest tests/ -q` = **1680 passed, 24 skipped, 1 xfailed, 0 failed**
—— 这是本项目首次全量零失败。

> 注：本报告 §2.2 原列了三条建议；第 1 条「把常量改成 3165f786」因第 3 条口径变更而**被取代** ——
> 新口径下正确值是 `09cef0f6…`。两条不能同时做，否则会互相矛盾。

---

## 3. 附录：复现

```bash
# 统一指纹（HEAD）
cat > /tmp/fp.py <<'PY'
import sys, json, random, hashlib, os
root = sys.argv[1]; sys.path.insert(0, root)
os.environ['LOTT_TEST_REAL_DB'] = '1'
from lottery import config as _cfg; _cfg.guard_prod_db(allow_real=True)
from lottery import db, engine
d = db.load_draws()
o = engine.predict_next(d, use_llm=False, use_ml=False, n_tickets=5, persist=False,
                        rng=random.Random(42))
VOL = {"generated_at","elapsed_ms","snapshot","note_zh","updated_at","patterns_summary"}
def strip(x):
    if isinstance(x, dict): return {k: strip(v) for k,v in x.items() if k not in VOL}
    if isinstance(x, list): return [strip(v) for v in x]
    return x
print(hashlib.sha256(json.dumps(strip(o), sort_keys=True, ensure_ascii=False)
                     .encode()).hexdigest()[:32])
PY

LOTT_HOME=/root/DSH/应用/ssqyuce .venv/bin/python /tmp/fp.py /root/DSH/应用/ssqyuce
# → 3165f786e1f6710450bec49a2fbb5e6b
```

**环境要点**：必须设 `LOTT_HOME` 指向数据根。`LOTT_DATA_DIR` 是**不存在的变量名**，设了会被静默忽略
（见 `lottery/config.py` 头注释，项目里因此踩过两次写生产库的坑）。worktree 里 `data/` 被 gitignore 不存在，
不设 `LOTT_HOME` 会让 `load_draws()` 返回空 → IndexError。

**worktree 用法**：`git worktree add /tmp/w_<sha> <sha>` → 测完
`git worktree remove --force /tmp/w_<sha> && git worktree prune`。不要在共享工作区 `git stash`。
