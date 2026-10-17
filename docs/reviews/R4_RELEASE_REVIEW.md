# R4 评审：测试与发布工程（V10 改进方案的工程交付部分）

> 评审对象：`docs/V10_IMPROVEMENT_PLAN.md`（方案稿 v1.0，2026-10-17）
> 评审维度：测试规约可执行性 / 测试运行成本 / 前端改版风险 / 部署与回滚 / 版本与文档同步 / 交付流程完整性
> 评审基线：仓库 `/root/DSH/应用/ssqyuce`，HEAD = `af5b601`，评审日期 2026-10-17
> 评审方法：全部数字为本机实测（命令与输出见各处证据块）；评审期间未修改任何 `.py` / 前端 / 配置，未重启容器。

---

## 1. 一句话结论

**需修改后认可** —— 方案的工程骨架（任务拆解、验收口径、诚实边界）是扎实的，但有 4 处**必须前置修正**才能排期：基线数字写错（381 → **480**）、新增测试的文件命名会触发项目历史上真实的 DB_PATH 泄漏缺陷、生产库**当前没有任何备份**（唯一数据副本处于无保护状态）、以及 7 个提交（V6–V9）从未推送到 GitHub 导致远端仍停在 v2.1.0。

---

## 2. 评分表

| 维度 | 分数 | 理由 |
|---|---|---|
| **测试规约可执行性** | **2 / 5** | 基线数字实测为 480 passed，方案写 381（错 **99** 个）；`conftest.py` 根本不存在，无任何全局隔离设施；19 处裸赋值 `config.DB_PATH` 仍在泄漏，V10 新测试按 `test_v10_*.py` 命名排序在泄漏源**之后**，实测已复现失败。 |
| **测试成本可控性** | **4 / 5** | 好消息：实测各项"重"模拟在 numpy 向量化后都很便宜（ARL0 标定 0.18s、10 万次 MC 2.17s、1000 次选择性推断 1.8s）。真正的成本风险集中在**端到端 `predict_next`**（0.61s/次）与**已有的 27.61s 单测**。方案未给任何慢测试闸门机制（`LOTT_SLOW_TESTS` 不存在）。 |
| **前端风险** | **3 / 5** | `app.js` 3603 行 / `style.css` 592 行，V10-10 规模可控（约 +300~450 行）。`node` v24.19.0 可用且 `node --check` 通过。但存在**既有 `?v=` 不一致**（`doctor` 实测 WARN），且断言测试只覆盖 app.js/style.css 两路、漏掉 echarts 那一路。 |
| **部署回滚** | **1 / 5** | `data/` 绑定挂载是唯一数据副本，但**备份 cron 从未安装**（`/etc/cron.d/ssq-backup` 不存在、`/backup/` 不存在），DEPLOY.md 里那段是"待配置"的示例而非既成事实。迁移在 `get_conn()` 懒加载时自动执行且**无回滚路径**。方案对此只有一句"先 `cp` 一下"。 |
| **版本与文档同步** | **2 / 5** | 升版涉及 **7 处**文件（方案只列了 5 处），其中 `web/index.html` 有 2 个 `?v=` 令牌（含 echarts）+ 1 处硬编码 `v2.5.0`；README/DEPLOY.md **已经漂移**（仍写 v2.4.0 / V8 / 20261018.1，实际 2.5.0 / V9 / 20261018.3）。 |
| **交付流程完整性（V10-12）** | **2 / 5** | PAT 有效（scope `repo, workflow`）✅，但 `origin/main` 停在 v2.1.0 / V5，**7 个提交、6525 行改动从未推送**。方案 §3.2.6 的"部署"与 M3 的"推 GitHub"之间缺 6 个步骤。 |

**加权印象**：问题不在方案的思想，而在**方案把当前环境的真实状态当作了一个更理想的状态**（381 个测试、有 conftest、有备份、远端同步）。这几处一旦按方案默认值执行，会在第一天就踩坑。

---

## 3. 问题清单

### P0（阻塞：不修则任务无法按方案验收 / 有数据丢失风险）

#### P0-A　基线数字错误：381 → **480**

方案 `docs/V10_IMPROVEMENT_PLAN.md:287` 写「当前 `python -m pytest -q` 为 **381 passed**（2026-10-15 记录）」，`docs/PROJECT_CONTEXT.md:231` 同样写「当前本地全套 **381 passed**（2026-10-15 实测）」。

实测（当前 HEAD `af5b601`）：

```
$ .venv/bin/python -m pytest -q
480 passed, 1 warning in 84.04s (0:01:24)

$ .venv/bin/python -m pytest -q --collect-only | tail -1
480 tests collected in 1.26s
```

> 第二次跑（带 `--durations`）为 `480 passed in 114.29s`。**耗时 84~114 秒，随机器负载波动 ±36%**，这也说明"耗时阈值"不能定太死（见 P0-D）。

差值 **99** 个测试来自 V6/V7/V8 批次（`test_v6_regime.py` 56、`test_v7_rules_closure.py` 24、`test_v8_eras_tasks.py` 19 = 99），即 381 是 V5 时代的数字，从未随 V6–V9 更新。

**更正为**：全量基线 **480 passed / 0 failed**。方案 §3.2.5 与 M3 判据中的「≥ 381 + 新增数」必须改为「**≥ 480 + 新增数**」。

