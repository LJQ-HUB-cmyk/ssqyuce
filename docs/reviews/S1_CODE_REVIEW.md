# S1 代码正确性评审（返工 commit `8955d37`）

评审人：s1-code-reviewer　　评审对象：V10-00b/c/d 返工落地代码
基线：HEAD=`8955d37`，工作树干净，全量 `pytest -q` = **485 passed**
评审方式：只读代码 + 亲手构造反例/变异实验；**不采信自评文档结论**（`V10_REWORK_VERIFICATION.md` / `V10-00_IMPL_NOTES.md` 仅用于列出"待验证的声称"）
实验产物全部在 `/tmp`，仓库内仅新增本文件；仓库源码 0 改动（`git status` 已核验）。

---

## 1. 一句话结论

**有条件通过**——测试隔离（V10-00b）**证实成立**，是这次返工里唯一真正做扎实的部分；但 V10-00d 的两项核心承诺**不成立**：
① **迁移前自动备份在真实生产升级路径上会被完全跳过**（有 3515 期数据的老库 = `current_version()==0` → 不备份）；
② 声称的 SAVEPOINT 嵌套路径**在实际调用中永远走不到，且在调用方有未提交事务时会无限挂起**（已用 `timeout 10` 复现 exit 124）。
这两条恰好击中当初立项的理由（"生产库零备份"），**必须在开工 V10-01 前修掉**。

---

## 2. 评分表

| 维度 | 分数 | 理由（均有实验证据，见 §5） |
|---|---|---|
| **测试隔离正确性** | **4 / 5** | autouse fixture 在 `get_conn`、子线程、异常/skip、全部 `DATA_DIR` 派生路径（含 `llm_lab.CLAIMS_FILE`、`patterns_runtime.SNAPSHOT_FILE`）下**全部无泄漏**；随机顺序 485 passed；跑完测试生产 `data/` 的 md5 与 mtime **一字未变**。扣 1 分：`monkeypatch.setenv("LOTT_HOME"/"LOTT_DB")` 在 13 个测试文件里**完全无效**（config 在 import 期就固化，实验见 §5-A2），属于误导性死代码；且 conftest 只兜住 8 个路径属性，非路径全局（`API_TOKEN`/`BLUE_MODE`…）仍会泄漏（§5-A6）。 |
| **迁移事务边界** | **2 / 5** | 好的部分经实测成立：autocommit+`BEGIN IMMEDIATE` 下 `CREATE TABLE`/`CREATE INDEX`/`ALTER TABLE` **确实全部回滚**、`isolation_level` 复原、SAVEPOINT 语义本身正确（不误提交、不丢调用方数据）。但：**备份触发条件写错致生产升级零备份（P0-1）**；**`conn.backup()` 在调用方事务内无限挂起（P0-2）**，使 SAVEPOINT 分支成为不可达代码；该分支**测试零覆盖**（变异删掉它 5 条测试全绿）。 |
| **备份脚本** | **3 / 5** | 核心设计选型正确并实测有效：`Connection.backup()` 热备、`flock` 并发互斥（含 kill -9 后锁自动释放）、归档/`llm_config.json` 600、备份目录 700、`restore` 拒绝覆盖生产 `data/`、GNU tar 拒绝 `..` 穿越、`verify` 退出码 1/2 正确。扣分：**tar 中途失败会留下撕裂归档并占住 retention 名额与"最新"位置（P1-3，实测）**；**归档自检失败只 log、脚本仍 exit 0（P1-4，实测）**且排错用的 `/tmp/.ssq-backup-verify.$$` 当场被 `rm` 删除；保留策略按 `ssq-backup-*.tar.gz` glob 删除，会误删人工放置的同前缀归档。 |
| **测试有效性** | **2 / 5** | 4 个变异体里 **MUTANT-4（整段删除 SAVEPOINT 分支）5 passed**，说明 5 条新测试**完全没有覆盖**返工文档重点强调的嵌套路径；MUTANT-1/2/3 能红，说明批次回滚、fail-closed、自动备份三条有真实约束力。另外 `test_health_exposes_schema_version` 与 `test_migrations_are_idempotent...` 存在**自指式断言**（拿 `migrations.MIGRATIONS[-1][0]` 和自己比），回归价值低。 |
| **断言未放宽** | **4 / 5** | 逐文件看 `git show 8955d37 -- tests/`：`test_m4_5_engineering` 的 `==2` → `== latest`（等价且有加强）；`test_v8_eras_tasks` 的 `test_bg_run_*` 把 try/finally 改成平铺 + `monkeypatch`，断言（`completed`/`note_zh`/`failed`+`boom`）**逐字保留**；4 处 `seeded_db(600)` 是从"依赖生产库恰好有数据"改为"依赖合成库"，**判定分支未变**（属加强，非削弱）。未发现删除断言或加 `try/except: pass` 吞断言的情况。扣 1 分：`test_m4_3_notify.py` 仍保留裸赋值 `config.NOTIFY_WEBHOOK`（非路径类，不还原）。 |

