"""全局配置：路径、多尺度窗口、LLM 通道（OpenAI 兼容，多模型可自定义）。

安全约定：仓库中不保存任何 LLM API URL / Key，全部通过环境变量注入。
配置方式见 .env.example（Docker Compose 自动读取 .env；本地运行请先 source）。
"""
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

BASE = Path(os.environ.get("LOTT_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR = BASE / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("LOTT_DB", str(DATA_DIR / "ssq.db")))

DATA_URL = "http://e.17500.cn/getData/ssq.TXT"
STARTED_AT = time.time()
# M4.5：配置后启用所有 /api/* 的 Bearer Token 校验；空值保持兼容开放。
API_TOKEN = (os.environ.get("LOTT_TOKEN") or "").strip() or None
# M4.4：可选备用源，需返回与 ssq.TXT 相同的 31 字段格式；空值则只运行主源。
BACKUP_DATA_URL = (os.environ.get("LOTT_BACKUP_DATA_URL") or "").strip() or None

# ---------- 版本信息（前端主页 / API / GitHub 说明统一引用） ----------

APP_VERSION = "2.5.0"          # v9：最新预测口径修复 + 诊断整注粘贴 + 规律门槛解释
APP_BUILD = "2026-10-V9"       # 构建标识（V9 交付批次）
APP_MILESTONES = {
    "M1": {"status": "done",    "desc": "前端 Tab 工作台 + 规律库扩容 29 条 + 自动挖掘管道 + 任务系统"},
    "M2": {"status": "done",    "desc": "GBDT/RF 概率模型 + 滚动 Brier 加权融合 + 概率校准 + 蓝球独立投票 + ML walk-forward 评估"},
    "M3": {"status": "done", "desc": "研究闭环：LLM 三轮辩论完善与离线评估、挖掘管道增强、规律研究台"},
    "M4": {"status": "in_progress", "desc": "长期运营：在线累积报表、方法 A/B 开关、通知、多源对账、运维工程"},
    "M5": {"status": "done", "desc": "交互升级：回放命中可视化、数据分析面板增强（走势/分布/遗漏/蓝球）、评估报告信息架构重组"},
    "U1": {"status": "done", "desc": "极简预测首页：7 Tab 改 2 层导航（本期推荐 + 更多抽屉），票卡默认折叠 + 匹配度星级"},
    "U2": {"status": "done", "desc": "蓝球专项：blue_specialist 专用模型 + 蓝球覆盖优先 + 运行命中率回退 + 复式/胆拖投注模式"},
    "U3": {"status": "done", "desc": "方法自适应降权 + LLM 角色重定位（组合结构优化，蓝球交由专用模型）"},
    "U4": {"status": "done", "desc": "组合命中覆盖优化：贪心 + 模拟退火的 coverage_r 红球 / 蓝球覆盖优化器"},
    "U5": {"status": "done", "desc": "单注复盘战绩卡 + 跟我买一键选注 + 中奖回执闭环"},
    "U6": {"status": "done", "desc": "推理型模型自动关闭思考兜底（reasoning_content 非空时注入 thinking:disabled 重试）"},
    "U7": {"status": "done", "desc": "LLM 实际生成注数模式：以大模型真实产出注数为准，不再用统计/ML 候选补齐"},
    "U8": {"status": "done", "desc": "推理型模型输出预算自适应（含预算记忆）+ 设置页模型唯一事实来源 + 观察轮报错聚合 + 上期开奖回执列出上期推荐逐注命中"},
    "U9": {"status": "done", "desc": "历史战绩口径修正：期数取接口实际去重样本（不再是用字面量 N / 各方法相加），样本不足不给最佳方法结论；卡片下移至「上期开奖回执」之后"},
    "V4": {"status": "done", "desc": "诚实口径改造：/api/rigor/randomness + /api/rigor/signal + /api/portfolio/expected-value 三个端点（缺 rigor 模块时优雅降级 503）；前端不再把随机波动当能力展示——所有比率必须带样本量与「样本不足，不构成结论」提示，新增「随机性检验」卡展示最小可检出提升量"},
    "V9": {"status": "done", "desc": "①修复「刷新后又显示旧预测」的根因：本地残留一条越界期号 2026401（2026-10-15 由已修复的 next_issue() 生成），它在文本与数值排序下都大于同年合法期号，导致 /api/predictions/last 永远返回它。改为按落库时间取候选 + 期号合法性过滤（_valid_issue），并清理脏数据；同时把库内 7 处 ORDER BY issue DESC 全部改为 CAST 数值排序。②「我的号码诊断」改为整注粘贴输入（01 06 12 21 24 31 + 12），不再分蓝/红两个框；解析支持带 + / 不带 + / 逗号 / 前导零。③规则分层语义修正：noise（各阶段未检出）不再是 rejected，改归 monitor——界面上「5 条不可用」实际是「证据不足」，与「已证伪」区分开；并把「为什么没有可用规律」写成可读原因（三道门槛 + 实测的重号率 bootstrap 置信区间）。"},
    "V8": {"status": "done", "desc": "阶段切分与体验修复：①阶段改为数据驱动的自适应切分（默认 block=150 期一段，历史约 23 段；可切回自然年 9 段对照），并对命中数指标改用配对差值 t 检验口径（此前误用二项比例，方差算错导致 p 失真）；实测 family-wise 假阳性 0.045、+0.15 红球/期效应检出力 0.65（自然年口径仅 0.31）②长耗时 AI 闭环/LLM 工坊改为后台任务（/api/rules/closed-loop/async、/api/llm_lab/run/async），前端每 3 秒轮询显示阶段+进度+已用时间，可离开页面 ③全站号码格式统一为 01 06 12 21 24 31 + 12（formatTicket/formatReds/parseRedsInput 单一来源），诊断卡回显统一格式并支持空格/逗号/顿号输入 ④预测/健康接口防前端缓存，服务启动后异步刷新规律快照 ⑤修复「更多→规律时效」入口抽屉未收起"},
    "V7": {"status": "done", "desc": "AI 规律闭环 + 数据对齐：①目标期号改为按真实开奖日历推算（每周二/四/日，BT.expected_issue），数据落后时明确报「缺几期/落后几天」，不再出现 2026401 这类越界期号；抓取改为全量对齐（补回漏抓期）。②本期推荐显示生成时间、数据基准、注数口径；/api/predictions/last 返回 generated_at/data_basis。③新增 lottery/patterns_runtime.py 规律运行时快照（data/patterns_runtime.json），把「阶段时效 + 维度扫描 + 假说库」压缩成低延迟可用清单，engine.predict_next 每期消费：可用规律=阶段持续有效且 A/B 验证通过，其余只监控/否决；默认 shadow 模式（记录影响不改号），LOTT_RULE_APPLY=1 生效。④票级「规律引用」由引擎按真实命中写入，不再由 LLM 自填。⑤/api/rules/{runtime,refresh,closed-loop} 与前端「本期规律使用情况」卡：挖掘→验证→注册→刷新快照→出号，一键闭环"},
    "V6": {"status": "done", "desc": "阶段有效性 + 多维度规律：新增 lottery/regime.py（9 阶段切分 + 逐阶段检验 + BH 校正 + 近 3 期加权 + 持续/新兴/衰减/陈旧/反转/伪规律状态机 + 近 150 期加权检验）、lottery/discovery.py（20 维度族 × 91 组参数的 as-of 快速筛选与阶段画像，单次扫描约数秒）、lottery/llm_lab.py（LLM 规律工坊：全历史阶段序列 + 维度表 + 搜索成本喂给模型，三轮提规律/自我质疑/修订，每条规律强制映射维度表并做 as-of 回放，映射不到判不可验证）；A/B 验证修复（两臂独立采样，结构类假说不再恒为 0 差异；对照组由 uniform 改为实际部署基线）；前端新增「规律时效」工作台（阶段热力图 + 维度扫描 + LLM 工坊 + 检索式核验）"},
    "V5": {"status": "done", "desc": "自进化系统：lottery/battle.py 方法级 walk-forward 战斗回放（红球 Wilcoxon + 蓝球 McNemar + Bonferroni/BH 校正）+ lottery/hypothesis.py LLM 规律提炼与假说库（LLM 失败必降级）+ lottery/evolution.py 进化闭环（假说→出号策略→A/B 实战验证→validated/rejected 裁决）；调度器每期开奖后自动跑一轮进化；前端「实战战绩」子页呈现战绩榜（ROI 双口径）、两两对比（未校正 N→校正 M）、规律库与进化操作"},
}

# ---------- LLM 通道（全部来自环境变量，无仓库内置密钥/地址） ----------

LLM_DISABLED = os.environ.get("LOTT_LLM_DISABLED", "0") == "1"

# 主通道：OpenAI 兼容端点
LLM_BASE_URL = (os.environ.get("LOTT_LLM_BASE_URL") or "").rstrip("/") or None
LLM_API_KEY = os.environ.get("LOTT_LLM_API_KEY") or None
LLM_MODEL = os.environ.get("LOTT_LLM_MODEL") or "minimax-m3"

# 多模型：同一主通道下使用的模型列表（逗号分隔），默认仅 LLM_MODEL
_models_env = [m.strip() for m in os.environ.get("LOTT_LLM_MODEL_LIST", "").split(",") if m.strip()]
LLM_MODEL_LIST: List[str] = _models_env or ([LLM_MODEL] if LLM_MODEL else [])

# 附加独立通道：JSON 数组，每个元素 {"name","base_url","api_key","model"}
LLM_EXTRA_MODELS: List[Dict] = []
try:
    raw = os.environ.get("LOTT_LLM_EXTRA_MODELS", "")
    if raw.strip():
        LLM_EXTRA_MODELS = json.loads(raw)
        if not isinstance(LLM_EXTRA_MODELS, list):
            LLM_EXTRA_MODELS = []
except (json.JSONDecodeError, TypeError):
    LLM_EXTRA_MODELS = []

LLM_SAMPLES = int(os.environ.get("LOTT_LLM_SAMPLES", "3"))          # LLM 多轮采样次数（并发）
TICKETS_PER_LLM_CALL = int(os.environ.get("LOTT_TICKETS_PER_CALL", "5"))
N_TICKETS = int(os.environ.get("LOTT_N_TICKETS", "10"))             # 最终输出注数
# LLM_ONLY_OUTPUT（默认 1）：LLM 通道产出候选时，最终注数以 LLM **实际返回**的注数
# 为准，不再用统计/ML 候选补齐到 N_TICKETS。避免“宣称 10 注、实际仅 4 注来自
# LLM”的口径不一致。设 0 恢复旧的“候选池混选 + 补齐到 N_TICKETS”行为。
LLM_ONLY_OUTPUT = os.environ.get("LOTT_LLM_ONLY_OUTPUT", "1") == "1"
LLM_TIMEOUT = float(os.environ.get("LOTT_LLM_TIMEOUT", "60"))
# 大输出预算（推理型模型）单次调用的宽松超时：预算抬到上限后单次要跑 40~120s，
# 仍用 60s 会先被判读取超时而白白重试一次。
LLM_LONG_TIMEOUT = float(os.environ.get("LOTT_LLM_LONG_TIMEOUT", "180"))
# 推理型模型（如 deepseek-v4-flash / glm-5.3 等）会把 max_tokens 全部耗在
# reasoning_content 上，content 为空且 finish_reason=length。此时按需把输出预算
# 抬到本上限再重试一次（两次都能被 deadline 钳制）。
# 默认 32000：实测 agent-router/deepseek-v4-flash 需要 ≥3 万输出预算才吐出正文。
LLM_MAX_TOKENS = int(os.environ.get("LOTT_LLM_MAX_TOKENS", "32000"))
# 学到的「每个模型可用输出预算」持久化文件（首次探测成功后记住，避免每次预测
# 都从被截断的小预算重试）。键为 "<base_url>|<model>"。
LLM_BUDGET_FILE = DATA_DIR / "llm_budget.json"
# M4.5 快速失败：单次预测 LLM 阶段（观察+选号+校验全部轮次）的墙钟预算（秒）。
# 默认 75s：加统计部分约 5s 后仍在 Cloudflare 100s 代理超时之内，避免 HTTP 524。
# 超预算即判定“大模型超时”，Web 预测直接失败提示，不再降级为纯统计。
LLM_TOTAL_TIMEOUT = float(os.environ.get("LOTT_LLM_TOTAL_TIMEOUT", "75"))

# M4.5：请求级附加参数（合并进每个 chat/completions 请求体）。
# 背景：部分网关的推理型模型会把 max_tokens 全部耗在 reasoning 上（content 为空），
# 需要按模型下发 reasoning_effort / enable_thinking 等开关控制思考行为。
# LOTT_LLM_EXTRA_BODY：JSON 对象，对所有模型生效（如 {"reasoning_effort":"low"}）。
# LOTT_LLM_EXTRA_BODY_MAP：JSON 对象（模型名 → 参数对象），叠加在默认之上，按模型覆盖。
LLM_EXTRA_BODY_DEFAULT: Dict = {}
LLM_EXTRA_BODY_BY_MODEL: Dict[str, Dict] = {}


def _load_extra_body_env() -> None:
    global LLM_EXTRA_BODY_DEFAULT, LLM_EXTRA_BODY_BY_MODEL
    raw = (os.environ.get("LOTT_LLM_EXTRA_BODY") or "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                LLM_EXTRA_BODY_DEFAULT = obj
            else:
                print("[config] 警告：LOTT_LLM_EXTRA_BODY 必须是 JSON 对象，已忽略")
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[config] 警告：LOTT_LLM_EXTRA_BODY 解析失败（{e}），已忽略")
    raw = (os.environ.get("LOTT_LLM_EXTRA_BODY_MAP") or "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                cleaned = {str(k): v for k, v in obj.items() if isinstance(v, dict)}
                bad = len(obj) - len(cleaned)
                if bad:
                    print(f"[config] 警告：LOTT_LLM_EXTRA_BODY_MAP 有 {bad} 个非对象值已忽略")
                LLM_EXTRA_BODY_BY_MODEL = cleaned
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[config] 警告：LOTT_LLM_EXTRA_BODY_MAP 解析失败（{e}），已忽略")


_load_extra_body_env()

# ---------- M4.2 方法 A/B 开关 ----------
# 运营筛查阈值：60 期开始提示，连续 120 期无显著差异才允许人工考虑关闭。
METHOD_RECOMMENDATION_MIN_ISSUES = int(os.environ.get("LOTT_METHOD_RECOMMENDATION_MIN_ISSUES", "60"))
METHOD_RECOMMENDATION_DISABLE_ISSUES = int(os.environ.get("LOTT_METHOD_RECOMMENDATION_DISABLE_ISSUES", "120"))
METHOD_RECOMMENDATION_ALPHA = float(os.environ.get("LOTT_METHOD_RECOMMENDATION_ALPHA", "0.05"))

# ---------- V7 规律 → 出号 ----------
# 规律命中一个号码给该注加多少「置信度分」。刻意温和（默认 1.0/个）：
# 规律只能在候选池内部重排，不能凌驾于概率模型之上。
# 尺度依据（实测）：候选池内置信度区间只有约 20~35 分、极差 3~6 分，
# 所以 1 个命中（+1.0）足以在池内挪动一两位，但打不过「整体高 4 分以上」的注——
# 这正是"温和偏置"想要的力度。调大（如 3.0）会让规律压过概率模型，不建议。
RULE_CONF_BOOST = float(os.environ.get("LOTT_RULE_CONF_BOOST", "1.0"))
# LOTT_METHODS（逗号/空格分隔，未设置 = 全部启用）：可关闭/保留任意方法通道
# （stat 各基线 / ML / LLM / uniform），令牌可写全名（stat:freq）或族名（stat）。
# LOTT_METHOD_MODE：production（默认，严格按开关过滤）/ research（忽略开关、全部启用，
# 供方法对比实验；与决策规则「未经 120 期 paired 验证的方法仅以研究模式存在」对应）。
from . import methods as _methods

METHODS_RAW = (os.environ.get("LOTT_METHODS") or "").strip()          # 原始开关字符串
METHOD_MODE = _methods.normalize_mode(os.environ.get("LOTT_METHOD_MODE"))  # production / research
METHODS_SPEC = _methods.implement_spec(METHODS_RAW)                   # 解析规格，供引擎过滤候选

# 运行时方法配置持久化（Web 设置页写入，优先于 .env，重启不丢失）
METHODS_CONFIG_FILE = DATA_DIR / "methods_config.json"

# ---------- U2 蓝球专项 ----------
# LOTT_BLUE_MODE：model（默认，用 blue_specialist 等模型）/ uniform（整期回退 16 个均匀随机）
BLUE_MODE = (os.environ.get("LOTT_BLUE_MODE") or "model").strip().lower()
if BLUE_MODE not in ("model", "uniform"):
    BLUE_MODE = "model"
# LOTT_BLUE_COVER：picked 阶段是否按“蓝球去重覆盖优先”（1=是，默认；0=沿用旧的同蓝上限）
BLUE_COVER = os.environ.get("LOTT_BLUE_COVER", "1") == "1"
# 蓝球模型运行命中率回退阈值：近 BLUE_FALLBACK_WINDOW 期命中率低于该值 → 该期回退 uniform
BLUE_FALLBACK_WINDOW = int(os.environ.get("LOTT_BLUE_FALLBACK_WINDOW", "30"))
BLUE_FALLBACK_MIN_RATE = float(os.environ.get("LOTT_BLUE_FALLBACK_MIN_RATE", "0.045"))
# V4-C 修复：回退判定必须达到的最小样本量（期）。30 期样本的命中率标准误为
# sqrt(0.0625*0.9375/30) ≈ 4.42%，远大于 6.25%-4.5%=1.75% 的差额——在这个样本量
# 上做回退决策等于在噪声上决策。0（默认）= 由 engine._min_significant_blue_sample()
# 用 1.96*sd/delta 反推（≈735 期）；显式设正值则用它，但仍不会低于 BLUE_FALLBACK_WINDOW。
BLUE_FALLBACK_MIN_SAMPLE = int(os.environ.get("LOTT_BLUE_FALLBACK_MIN_SAMPLE", "0"))
# 随机蓝球命中理论基线（1/16）
BLUE_RANDOM_RATE = 1.0 / 16

# ---------- U2 投注模式（single / blue_compound / dan_tuo） ----------
BET_MODE = (os.environ.get("LOTT_BET_MODE") or "single").strip().lower()
if BET_MODE not in ("single", "blue_compound", "dan_tuo"):
    BET_MODE = "single"
# blue_compound：红 6 固定 + 蓝 k 个 → k 注（蓝球复式展开）
BLUE_COMPOUND_K = int(os.environ.get("LOTT_BLUE_COMPOUND_K", "5"))
# dan_tuo：红胆 n 个固定 + 红拖补齐 6 红 + 蓝 1 个 → 展开多注
DAN_TUO_DANS = int(os.environ.get("LOTT_DAN_TUO_DANS", "4"))
DAN_TUO_TUOS = int(os.environ.get("LOTT_DAN_TUO_TUOS", "5"))

# ---------- U3 方法自适应降权 ----------
# LOTT_ADAPTIVE_ENABLED：1=开（默认），0=关（保留人工 A/B 开关语义）
ADAPTIVE_ENABLED = os.environ.get("LOTT_ADAPTIVE_ENABLED", "1") == "1"
# 连续 LOTT_ADAPTIVE_K 期命中低于随机基线 → 自动降权 50%（0.5）或移出候选池
ADAPTIVE_K = int(os.environ.get("LOTT_ADAPTIVE_K", "20"))
ADAPTIVE_WINDOW = int(os.environ.get("LOTT_ADAPTIVE_WINDOW", "60"))
ADAPTIVE_DOWNWEIGHT_FACTOR = float(os.environ.get("LOTT_ADAPTIVE_DOWNWEIGHT_FACTOR", "0.5"))
# 命中比较口径：red（红球平均命中）/ blue（蓝球命中率）/ prize（≥五等奖率）
ADAPTIVE_METRIC = (os.environ.get("LOTT_ADAPTIVE_METRIC") or "red").strip().lower()
# 自适应状态持久化文件（不修改 SQLite schema）
ADAPTIVE_STATE_FILE = DATA_DIR / "adaptive_state.json"

# ---------- U4 组合覆盖优化 ----------
# coverage 模式：目标“至少命中 r 个红球”，蓝球覆盖数，退火迭代上限
COVERAGE_R = int(os.environ.get("LOTT_COVERAGE_R", "3"))
COVERAGE_TRIALS = int(os.environ.get("LOTT_COVERAGE_TRIALS", "3000"))
COVERAGE_BLUE_K = int(os.environ.get("LOTT_COVERAGE_BLUE_K", "4"))
# V4-C 覆盖优化目标函数：
#   expected_value（默认）= 奖金期望 + 蓝球覆盖奖金（真实可优化的口径）
#   coverage              = 旧口径（红球并集大小 + 蓝球覆盖数），向后兼容
COVERAGE_OBJECTIVE = (os.environ.get("LOTT_COVERAGE_OBJECTIVE") or "expected_value").strip().lower()
if COVERAGE_OBJECTIVE not in ("coverage", "expected_value"):
    COVERAGE_OBJECTIVE = "expected_value"


def methods_status() -> Dict:
    """当前方法开关状态：模式 / 原始字符串 / 解析规格（供 API 与前端展示）。"""
    return {
        "mode": METHOD_MODE,
        "raw": METHODS_RAW,
        "spec": {
            "mode": METHODS_SPEC.get("mode", "all"),
            "tokens": sorted(METHODS_SPEC.get("tokens", set())),
        },
    }


def set_methods(raw: Optional[str] = None, mode: Optional[str] = None) -> Dict:
    """运行时更新方法开关（Web 设置页写入，立即生效并持久化）。

    raw: LOTT_METHODS 字符串（None = 不变）；mode: production / research（None = 不变）。
    同时更新模块全局与 os.environ，供引擎“后续预测”即时读取。
    """
    global METHODS_RAW, METHOD_MODE, METHODS_SPEC
    if raw is not None:
        METHODS_RAW = str(raw or "").strip()
        err = _methods.validate_raw(METHODS_RAW)
        if err:
            raise ValueError(err)
        os.environ["LOTT_METHODS"] = METHODS_RAW
    if mode is not None:
        METHOD_MODE = _methods.normalize_mode(mode)
        os.environ["LOTT_METHOD_MODE"] = METHOD_MODE
    METHODS_SPEC = _methods.implement_spec(METHODS_RAW)
    if not _save_methods_config():
        raise OSError("方法配置持久化失败")
    print(f"[config] 方法开关已更新: mode={METHOD_MODE}, raw='{METHODS_RAW}'")
    return methods_status()


def _load_runtime_methods_config() -> None:
    """启动时读取 data/methods_config.json（若存在），覆盖方法开关配置。"""
    global METHODS_RAW, METHOD_MODE, METHODS_SPEC
    try:
        if not METHODS_CONFIG_FILE.exists():
            return
        conf = json.loads(METHODS_CONFIG_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        print("[config] 警告：methods_config.json 解析失败，忽略运行时方法配置")
        return
    changed = False
    if "raw" in conf and isinstance(conf["raw"], str):
        candidate = conf["raw"].strip()
        if _methods.validate_raw(candidate):
            print("[config] 警告：methods_config.json 含非法 methods，忽略运行时配置")
            return
        METHODS_RAW = candidate
        os.environ["LOTT_METHODS"] = METHODS_RAW
        changed = True
    if "mode" in conf and isinstance(conf["mode"], str):
        METHOD_MODE = _methods.normalize_mode(conf["mode"])
        os.environ["LOTT_METHOD_MODE"] = METHOD_MODE
        changed = True
    if changed:
        METHODS_SPEC = _methods.implement_spec(METHODS_RAW)
        print(f"[config] 已加载运行时方法配置（mode={METHOD_MODE}, raw='{METHODS_RAW}'）")


def _save_methods_config() -> None:
    """把当前方法开关写入 data/methods_config.json（与 llm_config.json 同目录）。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"mode": METHOD_MODE, "raw": METHODS_RAW}, ensure_ascii=False, indent=2)
        tmp = METHODS_CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(METHODS_CONFIG_FILE))
    except OSError as e:
        print(f"[config] 方法配置写入失败: {e}")
        return False
    return True

# ---------- M3 LLM 离线评估与研究专用配置 ----------

LLM_EVAL_ISSUES = int(os.environ.get("LOTT_LLM_EVAL_ISSUES", "60"))     # 三通道 walk-forward 窗口（期）
LLM_EVAL_TICKETS = int(os.environ.get("LOTT_LLM_EVAL_TICKETS", "5"))    # 每期每通道注数
LLM_EVAL_SEED = int(os.environ.get("LOTT_LLM_EVAL_SEED", "42"))         # 随机基线种子（stat/random 可复现）
LLM_EVAL_MODEL = os.environ.get("LOTT_LLM_EVAL_MODEL") or None          # 评估专用模型（可选，轻量优先，控成本）
LLM_EVAL_PRICE_PER_1M = float(os.environ.get("LOTT_LLM_EVAL_PRICE_PER_1M", "1.0"))  # 每百万 token 估算价（USD，仅展示）
LLM_VERIFY_MODEL = os.environ.get("LOTT_LLM_VERIFY_MODEL") or None      # 第三轮校验模型（M3.2 接入）
LLM_VERIFY_ENABLED = os.environ.get("LOTT_LLM_VERIFY", "1") == "1"      # 第三轮校验开关（生产默认开；离线评估按通道可控）


def llm_configured() -> bool:
    """是否存在任何可用的 LLM 通道（主通道或附加通道）。"""
    if LLM_DISABLED:
        return False
    if LLM_BASE_URL and LLM_API_KEY and LLM_MODEL_LIST:
        return True
    return bool(LLM_EXTRA_MODELS)


def llm_model_list() -> List[Dict]:
    """返回全部可用模型配置（主通道模型列表 + 附加通道），供引擎多模型采样。"""
    cfgs: List[Dict] = []
    if not LLM_DISABLED and LLM_BASE_URL and LLM_API_KEY:
        for m in LLM_MODEL_LIST:
            cfgs.append({
                "name": m, "base_url": LLM_BASE_URL, "api_key": LLM_API_KEY, "model": m,
            })
    for extra in LLM_EXTRA_MODELS:
        name = extra.get("name") or extra.get("model", "extra")
        if extra.get("base_url") and extra.get("api_key") and extra.get("model"):
            cfgs.append({
                "name": name, "base_url": str(extra["base_url"]).rstrip("/"),
                "api_key": str(extra["api_key"]), "model": str(extra["model"]),
            })
    return cfgs

# 多尺度窗口（0 表示全量；其余为最近 N 期）
WINDOWS = {"long": 0, "mid": 150, "short": 30}

# 回测参数
BACKTEST_MIN_N = int(os.environ.get("LOTT_BT_MIN_N", "30"))   # 最少触发样本量
BACKTEST_HORIZON = int(os.environ.get("LOTT_BT_HORIZON", "1"))  # 结果检验窗口（期）

# 离线评估
OFFLINE_EVAL_ISSUES = int(os.environ.get("LOTT_OFFLINE_EVAL_ISSUES", "120"))

# ---------- M2 ML 概率模型（GBDT + 随机森林 + 校准） ----------

ML_ENABLED = os.environ.get("LOTT_ML_ENABLED", "1") == "1"          # 是否把 ML 概率接入集成融合
ML_MIN_START = int(os.environ.get("LOTT_ML_MIN_START", "300"))      # 最少历史期数才开始训练
ML_REFIT_EVERY = int(os.environ.get("LOTT_ML_REFIT_EVERY", "10"))   # 滚动评估时每 N 期重训一次
ML_EVAL_WINDOW = int(os.environ.get("LOTT_ML_EVAL_WINDOW", "60"))   # ML 滚动评估窗口（期）
ML_N_ESTIMATORS = int(os.environ.get("LOTT_ML_N_ESTIMATORS", "60")) # 树数量（RF 与 GBDT 通用上限）
ML_MAX_DEPTH = int(os.environ.get("LOTT_ML_MAX_DEPTH", "5"))        # 树深度
ML_CAL_CV = int(os.environ.get("LOTT_ML_CAL_CV", "2"))              # 概率校准折叠数（小=快）

# 调度（开奖日：周二/四/日 21:35 后自动 抓取+评估+预测）
SCHEDULER_ENABLED = os.environ.get("LOTT_SCHEDULER", "0") == "1"
DRAW_WEEKDAYS = (1, 3, 6)  # 周一=0 … 周日=6 -> 周二/四/日
DRAW_TIME = "21:35"

# ---------- M4.3 开奖通知 ----------
NOTIFY_WEBHOOK = (os.environ.get("LOTT_NOTIFY_WEBHOOK") or "").strip()
NOTIFY_SMTP_HOST = (os.environ.get("LOTT_NOTIFY_EMAIL_SMTP_HOST") or "").strip()
NOTIFY_SMTP_PORT = int(os.environ.get("LOTT_NOTIFY_EMAIL_SMTP_PORT", "587"))
NOTIFY_SMTP_TLS = os.environ.get("LOTT_NOTIFY_EMAIL_SMTP_TLS", "1") == "1"
NOTIFY_SMTP_USER = (os.environ.get("LOTT_NOTIFY_EMAIL_SMTP_USER") or "").strip()
NOTIFY_SMTP_PASSWORD = os.environ.get("LOTT_NOTIFY_EMAIL_SMTP_PASSWORD") or ""
NOTIFY_EMAIL_FROM = (os.environ.get("LOTT_NOTIFY_EMAIL_FROM") or "").strip()
NOTIFY_EMAIL_TO = (os.environ.get("LOTT_NOTIFY_EMAIL_TO") or "").strip()


# ---------- 运行时 LLM 配置持久化（Web 界面写入，优先于 .env） ----------

LLM_CONFIG_FILE = DATA_DIR / "llm_config.json"


def load_runtime_llm_config() -> None:
    """启动时读取 data/llm_config.json（若存在），覆盖 LLM 通道配置。"""
    global LLM_DISABLED, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_MODEL_LIST, LLM_SAMPLES
    global LLM_VERIFY_ENABLED, LLM_TOTAL_TIMEOUT
    global LLM_EXTRA_BODY_DEFAULT, LLM_EXTRA_BODY_BY_MODEL
    if not LLM_CONFIG_FILE.exists():
        return
    try:
        with open(LLM_CONFIG_FILE, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except (json.JSONDecodeError, OSError):
        print("[config] 警告：llm_config.json 解析失败，忽略运行时配置")
        return
    if "disabled" in conf:
        LLM_DISABLED = bool(conf["disabled"])
    if conf.get("base_url"):
        LLM_BASE_URL = str(conf["base_url"]).rstrip("/") or None
    if conf.get("api_key"):
        LLM_API_KEY = str(conf["api_key"])
    if conf.get("model"):
        LLM_MODEL = str(conf["model"])
        # U6 修复：设置页保存的模型是唯一事实来源，**替换**（而非追加）模型列表，
        # 只保留这一个有效模型，避免旧通道的 glm/qwen（magpie 上 404）混入多模型轮转。
        LLM_MODEL_LIST = [LLM_MODEL]
    if isinstance(conf.get("samples"), int) and conf["samples"] > 0:
        LLM_SAMPLES = int(conf["samples"])
    if isinstance(conf.get("verify"), bool):
        LLM_VERIFY_ENABLED = bool(conf["verify"])
    if isinstance(conf.get("total_timeout"), (int, float)) and conf["total_timeout"] > 0:
        LLM_TOTAL_TIMEOUT = float(conf["total_timeout"])
    # U6 修复：设置页通道可自带 extra_body（如关闭思考），避免推理型模型
    # 因 reasoning_content 占满预算而触发“放大重试”导致总耗时超限。
    if isinstance(conf.get("extra_body"), dict) and conf["extra_body"]:
        LLM_EXTRA_BODY_DEFAULT = dict(conf["extra_body"])
        if LLM_MODEL:
            LLM_EXTRA_BODY_BY_MODEL = dict(LLM_EXTRA_BODY_BY_MODEL)
            LLM_EXTRA_BODY_BY_MODEL[LLM_MODEL] = dict(conf["extra_body"])
    print(f"[config] 已加载运行时 LLM 配置（{LLM_CONFIG_FILE.name}）")

DATA_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

# 加载运行时配置（须在 DATA_DIR 创建后）
load_runtime_llm_config()
_load_runtime_methods_config()