各文件当前用例数（实测）：

```
 57 tests/test_v4_rigor.py          24 tests/test_v7_rules_closure.py
 56 tests/test_v6_regime.py         23 tests/test_v4_blend.py
 49 tests/test_v4_battle.py         19 tests/test_v8_eras_tasks.py
 46 tests/test_v4_hypothesis.py     18 tests/test_v3_u2_u3_u4.py
 43 tests/test_v4_coverage.py       17 tests/test_v4_honest_ui.py
 32 tests/test_v4_evolution.py      17 tests/test_v4_evolution_ui.py
（其余 17 个文件合计 90）
```

---

#### P0-B　`tests/conftest.py` **不存在**，且 V10 的文件命名会踩中真实的 DB_PATH 泄漏

方案 §1 的评审委托里假设"在现有 `tests/conftest.py` / 测试隔离约定下"，实测：

```
$ ls tests/conftest.py conftest.py pytest.ini setup.cfg pyproject.toml tox.ini
ls: cannot access 'tests/conftest.py': No such file or directory
（全部不存在）
$ git log --all --oneline -- tests/conftest.py conftest.py
（无历史，从未存在过）
```

即：**项目没有任何全局 fixture、没有 pytest 配置、没有 `testpaths`、没有随机顺序插件约束**。隔离完全依赖每个用例自觉使用 `monkeypatch`。

当前仍有 **19 处裸赋值** `config.DB_PATH = tmp_path/...`（无 `monkeypatch`、无 finally 还原）：

```
$ grep -rn "config\.DB_PATH = " tests/*.py | grep -v "old = \|monkeypatch" | wc -l
19
```

泄漏源文件（实测，均按字母序排在 `test_v4_*` **之前**）：
`test_llm_compat.py`、`test_llm_failfast.py`、`test_llm_only_output.py`、`test_m4_2_api.py`、`test_m4_2_engine.py`、`test_m4_3_notify.py`、`test_m4_4_data_check.py`、`test_m4_4_db_source.py`、`test_m4_5_engineering.py`、`test_m4_eval.py`、`test_mining_artifact.py`、`test_v3_llm_budget.py`、`test_v3_llm_thinking_fallback.py`、`test_v3_u2_u3_u4.py`

**已复现的现存缺陷**（只要顺序不是字母序就会挂）：

```
$ .venv/bin/python -m pytest tests/test_m4_5_engineering.py tests/test_v4_honest_ui.py -q
FAILED tests/test_v4_honest_ui.py::test_rigor_missing_module_does_not_break_startup
  AssertionError: 缺模块应降级为 503，实际 400   ← 400=「本地暂无开奖数据」，即撞上了被泄漏的空库
1 failed, 18 passed in 2.00s

$ .venv/bin/python -m pytest tests/test_v4_honest_ui.py -q          # 单独跑
17 passed in 3.10s                                                  # 通过
```

实测 4 个独立污染源都能复现（`test_m4_2_engine` / `test_mining_artifact` / `test_m4_eval` / `test_llm_only_output` 各自 + honest_ui 均 1 failed）。

**为什么这对 V10 是 P0**：全量 `pytest -q` 目前能过 480，**纯属字母序巧合**——泄漏源都在 `test_v4_*` 之前，而 `test_v4_honest_ui.py:239` 是唯一受害用例。方案的 12 个新测试文件全部命名为 `test_v10_*.py`，**字母序排在 `test_v4_*` 之后**，正好落在泄漏源下游。我构造了一个等价探针验证（用完即删，工作树已复原）：

```
# 探针：断言 DB_PATH 仍指向真实库（>3000 期）
$ .venv/bin/python -m pytest tests/test_v10_zzz_ordering_probe.py -q
1 passed in 0.01s                                  ← 单独跑：通过

$ .venv/bin/python -m pytest tests/test_m4_eval.py tests/test_v10_zzz_ordering_probe.py -q
FAILED tests/test_v10_zzz_ordering_probe.py::test_probe_real_db_available
  assert 0 > 3000   ← DB_PATH 被泄漏成已删除的空库，count_draws() = 0
1 failed, 4 passed in 0.90s

$ .venv/bin/python -m pytest tests/test_v4_honest_ui.py tests/test_v10_zzz_ordering_probe.py -q
18 passed in 4.24s                                 ← 在 honest_ui 之后反而没事
```

**结论**：V10 的新测试若依赖真实库（`replay_results` 回放、样本量展示、`eval_details` 分列等 P0-5/P1-4 场景都会读库），**在 `pytest -q` 全量下会有相当概率直接挂在 `count_draws()==0` 上，而单独跑 `pytest tests/test_v10_xxx.py -q` 却是绿的**——方案 §3.1 给的验收命令恰好都是"单独跑单个文件"，这会完美地把问题藏起来，直到全量回归或 CI 才炸。

**必须前置解决**：(1) 建 `tests/conftest.py`，提供 `isolated_db` fixture + autouse 的 `config.DB_PATH` 快照/还原；(2) 把 19 处裸赋值收敛到 `monkeypatch`；(3) 强制新测试**不得**依赖真实库（用 fixture 造库）。

---