---

## 3. 问题清单

### P0-1　迁移前自动备份在"真实生产升级"路径上被完全跳过

**文件**：`lottery/migrations.py:190`（`if backup and current > 0:`）

代码用"版本号 > 0"代理"库里有既有数据"，但真实老库**没有 `schema_version` 表**，`current_version()` 就是 0。于是：**有 3515 期真实数据的生产库升级时，一行备份都不会做**，直接进事务迁移。这恰好是本次返工立项要解决的场景（R4 P0-C「生产库零备份」）。

模块 docstring `migrations.py:22` 的声称是"全新库（`schema_version` 还是 0、**没有既有数据**）不做无意义备份"——代码只实现了前半句，`没有既有数据` 这个条件根本没检查。

**复现**（实测输出见 §5-E）：

```bash
cd /root/DSH/应用/ssqyuce && .venv/bin/python - <<'EOF'
import sqlite3, sys, tempfile, pathlib
sys.path.insert(0, ".")
from lottery import migrations
tmp = pathlib.Path(tempfile.mkdtemp()); db = tmp / "legacy_ssq.db"
c = sqlite3.connect(db)
c.executescript("CREATE TABLE draws(issue TEXT PRIMARY KEY,date TEXT,r1 INT);"
                "CREATE TABLE predictions(issue TEXT);")
c.executemany("INSERT INTO draws VALUES(?,?,1)", [(f"2026{i:03d}", "2026-01-01") for i in range(500)])
c.commit()
print("期数:", c.execute("SELECT count(*) FROM draws").fetchone()[0])   # 500
print("current_version:", migrations.current_version(c))               # 0  ← 关键
migrations.apply(c)                                                    # backup=True 默认
b = migrations.backup_path_for(c, 0)
print("备份是否存在:", b.exists())                                      # False ← 缺陷
EOF
```

**修法**：备份条件改为"库里有既有数据就备份"，例如 `current > 0 or _has_existing_data(conn)`（检查 `sqlite_master` 里是否已有 `draws`/`predictions` 等业务表或 `SELECT count(*) FROM draws`），并补一条测试。

---

### P0-2　`backup_before_migrate` 在调用方已有事务时**无限挂起**，SAVEPOINT 分支不可达

**文件**：`lottery/migrations.py:126`（`conn.backup(target)`），调用点 `migrations.py:190-192` 位于 `_run_batch` 的 `in_transaction` 判断（`:147`）**之前**。

`Connection.backup()` 对 SQLITE_BUSY/LOCKED 是**无限 sleep 重试**（CPython 实现里 `while True` + `sleep`）。当源连接持有写锁且事务未提交时，`conn.backup(target)` 永远拿不到一致快照 → **进程永久挂住**。结果是：调用方已有事务时，代码在到达 SAVEPOINT 逻辑（`:147`）之前就卡死，**该分支成了不可达代码**。

**复现**（实测 exit 124 = 超时，见 §5-B）：

