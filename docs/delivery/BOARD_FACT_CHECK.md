# 看板 ⊆ 代码事实 —— 人工核对清单

> 核对人：严QA · 2026-10-21 15:12（UTC）
> 方法：对每个状态标记，回答三问
> ① 该状态是「代码事实」还是「别人告诉我的」？
> ② 若是代码事实，依据是**哪一行**？（必须能贴出来）
> ③ 若依据是文档/报告，那它**本身有多老**？
>
> 覆盖范围：**我曾发现错位的 5 个 + 全部 P0/P1**（按 Lead 要求）。
>
> ⚠️ 与 `docs/delivery/repro/board_status_check.py` 的关系：
> **不合并。** 脚本查「已声明的符号」防回归；本清单查「人眼发现的新错位」防遗漏。
> 两者互补，一个是自动化，一个是人工发现。

---

## 一、状态总表（15:12 实测）

| 项 | 看板状态 | 我的实测 | 一致？ |
|---|---|---|---|
| **D-01** | 待验收（两卡已接） | `app.js` 有 `renderBlueCoverage` / `fillHonestBounds`；探针未跑 | ⚠️ **部分** |
| **D-02** | 待签收（代码未动） | `engine.py` 3107 行、`git status` 干净、`lottery/engine/` 不存在 | ✅ 一致 |
| **D-03** | 进行中 | `routes_portfolio.py` 有 8 处 `Query(`、400/503 分支齐全；`test_d03_boundary_hardening.py` 存在 | ⚠️ **实为「待验收」** |
| **D-04** | 待验收 | `lottery/routes_capabilities.py` 20565 字节（13:51）、端点 + 守卫测试在位 | ✅ 一致 |
| **D-05** | 实施进行中 | `docs/delivery/D05_COPY_SPEC.md` 存在 | ✅ 一致 |
| **D-06** | 实施进行中 | 同上（规格合一） | ✅ 一致 |
| **D-07** | 进行中 | `idle_mining.py:682` 有 `usable_semantics`；禁并列测试在位 | ✅ 一致 |
| **D-08** | ✅ 已签收（`9214692`） | 四条缺陷全落地；五判据 + 我加的两条 | ✅ 一致 |
| **D-11** | 已交付 | `docs/delivery/QA_GATE.md` 存在 | ✅ 一致 |
| **D-12** | 待验收（有撞车） | 引号豁免已删；`test_quoted_visible_mention_is_not_exempt` 在位 | ⚠️ **实为「待复验」** |
| **D-13** | **未开始** | `docs/reviews/` 下**无**对应交付物（`ls *BOUND*` 零命中） | ✅ **确实未开始** |
| **D-14** | 新增 | `tests/test_qa_self_guards.py` 有 2 条 `test_pass_constraints_parity_*` | ⚠️ **实为「已完成」** |
| **D-16** | 新立项 | `4fd5a0e` 已提交；`no_fit_on_request_path.py` 结构判据在位 | ⚠️ **实为「已签收」** |
| **D-17** | 待 D-08 之后 | 静默 `except` 仍在（`api_app.py` 多处） | ✅ 一致（待做） |

---

## 二、四处需要改的（证据齐全）

### ① D-16：看板「新立项」→ 实为**已签收**

```bash
$ git log --oneline -1 -- lottery/popularity.py
4fd5a0e fix(D-16): 冷启动 28.5s —— 请求路径与拟合路径结构解耦
$ ls docs/delivery/repro/no_fit_on_request_path.py
docs/delivery/repro/no_fit_on_request_path.py
```
**证据层级**：代码事实（已提交 commit + 结构判据脚本存在）。
**我的验收结论**：三组数据 + 五档变异全部通过，Lead 已口头签收。
→ **看板应改「✅ 已签收」**。

### ② D-14：看板「新增/待定」→ 实为**已完成**

```bash
$ grep -l "test_pass_constraints_parity" tests/*.py
tests/test_qa_self_guards.py
```
两条用例：`test_pass_constraints_parity_branch_directly`、
`test_pass_constraints_parity_is_isolated_from_other_dims`（后者是我加的反空转）。
**证据层级**：代码事实。
→ **看板应改「✅ 已完成（严QA 自建，含变异实证）」**。

### ③ D-03：看板「进行中」→ 实为**待验收**

```bash
$ grep -c "Query(" lottery/routes_portfolio.py        → 8
$ grep -n "status_code=503\|status_code=400" ...      → 400×4 + 503 分支
$ ls tests/test_d03_boundary_hardening.py             → 存在，31 条
```
**证据层级**：代码事实 + 测试存在。
⚠️ **但我不能仅凭"文件存在"判它完成** —— 需要按判据逐条变异。**标「待验收（判据未跑）」更准确。**

