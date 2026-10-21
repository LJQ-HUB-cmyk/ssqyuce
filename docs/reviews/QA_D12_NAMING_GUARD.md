# D-12 验收报告：命名纪律测试排除规则收窄

> 验收人：严QA（独立验收）｜日期：2026-10-21
> 被验对象：`tests/test_v14_naming_discipline.py`（工作区未提交改动，基准 HEAD `db634e3`）
> 工作区全量：**1488 passed, 24 skipped, 1 xfailed**（基线 1358，净增 130，skip 未增）
> 隔离副本：`/tmp/qa/ws_now`（工作区 tar 副本，全程未触碰共享工作区）

---

## 结论：⚠️ **有条件通过**

守卫的**主体是有效的**（4 个变异中 3 个正确捕获，1 个暴露轻微缺陷），
但**存在一个可复现的规则级漏洞**：引号包裹的用户可见文案会被整套豁免。

**放行条件**：修复 §3 的漏洞 1（引号豁免过宽），或书面确认“引号包裹的禁用词视为合规”。
后者不成立 —— 它正是 D-12 要修的形态。

---

## 1. 声称完成的内容（据 `git diff` 与文档）

`tests/test_v14_naming_discipline.py` 从 162 行扩到 **599 行**（+437），新增两组守卫：

| 组 | 位置 | 核心函数 | 目标 |
|---|---|---|---|
| 第一套 | `:202-349` | `_strip_js_comments` / `_strip_html_comments` / `_render_offenders` | 剥注释后看渲染面 |
| 第二套 | `:370-599` | `_strip_comments_keep_lines` / `_normalize_concat` / `_render_surface_lines` / `_is_discipline_mention` | 保留行号 + 字符串拼接归一化 + 纪律引用豁免 |

并同步改了前端文案（已核实是真改，不是改注释蒙混）：

```
app.js:911  metricItem("可回放且通过前置注册的规律", ...)
app.js:927  '<th>通过前置注册的规律</th>'
app.js:1038 toast("检验完成：" + _n + " 条通过前置注册" ...)
```

`web/index.html` 原红字行（`0f5656d:333`「后端纪律明令禁止把它叫『可用规律』」）已删除，
替换为注释说明「原红字行…已删除」。

---

## 2. 我的实测结果

### 2.1 测试

```
cd /root/DSH/应用/ssqyuce && .venv/bin/python -m pytest tests/ -q --ignore=tests/frontend
→ 1488 passed, 24 skipped, 1 xfailed, 1 warning in 206.59s (0:03:26)
```

| 项 | 基线 | 当前 | 判定 |
|---|---|---|---|
| passed | 1358 | **1488** | ✅ +130 |
| skipped | 24 | **24** | ✅ 未增 |
| xfailed | 1 | **1** | ✅ 未增 |

新增 130 条对应 D-03（边界加固）、D-04（能力清单）、D-07（usable 语义）、D-12（命名守卫），
**均有测试文件对应，无「凭空多出来的 passed」**。

### 2.2 变异测试（4 个，逐个注入 → 定点跑 → 复原）

| # | 变异 | 注入位置 | 结果 | 判定 |
|---|---|---|---|---|
| N1 | 渲染面注入 `"可用规律 12 条"` | `app.js` | `test_usable_law_guard_covers_app_js` + `test_no_rendered_banned_phrase[static/app.js]` **2 failed** | ✅ 捕获（两套都抓到） |
| N2 | HTML 正文注入 `<h3>可用规律 · 12 条</h3>` | `index.html` | **3 failed**（含旧守卫 `test_index_html_does_not_call_...`） | ✅ 捕获 |
| N3 | 让 `_is_discipline_mention` **恒 return True**（守卫空转） | 守卫自身 | `test_discipline_mention_is_exempt_but_plain_use_is_not` **1 failed** | ✅ 捕获（反空转设计有效） |
| N4 | 把 `index.html` 改名（文件缺失） | 页面文件 | **2 failed, 1 skipped** | ⚠️ 见缺陷 2 |

**N3 是最重要的一条**：它证明这套守卫**不是空转的** —— 把豁免规则放宽会立刻变红。
这在同类守卫测试里并不常见，值得肯定。

### 2.3 规则级冲突测试（**关键发现**）

两套守卫的豁免规则**不一致**，且第二套有系统性漏洞。

