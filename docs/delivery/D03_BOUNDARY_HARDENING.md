# D-03 后端端点边界加固 · 端点 × 场景矩阵

> 交付人：李工（后端）｜日期：2026-10-21｜代码基准：`db634e3` + 本轮改动
> 复现命令：`bash docs/delivery/repro/verify_d03_d04_d07.sh`
> 测试文件：`tests/test_d03_boundary_hardening.py`（31 条，全部为失败路径）

## 〇、测试基线（当次实测，非转述）

```
cd /root/DSH/应用/ssqyuce && .venv/bin/python -m pytest tests/ -q --ignore=tests/frontend
→ 1480 passed, 24 skipped, 1 xfailed in 482.85s
```

| 项 | 基线 | 当次 | 结论 |
|---|---|---|---|
| passed | 1358 | **1480** | ✅ +122 |
| skipped | 24 | **24** | ✅ 未增 |
| xfailed | 1 | **1** | ✅ 未增 |

## 一、参数校验矩阵

测试方式：容器内无 `httpx`，故**直接调用路由函数**，再另用 signature 元数据断言
`Query(ge/le)` 约束**真的挂在参数上**（那是 HTTP 层 422 的依据）。

| 端点 | 参数 | 非法输入 | 期望 | 实际 | 结论 |
|---|---|---|---|---|---|
| `/api/portfolio/popularity` | `reds` | `"1,2,3"`（不足 6） | 400 | 400 | ✅ |
| 同上 | `reds` | `"1,2,3,4,5,6,7"`（超出 6） | 400 | 400 | ✅ |
| 同上 | `reds` | `"1,2,3,4,5,99"`（越界） | 400 | 400 | ✅ |
| 同上 | `reds` | `"0,1,2,3,4,5"`（下界越界） | 400 | 400 | ✅ |
| 同上 | `reds` | `"1,2,3,4,5,5"`（重复） | 400 | 400 | ✅ |
| 同上 | `reds` | `"a,b,c,d,e,f"`（非数字） | 400 | 400 | ✅ |
| 同上 | `reds` | `"1,2,3,4,5,"`（空段→不足 6） | 400 | 400 | ✅ |
| 同上 | `reds` | `"1;2;3;4;5;6"`（错误分隔符） | 400 | 400 | ✅ |
| 同上 | `reds` | 未传（`Query` 对象兜底） | 400/422 | 400 | ✅ |
| 同上 | `reds` | `"１,２,３,４,５,６"`（全角） | **见注** | 200/503 | ⚠️ 接受 |
| `/api/portfolio/blue-coverage` | `n_tickets` | `"abc"` / `""` / `"1e9999"` / `None` | 422 | 函数层抛类型错，HTTP 层 422 | ✅ |
| 同上 | `n_tickets` | `0` / `-1` / `-999` | 422 | 函数层夹到 0，HTTP 层 422 | ✅ |
| 同上 | `n_tickets` | `101`（>le=100） | 422 | HTTP 层 422（约束存在已断言） | ✅ |
| `/api/capabilities` | 无参 | — | 200 稳定 | 200 稳定 | ✅ |

**注（全角数字）**：`int("１") == 1` 是 Python 原生行为，全角数字**会被正常解析**为
1~6，且解析后仍受 1~33 与去重校验。这是真实行为，已在
`test_fullwidth_reds_are_accepted_by_int_parsing` 中如实记录，**不冒充缺陷、也不掩盖**。

## 二、503 降级矩阵（三场景）

| 场景 | 端点 | 期望 | 实际 | degraded 可辨识 | 结论 |
|---|---|---|---|---|---|
| **模型未拟合** | `/api/portfolio/popularity` | 503 | 503 | `ok=false` + `error` 含「未就绪」 | ✅ |
| **常量源故障**（模拟 DB/模块异常） | `/api/capabilities` | 200 + `degraded=true` | `constants.alpha=null`，`degraded=true`，`degraded_constants=["alpha"]` | ✅ |
| **常量源抛错**（模拟 DB 只读） | `/api/capabilities` | 不静默 | `constants_meta.errors["single"]` 含原始错误串 | ✅ |
| **挖掘停机**（`LOTT_IDLE_MINING=0`，当前默认） | `/api/capabilities` | 列入 `degraded_items` | `id=["idle_mining"]` + `how_to_restore` | ✅ |
| **DB 不可读**（模拟 `sqlite3.OperationalError`） | `/api/capabilities` | 不 500 | 200，`degraded_items` 含 `popularity_model` | ✅ |
| **挖掘已开启**（反向用例） | `/api/capabilities` | 不得报降级 | `idle_mining` 不在降级列表 | ✅ |

