#!/usr/bin/env bash
# isolated_test.sh —— 一键隔离：在临时库副本上跑命令，绝不碰生产数据。
#
# 为什么有这个脚本（2026-10-21 事故留档）：
#   李工首次做 CSRF 实测时把 `LOTT_DATA_DIR` 当成数据目录变量（实际是
#   `LOTT_HOME` / `LOTT_DB`），脚本直接把测试账号写进了生产库
#   `ssqyuce/data/ssq.db`，造出 user_id=5 / tester1 及 2 条会话、2 条审计。
#   靠「记得改路径」不可靠 —— 靠工具的默认值才可靠。
#
# 用法：
#   scripts/isolated_test.sh -- <shell 命令或 python 脚本>
#   scripts/isolated_test.sh -c '<python 代码>'
#   scripts/isolated_test.sh --keep -- -c '<python 代码>'   # 保留临时目录便于事后检查
#
# 例：
#   scripts/isolated_test.sh -c 'from lottery import auth; print(auth.SESSION_TTL_SECONDS)'
#   scripts/isolated_test.sh -- .venv/bin/python -m pytest tests/test_mu1_auth.py -q
#
# 保证：
#   1. 数据库一律指向临时副本（cp 生产库），生产库只读打开一次用于复制；
#   2. LOTT_HOME 指向空的临时目录，避免把 config 派生的 json 写进 repo；
#   3. 临时目录在命令结束后删除（--keep 除外），失败也删；
#   4. 退出码原样透传，CI 可直接用；
#   5. 仓库外脚本也能 import lottery（脚本已把仓库根前置进 PYTHONPATH）：
#        scripts/isolated_test.sh -- .venv/bin/python /tmp/my_probe.py
# 为什么**故意不加 -e**（2026-10-21 补注，别误以为漏写）：
#   本脚本的职责是「替调用者搭好隔离环境，然后原样跑他的命令、原样返回他的退出码」。
#   若加 -e，任意一步（比如 `cp` 因磁盘满失败）都会让 shell 提前退出，
#   而 `trap cleanup EXIT` 里的 `rc=$?` 拿到的是失败那步的码，不是调用者的码，
#   退出码语义就被改变了 —— 对 CI 而言「我的测试失败了」和「隔离环境没准备好」
#   会混成同一件事，没法区分。
#   不加 -e 也不会掩盖失败：下面两处都显式判了 rc 并 return/exit。
#   真正必须失败即停的地方，脚本里用 `|| exit 1` 逐个点名（比全局 -e 更精确）。
#
# 但**必须**保留 -u 与 pipefail：变量拼错（如 $LOTT_DB 拼成 $LOTT_DB_）不加 -u
# 会静默变成空串，进而可能把库路径退化成相对路径 —— 这正是要防的事故类型。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
PROD_DB="$REPO_ROOT/data/ssq.db"

KEEP=0
if [[ "${1:-}" == "--keep" ]]; then KEEP=1; shift; fi

if [[ "${1:-}" != "--" && "${1:-}" != "-c" ]]; then
  cat >&2 <<'USAGE'
用法:
  scripts/isolated_test.sh -- <命令...>        # 直接执行命令
  scripts/isolated_test.sh -c '<python 代码>'  # 用 venv python 执行代码
  scripts/isolated_test.sh --keep -- ...       # 保留临时目录
USAGE
  exit 2
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/lott-isolated.XXXXXX")"

# ---------------------------------------------------------------------------
# 生产库写入护栏（2026-10-21 新增）
#
# 为什么不能只看 sha256：本库跑在 WAL 模式，写入先落 ssq.db-wal，主库文件
# 字节不变。实测：向生产库写入 1 行后，ssq.db 的 sha256 仍是 70207783f9bf7667
# （未变），只有 ssq.db-wal 变了。所以 sha256 一致性**不能**证明"没写库"。
#
# 这里改为按内容指纹：users / user_sessions 行数 + 最大 user_id + 审计最大 rowid。
# 命令结束后比对，变了就直接报错并以非零码退出，把"生产库被写"变成一次可见失败。
# ---------------------------------------------------------------------------
PROD_FINGERPRINT_BEFORE=""
prod_fingerprint() {
  [[ -f "$PROD_DB" ]] || { echo "absent"; return; }
  "$REPO_ROOT/.venv/bin/python" - "$PROD_DB" <<'PY' 2>/dev/null || echo "unreadable"
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
def one(sql, default=0):
    try:
        return c.execute(sql).fetchone()[0] or default
    except sqlite3.Error:
        return default
print("users=%s|sessions=%s|max_uid=%s|audit_max=%s|sessions_max=%s" % (
    one("SELECT COUNT(*) FROM users"),
    one("SELECT COUNT(*) FROM user_sessions"),
    one("SELECT COALESCE(MAX(user_id),0) FROM users"),
    one("SELECT COALESCE(MAX(rowid),0) FROM user_audit_log"),
    one("SELECT COUNT(*) FROM user_sessions"),
))
PY
}

