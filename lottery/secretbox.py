"""API Key 的对称加密（MU-2）。

## 为什么需要加密，而不是"文件权限 600 就够了"

MU-1 之前，API Key 存在 `data/llm_config.json`（0600 明文）。MU-2 把它搬进
`user_llm_config.api_key_encrypted`，而 DB 与 JSON 文件在**同一个目录、同一块盘**上，
单看磁盘权限并没有变好。真正变糟的是**暴露面**：

* `data/backups/ssq.db.pre-*` 是本项目的例行备份（每次迁移前都自动做一份），
  备份会被 `cp`、被 `/tmp` 拷贝、被 tar 打包带走 —— 明文 Key 就跟着走；
* 测试、迁移演练、故障排查都会复制库文件（本会话就复制过十几份）。

所以加密不是"锦上添花"，而是**把密钥搬进 DB 这个动作必须的补偿**：
库文件可以被复制、被备份、被误发，而主密钥不在库里。

## 主密钥从哪来

优先级：`LOTT_MASTER_KEY` 环境变量 → `data/.secret_key` 文件（自动生成，0600）。

为什么不强制只从 env 来：单机部署里"忘了配 env"会导致所有用户的 Key 写入失败，
那会让整个功能形同虚设。自动生成一份落在 data 目录是**务实的降级**，代价要写清楚：
**备份库文件时不要把 `.secret_key` 一起带走**，否则加密只防住了"只看 DB 的人"。

## 失败语义

* 解密失败（换过主密钥 / 密文损坏）→ 返回 `None` + 告警，**绝不抛异常**。
  理由：一条坏密钥不该让整站 500，更不能让用户"打不开设置页"。
  上层据此判定"该用户未配置 LLM"，走纯统计通道。
* 主密钥缺失且无法生成 → 抛 `SecretBoxUnavailable`，由调用方明确拒绝"写自备 Key"，
  **绝不静默存明文**（那会让加密变成一句空话）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import config

# 主密钥缓存（进程级）。None = 尚未解析；False = 解析失败
_MASTER_KEY: Optional[bytes] = None
_MASTER_KEY_FAILED = False

SECRET_KEY_FILENAME = ".secret_key"


class SecretBoxUnavailable(RuntimeError):
    """主密钥不可用：拒绝写明文，也拒绝假装加密。"""


def _secret_key_path() -> Path:
    return Path(config.DATA_DIR) / SECRET_KEY_FILENAME


def _generate_and_store() -> Optional[bytes]:
    """生成一份主密钥并落盘（0600）。失败返回 None。"""
    try:
        from cryptography.fernet import Fernet
    except ImportError:      # pragma: no cover —— 依赖缺失时给出可执行的提示
        raise SecretBoxUnavailable(
            "缺少 cryptography 依赖，无法加密保存 API Key。"
            "请 pip install -r requirements.txt（或改用系统共享配置）。")
    key = Fernet.generate_key()
    path = _secret_key_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先以 0600 创建，再写内容：避免"短暂存在一个 644 的密钥文件"的窗口
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        print(f"[secretbox] 已生成主密钥 {path}（0600）。"
              f"备份库文件时**不要**把它一起带走。")
        return key
    except OSError as e:
        print(f"[secretbox] 无法写入主密钥文件：{e}")
        return None


def master_key() -> bytes:
    """取主密钥。缺失时自动生成一份。**拿不到就抛异常，不返回明文通道。**"""
    global _MASTER_KEY, _MASTER_KEY_FAILED
    if _MASTER_KEY:
        return _MASTER_KEY
    if _MASTER_KEY_FAILED:
        raise SecretBoxUnavailable("主密钥不可用（此前已失败）")

    env = (os.environ.get("LOTT_MASTER_KEY") or "").strip()
    if env:
        _MASTER_KEY = env.encode("ascii", "ignore")
        return _MASTER_KEY

    path = _secret_key_path()
    if path.exists():
        try:
            data = path.read_bytes().strip()
            if data:
                _MASTER_KEY = data
                return _MASTER_KEY
        except OSError as e:
            print(f"[secretbox] 读取主密钥失败：{e}")

    key = _generate_and_store()
    if not key:
        _MASTER_KEY_FAILED = True
        raise SecretBoxUnavailable("主密钥缺失且无法生成（检查 data 目录权限）")
    _MASTER_KEY = key
    return key


def reset_cache() -> None:
    """测试用：清掉进程内缓存，让下一次调用重新解析主密钥。"""
    global _MASTER_KEY, _MASTER_KEY_FAILED
    _MASTER_KEY = None
    _MASTER_KEY_FAILED = False


def encrypt(plaintext: str) -> str:
    """加密一个字符串，返回 Fernet token（可直接存 TEXT 列）。

    空输入返回空串（= "没有 Key"），不加密空值 —— 让"未配置"在库里是 `''`
    而不是一个看不出含义的密文。
    """
    if not plaintext:
        return ""
    from cryptography.fernet import Fernet
    return Fernet(master_key()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: Optional[str]) -> Optional[str]:
    """解密。**任何失败都返回 None**（见模块 docstring 的失败语义）。"""
    if not token:
        return None
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        return None
    try:
        return Fernet(master_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except SecretBoxUnavailable:
        return None
    except InvalidToken:
        print("[secretbox] 解密失败：主密钥与密文不匹配（换过密钥或密文损坏）")
        return None
    except Exception as e:      # noqa: BLE001 —— 畸形 token 一律视为"没有 Key"
        print(f"[secretbox] 解密异常：{type(e).__name__}")
        return None


def mask(api_key: Optional[str]) -> str:
    """脱敏展示：`sk-****abcd`。

    长度 < 8 时**全部隐去**：短 Key 保留首尾会显著提高爆破命中率
    （U2 §2.3）。规则与 `user_llm_api._mask_key` 必须一致 —— 这里集中实现，
    另一处改为调用本函数，避免两条规则漂移。
    """
    if not api_key:
        return ""
    if len(api_key) < 8:
        return "****"
    return f"{api_key[:3]}****{api_key[-4:]}"