#### P0-C　生产库**当前没有任何备份**，"唯一数据副本"处于无保护状态

方案 §3.2.7 写「任何迁移前先 `cp data/ssq.db data/ssq.db.bak.$(date +%F)`」，`DEPLOY.md:203` 更写着「备份（**当前唯一安全网，务必配置**）」并给出 cron 行。实测：

```
$ cat /etc/cron.d/ssq-backup
cat: /etc/cron.d/ssq-backup: No such file or directory
$ ls -la /backup/
ls: cannot access '/backup/': No such file or directory
$ crontab -l | grep -c ssq
0
$ ls -la data/*.bak*
ls: cannot access 'data/*.bak*': No such file or directory
```

**DEPLOY.md 第七节的备份 cron 从未安装过**，`/backup/` 目录也不存在。也就是说 `docs/PROJECT_CONTEXT.md:262` 里 P0 项「补生产级备份与恢复校验」至今**未完成**，而方案默认它是已完成的基础设施。

叠加两个放大风险：

1. **迁移是懒加载自动执行的，无回滚路径**。`lottery/db.py:164-173`：
   ```python
   def get_conn():
       ...
       _CONN.executescript(SCHEMA)
       from . import migrations
       migrations.apply(_CONN)      # ← 首次连库即自动跑全部迁移，无 dry-run、无备份钩子
       _ensure_columns(_CONN)
   ```
   V10 要新增 3 张表（`experiments`、`llm_proposals`、`replay_results`），按方案写法是往 `lottery/migrations.py:35` 的 `MIGRATIONS` 追加条目。容器 `entrypoint.sh` 第一件事就是 `python -m lottery.cli fetch` → 触发 `get_conn()` → **迁移在容器启动的前几秒就落地了**。`docker compose up -d --build` 一旦因别的原因失败要回滚镜像，**数据库已经前进到新 schema**（`schema_version` 已是 3/4/5），旧镜像代码面对新库虽然勉强能跑（`CREATE TABLE IF NOT EXISTS` 兼容），但**新表里的数据在回滚后成为孤儿**，且如果某个迁移写了 `ALTER TABLE`，回滚 = 不可逆。

2. **`data/` 271MB，其中 265MB 是 `data/ml_models/`（5 个 55MB joblib）**。`cp data/ssq.db` 只备份了 1.4MB 的库，`hypotheses.json` / `adaptive_state.json` / `llm_budget.json` / `patterns_runtime.json` / `raw/` 全都没备份 —— 而 `DEPLOY.md:214` 明确要求这些必须一起备份。

**必须前置解决**：在 V10 任何迁移落地**之前**，先真正安装备份并做**一次恢复演练**（见第 4 节、第 5 节 checklist）。

---

#### P0-D　7 个提交从未推送，远端停在 v2.1.0；方案把"推 GitHub"当成一步就完了

方案 V10-12 的验收只写"全套回归"，M3 写"推 GitHub 并核验"。实测交付状态：

```
$ git log --oneline -1 origin/main
4eaa2fa docs: 部署形态更正为单节点（R71s.AI / BF.US 已不存在、ZT.JP.Plus 已不部署本应用）
$ git branch -vv
* main af5b601 [origin/main: ahead 7] feat(v9): ...
$ git show origin/main:lottery/config.py | sed -n '26,27p'
APP_VERSION = "2.1.0"          # v5 方案：自进化交付批次
APP_BUILD = "2026-10-V5"
$ git show HEAD:lottery/config.py | sed -n '26,27p'
APP_VERSION = "2.5.0"
APP_BUILD = "2026-10-V9"
```

**GitHub 上的 `main` 仍是 v2.1.0 / V5，落后本机 7 个提交、6525 行新增**（V6/V7/V8/V9 四个版本批次全部未推送）。而 `docs/PROJECT_CONTEXT.md:43` 记录的是「本地 HEAD = `origin/main` = GitHub 远端（三方 SHA 一致）」——**该记录已失效**。

按 V10-12 直接执行"提交 V10 并推送"，会把 V6–V9 + V10 共 8 个提交**一次性**推上去，风险与可回滚性都远比逐个版本推送差，且中间任何一次 `git bisect` 都失去意义。

好消息（已核验，方案 §5 流程本身仍成立）：

```
$ cat /root/DSH/githubtoken.txt | wc -c
40                                     ← PAT 存在
$ curl -H "Authorization: token $PAT" https://api.github.com/repos/jiam9069/ssqyuce
x-oauth-scopes: repo, workflow          ← 有推送权限，API 返回 200
$ git remote -v
origin  https://github.com/jiam9069/ssqyuce.git (fetch/push)
$ git diff --check                      ← clean
（未推送提交中无 data/*.db、无 .env、无硬编码密钥：grep sk-*/api_key 结果为空）
```

---

### P1（强烈建议：影响可维护性 / 会误导执行者）

#### P1-1　方案列出的 12 个验收测试文件，命名会与"单独跑验证"形成假绿灯

方案 §3.1 每个任务的验收命令都是 `pytest tests/test_v10_xxx.py -q`（V10-02 的"同上"指向上一个文件，**写错了**：应指 `tests/test_v10_registry.py`）。单独跑单文件**恰好规避了 P0-B 的泄漏**，无法证明全量可用。

