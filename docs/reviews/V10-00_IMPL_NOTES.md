# V10-00 前置 Gate 实现记录（测试隔离 / 备份 / 迁移加固）

> 任务：共享任务板 `task-2`（V10-00b + V10-00c + V10-00d），依据
> `docs/reviews/V10_REVIEW_SUMMARY.md` §三 G8、§四、§七 Gate-3/4/5 与
> `docs/reviews/R4_RELEASE_REVIEW.md` P0-B / P0-C。
> 基线：全量 `pytest -q` = **480 passed**，工作树无代码改动（只有 3 个未跟踪 docs）。
> 结论：三项 Gate 全部完成，**零产品行为变化**（未改 `predict_next` / `engine.py` /
> `config.py` 默认开关 / 版本号 / `web/`）。

---

## 一、V10-00b 测试隔离

### 问题（为什么要做）

* `tests/conftest.py` **不存在**（git 历史中也从未有过），项目没有全局 fixture。
* `tests/` 下仍有 **19 处**裸赋值 `config.DB_PATH = ...`（无 `monkeypatch`、无还原）。
* `lottery/db.py:160` 有**模块级连接缓存** `_CONN`，`get_conn()` 只在 `_CONN is None`
  时连库。某用例把 `config.DB_PATH` 改到临时库后，**后续用例**会继续用这个（可能已删除
  的）连接/路径。
* 全量 480 全绿**纯属字母序巧合**：污染源都在 `test_v4_*` 之前；R4 复现
  `pytest tests/test_m4_5_engineering.py tests/test_v4_honest_ui.py -q` → **1 failed**
  （`缺模块应降级为 503，实际 400`，400 = 撞上被泄漏的空库）。

### 改了什么

**新增 `tests/conftest.py`**（统一隔离机制，三层）：

1. `isolated_env`（fixture）+ `_auto_isolate_lottery_paths`（**autouse**，自动应用到
   全部用例）：每个测试天然拿到 `tmp_path/isolated` 下的独立
   `BASE / DATA_DIR / RAW_DIR / DB_PATH`，以及 `config` 上由 DATA_DIR 派生的
   `LLM_BUDGET_FILE / METHODS_CONFIG_FILE / ADAPTIVE_STATE_FILE / LLM_CONFIG_FILE`；
   并同步重定向两个在 import 期就把 DATA_DIR 固化成模块常量的路径
   （`llm_lab.CLAIMS_FILE`、`patterns_runtime.SNAPSHOT_FILE`），否则测试会读写生产
   `data/` 里的同名文件。
   隔离前 `db.close()`、结束后再 `db.close()`（= 清空 `_CONN` 缓存）→ **不串库**；
   `pytest.MonkeyPatch()` 实例 + 显式回写原值双保险 → **泄漏为零**。
2. `make_draws()`：合成开奖数据工厂（期号升序、`reds/order/blue`，结构同 `db.load_draws()`）。
3. `seeded_db()`：把合成数据写进当前临时库，替代「依赖生产库恰好有数据」。

**19 处裸赋值全部迁移**到 `monkeypatch.setattr(config, ...)`（+ `db.close()` 重置连接
缓存）；`os.environ[...] = ...` 同步改为 `monkeypatch.setenv`。涉及 14 个文件：

```
tests/test_llm_compat.py          tests/test_m4_eval.py
tests/test_llm_failfast.py        tests/test_mining_artifact.py
tests/test_llm_only_output.py     tests/test_v3_llm_budget.py
tests/test_m4_2_api.py            tests/test_v3_llm_thinking_fallback.py
tests/test_m4_2_engine.py         tests/test_v3_u2_u3_u4.py（4 处）
tests/test_m4_3_notify.py         tests/test_v4_honest_ui.py
tests/test_m4_4_db_source.py      tests/test_v8_eras_tasks.py（2 处，含旧 try/finally）
tests/test_m4_5_engineering.py
```