### ④ D-12：看板「待验收（有撞车）」→ 实为**待复验（撞车已解除）**

```bash
$ grep -c "def _is_discipline_mention" tests/test_v14_naming_discipline.py  → 0（已删）
$ grep -c "test_quoted_visible_mention_is_not_exempt"                       → 1（防复发守卫）
```
**撞车已解除**：李工的 6 条 + 我的 5 条共存，23 passed。
→ **看板应改「待复验（23 passed，无撞车）」**。

---

## 三、两条「看着像完成、但要小心」的

### D-01 前端接线
`app.js` 里 `renderBlueCoverage` / `fillHonestBounds` 都在，**但浏览器探针未跑**。
Lead 已决定：**合并到 D-01 一起验**（前端稳定后一次性跑）。
→ **看板应注明「探针未验证」**，不能只写「两卡已接并渲染成功」——
「代码里有」≠「页面上能看到」。

### D-13 能力边界文档
`ls docs/reviews/*BOUND* docs/reviews/*V14_CAP*` → **零命中**。
→ **看板「未开始」准确**。✅

---

## 四、我这次核对**没能覆盖**的

诚实标注：

| 项 | 为什么没覆盖 |
|---|---|
| D-01 的 19 条「本轮不做」 | 看板自己写「本轮不做」，无需核 |
| D-05/D-06 的实施细节 | 是文案，需浏览器渲染才能验，归 D-01 探针 |
| D-02 的方案内容 | 1219 行方案我读过结构，但**没逐条验算**其 10 刀切法是否成立 |
| D-17 的修复 | 未开始，无处可核 |

---

## 五、这份清单的局限（必须写明）

> **它只能发现「我能想到要去 grep 的东西」。**

**我两次差点漏**：
1. D-16 我一开始以为"新立项"正确 —— 直到我去查 `git log` 才发现 `4fd5a0e` 已提交。
2. D-14 我以为"待定"正确 —— 直到我 grep 自己写的测试文件。

**换句话说：这份清单的有效性，取决于我是否想到去查某个符号。**
**这正是齐交付的脚本不可替代的原因** —— 脚本只要声明了，就永远会查。

**两者必须并存：**
- **脚本**：防「声明过的东西回退」
- **人工**：防「没人想到要声明的东西」

---

## 六、建议的动作（给齐交付）

| # | 建议 | 理由 |
|---|---|---|
| 1 | D-16 → `✅ 已签收` | 代码 + 结构判据 + 三组数据齐 |
| 2 | D-14 → `✅ 已完成` | 两条用例在位且有变异实证 |
| 3 | D-03 → `待验收（判据未跑）` | 代码齐但未逐条验 |
| 4 | D-12 → `待复验（撞车已解除）` | 23 passed，无并发冲突 |
| 5 | D-01 → 加注「浏览器探针未验证」 | 代码在 ≠ 页面可见 |
| 6 | **新增 D-18（P2）**：16 处 `assert ... in src` AST 化 + 元守卫 | 反空转/反误报，见 §七 |

---

## 七、附：本轮新发现（不在看板上）

### D-18 候选：16 处 `assert ... in src` 源码断言（P2）

```bash
$ grep -rn '^\s*assert .*in src' tests/*.py | wc -l    → 16
$ grep -rln '^\s*assert .*in src' tests/*.py
tests/test_v14_naming_discipline.py    (3)
tests/test_v10_ticket_distribution.py  (4)
tests/test_v10_llm_arm.py              (4)
tests/test_v4_rigor.py                 (2)
tests/test_v4_evolution_ui.py          (1)
tests/test_mu0_prereg_wiring.py        (2)
tests/test_v14_stats_package.py        (1)
```

**共同的病**：`getsource` 含 docstring/注释 ⇒「源码里出现过字符串」≠「代码真的做了某事」。
**两个方向都会错**：
- **假通过**：删掉真实调用、只在注释里留着 → 仍绿（本轮判据⑤ 就是这么被抓的）
- **假红**：注释里写「本函数不使用 X」→ 守卫无辜变红，逼人改注释

**建议**：
1. 立 **D-18（P2）**：逐批 AST 化
2. **加元守卫**（比逐条改更重要）：
   ```python
   # 扫 tests/**.py，发现 `assert ... in src` 且不在白名单 → 红
   # 白名单初始 16 处，每改完一处移除一条（进度可度量）
   ```
   **元守卫保证「不再新增」，逐条改只减少存量。**

> **注**：这 16 处**不在我写域**（多为他人文件），我不擅自改，仅登记。