要求：验收命令统一为两条——
```bash
pytest tests/test_v10_xxx.py -q          # 快速反馈
pytest -q                                # 必须 0 failed（这才是真实闸门）
```

#### P1-2　没有任何慢测试闸门，方案也未提出；`LOTT_SLOW_TESTS` 不存在

```
$ grep -rn "SLOW\|SKIP_SLOW" tests/*.py lottery/*.py
（无匹配）
$ grep -rn "pytest.mark" tests/*.py | grep -v parametrize | grep -v skipif
（无 skip / slow 标记，仅 1 处 skipif 用于模块缺失）
```

方案 §3.2 未提"默认快速 / 环境变量放大"的机制，而 P0-3、P1-1、P1-2 明确要求 10 万次采样、1000 次模拟、1000 条 ARL 序列。必须补上（成本实测与阈值见 P1-3、第 4 节）。

#### P1-3　成本实测量化：模拟本身不贵，贵的是端到端路径

实测（`.venv/bin/python`，Python 3.13.5 / numpy 向量化 vs 纯 Python 循环）：

| 模拟类型 | 规模 | 纯 Python 循环 | numpy 向量化 |
|---|---|---|---|
| CUSUM ARL0 标定 | 1000 序列，跑满至 ~500 步 | **0.88 s** | **0.18 s** |
| 票单分布 MC 交叉校验 | 10 万次 × 10 注 | **7.7 s** | **2.17 s** |
| 选择性推断 α 校准（TN-MLE） | 1000 次 × 300 期 | — | **1.8 s** |
| `rigor.strategy_significance` | 单次（n=3515, n_trials=91） | **2.6 ms** | — |
| `engine.coverage_optimize` | trials=3000 | **0.02 s** | — |
| **`engine.predict_next`** | 400 期、10 注、纯统计 | **0.61 s** | — |

推算（按方案要求的最小用例数）：

- `test_v10_cusum.py` ≥ 12 例，若每例都做 ARL0 标定：**2.1 s（向量化）/ 10.5 s（循环）**
- `test_v10_ticket_distribution.py` ≥ 20 例，若 3 例做 10 万次 MC：**6.5 s / 23 s**
- `test_v10_selective.py` 1000 次校准：**~2 s**
- `test_v10_channels.py` ≥ 8 例 + `test_v10_replay.py` ≥ 10 例若做端到端 `predict_next`：**≈ 11 s**
- 其余（registry / llm_decision / ml_calibration / attribution / cost）以逻辑断言为主：**≈ 5~10 s**

**合计新增 ≈ 30~50 s**（向量化良好）到 **≈ 90~120 s**（朴素循环 + 端到端滥用）。

叠加当前全量 **84.04 s**（另一次 114.29 s），V10 后全量将在 **115~165 s**（好）到 **~230 s**（差）。**不算失控，但已经到"人不想在每次提交前跑"的边缘**——这正是方案 §3.2.5 要求"每次提交前跑全量"的软肋。

注意已有的一处"定时炸弹"：`tests/test_v3_u2_u3_u4.py::test_blue_coverage_no_duplicate_across_ui_ticket_counts` 单测 **27.61 s**（3 档注数 × 6 个 seed 端到端 `predict_next`）。V10-04 改通道默认值后，这类端到端用例的行为会变，若数量再增几个就是灾难。

#### P1-4　`?v=` 不是只有 2 个令牌，`doctor` 已报不一致

方案 §3.2.6 只提"升 `?v=` 与 `APP_VERSION`/`APP_BUILD`"。实测 `web/index.html` 有 **3 处 `?v=`、2 个不同令牌**：

```
$ grep -o "v=[0-9]\{8\}\.[0-9]*" web/index.html | sort | uniq -c
      1 v=20260817.7      ← web/static/vendor/echarts.min.js（第 10 行）
      2 v=20261018.3      ← style.css（第 7 行）、app.js（第 586 行；另 footer 徽标见下）

$ .venv/bin/python -m lottery.cli doctor | grep 缓存号
  [WARN] 缓存号不一致: ['20261018.3', '20260817.7']
```

`lottery/cli.py:127-132` 的 `doctor` 逻辑是"令牌数 == 1 才 OK"，因此**当前 `doctor` 就已经是 WARN 状态**。V10 若只改 app.js/style.css 两路，`doctor` 仍会 WARN，而且 `test_v4_honest_ui.py:151` 只比较 app.js 与 style.css 两者（**不涉及 echarts**），所以测试会绿、doctor 会黄，两者口径不一致。

要求：V10-10 明确"三处 `?v=` 全部统一"，并让 `doctor` 与测试口径一致（要么把 echarts 也纳入断言，要么 `doctor` 允许 vendor 独立版本号）。

#### P1-5　`web/index.html` 的版本徽标是硬编码，无测试覆盖

```
$ sed -n '571p' web/index.html
      <footer>双色球智能预测分析系统 <span class="badge badge-ver" id="verText">v2.5.0</span>（M1-M5 + U1-U9 + V4~V8 已上线）...
```

`verText` 里写死 `v2.5.0` 且文案仍写"V4~V8 已上线"（实际已到 V9）。测试对版本的断言是**下界式**的，不会因升版失败，**也不会提醒你忘了改徽标**：