**4 个原本隐性依赖生产库的用例**改为造合成库（**断言一条未改**）：
`test_v4_honest_ui::test_rigor_missing_module_does_not_break_startup`、
`test_v8_eras_tasks::test_regime_overview_endpoint_accepts_era_mode`、
`::test_async_closed_loop_endpoint_returns_task_id`、
`::test_async_llm_lab_endpoint_returns_task_id`。
（这 4 个端点只有在库非空时才走到 200/503 分支；此前靠生产库 3515 期数据"恰好"成立。）
`test_v8_eras_tasks::test_bg_run_marks_task_completed_and_failed` 的裸赋值 +
手工改 `db._CONN` 改为 `monkeypatch.setattr` + `db.close()`。

### 验证（实测输出）

```text
# 1) 全量（字母序）
$ .venv/bin/python -m pytest -q
485 passed in ~75s            # 480（原有，全部通过）+ 5（V10-00d 新增）

# 2) 随机顺序（无 pytest-randomly，用 --co 取 484 个 nodeid 后固定 seed 打乱）
$ mapfile -t IDS < /tmp/v10probe/order_99.txt && .venv/bin/python -m pytest -q "${IDS[@]}"
485 passed in 67.75s          # 最终状态、种子 99 的随机顺序
# 另跑过 seed 1 / seed 7（当时 480）、seed 2026（当时 484）均全绿

# 3) R4 原始复现命令（改造前 1 failed）
$ .venv/bin/python -m pytest tests/test_m4_5_engineering.py tests/test_v4_honest_ui.py -q
19 passed
$ .venv/bin/python -m pytest tests/test_m4_eval.py tests/test_v4_honest_ui.py -q
21 passed
$ .venv/bin/python -m pytest tests/test_v4_honest_ui.py tests/test_m4_eval.py -q   # 反向
21 passed

# 4) 逆向验证「隔离真的生效 + 泄漏为零」
#    临时插件 /tmp/v10probe/probe_plug.py：用 pytest_runtest_logfinish（fixture finalizer
#    全部跑完之后）逐用例断言 config.BASE/DATA_DIR/RAW_DIR/DB_PATH 已回到真实值；
#    临时探针用例 tests/test_v10_zzz_probe_tmp.py（用完即删）断言：
#    「自己看到的是本用例独有的空临时库，不是生产库、也不是别人的残留」。
$ PYTHONPATH=/tmp/v10probe .venv/bin/python -m pytest -q -p probe_plug
[probe] 检查用例数: 485，泄漏用例数: 0
485 passed in 71.48s          # 最终状态：485 条全量跑完，逐用例断言"已还原"，0 泄漏

$ PROBE_EXPECT_ISOLATED=1 ... pytest -q -p probe_plug tests/test_m4_eval.py \
    tests/test_mining_artifact.py tests/test_v10_zzz_probe_tmp.py
9 passed        # 探针在污染源（test_m4_eval / test_mining_artifact）之后仍拿到自己的干净库
```

> 说明：R4 当初的探针断言是「后续用例应看到**真实库** >3000 期」。本方案把方向改成更强
> 的「后续用例看到**自己**的临时库」——因为现在没有任何用例应该依赖生产库内容（这既是
> R4 的整改要求「新测试不得依赖真实库」，也顺带消除了"生产库变了测试结果就变"的隐患）。

---

## 二、V10-00c 备份（真实安装 + 恢复演练）

### 改了什么

**新增 `scripts/ssq_backup.sh`**（`chmod 750`，符号链接 `/usr/local/bin/ssq-backup`）：

* `data/ssq.db`：用 Python `sqlite3.Connection.backup()` 做**在线一致性热备**
  （**不是 `cp`**—— cp 正在写的库会得到撕裂副本）；