```bash
cd /root/DSH/应用/ssqyuce && timeout 10 .venv/bin/python - <<'EOF'
import sqlite3, sys, tempfile, pathlib
sys.path.insert(0, ".")
from lottery import migrations
tmp = pathlib.Path(tempfile.mkdtemp())
c = sqlite3.connect(tmp / "prod.db")
c.executescript("CREATE TABLE draws(issue TEXT PRIMARY KEY,date TEXT);CREATE TABLE predictions(issue TEXT);")
migrations.apply(c, backup=False)                       # 先到 v2
migrations.MIGRATIONS = list(migrations.MIGRATIONS) + [
    (3, "v3", lambda conn: conn.execute("CREATE TABLE v3(x)"))]
c.execute("INSERT INTO draws (issue,date) VALUES('CALLER','2026-01-01')")
print("in_transaction =", c.in_transaction, flush=True)  # True
migrations.apply(c)                                      # ← 永久挂起
print("永不打印")
EOF
echo "退出码=$?"     # 124
```

**影响面**：`db.get_conn()` 只在首次建连接时调 `apply`，此时连接是干净的，所以**线上当前不触发**；但这是"潜伏性挂死"——任何未来在事务内触发迁移的调用方（新迁移脚本、维护命令、测试）都会卡死无日志。且它让 `docs/reviews/V10-00_IMPL_NOTES.md` 重点宣讲的"SAVEPOINT 嵌套"能力**实际不存在**。

**修法**：先判定 `conn.in_transaction` 再决定备份策略——已在事务中时，要么用独立只读连接做快照（`sqlite3.connect(db_file, mode=ro)` + backup），要么先让调用方提交/退化为"跳过备份 + 显式告警"。同时给 `Connection.backup` 的挂起加兜底（`progress` 回调 + 中止）。

---

### P1-3　`tar` 中途失败留下撕裂归档，占住 retention 名额与"最新备份"位置

**文件**：`scripts/ssq_backup.sh:248`（`tar -czf "$archive" .` 直接写最终文件名）

脚本对 `ssq.db` 走了"先 `.tmp` 再 rename"的正确模式，但归档本身没有。tar 失败（磁盘满、被打断）会在备份目录留下**永久性的半截归档**，而 `:262` 的 retention 与 `:279` 的 `latest_archive` 都用 `ls ssq-backup-*.tar.gz` 计数/取最新 → 坏档占名额、被当"最新备份"。

**复现**（实测：留下 142,370 字节的坏归档，正常应 200MB+，见 §5-Q）：

```bash
SB=/tmp/s1trunc
# 造 200MB raw 拉长打包窗口后，在 tar 运行中 kill -9 它
# 结果：ssq-backup-*.tar.gz 存在但 `tar -tzf` 报 "Unexpected EOF"，且权限仍是 644
# （chmod 600 在 :249，tar 失败后根本没执行到）
```

**附带**：撕裂归档权限是 `-rw-r--r--`（644），因为 `chmod 600`（`:249`）在失败后不可达 → **含 API Key 的半截归档短暂为 644**。

**修法**：`tar -czf "$archive.tmp" . && mv "$archive.tmp" "$archive"`，失败时 `rm -f` 临时档；retention 与 latest 只认通过 `verify` 的文件。

---

### P1-4　备份自检失败被静默吞掉（exit 0），排错现场被当场删除

**文件**：`scripts/ssq_backup.sh:253-258`

```bash
if verify_dir "$staging" >/tmp/.ssq-backup-verify.$$ 2>&1; then
  log "备份自检通过..."
else
  log "ERROR: 备份自检失败，详见 /tmp/.ssq-backup-verify.$$"   # 只 log，不 exit
fi
rm -f /tmp/.ssq-backup-verify.$$                               # 马上删，事后无从查
```

实测（§5-K）：在 `data/` 放一个非法 JSON → 触发自检失败 → **脚本 exit 0**，cron 认为成功；日志指向的详情文件已被删除。

**修法**：自检失败 `die`（或写入一个显式失败标记状态），并保留详情到固定路径（如 `$BACKUP_DIR/.last-verify.log`）。

---

### P1-5　备份脚本缺失的生产化细节（合并陈述）

| 项 | 文件:行 | 说明 |
|---|---|---|
| 保留清理误删面 | `scripts/ssq_backup.sh:262` | 按 `ssq-backup-*.tar.gz` glob 删除，会误删人工放入的同前缀归档（实测 `important-manual.tar.gz`/`ssq.db.bak` 未被误删，但同前缀的会被删）。建议只删带 MANIFEST 且通过 verify 的自产归档。 |
| 同秒 `ts` 覆盖 | `:195-196` | 归档名用秒级时间戳；同一秒内两次成功备份会互相覆盖（实测因 flock 恰好串行未触发，但窗口真实存在）。加毫秒或 `mktemp` 唯一后缀。 |