```
$ grep -rn "APP_VERSION\|APP_BUILD" tests/*.py
tests/test_v4_evolution_step.py:240: assert _ver(config.APP_VERSION) >= (2, 1, 0)
tests/test_v4_evolution_step.py:243: assert _ver(config.APP_BUILD.split("-")[-1].lstrip("V")) >= (5,)
tests/test_v4_honest_ui.py:167:      assert _ver(config.APP_VERSION) >= (2, 0, 0)
tests/test_v4_honest_ui.py:173:      assert "2026-10-V" in config.APP_BUILD
```

**逐条解读（这是评审委托里问的"是否有断言会因升版失败"的答案）**：

- `APP_VERSION = "3.0.0"` → `_ver(...) >= (2,1,0)` **通过**；`>= (2,0,0)` **通过**。
- `APP_BUILD = "2026-10-V10"` → `"2026-10-V" in APP_BUILD` **通过**；`split("-")[-1].lstrip("V")` = `"10"` → `_ver("10") >= (5,)` **通过**。
- `?v=` 断言 `>= (20261015, 2)` 且 app.js/style.css **必须相等** → 升到 `20261017.4` 之类**通过**；但**只升 app.js 不升 style.css 会失败**（这条是有效保护，V10-10 必须两个一起升）。

**结论：没有任何测试会因升到 3.0.0 / V10 而失败**。这既是好消息（升版顺畅）也是坏消息（**升版没有测试兜底**：漏改 `verText`、漏改 README 都不会红）。建议 V10-12 加一条测试：`config.APP_VERSION` 去掉 `v` 后必须出现在 `web/index.html` 的 `verText` 里。

#### P1-6　README / DEPLOY.md 已经漂移，且方案把 `.env.example` 列进 V10-04

实测文档版本现状：

| 文件 | 声称 | 实际代码 |
|---|---|---|
| `README.md:18` | 「最近升级概要（**v2.4.0** · V8）」 | 2.5.0 / V9 |
| `DEPLOY.md:81` | 「当前版本：`?v=20261018.1`（`APP_VERSION=2.4.0` / `APP_BUILD=2026-10-V8`）」 | `?v=20261018.3` / 2.5.0 / V9 |
| `DEPLOY.md:104` | 示例用 `?v=20261017.1` 验缓存 | 20261018.3 |
| `docs/PROJECT_CONTEXT.md:9-11` | 「发布版本 v2.2.0 / 2026-10-V6」§2 | 2.5.0 / V9 |
| `docs/PROJECT_CONTEXT.md:231` | 「381 passed（2026-10-15 实测）」 | 480 passed |
| `.env.example` | **无任何版本号**（只有 `LOTT_LLM_ONLY_OUTPUT=1` 注释，第 44 行） | — |

即 V9 那次交付**没有同步 README/DEPLOY/PROJECT_CONTEXT**。V10-12 的任务描述里必须包含"**先补齐 V8/V9 的文档漂移**"，否则 V10 的文档会在一个已经错了两版的基线上继续写。

`.env.example` 只有 `LOTT_LLM_ONLY_OUTPUT` 的注释行（第 44 行），V10-04 改默认值时必须同步改这行注释与默认值说明，并新增 `LOTT_PREDICT_CHANNELS` / `LOTT_LLM_DECISION` / `LOTT_SLOW_TESTS`。

---

### P2（提示 / 建议）

#### P2-1　测试环境与运行环境 Python 版本不一致

```
$ .venv/bin/python --version      → Python 3.13.5
$ docker exec ssq-predictor python --version → Python 3.11.17
```

本地全绿**不能证明容器内可用**（3.13 与 3.11 的类型语法/库行为有差异）。建议 V10-12 增加一步容器内冒烟：

```bash
docker exec ssq-predictor python -c "import lottery.api_app, lottery.registry, lottery.rigor; print('ok')"
```

#### P2-2　`?v=` 升版对公网缓存的验证方式应写进 checklist

`DEPLOY.md:104` 的验证命令依赖 `x-cache-status` 头。实测本机与公网 `/api/info` 均为 `2.5.0 / 2026-10-V9`，说明 V9 已上线；V10 发布后应验证 `curl -sI "https://ssq.ff88.de/static/app.js?v=<新令牌>"` 返回 `MISS`（新键）且浏览器能看到三张新卡。

#### P2-3　`docs/v10_probe_tickets.py` 未纳入 `tests/`，且带随机性

方案附录声称可复现，但该脚本用 `np.random.default_rng()` 无固定 seed（`docs/v10_probe_tickets.py:16,20,26`），且未被任何测试引用。建议要么进 `tests/`（固定 seed 变成回归用例），要么明确标注"一次性探针，不可复现"。

#### P2-4　模型文件清理（P1-3）应放进 V10-08 而不仅是文档

`data/ml_models/` 实测 265MB / 5 个 joblib（`3510, 3511, 3513, 3514, 3515` 期），`data/` 共 271MB。磁盘尚有 54G 可用，不紧急；但既然 P1-3 要求"只保留最近 2 个版本"，应写进 V10-08 的可执行验收（`ls data/ml_models/*.joblib | wc -l` ≤ 2）。

---

## 4. 必须前置解决的事项

> 以下 5 项必须在 V10-01 开工**之前**完成，否则后续所有测试/部署结论都建立在错误基础上。

