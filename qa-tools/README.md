# qa-tools · 团队质量工具（刘工维护）

> 位置理由（2026-10-21 lead 提出后重定）：这里的工具**不是产品代码**，而是团队复现/取证工具。
> 放在 `web/` 下会被当作前端交付物的一部分（构建检查、Dockerfile 打包可能带进镜像），
> 也会让后来的人误以为 `web/tools/` 是产品功能目录。放 `qa-tools/` 的分类含义是：
> **「谁都能用、与产品无关、长期存在」**。

> ⚠️ **两个工具二选一，不要串联。** `delay_harness.py` 自己是延迟源，`delay_addon.py`
> 也是延迟源；若把「前端 → addon → harness」串起来，延迟会**相加**
> （实测：addon 3s + harness 3s = `6.012743s`），此时 `--set delay=3` 看起来像「不生效」，
> 实际是被另一个源叠了一倍。严QA 2026-10-21 实测记录，本 README 据此补注。

## delay_harness.py · 时序探针（零依赖，仅 Python 标准库）

给两类时序缺陷提供可控环境，**两种模式语义相反，禁止混用**（详见
`docs/team/engineering/FRONTEND.md` §1.3.1）：

| 模式 | 用途 | 命令 | 判据 |
|---|---|---|---|
| `race`（默认） | 竞态：旧响应覆盖新结果 | `python3 qa-tools/delay_harness.py --port 18099` | 最终渲染只反映**最后发起**的请求 |
| `cheap` | 防重复提交：连点是否被拦 | `python3 qa-tools/delay_harness.py --port 18100 --mode cheap` | **发出**的请求恰 1 条（不是返回条数） |

```bash
# 每轮复现前必须复位（有状态设计，不复位则第 2 轮起延迟序列错乱、复现率失真）
curl -s localhost:18099/reset
curl -s -o /dev/null -w '%{time_total}\n' localhost:18099/api/items   # race 模式约 3s
curl -s -o /dev/null -w '%{time_total}\n' localhost:18099/api/items   # 第 2 次约 0s
curl -s localhost:18099/stats                                          # 原始计数与延迟序列
```

实测（2026-10-21）：`3.001208s / 0.001239s`，`{"total":2,"slow_count":1,...}`；
reset 后重跑同样可重复；**不 reset 直接跑则第 1 次只有 `0.001646s`** —— 这就是复现率会失真的原因。

## delay_addon.py · mitmproxy 延迟 addon（代理链路）

```bash
mitmdump -q -s qa-tools/delay_addon.py --set delay=3.0 --set target=/api/items -p 8080
curl -x http://127.0.0.1:8080 -s -o /dev/null -w 'matched   %{time_total}s\n' http://TARGET_HOST/api/items
curl -x http://127.0.0.1:8080 -s -o /dev/null -w 'unmatched %{time_total}s\n' http://TARGET_HOST/api/other
curl                        -s -o /dev/null -w 'direct    %{time_total}s\n' http://TARGET_HOST/api/items
```

实测（mitmproxy **12.2.3**）：`3.011354s / 0.005292s / 0.000866s`，`--set delay=1.5` → `1.507708s`。

**坑（已修，勿重犯）**：mitmproxy 12.x 的 option **不支持 `float`**，
`loader.add_option("delay", float, 3.0, ...)` 会抛
`NotImplementedError: unsupported option type: <class 'float'>` 且进程启动即退出；
本 addon 用 `str` 传参、在 `configure()` 里 `float()` 解析。
函数式 addon 还必须写 `addons = [...]` 注册；浏览器不读 `HTTPS_PROXY` 环境变量，
Chrome 需 `--proxy-server=http://127.0.0.1:8080`。

## 前端字符串断言的强制两步（2026-10-21，lead 要求固化）

本项目的测试多用字符串匹配断言前端行为，这是**假绿高发区**。已实测两层坑，两层都会让「删掉实现」仍然全绿：

1. **注释含关键词**：`assert "www-authenticate" in js.lower()` 在删掉代码判定后仍通过——文件头注释里写着这个词。
   → 断言前**先剥离注释**（去 `/* ... */` 与 `//`，注意避开 `http://`）。
2. **函数定义能匹配「函数被调用」的正则**：`re.search(r"recoverCsrfAndHint\(path\)", js)` 命中的是
   `async function recoverCsrfAndHint(path) {` 这行**定义**。
   → 要求语句级特征，例如末尾分号：`re.search(r"recoverCsrfAndHint\s*\(\s*path\s*\)\s*;", js)`。

**强制两步**：剥离注释 → 变异验证（删掉/改坏实现，确认测试**真的变红**）。
变异清单与实例见 `docs/team/engineering/FRONTEND.md` §1.3.2。

> 变异验证为什么必须做：一条抓不住回归的断言，比没有断言更危险——它占着「已验证」的位置。
> 上述两条判据都是先被变异测试打红、再回填进文档的，不是读代码想出来的。