* `data/` 其余运行时状态：`hypotheses.json`、`adaptive_state.json`、`llm_budget.json`、
  `llm_config.json`（含 API Key → 归档内与源文件均 `chmod 600`）、`methods_config.json`、
  `llm_claims.json`、`patterns_runtime.json`、`raw/`，以及任何新增的 `*.json`；
* **明确排除 `data/ml_models/`**（265MB，可由训练重建），写入归档内的 `MANIFEST.txt`；
* 子命令：`backup`（默认）/ `list` / `verify` / `restore`；`flock` 防并发；备份后立即
  自检（`integrity_check` + 期数 + 全部 json 可解析）；保留最近 **14** 份；日志
  `/var/log/ssq-backup.log` **只记路径/大小/期数/校验结果，绝不写密钥**；
  `restore` 有护栏：**拒绝**直接恢复到生产 `data/`。

**安装 `/etc/cron.d/ssq-backup`**（root:root 644，每日 03:17）：

```cron
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
17 3 * * * root /usr/local/bin/ssq-backup backup >>/var/log/ssq-backup.cron.log 2>&1
```

**顺手收紧权限**：`data/llm_config.json` 由 644 → **600**（含 LLM API Key）。

### 验证（实测）

**cron 真的会触发**（不是只写文件）：临时追加 `* * * * *` 测试行，12:34:01 实际产出了
`/backup/ssq-backup-20261017-123401.tar.gz`，随后删除测试行、恢复每日 03:17（再观察 70s
无新归档产生）：

```text
$ ls -l /backup/
-rw------- 1 root root 2196331 Oct 17 12:33 ssq-backup-20261017-123349.tar.gz
-rw------- 1 root root 2196338 Oct 17 12:34 ssq-backup-20261017-123401.tar.gz
$ cat /var/log/ssq-backup.cron.log
... sqlite 热备完成：暂存 ssq.db，期数=3515（1.4M）
... 备份完成：/backup/ssq-backup-20261017-123401.tar.gz（2.1M），排除 ml_models/，缺失=methods_config.json
... 备份自检通过：期数=3515，全部 *.json 均可 json.load
... 现存备份份数：2（保留上限 14）
```

**恢复演练（从真实的 `/backup` 归档解到 `/tmp`，用项目 venv 校验）**：

```text
$ /usr/local/bin/ssq-backup restore /backup/ssq-backup-20261017-123401.tar.gz /tmp/restore-test
{"dir": "/tmp/restore-test", "ok": true, "integrity": "ok", "draws": 3515,
 "schema_version": 2, "raw_files": 10,
 "files": {"adaptive_state.json":2, "hypotheses.json":9345, "llm_budget.json":73,
           "llm_claims.json":5657, "llm_config.json":208,
           "mining_artifact.json":129074, "mining_latest.json":824,
           "patterns_runtime.json":14021}, "errors": []}

$ LOTT_DB=/tmp/restore-test/ssq.db .venv/bin/python -c \
    "from lottery import db; d=db.load_draws(); print(len(d), db.max_issue()); assert len(d)==3515"
load_draws() 期数 = 3515
count_draws() = 3515
max_issue = 2026118
ASSERT OK: 期数 == 3515

$ # 5 个 runtime json（实际归档里是 6 个）逐个 json.load
hypotheses.json: OK (9345 bytes, mode 0o644)
adaptive_state.json: OK (2 bytes, mode 0o644)
llm_budget.json: OK (73 bytes, mode 0o644)
llm_config.json: OK (208 bytes, mode 0o600)     ← 含密钥，恢复后仍 600
llm_claims.json: OK (5657 bytes, mode 0o644)
patterns_runtime.json: OK (14021 bytes, mode 0o644)
```

**DEPLOY.md 第七节**已从「待配置示例」改为**已安装的真实命令**（cron 原文、日常命令、
恢复步骤四步、迁移前自动备份与回滚、`.env` 不在脚本覆盖范围需单独备份），并修正了
第六节「本机升级」里 `cp data/ssq.db /tmp/...` 的旧说法（改为调用备份脚本 + 说明迁移前
自动热备）与第一节「备份 = 拷贝该目录」的过时表述。