| # | 事项 | 判据 |
|---|---|---|
| **1** | **安装真正的备份 + 恢复演练**（P0-C）。`/etc/cron.d/ssq-backup` 写入 DEPLOY.md:209 那条 cron（需先 `mkdir -p /backup`），立即手动跑一次，并**实际解压到临时目录校验 `ssq.db` 可读、期数 3515**。 | `ls /backup/*.tar.gz` 有文件；`sqlite3`/python 读出 `count_draws()==3515`；`/etc/cron.d/ssq-backup` 存在 |
| **2** | **建 `tests/conftest.py`**（P0-B）：提供 `isolated_db(tmp_path, monkeypatch)` fixture；`autouse` fixture 在每个测试前后快照/还原 `config.DB_PATH`；把 19 处裸赋值改为 `monkeypatch.setattr`。 | `pytest tests/test_m4_eval.py tests/test_v4_honest_ui.py -q` → **0 failed**（当前 1 failed） |
| **3** | **更正基线并落字**（P0-A）：方案 §3.2.5 与 M3 判据改为「≥ **480** + 新增数 / 0 failed」；同步修正 `docs/PROJECT_CONTEXT.md:231`。 | 两处文档不再出现 381 |
| **4** | **先把 7 个未推送提交推上去**（P0-D），并记录推送前后的 `origin/main` SHA；再开始 V10 工作。同时更新 `docs/PROJECT_CONTEXT.md:43` 那条已失效的"三方 SHA 一致"。 | `git log --oneline -1 origin/main` == 本机 HEAD 的父提交；GitHub API 看到 v2.5.0 |
| **5** | **补上慢测试闸门机制**（P1-2）：约定 `LOTT_SLOW_TESTS=1` 才放大规模的统一做法，并写进方案 §3.2。 | 新增测试的默认路径不含 10 万次/1000 序列规模 |

### 阈值建议（可直接写进 §3.2.5）

| 指标 | 建议阈值 | 依据 |
|---|---|---|
| 全量 `pytest -q` 默认耗时 | **≤ 180 s** | 当前实测 84.04 s / 114.29 s（两次），V10 新增预计 30~50 s → 约 115~165 s，留 15~65 s 余量 |
| 全量默认耗时（硬上限） | **≤ 240 s（4 分钟）**，超过必须拆分 | 超过 4 分钟人会跳过；`mem_limit` 与 CPU 争抢会让波动放大 |
| 单个测试默认耗时 | **≤ 2 s**；> 5 s 必须标记 `@pytest.mark.slow` | 现有最大 27.61 s（见 P1-3）已超标 |
| 慢测试规模上限 | 默认 **≤ 2000 次**模拟；10 万次/1000 序列仅在 `LOTT_SLOW_TESTS=1` | 实测 10 万次 MC = 2.17 s（向量化）/ 7.7 s（循环） |
| 新增测试总数 | **≤ 100**（方案下限 12+15+20+8+10+12+10 = 约 87） | 与 480 基线合并后 ≤ 580 |
| 端到端 `predict_next` 用例 | **≤ 6 例**，且 `use_llm=False, use_ml=False` | 单次 0.61 s |

标准写法建议：

```python
# tests/conftest.py
import os, pytest
SLOW = os.environ.get("LOTT_SLOW_TESTS") == "1"

def slow_scale(default: int, slow: int) -> int:
    return slow if SLOW else default

# 用例内
n_sims = slow_scale(2_000, 100_000)
```

---

## 5. V10 发布 checklist（可直接贴进任务描述）

> 标注 **[前置]** 的必须在 V10-01 开工前完成；其余按 M1/M2/M3 执行。

### A. 前置（发布前的基础设施，不属于任何单个任务）

- [ ] **[前置]** `mkdir -p /backup`，写入 `/etc/cron.d/ssq-backup`（内容见 `DEPLOY.md:209`），`chmod 644`，手动执行一次并确认 `/backup/ssq_YYYY-MM-DD.tar.gz` 生成
- [ ] **[前置]** **恢复演练**：`tar -tzf` 列出内容含 `data/ssq.db`、`data/hypotheses.json`、`data/adaptive_state.json`、`data/llm_budget.json`、`data/patterns_runtime.json`、`data/raw/`、`data/ml_models/`；解压到 `/tmp/restore_test` 后用 python 读出 `count_draws() == 3515`
- [ ] **[前置]** 新建 `tests/conftest.py`：`isolated_db` fixture + autouse 的 `config.DB_PATH` 快照/还原
- [ ] **[前置]** 把 19 处裸赋值 `config.DB_PATH = ...` 改为 `monkeypatch.setattr(config, "DB_PATH", ...)`（清单见 P0-B）
- [ ] **[前置]** 验证：`pytest tests/test_m4_eval.py tests/test_v4_honest_ui.py -q` → 0 failed
- [ ] **[前置]** 推送 7 个未推送提交：`git -c credential.helper= push "https://jiam9069:${PAT}@github.com/jiam9069/ssqyuce.git" main`（`PAT` 取 `/root/DSH/githubtoken.txt`），随后 `git update-ref refs/remotes/origin/main <sha>`，并用 GitHub API 核验 `APP_VERSION` == 2.5.0
- [ ] **[前置]** 更正方案基线 381 → **480**（§3.2.5 与 §3.3 M3 判据），同步 `docs/PROJECT_CONTEXT.md:231`
- [ ] **[前置]** 在 §3.2 增加"慢测试闸门"条目：`LOTT_SLOW_TESTS=1` 才放大规模，默认全量 ≤ 180 s