---

### P2-1　自动备份文件权限 0644，且无保留/清理策略

**文件**：`lottery/migrations.py:124-129`。`sqlite3.connect(str(tmp))` 按 umask 创建 → **0644**（实测 `0o644`），内含全部开奖/预测数据。每个版本各留一个完整库副本且**永不清理**（实测 v2、v3 各一份并存）。虽无密钥，但属数据面暴露 + 无界增长。建议 `os.chmod(0o600)` 且写入 MANIFEST/时间戳并交由 `ssq_backup.sh` 统一清理。

### P2-2　`monkeypatch.setenv("LOTT_HOME"/"LOTT_DB")` 在 13 个测试文件中完全无效

**位置**：`tests/test_m4_eval.py:8`、`test_m4_3_notify.py:3`、`test_llm_compat.py:30`、`test_m4_2_api.py:12` 等 13 处（`grep -rn 'setenv("LOTT' tests/`）。`config.py:12/15` 在 import 期就把 env 固化，fixture 生效后再 `setenv` **不影响任何 config 属性**（实测 §5-A2：`env-生效=False`）。这些行给人"已隔离"的错觉，实际全靠后面的 `monkeypatch.setattr(config, "DB_PATH", ...)` 或 autouse fixture 生效。建议删除或改为注释说明。

### P2-3　conftest 只兜路径属性，非路径全局仍会跨用例泄漏

`tests/conftest.py:43-52` 的 `_ISOLATED_CONFIG_ATTRS` 只含 8 个路径属性。实测（§5-A6）`config.API_TOKEN` / `config.BLUE_MODE` 的裸赋值会**泄漏到下一条用例**。这些是返工前既有问题（不在本次范围），但 conftest 自称"泄漏为零"（`:25-26`）属**过度声称**，建议改措辞或扩展为对所有 `config` 属性的快照还原。

### P2-4　`/api/health` 会因 `db.get_conn()` 触发懒迁移

**文件**：`lottery/api_app.py:71`。health 调 `db.get_conn()`，而 `db.py:171` 在首次建连时执行 `migrations.apply()`。即"健康检查"会成为**迁移执行者**——叠加 P0-1（无备份），第一次 `/api/health` 就会在无备份状态下改写生产库 schema。建议 health 用只读查询（如 `PRAGMA user_version` 或独立只读连接）读版本，把迁移留在显式启动流程。

### P2-5　两条新测试是自指式断言，回归价值低

**文件**：`tests/test_v10_migrations.py:126-135`（`assert j["schema_version"] == migrations.MIGRATIONS[-1][0]`）与 `:70-86`（幂等断言同样以 `MIGRATIONS` 自身为基准）。它们只能发现"health 没返回字段"，发现不了"版本判定逻辑错了"。建议硬编码期望值或独立推导。

---

## 4. 必须修复项（挡住 V10-01 开工）

1. **P0-1**：`migrations.apply` 的备份条件补"库内有既有数据"判定；补一条"老库（无 `schema_version` 表、有数据）升级必须留备份"的测试。
2. **P0-2**：修 `backup_before_migrate` 在调用方事务内挂死——先判 `in_transaction` 再备份（用旁路只读连接，或明确降级+告警），并对 `Connection.backup` 加超时/中止兜底；补一条"调用方已有事务时 `apply(backup=True)` 必须返回或在有限时间内明确报错"的测试。
3. **P1-3 / P1-4**：归档改为 `.tmp` + `mv`；自检失败必须非零退出并保留详情。加一条"tar 失败不得在备份目录留下 `ssq-backup-*.tar.gz`"的测试。
4. **P1-5（部分）**：retention/`latest_archive` 只认通过 verify 的自产归档，避免坏档占位与误删同前缀人工归档。

> 可以放到 V10-01 之后：P2-1 备份权限与清理策略、P2-2/P2-3 测试隔离的过度声称与遗留泄漏、P2-4 health 触发迁移、P2-5 自指断言。
> 说明：P2-2/P2-3 是返工前既有问题，**不构成"返工造假"**，但文档里的"泄漏为零"需要收敛成准确表述。