---

## 三、V10-00d 迁移加固

### 改了什么（`lottery/migrations.py`）

1. **批次事务**：所有待执行迁移在同一事务内执行；任一迁移抛异常 → 整体 `ROLLBACK`
   （**含 DDL**），`schema_version` 不前进，异常原样抛出。
   * 关键实现细节：Python `sqlite3` 的 LEGACY 隔离级别对 **DDL 不开启隐式事务**
     （`CREATE TABLE` 自带提交，事后 ROLLBACK 撤不回来）。因此显式在
     `isolation_level=None`（autocommit）下 `BEGIN IMMEDIATE`，结束后恢复原隔离级别。
   * 若调用方**已有未提交事务**，改用 `SAVEPOINT ssq_migrations` 嵌套：失败只回滚到
     迁移开始处，**绝不提交**调用方未完成的工作。
2. **迁移前自动备份**：真正有待执行迁移且当前版本 > 0 时，先
   `sqlite3.Connection.backup()` 热备到**与被迁移库同目录**的
   `data/ssq.db.pre-migrate-v<当前版本>`（先写 `.tmp` 再 `os.replace`）；
   **备份失败 → 中止迁移（fail-closed）**。全新库（version=0）不做无意义备份；
   内存库没有文件可备份时自动跳过。
   * 路径取自 `PRAGMA database_list` 的主库文件而非写死 `config.DATA_DIR`：这样
     "备份的一定是正在迁移的那个库"，测试用临时库时也不会把备份写进生产 `data/`。
3. 新增 `current_version()` / `latest_version()` / `db_file_path()` /
   `backup_path_for()` / `backup_before_migrate()` 供测试与运维复用；
   `status()` 语义不变。
4. `lottery/api_app.py::health` 增加 `schema_version` / `schema_latest`（读
   `migrations.status`，异常时降级不 500）。
5. `tests/test_m4_5_engineering.py:9` 的 `apply(conn) == 2` 改为
   `== migrations.MIGRATIONS[-1][0]`，并保留/强化幂等断言（行数 == len(MIGRATIONS)）。

### 新增测试 `tests/test_v10_migrations.py`（5 条）

* **① 注入故意失败的迁移** → 整体回滚、`schema_version` 不前进、自动备份文件存在且可读、
  同一批里"先成功"的那条迁移也一起回滚（证明是批次事务而非逐条提交）；
* ② 正常迁移幂等（版本 == `MIGRATIONS[-1][0]`，重复 apply 不重复写、不产生多余备份）；
* ③ **fail-closed**：备份函数抛错（模拟磁盘满）→ 迁移不得执行、版本不前进、DDL 未落地；
* ④ 内存库 apply 不因备份跳过而报错；⑤ `/api/health` 暴露 `schema_version`。

### 验证（实测）

```text
$ .venv/bin/python -m pytest tests/test_v10_migrations.py tests/test_m4_5_engineering.py -q
7 passed

# 生产库副本上的真实迁移路径（复制一份 3515 期库再跑，不碰生产库）
当前版本: 2  status: {'version': 2, 'latest': 2}
apply: 2      backup path: /tmp/.../ssq.db.pre-migrate-v2   # 已最新 → 无待执行，不产生备份
第二次 apply（幂等）: 2
draws 期数: 3515
```

---

## 四、最终校验与硬约束自查

```bash
$ .venv/bin/python -m pytest -q                 # 485 passed（480 原有 + 5 新增）
$ .venv/bin/python -m compileall -q lottery tests && echo OK      # OK
$ git diff --check && echo OK                                     # OK（无空白错误）
$ git diff --stat -- web/ lottery/engine.py                       # 空（未改前端/出号逻辑）
$ git diff lottery/config.py | head                               # 空（未改默认开关值）
$ grep -n 'APP_VERSION =\|APP_BUILD =' lottery/config.py
26:APP_VERSION = "2.5.0"        # 未升版
27:APP_BUILD = "2026-10-V9"     # 未升版
```