### B. 每个任务的通用验收（贴到每个 V10-xx 任务里）

- [ ] `pytest tests/test_v10_xxx.py -q` 通过（快速反馈）
- [ ] **`pytest -q` 全量 0 failed，且总数 ≥ 480 + 本任务新增数**（当前实测基线 480）
- [ ] 新测试**不依赖真实 `data/ssq.db`**（必须用 `isolated_db` fixture 造库）；若必须读真库，需在 `conftest.py` 中显式声明且不得裸赋值 `config.DB_PATH`
- [ ] 新测试文件命名后，**单独跑与全量跑结果一致**（专门验证 P0-B：`pytest -q` 与 `pytest tests/test_v10_xxx.py -q` 都 0 failed）
- [ ] 模拟类测试默认规模 ≤ 2000 次；10 万次/1000 序列的只在 `LOTT_SLOW_TESTS=1` 下跑
- [ ] 所有随机数固定 seed；检验类功能同时有"(a) 注入偏置必须检出 / (b) 纯随机不得误报"两例
- [ ] `python -m compileall lottery` 通过
- [ ] 前端有改动时 `node --check web/static/app.js` 通过（已实测 node v24.19.0 可用）
- [ ] `git diff --check` 干净
- [ ] 不得修改他人负责的文件（§3.1 表格）

### C. V10-10 前端专项

- [ ] `web/static/app.js` 改动后 `node --check web/static/app.js` 通过
- [ ] `web/index.html` **三处 `?v=` 全部统一为同一新令牌**（style.css 第 7 行、app.js 第 586 行、**vendor/echarts.min.js 第 10 行**）
- [ ] `app.js` 与 `style.css` 的 `?v=` **必须字面相等**（`test_v4_honest_ui.py:151` 会断言）
- [ ] 新令牌**严格大于** `20261018.3`（当前值）
- [ ] `python -m lottery.cli doctor` → `缓存号一致: ?v=<新令牌>`（**当前实测为 WARN**，V10-10 后应变 OK）
- [ ] `web/index.html` 的 `verText` 徽标同步为 `v3.0.0`，且 footer 文案去掉"V4~V8"改为覆盖到 V10
- [ ] 三张新卡（票单分布 / 实验登记簿 / AI 技能）在浏览器实测可见；"改变分布不改变 EV"提示条**必须**与分布卡同时出现
- [ ] echarts 用法保持 `echartsInit(id)`（`app.js:1325`，内部已处理 `undefined` 降级），**不得**新增 `import`/`require`（vendored 全局脚本）

### D. V10-12 版本与文档（7 处文件，逐条打勾）

- [ ] `lottery/config.py:26` → `APP_VERSION = "3.0.0"`
- [ ] `lottery/config.py:27` → `APP_BUILD = "2026-10-V10"`
- [ ] `lottery/config.py:28` `APP_MILESTONES` → 追加 `"V10": {"status": "done", "desc": ...}`
- [ ] `web/index.html:7` → `style.css?v=<新令牌>`
- [ ] `web/index.html:10` → `vendor/echarts.min.js?v=<新令牌>`
- [ ] `web/index.html:586` → `app.js?v=<新令牌>`
- [ ] `web/index.html:571` → `verText` 徽标 `v3.0.0` + footer 文案
- [ ] `README.md:18` → 「最近升级概要（v3.0.0 · V10 ...）」；**并补齐 V9（2.5.0）缺失的升级概要**
- [ ] `README.md` 「版本记录」段追加 `### v3.0.0（V10 ...）`
- [ ] `DEPLOY.md:81` → 「当前版本：`?v=<新令牌>`（`APP_VERSION=3.0.0` / `APP_BUILD=2026-10-V10`）」；**并补齐 V8/V9 的漂移**
- [ ] `DEPLOY.md:104` → 验证命令里的 `?v=` 示例更新
- [ ] `docs/PROJECT_CONTEXT.md:9-11` §2 发布状态 → v3.0.0 / 2026-10-V10 / 容器 healthy
- [ ] `docs/PROJECT_CONTEXT.md:40` V10 追加说明段（新增端点、`?v=`、3 张新卡）
- [ ] `docs/PROJECT_CONTEXT.md:231` → **480 passed** → V10 后的新数字
- [ ] `docs/PROJECT_CONTEXT.md:262` P0「补生产级备份」→ 若已按 A 段完成，勾掉或更新为"已配置 + 已演练"
- [ ] `.env.example:44` → `LOTT_LLM_ONLY_OUTPUT` 默认值改 `0` 并更新注释；新增 `LOTT_PREDICT_CHANNELS` / `LOTT_LLM_DECISION` / `LOTT_SLOW_TESTS`
- [ ] `docker-compose.yml` 的 `environment` → 若新配置需要从 `.env` 注入，同步新增条目
- [ ] **无测试会因升版失败**（已核验：`APP_VERSION >= (2,1,0)` / `APP_BUILD` 含 `2026-10-V` 均为下界断言）；但**也没有测试保护**，建议新增 `verText` 与 `APP_VERSION` 一致性测试