if [[ -f "$PROD_DB" ]]; then
  PROD_FINGERPRINT_BEFORE="$(prod_fingerprint)"
fi

cleanup() {
  # 退出码来源（两个坑都踩过，已实测）：
  #  1) `local rc=$?` 本身没问题（$? 在同行内先展开），但**在 `exit N` 触发的
  #     trap 里** `$?` 恒为 0，N 会丢失 —— 所以真实码改由 $TMP/.exit_code 传递；
  #  2) `local x; x=$?` 会得到 0（local 自身成功），属反例，勿用。
  local rc=0
  if [[ -f "$TMP/.exit_code" ]]; then
    rc="$(cat "$TMP/.exit_code" 2>/dev/null || echo 0)"
  fi
  [[ "$rc" =~ ^[0-9]+$ ]] || rc=1
  # 生产库护栏：先比对内容指纹，再清理临时目录
  # 生产库护栏：先比对内容指纹，再清理临时目录
  if [[ -n "$PROD_FINGERPRINT_BEFORE" ]]; then
    local after; after="$(prod_fingerprint)"
    if [[ "$after" != "$PROD_FINGERPRINT_BEFORE" ]]; then
      {
        echo ""
        echo "#### 生产库内容发生变化！####"
        echo "  之前: $PROD_FINGERPRINT_BEFORE"
        echo "  之后: $after"
        echo "  你的命令写到了生产库 $PROD_DB —— 检查是否漏设 LOTT_HOME / LOTT_DB，"
        echo "  或代码里硬编码了库路径（config.DB_PATH 被直接赋值等）。"
        echo "  生产库现已被污染，需要人工核查并清理。"
      } >&2
      rc=99
    fi
  fi
  if [[ $KEEP -eq 1 ]]; then
    echo "[isolated] 保留临时目录: $TMP" >&2
  else
    rm -rf "$TMP"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

export LOTT_HOME="$TMP/home"
mkdir -p "$LOTT_HOME/data" "$LOTT_HOME/data/raw"

# 关键：数据库用「临时副本」，不是符号链接、不是原路径
if [[ -f "$PROD_DB" ]]; then
  cp "$PROD_DB" "$TMP/ssq.db"
  # WAL 副本一致性：把 -wal 一起拷，避免读副本时丢未 checkpoint 的事务
  [[ -f "${PROD_DB}-wal" ]] && cp "${PROD_DB}-wal" "$TMP/ssq.db-wal"
  echo "[isolated] 已复制生产库快照 → $TMP/ssq.db" >&2
else
  echo "[isolated] 生产库不存在，使用空库: $TMP/ssq.db" >&2
fi
export LOTT_DB="$TMP/ssq.db"

# 测试期默认值：关掉启动期后台线程（conftest 同样处理），鉴权按需由调用方覆盖
export LOTT_BOOT_REFRESH_RULES="${LOTT_BOOT_REFRESH_RULES:-0}"

# 让**仓库外**的脚本也能 import lottery（严QA 2026-10-21 报告）。
# 原因：`python /tmp/xxx.py` 时 sys.path[0] 是脚本所在目录（/tmp），不是 cwd，
# 所以即使下面 cd 到仓库根，`from lottery import ...` 仍报 ModuleNotFoundError。
# 修法：把仓库根前置进 PYTHONPATH。
# 外部脚本推荐跑法：
#   scripts/isolated_test.sh -- .venv/bin/python /tmp/my_probe.py
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

cd "$REPO_ROOT" || exit 1

# 注意：这里**不能**用 exec —— exec 会用目标命令替换当前 shell，
# EXIT trap 随之失效，临时目录永不清理（2026-10-21 实测踩到并修复）。
# 正确做法：先跑命令拿到退出码，让 trap 在 shell 退出时清理，再原样返回该码。
if [[ "$1" == "-c" ]]; then
  shift
  .venv/bin/python -c "${1:?缺少 python 代码}"
  rc=$?
else
  shift  # 吃掉 "--"
  "$@"
  rc=$?
fi

# 把命令退出码写进文件再 exit。
# 为什么不用 `exit $rc` 直接传：`exit N` 会触发 trap，而 trap 里 `local rc=$?`
# 拿到的是 `exit N` 这一动作之前的 $?（恒为 0），N 会被静默丢失 ——
# 2026-10-21 实测：护栏已正确检测到生产库被写，却仍以 0 退出。
# 用文件传递，cleanup 读取真实码，护栏判定可以覆盖它（99）。
echo "$rc" > "$TMP/.exit_code"
exit "$rc"
