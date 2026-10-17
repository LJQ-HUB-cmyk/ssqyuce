"""V10-00a 契约冻结测试：``resolve_channels()`` 唯一判据 + 开关收敛。

规格来源：``docs/V10_IMPROVEMENT_PLAN.md`` §2.1.2（v2.1），
修复依据：``docs/reviews/R1_ARCHITECTURE_REVIEW.md`` P0-1（第 7 套开关）、
``docs/reviews/S2_PLAN_FEASIBILITY_REVIEW.md`` B3（source 层无法判定）。

测试表 T1–T10 与方案 §2.1.2 一一对应。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def clean_methods(tmp_path, monkeypatch):
    """构造一个**干净**的方法开关环境（临时 LOTT_HOME，无 methods_config.json）。

    T1/T3 必须用它的原因（S2 B3）：生产机可能已被设置页写过
    ``data/methods_config.json``，若不隔离，``source`` 会命中文件层，
    导致"什么都不设 → default"的断言在别的机器上必红。
    """
    from lottery import config, methods

    cfg_file = tmp_path / "methods_config.json"
    monkeypatch.setattr(config, "METHODS_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(methods, "_methods_config_file", lambda: cfg_file)
    monkeypatch.delenv("LOTT_METHODS", raising=False)
    monkeypatch.delenv("LOTT_METHOD_MODE", raising=False)
    monkeypatch.delenv("LOTT_LLM_ONLY_OUTPUT", raising=False)
    methods._WARNED.clear()
    return cfg_file


def test_t1_defaults_when_nothing_set(clean_methods):
    """T1：什么都不设 → source=default，且默认是 allow-list stat,ml,uniform。

    ⚠️ ``blend`` 必须断言为 **False**：代码默认是 allow-list ``"stat,ml,uniform"``，
    ``blend`` 不在其中（``methods.FAMILIES`` 里确实有 blend 族，但 allow-list
    语义是"只启用列出的"）。若断言 blend=True 会与默认值互斥、必红。
    """
    from lottery import methods

    ch = methods.resolve_channels()
    assert ch["source"] == "default"
    assert ch["spec_raw"] == "stat,ml,uniform"
    assert ch["families"] == {
        "stat": True, "blend": False, "ml": True, "uniform": True, "llm": False,
    }
    assert ch["llm_output_mode"] == "off"
    assert ch["llm_can_pick"] is False
    assert ch["mode"] == "production"


def test_t2_env_layer_is_detected(clean_methods, monkeypatch):
    """T2：设置 .env 变量 → source=.env（用 `in os.environ`，不是 .get）。"""
    from lottery import methods

    monkeypatch.setenv("LOTT_METHODS", "stat,ml")
    assert methods.resolve_layer() == ".env"
    ch = methods.resolve_channels()
    assert ch["source"] == ".env"
    assert ch["spec_raw"] == "stat,ml"
    assert ch["families"]["uniform"] is False


def test_t3_json_layer_beats_env(clean_methods, monkeypatch):
    """T3：methods_config.json 存在 → 优先于 .env（优先级链第一层）。"""
    from lottery import methods

    monkeypatch.setenv("LOTT_METHODS", "stat,ml")
    clean_methods.write_text(
        json.dumps({"raw": "stat,ml,uniform", "mode": "production"}), encoding="utf-8"
    )
    assert methods.resolve_layer() == "methods_config.json"
    ch = methods.resolve_channels()
    assert ch["source"] == "methods_config.json"
    assert ch["spec_raw"] == "stat,ml,uniform"
    assert ch["families"]["uniform"] is True


def test_t4_source_not_confused_by_env_writeback(clean_methods, monkeypatch):
    """T4（S2 B3 核心）：json 的生效值被 config 写回 os.environ 后，source 仍须是 json。

    这正是 `os.environ.get(...)` 判定的致命缺陷：写回之后 .get 看得到值，
    但那个值**来自 json 而非 .env**。必须用"查文件"判定。
    """
    from lottery import methods

    clean_methods.write_text(json.dumps({"raw": "-llm"}), encoding="utf-8")
    # 模拟 config._load_runtime_methods_config 把生效值写回环境变量
    monkeypatch.setenv("LOTT_METHODS", "-llm")

    assert methods.resolve_layer() == "methods_config.json", \
        "写回 os.environ 后仍必须识别为文件层（否则 source 判据失效）"


def test_t5_llm_output_mode_legacy_alias(clean_methods, monkeypatch):
    """T5：旧开关 LOTT_LLM_ONLY_OUTPUT 作为别名仍可用（1→only / 0→off）。"""
    from lottery import methods

    monkeypatch.setenv("LOTT_METHODS", "llm")        # 打开 llm 族
    monkeypatch.setenv("LOTT_LLM_ONLY_OUTPUT", "1")
    ch = methods.resolve_channels()
    assert ch["llm_output_mode"] == "only"

    monkeypatch.setenv("LOTT_LLM_ONLY_OUTPUT", "0")
    ch = methods.resolve_channels()
    assert ch["llm_output_mode"] == "off"


def test_t6_deny_list_beats_llm_output_enabled(clean_methods, monkeypatch):
    """T6：拒绝列表优先——`-llm` 时即使 LOTT_LLM_ONLY_OUTPUT=1，出号模式也强制 off。"""
    from lottery import methods

    monkeypatch.setenv("LOTT_METHODS", "-llm")
    monkeypatch.setenv("LOTT_LLM_ONLY_OUTPUT", "1")
    ch = methods.resolve_channels()
    assert ch["families"]["llm"] is False
    assert ch["llm_output_mode"] == "off", "族被关闭时出号模式必须强制 off"
    assert ch["llm_can_pick"] is False


def test_t7_llm_can_pick_requires_production(clean_methods, monkeypatch):
    """T7：research 模式下 llm_can_pick 恒 False（研究模式不参与生产出号）。"""
    from lottery import methods

    monkeypatch.setenv("LOTT_METHODS", "llm")
    monkeypatch.setenv("LOTT_LLM_ONLY_OUTPUT", "1")
    monkeypatch.setenv("LOTT_METHOD_MODE", "research")
    ch = methods.resolve_channels()
    assert ch["mode"] == "research"
    assert ch["llm_can_pick"] is False
    assert ch["research_arm"]["llm_probe"] is True   # 研究臂里可以探测

    prod = methods.resolve_channels(force_production=True)
    assert prod["mode"] == "production"


def test_t8_fingerprint_is_stable_and_sensitive(clean_methods, monkeypatch):
    """T8：fingerprint 确定性（同配置同值），且对配置变化敏感。"""
    from lottery import methods

    a = methods.resolve_channels()["fingerprint"]
    b = methods.resolve_channels()["fingerprint"]
    assert a == b and len(a) == 16

    monkeypatch.setenv("LOTT_METHODS", "stat")
    c = methods.resolve_channels()["fingerprint"]
    assert c != a, "配置变化后 fingerprint 必须变化（否则无法审计哪套管开关出的号）"


def test_t9_no_module_reads_switches_directly():
    """T9：静态闸门——除 methods.py / config.py 外不得直读开关环境变量。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "lottery"
    allowed = {"methods.py", "config.py"}
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name in allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "LOTT_METHODS" in line or "LOTT_LLM_ONLY_OUTPUT" in line:
                # 注释与文档字符串提及可以接受，实际读取不行
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                if 'os.environ' in line or 'getenv' in line:
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert not offenders, "存在绕过 resolve_channels() 直读开关的位置：\n" + "\n".join(offenders)


def test_t10_coverage_objective_alias_keeps_legacy_value():
    """T10：COVERAGE_OBJECTIVE 值域变更后，旧值 expected_value 仍映射到新名。"""
    from lottery import config

    aliases = config.COVERAGE_OBJECTIVE_ALIASES
    assert aliases["expected_value"] == "ev_conserving"
    assert aliases["coverage"] == "coverage"
    # 当前生效值必须是合法目标之一
    assert config.COVERAGE_OBJECTIVE in {"ev_conserving", "coverage", "pmax_high", "custom_weight"}


def test_channels_snapshot_matches_resolve(clean_methods):
    """附加：config.CHANNELS 快照与 resolve_channels() 一致，且 refresh 可刷新。"""
    from lottery import config, methods

    fresh = methods.resolve_channels()
    assert config.CHANNELS["fingerprint"] == fresh["fingerprint"]
    refreshed = config.refresh_channels()
    assert refreshed["fingerprint"] == fresh["fingerprint"]
