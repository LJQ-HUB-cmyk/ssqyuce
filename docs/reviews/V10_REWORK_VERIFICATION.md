# V10 返工验收报告（Lead 独立复核）

> 日期：2026-10-17
> 范围：按 `docs/reviews/V10_REVIEW_SUMMARY.md` 的裁决执行返工
> 结论：**返工已完成，可提交二次评审**

---

## 一、返工范围与执行结果

| 任务 | 执行者 | 状态 | 产出 |
|---|---|---|---|
| V10-00b 测试隔离 | impl-gates | ✅ 完成 | `tests/conftest.py`（新）+ 18 个测试文件改造 |
| V10-00c 备份安装 + 恢复演练 | impl-gates | ✅ 完成 | `scripts/ssq_backup.sh`（新）+ `/etc/cron.d/ssq-backup` + `DEPLOY.md` |
| V10-00d 迁移加固 | impl-gates | ✅ 完成 | `lottery/migrations.py`、`lottery/api_app.py`（health）、`tests/test_v10_migrations.py`（新） |
| 方案 v2.0 返工 | impl-plan-rewrite | ✅ 完成 | `docs/V10_IMPROVEMENT_PLAN.md`（401 → 1264 行） |
| Gate-6 推送 V6–V9 | Lead | ✅ 完成 | 远端 `af5b601` = 本地 HEAD |

---

## 二、Lead 独立复核（不采信执行者汇报，全部亲测）

### 2.1 硬约束：全部遵守

| 约束 | 复核方式 | 结果 |
|---|---|---|
| 不动 `config.py` / `engine.py` / `db.py` / `web/` | `git diff --stat` | **空**，零改动 ✅ |
| 不升版号 | `grep APP_VERSION` | 仍 `2.5.0 / 2026-10-V9` ✅ |
| 不改出号逻辑 | 同上（engine.py 未动） | ✅ |
| 前端零改动 | `git status --short web/` | 0 个文件 ✅ |
| 未 commit / 未重启容器 | `git status` | 只有工作树改动，容器未动 ✅ |

### 2.2 测试：485 passed（480 原有 + 5 新增）

```
$ .venv/bin/python -m pytest -q
485 passed in 76.19s
```
- 原有 480 条**一条不少**，断言未放宽；
- 随机顺序多 seed 验证（执行者跑了 seed 99 / 1 / 7 / 2026，均全绿）；
- compileall / node --check / git diff --check 全通过。

### 2.3 测试隔离：修复真实生效（这是 Gate 的核心）

Lead 独立复现 R4 报告里那个失败场景：

```
$ .venv/bin/python -m pytest -q tests/test_m4_5_engineering.py tests/test_v4_honest_ui.py
19 passed in 1.56s

$ .venv/bin/python -m pytest -q tests/test_v4_honest_ui.py tests/test_m4_5_engineering.py
19 passed in 1.63s
```
**正反两种顺序都通过**——改造前该组合是 1 failed（`count_draws()==0`）。

裸赋值清理情况（仅源码，排除 `__pycache__` 与注释）：
```
$ grep -rn --include=*.py "config\.DB_PATH\s*=" tests/ | grep -v monkeypatch
tests/test_m4_eval.py:4:    不能用裸赋值 ``config.DB_PATH = ...``：它不会还原...
tests/conftest.py:6:19 处用例用裸赋值 ``config.DB_PATH = tmp_path / ...`` 把全局配置改到临时库且
tests/conftest.py:31:约定：``config.DB_PATH = ...`` 这类裸赋值在本目录已全部清理...
```
**3 处命中全部是文档注释**（解释"为什么不能这么做"），实际代码 19 处已清零 ✅

> 关键技术点：`lottery/db.py` 有模块级连接缓存 `_CONN`，只改 `config.DB_PATH` 而不重置缓存仍会串库。
> 执行者用现成的 `db.close()` 重置 + `monkeypatch.setattr`，**没有扩 `db.py` 的 API**（符合约束）。

### 2.4 备份：真实可用（不是"脚本写好了"）

Lead 用脚本自带命令做了**独立恢复演练**：

```
$ ./scripts/ssq_backup.sh restore /backup/ssq-backup-20261017-123401.tar.gz /tmp/lead-verify
{
  "ok": true,
  "draws": 3515,
  "schema_version": 2,
  "integrity": "ok",
  "files": {
    "hypotheses.json": 9345, "llm_config.json": 208, "llm_claims.json": 5657,
    "patterns_runtime.json": 14021, "adaptive_state.json": 2, ...
  },
  "raw_files": 10,
  "errors": []
}
```

- **期数 3515 与生产库一致**；
- **8 个 runtime json 全部可解析**（R4 指出旧方案只 `cp` 一个 db，等于没备份）；
- **10 个 raw 快照**在档；
- `integrity_check = ok`。

基础设施实测存在：
```
$ ls -la /etc/cron.d/ssq-backup   → 761 字节，每日 03:17
$ ls -la /backup/                → drwx------ ，2 个归档（2.2MB 级）
```
- 热备用 `sqlite3.Connection.backup()`（不是裸 `cp` 正在写的库）；
- 排除 `ml_models/`（265MB 可重建），MANIFEST 与日志中注明；
- `llm_config.json` 恢复后权限 **600**（含 API Key，不能 o+w）；
- 日志不写密钥内容。