---

## 5. 我做过的变异测试 / 反例实验清单（全部实跑）

所有实验均在本机实跑；仓库源码 0 改动，变异体每次都用 `git`/备份文件还原并核验（`md5sum` 与 `git diff --stat` 均为空）。

### A. 测试隔离反例（探针文件临时放在 `tests/` 内，跑完即删）

| 编号 | 场景 | 结果 |
|---|---|---|
| A1 | 测试内再调 `db.get_conn()` | ✅ 连到 `/tmp/pytest-of-root/.../isolated/data/ssq.db`，未触生产库 |
| A2 | 仅 `setenv("LOTT_HOME"/"LOTT_DB")` | ⚠️ `env-生效=False`——**setenv 完全不改变 config**（P2-2） |
| A3 | 用例抛异常 | ✅ 异常后下一条用例 `draws=0`，无串库 |
| A4 | 用例被 skip | ✅ 无影响 |
| A5 | 子线程内 `db.get_conn()` | ✅ 仍指向隔离库 |
| A6 | 8 个路径属性 + 2 个模块常量 | ✅ 全部隔离；但裸写 `API_TOKEN`/`BLUE_MODE` 会泄漏到下一条用例（P2-3） |
| A7 | 裸写 `config.BASE`（`test_mining_artifact.py:51`）是否泄漏 | ✅ 不泄漏（下一用例 fixture 重新 setattr + teardown 回写 `_REAL_CONFIG`） |
| A8 | 跑完 485 条测试后生产 `data/` 完整性 | ✅ 9 个文件 md5 与 mtime **一字未变** |
| A9 | 随机顺序全量（seed=7，29 文件） | ✅ **485 passed** |
| A10 | R4 报告的双向复现命令 | ✅ 正/反序均 **19 passed** |