## 三、降级可辨识（最恶劣的撒谎：200 配空数据）

**已守住**。具体实现：

1. `popularity` 模型未就绪 → **503**，不返回假档位；
2. `capabilities` 常量取不到 → 该值为 `None`（**不是 0**）+ `degraded=true` + `constants_meta.errors` 留痕；
3. `degraded_items[*]` 每项强制带 `how_to_restore`（只报「坏了」不给出路 = 把问题甩给用户）；
4. 守卫测试 `test_no_endpoint_hides_failure_as_empty_200` 断言：`degraded=true` 时必须说明缺什么。

## 四、测试清单（31 条，全部失败路径）

| # | 用例 | 守的东西 |
|---|---|---|
| 1–9 | `test_popularity_invalid_reds_are_400_not_500` | 9 种非法 `reds` 均 400 |
| 10 | `test_popularity_missing_reds_default_is_not_500` | 缺参不 500 |
| 11–14 | `test_blue_coverage_invalid_n_tickets_is_rejected_before_logic` | 非整数在业务逻辑前被拒 |
| 15 | `test_blue_coverage_n_tickets_bounds_are_declared` | `ge=1/le=100` 约束真实存在 |
| 16–18 | `test_blue_coverage_non_positive_clamped_not_crash` | 负数夹到 0 |
| 19 | `test_capabilities_takes_no_params_and_never_500` | 无参端点稳定 |
| 20 | `test_popularity_unfitted_is_503_with_error_not_empty_200` | 503 而非空 200 |
| 21 | `test_capabilities_flags_degraded_when_constant_missing` | 常量缺失标 degraded |
| 22 | `test_capabilities_degraded_lists_idle_mining_when_off` | 挖掘停机可辨识 |
| 23 | `test_capabilities_degraded_entries_all_have_restore_path` | 降级必给恢复路径 |
| 24 | `test_capabilities_constant_error_is_visible_not_silent` | 故障留痕 |
| 25 | `test_capabilities_survives_db_read_failure` | DB 故障不 500 |
| 26 | `test_no_endpoint_hides_failure_as_empty_200` | 反向守卫 |
| 27–30 | `test_capabilities_has_no_banned_words` | 4 个禁用词 |
| 31 | `test_fullwidth_reds_are_accepted_by_int_parsing` | 如实记录真实行为 |

## 五、三态结论

| 事项 | 状态 | 依据 |
|---|---|---|
| 参数校验：非法输入 400/422，不 500 | **已验证** | 用例 1–19 实跑通过 |
| 503 降级：模型未拟合场景 | **已验证** | 用例 20 |
| 503 降级：DB 只读 / 模块故障 | **已验证** | 用例 21、24、25 |
| 503 降级：挖掘停机 `LOTT_IDLE_MINING=0` | **已验证** | 用例 22 |
| **LLM 不可用** 场景 | **未验证** | 见下节 |
| 降级可辨识（503 + degraded） | **已验证** | 用例 20、22、23、26 |
| 测试 ≥15 条全部失败路径 | **已验证** | 31 条 |

## 六、未做到的部分（如实列出）

1. **LLM 不可用场景未由本文件覆盖**。原因：`/api/llm/test` 等端点的 LLM 故障路径
   依赖 `llm_client` 的真实调用与超时，本轮我**没有**为它构造可控的故障注入
   （`llm_client` 的重试与超时策略不是我的写作用域，且强行 monkeypatch 其内部
   会与 W08 的 LLM 用例互相干扰）。现有覆盖在 `tests/test_w08_user_llm_api.py` 与
   `tests/test_v10_llm_arm.py`，**我未逐条核实其是否断言了 503 + degraded**。
   → 建议由 D-03 第二轮或 LLM 负责人补，不要算作本项已完成。
2. **未做 HTTP 层实测**（`curl` / TestClient）：容器内无 `httpx`，全部为函数直调 +
   signature 元数据断言。422 的 HTTP 行为由 FastAPI 框架保证，**我未亲眼看到 422 响应体**。
3. **`/api/capabilities` 未接入前端**：按分工属刘工（D-01/D-04 联动项），本项只交付后端。