`_is_discipline_mention` 的豁免条件是「禁用词被引号包裹」：
```python
for marker in (f"『{_BANNED_PHRASE}』", f"「{_BANNED_PHRASE}」",
               f"【{_BANNED_PHRASE}】", f"\"{_BANNED_PHRASE}\"",
               f"“{_BANNED_PHRASE}”", f"'{_BANNED_PHRASE}'"):
    if marker in line: return True      # ← 豁免
```

**问题：JS 的每个字符串字面量本来就被引号包裹。** 实测判定对照：

| 样本（均为用户可见） | 第二套判定违规? | 第一套判定违规? |
|---|---|---|
| `metricItem("可用规律", 0);` | ❌ **False（放过）** | ✅ True |
| `metricItem('可用规律', 0);` | ❌ **False（放过）** | ✅ True |
| `<span>「可用规律」是当前结论</span>` | ❌ **False（放过）** | ✅ True |
| `<p>『可用规律』0 条</p>` | ❌ **False（放过）** | ✅ True |
| `<td>【可用规律】</td>` | ❌ **False（放过）** | ✅ True |
| `h += '<b>「可用规律」</b>';` | ❌ **False（放过）** | ✅ True |
| `metricItem("可用规律（可回放）", 0);` | ✅ True | ✅ True |
| `<th>可用规律</th>` | ✅ True | ✅ True |

**端到端实证**（在副本 `app.js` 注入 `function _qaProbe(){ return metricItem("可用规律", 0); }`）：

```
FAILED tests/test_v14_naming_discipline.py::test_usable_law_guard_covers_app_js
1 failed, 22 passed
```

**只有第一套报了，第二套（号称更完善的那套）静默放过。**

---

## 3. 缺陷清单

### 缺陷 1（P1，建议打回）｜引号豁免过宽，正是 D-12 要修的形态

`_is_discipline_mention` 把「引号包裹」等同于「纪律引用」。
但**产品文案给术语加引号是最常见的强调写法**：

- 「可用规律」0 条  ← 引号是强调，不是引用
- `<td>【可用规律】</td>` ← 表格标签加方括号，同样是强调

**而 D-12 的原始诉求恰恰是**：「`index.html:333` 那句**转述禁用词**的行被放过」。
现在的守卫把"转述禁用词"从"含否定词就放过"改成了"加引号就放过" ——
**漏洞从一种形态迁移到另一种形态，没有真正关闭。**

**为什么第一套能抓到**：第一套的判据是「剥注释后含禁用词即违规」，
**根本不看引号**。它更严，也更符合 D-12 的要求。

**修复建议**（二选一）：

**A. 合并为一套（推荐）** —— 删掉第二套的引号豁免，统一用"剥注释即判"：
```python
def test_no_rendered_banned_phrase(rel):
    ...
    for lineno, line in _render_surface_lines(path):
        if _BANNED_PHRASE in line:          # 不再看引号
            offenders.append((lineno, line.strip()[:120]))
```
注释里的纪律声明本来就被 `_strip_*_comments` 剥掉了，**不需要再靠引号区分**。

**B. 保留两套，但让第二套"取并集"** —— 任何一套判定违规即算违规：
```python
assert not first_offenders and not second_offenders
```

### 缺陷 2（P2）｜`index.html` 缺失时静默 skip

```python
path = _WEB / rel
if not path.exists():
    pytest.skip(f"{rel} 不存在")
```

N4 实测：把 `index.html` 改名后 → `2 failed, **1 skipped**`。
该 skip 会让 **skipped 从 24 变成 25**，**直接触碰红线 1「skipped 增加」**。

**更严重的是语义**：前端文件丢失时，守卫应当**失败**（用户看不到页面了），
不该 skip（看起来"跳过了一个检查"，实际是"检查对象没了"）。

**修复建议**：`assert path.exists(), f"{rel} 不存在 —— 前端文件丢了，守卫无从谈起"`

### 缺陷 3（P3）｜两套守卫职责重叠，维护风险

599 行里有 **两套并行的剥注释实现** 和 **两套判定函数**，且规则不一致（缺陷 1）。
后改的人不知道该改哪套、以哪套为准。这在「镜像双源」有前科的项目里是明确警示。

**建议**：保留一套（第一套更严），第二套只保留 `_normalize_concat`（字符串拼接归一化，
这是第一套没有的真实增强）并入第一套。

### 缺陷 4（信息）｜原红字行的处理方式需要确认

`web/index.html` 里现在写的是：
```
<!-- 原红字行（"后端纪律明令禁止把它叫「可用规律」"）已删除：
```
用户看不到（在注释里），符合 D-12 要求。**但保留这段注释等于把禁用词又写进文件一次**，
与 F8「删注释里的『可用规律』」冲突。不阻塞，建议一并清理。