> 附带发现（也清理了）：一开始把探针放到 `/tmp`，因根目录不同 `tests/conftest.py` 未被加载，探针裸写 `data/probe_write.txt` **成功落到生产 data/**，已 `rm` 清理并核验无残留。这说明**隔离只对 `tests/` 下的收集生效**——任何在仓库根的临时脚本仍能写生产库，建议在 CI 里加一条"跑完测试 `git status --porcelain data/` 必须为空"的断言。

### B. 迁移事务边界实验

| 编号 | 实验 | 结果 |
|---|---|---|
| B1 | 调用方有未提交事务 + `apply()`（默认 backup） | ❌ **挂起**，`timeout 10` → exit 124（P0-2），faulthandler 定位到 `migrations.py:126 conn.backup(target)` |
| B2 | 同上但 `backup=False`（绕开挂起，测 SAVEPOINT 失败路径） | ✅ `in_transaction` 仍 True、调用方数据保活、`schema_version` 未前进、`fail_marker` 无残留、另一连接看不到未提交数据 |
| B3 | SAVEPOINT **成功**路径是否误提交调用方事务 | ✅ `apply` 后 `in_transaction` 仍 True，另一连接看不到调用方数据与新表 |
| B4 | autocommit 路径下 `ALTER TABLE` + `CREATE INDEX` 是否回滚 | ✅ `newcol` 无残留、`idx_probe` 无残留、版本未前进、`isolation_level` 复原为 `''` |
| B5 | 老库（有 500 期数据、无 `schema_version` 表）升级是否备份 | ❌ **无任何备份**（P0-1） |
| B6 | 同名备份反复失败是否无限增长 | ✅ 同版本只 1 份（inode 每次变化，即重写）；但跨版本 v2/v3 各留一份且永不清理（P2-1） |
| B7 | 自动备份文件权限 | ⚠️ `0o644`（P2-1） |
| B8 | 内存库 | ✅ 不报错、返回 `None`（与其测试一致） |
| B9 | `/api/health` 在 fresh 库 | ✅ `schema_version=2, schema_latest=2` |

### C. 变异测试（`tests/test_v10_migrations.py`，5 条）

| 变异体 | 结果 | 判定 |
|---|---|---|
| **MUTANT-1** 删 `BEGIN IMMEDIATE`（回到 LEGACY 隐式事务） | **1 failed**（`v10_ok_marker` 残留） | ✅ 测试有效 |
| **MUTANT-2** 备份失败被 `try/except: pass` 吞掉（fail-open） | **1 failed**（`DID NOT RAISE OSError`） | ✅ 测试有效 |
| **MUTANT-3** 完全不备份（`if False`） | **2 failed** | ✅ 测试有效 |
| **MUTANT-4** 整段删除 SAVEPOINT 嵌套分支 | **5 passed** ❌ | ⚠️ **零覆盖**：返工文档重点宣讲的能力没有任何测试保护 |
| （MUTANT-4 行为验证） | 去掉分支后：调用方 `in_transaction` 由 True → **False**，未提交数据被强制落盘 | 证明该分支**确实是必要防护**，却被 P0-2 弄成不可达 |

> 全部变异体已还原：`md5sum lottery/migrations.py` 与备份一致，`git diff --stat` 为空，`git status --short` 仅剩他人文档。

### D. 备份脚本实验（沙箱 `SSQ_APP_DIR`/`SSQ_BACKUP_DIR`，不碰生产）

| 编号 | 实验 | 结果 |
|---|---|---|
| D1 | 正常备份 | ✅ 归档 600、目录 700、`llm_config.json` 600、MANIFEST 完整、自检通过 |
| D2 | 归档内容 | ✅ 库 + 8 json + raw + MANIFEST；`llm_config.json` 含密钥明文（**应含**，已 600） |
| D3 | 并发两实例（含制造并发窗口） | ✅ flock 正确：一个干活、一个"本轮跳过"，**无撕裂** |
| D4 | stale lock（持有进程 `kill -9`） | ✅ 锁文件残留但内核锁自动释放，下一轮正常备份 |
| D5 | 保留清理误删 | ⚠️ `important-manual.tar.gz`/`ssq.db.bak`/其他文件**未被误删**；但 `ssq-backup-*` 前缀的人工归档会被计入/删除（P1-5） |
| D6 | 路径穿越（`../../escaped.txt`） | ✅ GNU tar 拒绝 `..`，未逃逸；`restore` 失败 |
| D7 | `restore` → 生产 `data/` | ✅ 被护栏拒绝（代码路径存在） |
| D8 | `verify` 损坏归档 / 不存在路径 | ✅ 退出码 2 / 1 |
| D9 | 归档自检失败（放非法 JSON） | ❌ **exit 0** + 详情文件被删（P1-4） |
| D10 | tar 中途被 kill -9 | ❌ 留下 142,370B **撕裂归档**（`Unexpected EOF`）、权限 644、留在备份目录被计数（P1-3） |
| D11 | 热备失败（`ssq.db` 缺失） | ✅ exit 1（fail-closed）、无归档、staging 已清理 |
| D12 | 日志目录不可写 | ✅ 不影响备份（exit 0） |
| D13 | verify 临时目录权限 | ✅ `mktemp -d` 为 700，用完清理 |

### E. 断言未放宽核查

`git show 8955d37 -- tests/` 逐文件（17 个文件）：
- `test_m4_5_engineering.py`：`== 2` → `== latest` + 新增 `count == len(MIGRATIONS)` 与 `max == latest` → **加强**；
- `test_v8_eras_tasks.py::test_bg_run_marks_*`：try/finally → 平铺 + monkeypatch，**断言逐字保留**；
- `test_v4_honest_ui.py` / `test_v8_eras_tasks.py` 4 处 `seeded_db(600)`：从"依赖生产库有数据"改为"合成 600 期"，**判定分支未变**；
- 其余 13 个文件：仅 `裸赋值 → monkeypatch.setattr` 的机械替换。
- **未发现**：删除断言、弱化比较（`==`→`in`/`>=`）、`try/except: pass` 吞断言、`assert True` 之类。

---

## 6. 附：本次评审未覆盖的范围

- 未审查 `docs/V10_IMPROVEMENT_PLAN.md` 的方案可行性（属 S2）；
- 未审查 `api_app.py` 中除 `/api/health` 外的 1860 行（非本次变更）；
- cron 实际触发时间点未等待（`/etc/cron.d/ssq-backup` 文件存在、每日 03:17、日志有真实 3515 期记录，视为基建到位）；
- 未做 xdist 并行的隔离验证（环境未安装 `pytest-xdist`）。