### E. V10-12 部署与回滚（**这是当前最薄弱环节**）

- [ ] 部署前：`tar -czf /backup/ssq_pre_v10_$(date +%F).tar.gz -C /root/DSH/应用/ssqyuce data` 并确认 `data/ssq.db` 在其中
- [ ] 部署前：记录 `sqlite3 data/ssq.db "select max(version) from schema_version"` 的当前值（实测为 **2**）
- [ ] 部署前：`docker image tag ssq-predictor:latest ssq-predictor:pre-v10`（保留可回滚镜像）
- [ ] 部署前：`cp data/ssq.db /tmp/ssq_pre_v10.db`（迁移前单独留一份 1.4MB 裸库，便于快速回滚）
- [ ] 部署：`docker compose up -d --build`
- [ ] 部署：轮询 `docker inspect --format='{{.State.Health.Status}}' ssq-predictor`，**2~2.5 分钟内 `starting`/`unhealthy` 属正常**（预热 fetch/backtest/ML；mem_limit 768m，实测预热期约 630MB）
- [ ] 部署后：`curl -fsS http://127.0.0.1:18000/api/info` 返回 `3.0.0` / `2026-10-V10`
- [ ] 部署后：确认三张新卡可加载；`/api/registry/list`、`/api/...` 新端点 200
- [ ] 部署后：`curl -sI "https://ssq.ff88.de/static/app.js?v=<新令牌>"` 返回 `x-cache-status: MISS`（新键）
- [ ] 部署后：`docker exec ssq-predictor python -c "import lottery.registry, lottery.replay, lottery.rigor; print('ok')"`（**验证 Python 3.11 容器内可用**，本地 venv 是 3.13）
- [ ] **回滚预案（必须写进任务，不能只放文档）**：
  ```bash
  docker compose down
  cp /tmp/ssq_pre_v10.db data/ssq.db          # 回滚库（schema_version 回到 2，新表消失）
  docker image tag ssq-predictor:pre-v10 ssq-predictor:latest
  docker compose up -d                        # 不 --build，直接用旧镜像
  ```
  并接受一个明确事实：**新增的 3 张表（experiments / llm_proposals / replay_results）里 V10 期间产生的数据会随库回滚一并丢失** —— 这是可接受的（都是新数据），但必须写进任务，避免执行者误以为"回滚能保住 V10 数据"。
- [ ] 迁移**必须有 forward-only 声明**：V10 新增的 `lottery/migrations.py:35` 条目只允许 `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN`，**不得** `DROP` / `RENAME` / 改列类型（否则回滚不可能，且 `get_conn()` 自动执行让事故无法挽回）
- [ ] 推送：`git -c credential.helper= push "https://jiam9069:${PAT}@github.com/jiam9069/ssqyuce.git" main` → `git update-ref refs/remotes/origin/main <sha>` → GitHub API 核验 `APP_VERSION == 3.0.0`
- [ ] 最终报告：记录本地测试数（480 + 新增）、GitHub 提交 SHA、容器重建后的 health 状态、`/api/info` 输出（单节点，按 §7 验收原则）

---

## 附：本次评审实测命令汇总（可复现）

```bash
cd /root/DSH/应用/ssqyuce

# 基线测试（结论：480 passed，非 381）
.venv/bin/python -m pytest -q                       # 480 passed in 84.04s
.venv/bin/python -m pytest -q --collect-only | tail -1   # 480 tests collected
.venv/bin/python -m pytest -q --durations=15        # 480 passed in 114.29s

# 测试隔离设施（结论：conftest.py 从未存在）
ls tests/conftest.py conftest.py pytest.ini pyproject.toml   # 全部 No such file
git log --all --oneline -- tests/conftest.py                  # 空
grep -rn "config\.DB_PATH = " tests/*.py | grep -v "monkeypatch" | wc -l   # 19

# 泄漏复现（结论：顺序一变就挂）
.venv/bin/python -m pytest tests/test_m4_5_engineering.py tests/test_v4_honest_ui.py -q
# → 1 failed (400 != 503)

# 备份（结论：完全没配）
cat /etc/cron.d/ssq-backup ; ls /backup/ ; ls data/*.bak*

# Git 交付状态（结论：远端停在 V5）
git branch -vv ; git log --oneline -1 origin/main
git show origin/main:lottery/config.py | sed -n '26,27p'   # 2.1.0 / V5

# 前端
wc -l web/static/app.js web/static/style.css   # 3603 / 592
node --version                                  # v24.19.0
node --check web/static/app.js                  # OK
grep -o "v=[0-9]\{8\}\.[0-9]*" web/index.html | sort | uniq -c
.venv/bin/python -m lottery.cli doctor | grep 缓存号   # WARN 不一致

# 环境差异
.venv/bin/python --version ; docker exec ssq-predictor python --version   # 3.13.5 / 3.11.17

# 数据与容器
du -sh data/ data/ml_models/ ; ls /root/DSH/githubtoken.txt
docker inspect ssq-predictor --format '{{.HostConfig.Memory}} {{.State.Health.Status}}'  # 805306368 healthy
```