---

## 4. 三态表

| 项 | 状态 | 证据 |
|---|---|---|
| 工作区全量测试不降 | **已验证** | `1488 passed, 24 skipped, 1 xfailed` |
| skipped / xfailed 未增 | **已验证** | 24 / 1，与基线一致 |
| 守卫能抓住渲染面注入（app.js） | **已验证** | N1 → 2 failed |
| 守卫能抓住渲染面注入（index.html） | **已验证** | N2 → 3 failed |
| 守卫不是空转（放宽豁免会变红） | **已验证** | N3 → 1 failed |
| 前端文案真改（非改注释蒙混） | **已验证** | `app.js:911/927/1038` 展示名已换 |
| 排除规则已「收窄到纯否定句」 | **有风险** | 实为「引号包裹即豁免」，见缺陷 1 |
| 两套守卫规则一致 | **有风险** | 实测 6 / 8 个样本判定相反 |
| 文件缺失时失败而非 skip | **有风险** | N4 → 1 skipped，会破红线 1 |

---

## 5. 判定

**⚠️ 有条件通过** —— 满足以下任一即可放行：

1. **（推荐）** 删除第二套的引号豁免，或改为两套并集判定；
2. 书面确认「引号包裹的禁用词视为合规」（**我不建议**，因为这与 D-12 的原始诉求矛盾）。

**另外必须修**：`index.html` 缺失时的 `pytest.skip` 改 `assert`（缺陷 2）。

**这不算严重失败**：守卫从「只扫 1 个文件、按行放过」进化到「扫 2 个文件、剥注释、归一化拼接」，
是**实质进步**，且反空转设计（N3）做对了。问题在于**收窄方式选错了维度**：
D-12 要的是「按**是否渲染**收窄」，实现成了「按**有无引号**收窄」。

---

## 6. 复现命令

```bash
# 建副本（不碰共享工作区）
SRC=/root/DSH/应用/ssqyuce; NOW=/tmp/qa/ws_now
rm -rf $NOW && mkdir -p $NOW && cd $SRC
tar -c --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.serena --exclude=data/archive --exclude=data/backups . | tar -x -C $NOW
ln -s $SRC/.venv $NOW/.venv

# 两套守卫规则对照（关键复现）
cd $NOW && .venv/bin/python - <<'EOF'
import sys, importlib.util
sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location("nd", "tests/test_v14_naming_discipline.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
for c in ['metricItem("可用规律", 0);', '<span>「可用规律」0 条</span>', '<th>可用规律</th>']:
    second = (m._BANNED_PHRASE in c) and not m._is_discipline_mention(c)
    first  = bool(m._render_offenders(c, lambda x: x))
    print(f"{c:42s} 二套违规={second}  一套违规={first}")
EOF
#  → metricItem("可用规律", 0);        二套违规=False  一套违规=True
#  → <span>「可用规律」0 条</span>      二套违规=False  一套违规=True
#  → <th>可用规律</th>                二套违规=True   一套违规=True

# 端到端：注入后单跑
cd $NOW && python - <<'EOF'
import pathlib
p = pathlib.Path("web/static/app.js"); t = p.read_text(encoding="utf-8")
old = "function setFree("
p.write_text(t.replace(old, 'function _qaProbe(){ return metricItem("可用规律", 0); }\n' + old, 1), encoding="utf-8")
EOF
.venv/bin/python -m pytest tests/test_v14_naming_discipline.py -q
#  → 1 failed, 22 passed   （第二套静默放过）
```

---

## 7. 我发现的、报告中未提到的问题

1. **两套守卫规则互相矛盾**（6/8 样本判定相反），而项目有「镜像双源」前科 —— 这是同类风险的再版。
2. **豁免维度选错**：D-12 要求「按是否渲染收窄」，实现成「按有无引号收窄」，
   **漏洞从「含否定词就放过」迁移到「加引号就放过」，没有关闭**。
3. **N4 的 skip 会破红线 1**（skipped 24→25），且语义上「文件丢了该失败」。
4. **`index.html` 里保留了一段含禁用词的「已删除说明」注释**，与 F8 冲突。
5. 顺带：`test_known_limit_runtime_construction_not_covered` 这类「把边界写成可执行用例」
   的做法**值得推广** —— 它防止后人误以为「过了守卫就没禁用词」。