* **未** `git commit`（留给 Lead 统一提交）；**未**重启/重建容器。
* `data/` 未被测试污染（隔离生效；跑完 `find data -maxdepth 1 -newermt '-2 hours'` 为空）。
* 临时探针文件已删除：`tests/test_v10_zzz_probe_tmp.py`（仅存在于运行期）。
  临时目录 `/tmp/v10probe`、`/tmp/restore-test`、`/tmp/bktest` 留在 `/tmp`，不在仓库内。

### 变更文件清单

```
新增：tests/conftest.py
       tests/test_v10_migrations.py
       scripts/ssq_backup.sh
修改：lottery/migrations.py（批次事务 + 迁移前热备 + 辅助函数）
       lottery/api_app.py（health: schema_version / schema_latest）
       DEPLOY.md（第六节升级步骤、第七节备份/恢复/回滚）
       tests/ 14 个文件（19 处裸 DB_PATH/DATA_DIR 赋值迁移 + 4 个用例改合成库）
系统：/etc/cron.d/ssq-backup（新增，每日 03:17）
       /usr/local/bin/ssq-backup → scripts/ssq_backup.sh（符号链接）
       /backup/（新建，权限 700；归档 600）
       data/llm_config.json 权限 644 → 600
```

---

## 五、遗留问题与需要 Lead 决策的点（**不是假装完成**）

1. **运行中的容器仍是旧镜像**：`ssq-predictor` 还在跑 V9 代码，所以**线上**
   `/api/health` 目前**没有** `schema_version` 字段——按任务约束我没有重启/重建容器。
   代码层已由 `tests/test_v10_migrations.py::test_health_exposes_schema_version` 证明；
   下次 `docker compose up -d --build` 后生效。
2. **`data/methods_config.json` 当前不存在**（系统一直用默认方法开关），因此本次归档里
   没有该文件，脚本在日志与 `MANIFEST.txt` 的 `missing=` 中如实记录；一旦 Web 端保存过
   方法开关，它就会自动进入后续备份。
3. **`.env`（含 `LOTT_LLM_API_KEY` / `LOTT_TOKEN`）不在 `data/` 内，本脚本不覆盖它**，
   DEPLOY.md 已写明需另行单独备份（附示例命令）。
4. **备份不覆盖 `data/ml_models/`**（265MB，5 个 joblib 可由训练重建）——按任务要求排除，
   已在脚本输出、`MANIFEST.txt`、DEPLOY.md 三处说明。若未来要求"整机灾难恢复零重建"，
   需追加一条低频（如每周）的 `ml_models/` 备份策略。
5. **`lottery/db.py` 未改**：隔离所需的"重置连接缓存"用现成的 `db.close()` 即可
   （它把 `_CONN` 置空，语义与"重置缓存"等价），不新增 API 以免扩大改动面。
   `db.get_conn()` 调用 `migrations.apply(_CONN)` 的签名保持兼容（返回 int 不变）。
6. **测试从 480 → 485**：新增 5 条迁移加固测试；**原有 480 条一条不少、断言未放宽**
   （4 个依赖生产库的用例改为合成数据，判定条件逐字未改）。
7. **慢测试闸门 `LOTT_SLOW_TESTS`**（裁决 §四 V10-00b 提到）本次**未实现**：它不在
   `task-2` 的三项交付里，且需要先定义哪些用例算慢（全量当前 73~77s，尚无超时压力）。
   建议放到 V10-01 的第一个提交里做，避免与本次隔离改动耦合。
8. **Gate-1 / Gate-2 / Gate-6 / Gate-7 / Gate-8 / Gate-9** 不在本任务范围（分别属于
   方案返工、契约冻结、推送 7 个提交、删除项清理、登记簿落点、AI 岗位），未动。