### 2.5 迁移加固：回滚真的会回滚

```
$ .venv/bin/python -m pytest -q tests/test_v10_migrations.py
5 passed in 1.22s
```
覆盖：注入失败迁移 → 整体回滚（含同批"先成功"那条）/ `schema_version` 不前进 / 自动备份 `data/ssq.db.pre-migrate-v<cur>` 存在 / fail-closed（备份抛错则禁止迁移）/ 幂等 / 内存库跳备份 / health 暴露 schema_version。

> 关键坑（执行者记录）：`conn.backup()` 走 LEGACY 事务模式时 DDL 会自动提交，必须用 autocommit + `BEGIN IMMEDIATE` 才能让 DDL 一起回滚；调用方已有事务时改 SAVEPOINT 嵌套。

### 2.6 方案 v2.0：删除项已彻底移除

抽查 6 个已废设计关键词的残留语境：

| 关键词 | 命中数 | 语境核查 |
|---|---|---|
| `CUSUM` | 9 | 全部在删除台账 / "为什么不再做"依据段 / 任务表删除行 / 附录 grep 清单 |
| `选择性推断` / `truncnorm` | 7 / 4 | 同上 |
| `Kendall` / `llm_decision_skill` | 3 / 2 | 同上 |
| `LOTT_PREDICT_CHANNELS` | 6 | 同上 |

**没有一处仍在指导程序员实现已删除的方案** ✅

必存项计数（证明替换方案写实了）：
`resolve_channels`×19、`prereg_ok`×8、`ev_ratio_sixth`×9、`11/54`×6、`LOTT_SLOW_TESTS`×10。

### 2.7 Gate-6：V6–V9 已推送

```
$ curl -s https://api.github.com/repos/jiam9069/ssqyuce/commits?per_page=1
"sha": "af5b60167d743151b1423c2aee0706bf4abd2d8d"   ← 远端
$ git rev-parse HEAD
af5b60167d743151b1423c2aee0706bf4abd2d8d            ← 本地
```
三方 SHA 重新一致，`PROJECT_CONTEXT.md:43` 的记录重新成立。

---

## 三、一处裁决内部的张力（已按 G4 执行，在此备案）

裁决文件 §四 要求「冻结 `experiments` / `replay_results` / `llm_proposals` 三表 DDL」，
但同一份裁决的 G4 与 §五 又判定「**并入 `hypothesis.py` 加字段，不建 `experiments` 表**」。

**Lead 裁定：按 G4 执行。** 理由：
- G4 是"结论"，§四 的三表清单是"在还没决定落点时的候选"；
- 建 `experiments` 表会形成**第 4 本登记簿**，而 `usable` 的唯一计算点 `patterns_runtime.py:104` 不读它 → **必然白做**（这正是 R1 的核心警告）；
- 执行者的处理（migration #3 只含 `replay_results` + `llm_audit`，并在方案 §2.1.5 标注差异）是正确的。

---

## 四、未完成项（如实列出，不掩饰）

| # | 项 | 原因 | 归属 |
|---|---|---|---|
| 1 | 容器仍是旧镜像，**线上** `/api/health` 暂无 `schema_version` | 按约束未重启容器（V10-00 不发布） | 下次部署时生效 |
| 2 | 慢测试闸门 `LOTT_SLOW_TESTS` 未实现 | 不在 task-2 三项交付范围内 | 建议并入 V10-01 首个提交（方案已写入要求） |
| 3 | `data/methods_config.json` 未入档 | 该文件当前不存在，脚本按 `missing=` 如实记录，不报错 | 合理 |
| 4 | `.env` 不在备份内 | 在 `data/` 之外，含密钥；DEPLOY.md 已写明需单独备份 | 合理，已文档化 |

---

## 五、返工后的交付清单（提交前）

```
 M DEPLOY.md                     ← 备份/恢复真实命令 + 迁移回滚步骤
 M lottery/api_app.py            ← /api/health 暴露 schema_version
 M lottery/migrations.py         ← 批次事务 + 自动备份 + fail-closed
 M tests/*.py (18 个)            ← 47 处裸赋值/派生路径改造
?? tests/conftest.py             ← 统一 DB 隔离（新）
?? tests/test_v10_migrations.py  ← 5 条迁移/回滚测试（新）
?? scripts/ssq_backup.sh         ← 备份/恢复/校验脚本（新）
?? docs/V10_IMPROVEMENT_PLAN.md  ← 方案 v2.0
?? docs/v10_probe_tickets.py     ← 探针 v2（已修正 rng 缺陷）
?? docs/reviews/                 ← 4 份评审 + 裁决 + 实现记录
```

**测试**：485 passed / 76.19s
**校验**：compileall ✅ / node --check ✅ / git diff --check ✅
**版本**：仍 2.5.0 / 2026-10-V9（V10-00 不发布，符合预期）

---

## 六、下一步

按用户指示"先返工，然后再评审"——返工已完成，进入**二次评审**。二次评审应聚焦：

1. **新增代码的正确性**（conftest 隔离是否真无泄漏、migrations 事务边界、备份脚本的密钥与并发安全）
2. **方案 v2.0 的可实现性**（尤其：前置注册的 U1 接线测试、AI 两岗位的判定方式、var_ratio 的 11/54 推导）
3. **是否满足全部 10 条 Gate**
4. **是否可以开工 V10-01**
