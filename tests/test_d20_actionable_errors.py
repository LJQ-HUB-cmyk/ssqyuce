"""D-20：错误文案必须「可行动」—— 用户看完知道下一步做什么。

## 判据（不是「文案好不好看」）

> **用户读完这句话，能否做出至少一个正确的动作？**

反例：「读取开奖数据失败」—— 用户不知道是自己操作错了、还是系统坏了、还是该等。
正例：「开奖数据暂时读取不到。稍后重试；若持续出现，请检查数据文件是否可读。」

## 与 D-21 的分界

| | 判据 | 修法 |
|---|---|---|
| **D-21**（说太多） | 泄漏了用户不该看的东西 | 内部名移到 `detail` |
| **D-20**（说太少） | 没告诉用户下一步能做什么 | 补「重试 / 检查什么 / 找谁」 |

两者修法不同，**不能混在一起修**，否则「D-21 完成」会被误读成「文案都好了」。
"""
from __future__ import annotations

import ast
import pathlib
import re

# 「可行动」的三种信号之一必须出现：
#   ① 重试类   —— 稍后重试 / 再试 / 刷新
#   ② 检查类   —— 检查 / 确认 / 请先 / 需要
#   ③ 归因类   —— 不是你的问题 / 不受影响 / 请反馈维护者
_ACTIONABLE = re.compile(
    # ① 重试类
    r"(稍后重试|再试|刷新|重试一次|会自动重试|重试)"
    # ② 检查/准备类
    r"|(检查|确认|请先|需要|启用|设置)"
    # ③ 归因/边界类 —— 告诉用户「这不是故障」或「这在预期内」
    r"|(不受影响|不是你的问题|反馈维护者|详见|不是可检验|无法给出|不会自动就绪|尚未有)"
)

# 允许的例外：参数错误类（用户能自行修正，A 类）
_PARAM_HINT = re.compile(r"(必须是|只支持|不能为空|恰好|互不相同)")


def _error_literals() -> list[tuple[str, int, str]]:
    out = []
    for py in sorted(pathlib.Path("lottery").glob("routes_*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "error"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    out.append((py.name, node.lineno, v.value))
    return out


def test_every_error_message_is_actionable():
    """每条 `error` 都必须给出至少一个可执行动作。

    ## 变异实证

    把任一 `error` 改回 `"读取开奖数据失败"` → 本测试必红。
    """
    bad = []
    for fname, lineno, text in _error_literals():
        t = text.strip()
        if _ACTIONABLE.search(t):
            continue
        if _PARAM_HINT.search(t):      # 参数错误：用户能自行修正，另算
            continue
        bad.append(f"  {fname}:{lineno}  {t[:60]}")
    assert not bad, (
        "以下 error 文案不可行动（用户读完不知道该做什么）：\n"
        + "\n".join(bad)
        + "\n\n补三选一：① 重试/刷新 ② 检查/确认什么 ③ 「不是你的问题 / 请反馈维护者」"
    )


def test_error_messages_do_not_end_with_bare_failure_word():
    """不得以「失败」「异常」「错误」单独结尾 —— 那是「说了等于没说」。

    例：「读取审计记录失败」→ 用户不知道下一步。
        改成「审计记录暂时读取不到。稍后重试；…」→ 有动作。
    """
    bad = []
    for fname, lineno, text in _error_literals():
        t = text.strip()
        if re.search(r"(失败|异常|错误|出错)$", t) and not _ACTIONABLE.search(t):
            bad.append(f"  {fname}:{lineno}  {t[:60]}")
    assert not bad, (
        "以下 error 以裸失败词结尾且无可行动指引：\n" + "\n".join(bad))


def test_empty_data_errors_explain_how_to_get_data():
    """「没有数据」类必须说明**怎么才有数据**，不能只说「暂无」。

    这是最高频的一类（本项目 24 处）——用户看到「本地暂无开奖数据」
    完全不知道是配置错了、还是该等、还是该去抓。
    """
    bad = []
    for fname, lineno, text in _error_literals():
        t = text.strip()
        if re.search(r"(暂无|没有|还没有).{0,6}(数据|开奖)", t):
            if not re.search(r"(抓取|导入|配置|详见|部署文档)", t):
                bad.append(f"  {fname}:{lineno}  {t[:60]}")
    assert not bad, (
        "「没有数据」类文案没说明怎么获取数据：\n" + "\n".join(bad))
