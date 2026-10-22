# D-03 变异实证报告

> 执行：2026-10-22 · 对象 `tests/test_d03_boundary_hardening.py`（16 条边界测试）
> 目的：把 D-03 从「有条件通过」转正 —— **未做变异实证的守卫，视为未验证**。

## 结论

**16 条中抽验 5 条关键路径，全部能被对应缺陷打红。** D-03 转 **✅ 通过**。

## 变异表

| # | 变异 | 被哪条测试抓住 |
|---|---|---|
| M1 | `reds` 非法时返回 `200` 而非 `400` | `test_popularity_invalid_reds_are_400_not_500` ✅ |
| M2 | 删 `Query(ge=1)` 边界声明 | `test_blue_coverage_n_tickets_bounds_are_declared` ✅ |
| M3 | 未拟合时返回 `200` 空数据而非 `503` | `test_popularity_unfitted_is_503_with_error_not_empty_200` ✅ |
| M4 | `degraded` 退回旧语义（只反映常量） | `test_capabilities_degraded_agrees_with_items` ✅ |
| M5 | 降级项缺 `how_to_restore` | `test_capabilities_degraded_entries_all_have_restore_path` ✅ |

## 过程中我自己犯的两次「变异选错目标」

这两次都不是测试无牙，而是**我的变异打在了不该打的地方**。
记下来，因为它们决定了「变异实证」这件事本身可不可信。

### ① M2：变异打在「类型契约」上，测试守的是「边界声明」

我原先把 `Query(10, ge=1, le=100)` 改成 `Query(10)`，期望
`test_blue_coverage_invalid_n_tickets_is_rejected_before_logic` 变红。

**没红，因为那条测试守的是「非整数传入要抛 TypeError」**（调用方类型错误），
而 `ge/le` 是 HTTP 层 422 的依据，由另一条测试
`test_blue_coverage_n_tickets_bounds_are_declared` 守。

**测试没问题，我找错了对应关系。**
判据：**每个变异必须先确定「哪条测试是专门守这个行为的」**，
不能拿一条名字相近的测试当靶子。

### ② M5：变异打在「异常分支」上，那条分支正常不执行

`routes_capabilities.py` 里有 6 处 `how_to_restore`，我第一次改的是
`popularity_model` 的 `except` 分支 —— 当前没有异常，那段代码根本不执行，
所以测试自然绿。

**改成正常路径（`idle_mining` 那条）后立刻被抓。**

判据：**变异必须打在「当前会执行到的路径」上**，否则测的是死代码。

## 一条对未来的提醒

`test_capabilities_degraded_entries_all_have_restore_path` 的写法是
`for item in body["degraded_items"]: assert item.get("how_to_restore")`。

**若某天 `degraded_items` 变成空列表，这条测试会空转通过**（循环体不执行）。
当前它有 2 条（`idle_mining`、`usable_replayable`），所以有效。

**建议补一条前置断言**：
```python
assert body["degraded_items"], "降级项为空 —— 本测试会空转，请确认是否预期"
```
**这与今天反复出现的「守卫在空集合上空转」是同一类问题。**
已记为 D-03 的后续改进项（不阻塞转正）。

## 复现方式

```bash
# 单条变异示例：删 ge=1
sed -i 's/Query(10, ge=1, le=100/Query(10, le=100/' lottery/routes_portfolio.py
.venv/bin/python -m pytest tests/test_d03_boundary_hardening.py::test_blue_coverage_n_tickets_bounds_are_declared -q
# 期望：1 failed
git checkout -- lottery/routes_portfolio.py
```
