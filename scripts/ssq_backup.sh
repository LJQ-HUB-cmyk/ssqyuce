#!/usr/bin/env bash
# ssq_backup.sh —— 双色球预测系统 data/ 备份 / 恢复 / 校验（V10-00c）
#
# 为什么需要它（docs/reviews/R4_RELEASE_REVIEW.md P0-C）：
#   生产库此前**零备份**：`/etc/cron.d/ssq-backup` 不存在、`/backup/` 不存在、
#   `crontab -l` 无 ssq 项、`data/*.bak*` 也没有。而 `data/` 里除了 1.4MB 的
#   `ssq.db`，还有 hypotheses.json / adaptive_state.json / llm_budget.json /
#   llm_config.json / methods_config.json / llm_claims.json / patterns_runtime.json
#   与 `raw/`——只 `cp` 一个 db 等于没备份。
#
# 设计要点：
#   * `ssq.db` 用 **Python sqlite3 `Connection.backup()`**（在线一致性热备），
#     **不是** `cp`：cp 一个正在写的库会得到撕裂的副本。
#   * 备份**全量 data/**，唯一排除 `ml_models/`（265MB、可由训练重建；归档里会写明）。
#   * 归档与目录权限 600/700：`llm_config.json` 内含 LLM API Key，**绝不能**落到 o+w 文件。
#   * 日志只写路径/大小/期数/校验结果，**绝不写任何密钥内容**。
#   * 保留最近 N 份（默认 14），旧归档自动清理。
#   * 提供 `restore` 与 `verify` 子命令，恢复演练可以一条命令做完并给出期数。
#
# 用法：
#   ssq_backup.sh                      # 备份（cron 默认调用）
#   ssq_backup.sh backup               # 同上
#   ssq_backup.sh list                 # 列出已有备份
#   ssq_backup.sh verify [归档|目录]    # 校验（默认校验最新的归档）
#   ssq_backup.sh restore <归档> <目标目录>   # 恢复演练：解到目标目录并校验
#
# 可用环境变量覆盖：SSQ_APP_DIR / SSQ_BACKUP_DIR / SSQ_BACKUP_KEEP /
#                   SSQ_BACKUP_LOG / SSQ_PYTHON
set -euo pipefail

APP_DIR="${SSQ_APP_DIR:-/root/DSH/应用/ssqyuce}"
DATA_DIR="$APP_DIR/data"
BACKUP_DIR="${SSQ_BACKUP_DIR:-/backup}"
RETENTION="${SSQ_BACKUP_KEEP:-14}"
LOG_FILE="${SSQ_BACKUP_LOG:-/var/log/ssq-backup.log}"
PY="${SSQ_PYTHON:-$APP_DIR/.venv/bin/python}"
DB_NAME="ssq.db"

# data/ 下必须备份的运行时文件（缺了不算失败，只是记一笔 missing）
RUNTIME_FILES=(
  hypotheses.json
  adaptive_state.json
  llm_budget.json
  llm_config.json
  methods_config.json
  llm_claims.json
  patterns_runtime.json
  mining_artifact.json
  mining_latest.json
)

log() {
  local ts line
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  line=$(printf '%s [ssq-backup] %s' "$ts" "$*")
  # 日志写失败绝不能把备份本身搞挂；同时打到 stdout 方便 cron 邮件/手工排查
  printf '%s\n' "$line"
  if [[ -n "${LOG_FILE:-}" ]]; then
    printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null || true
  fi
}

die() {
  log "ERROR: $*"
  exit 1
}

require_env() {
  [[ -d "$DATA_DIR" ]] || die "data 目录不存在：$DATA_DIR"
  [[ -x "$PY" ]] || die "Python 不可执行：$PY"
  command -v tar >/dev/null || die "缺少 tar"
}

# 只允许日志文件落在可写位置，权限 600（日志不含密钥，但统一收紧）
prepare_log() {
  local dir
  dir="$(dirname "$LOG_FILE")"
  mkdir -p "$dir" 2>/dev/null || true
  [[ -e "$LOG_FILE" ]] || : >"$LOG_FILE" 2>/dev/null || true
  chmod 600 "$LOG_FILE" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# sqlite 热备 + 校验（一次性 Python 脚本，入参：源库 目标库）
# ---------------------------------------------------------------------------
sqlite_snapshot() {
  "$PY" - "$@" <<'PYEOF'
import json
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
out = {"src": src, "dst": dst}
s = sqlite3.connect(src, timeout=30)
try:
    d = sqlite3.connect(dst)
    try:
        s.backup(d)                      # 在线一致性热备（非 cp）
        out["integrity"] = d.execute("PRAGMA integrity_check").fetchone()[0]
        out["draws"] = d.execute("SELECT count(*) FROM draws").fetchone()[0]
        out["max_issue"] = (d.execute("SELECT max(CAST(issue AS INTEGER)) FROM draws").fetchone()[0])
        try:
            out["schema_version"] = d.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except sqlite3.Error:
            out["schema_version"] = None
        d.commit()
    finally:
        d.close()
finally:
    s.close()
print(json.dumps(out, ensure_ascii=False))
PYEOF
}

# 校验一个目录里的备份内容：库完整性 + 期数 + 所有 json 可解析
verify_dir() {
  local dir="$1"
  "$PY" - "$dir" <<'PYEOF'
import json
import sqlite3
import sys
from pathlib import Path

d = Path(sys.argv[1])
result = {"dir": str(d), "ok": True, "files": {}, "errors": []}

db = d / "ssq.db"
if not db.exists():
    result["ok"] = False
    result["errors"].append("缺少 ssq.db")
else:
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        result["integrity"] = integrity
        result["draws"] = c.execute("SELECT count(*) FROM draws").fetchone()[0]
        try:
            result["schema_version"] = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        except sqlite3.Error:
            result["schema_version"] = None
        c.close()
        if integrity != "ok":
            result["ok"] = False
            result["errors"].append(f"integrity_check={integrity}")
    except sqlite3.Error as e:
        result["ok"] = False
        result["errors"].append(f"open db: {type(e).__name__}: {e}")

for p in sorted(d.glob("*.json")):
    try:
        with p.open("r", encoding="utf-8") as f:
            json.load(f)
        result["files"][p.name] = p.stat().st_size
    except Exception as e:                     # noqa: BLE001
        result["ok"] = False
        result["errors"].append(f"{p.name}: {type(e).__name__}: {e}")

raw = d / "raw"
if raw.is_dir():
    result["raw_files"] = sum(1 for _ in raw.iterdir())
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if result["ok"] else 2)
PYEOF
}

# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------
STAGING=""
cleanup_staging() {
  if [[ -n "${STAGING:-}" && -d "${STAGING:-}" ]]; then
    rm -rf -- "$STAGING"
  fi
  STAGING=""
}

cmd_backup() {
  require_env
  prepare_log
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"

  trap cleanup_staging EXIT
  # 顺手清掉上次异常退出留下的暂存目录（>1 天）
  find "$BACKUP_DIR" -maxdepth 1 -name '.staging.*' -mtime +1 -exec rm -rf {} + 2>/dev/null || true

  # 防并发：上一轮还没跑完就跳过本轮
  exec 9>"$BACKUP_DIR/.ssq-backup.lock"
  if ! flock -n 9; then
    log "上一次备份仍在运行，本轮跳过"
    exit 0
  fi

  local ts archive
  ts="$(date '+%Y%m%d-%H%M%S')"
  archive="$BACKUP_DIR/ssq-backup-$ts.tar.gz"
  STAGING="$(mktemp -d "$BACKUP_DIR/.staging.XXXXXX")"
  staging="$STAGING"
  chmod 700 "$staging"

  # 1) 库：一致性热备
  local snap_json
  snap_json="$(sqlite_snapshot "$DATA_DIR/$DB_NAME" "$staging/$DB_NAME")" \
    || die "sqlite 热备失败：$DATA_DIR/$DB_NAME"
  local draws
  draws="$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1]).get("draws"))' "$snap_json")"
  log "sqlite 热备完成：暂存 $DB_NAME，期数=$draws（$(du -h "$staging/$DB_NAME" | cut -f1)）"

  # 2) 运行时 json（含密钥的那个收紧到 600）
  local f missing=()
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$DATA_DIR/$f" ]]; then
      cp -p "$DATA_DIR/$f" "$staging/$f"
      if [[ "$f" == "llm_config.json" ]]; then
        chmod 600 "$staging/$f"
      fi
    else
      missing+=("$f")
    fi
  done
  # 其它 json（未来新增的运行时文件也别漏）
  shopt -s nullglob
  for f in "$DATA_DIR"/*.json; do
    local base
    base="$(basename "$f")"
    [[ -e "$staging/$base" ]] || cp -p "$f" "$staging/$base"
  done
  shopt -u nullglob

  # 3) raw/ 抓取原文
  if [[ -d "$DATA_DIR/raw" ]]; then
    cp -a "$DATA_DIR/raw" "$staging/raw"
  fi

  # 4) 明确记录被排除的重建型目录（ml_models/ 265MB）与缺失文件
  {
    echo "backup_time=$ts"
    echo "app_dir=$APP_DIR"
    echo "data_dir=$DATA_DIR"
    echo "draws=$draws"
    echo "sqlite_snapshot=$snap_json"
    echo "excluded=ml_models/（可由训练重建，265MB，不进归档）"
    echo "missing=${missing[*]:-无}"
    echo "restore_note=见 DEPLOY.md 第七节：tar -xzf <归档> -C <目标目录>"
  } >"$staging/MANIFEST.txt"

  # 5) 打包（600：归档内含 llm_config.json 的 API Key）
  ( cd "$staging" && tar -czf "$archive" . )
  chmod 600 "$archive"
  log "备份完成：$archive（$(du -h "$archive" | cut -f1)），排除 ml_models/，缺失=${missing[*]:-无}"

  # 6) 立刻校验一次（备份不能只看"文件生成了"）
  if verify_dir "$staging" >/tmp/.ssq-backup-verify.$$ 2>&1; then
    log "备份自检通过：期数=$draws，全部 *.json 均可 json.load"
  else
    log "ERROR: 备份自检失败，详见 /tmp/.ssq-backup-verify.$$"
  fi
  rm -f /tmp/.ssq-backup-verify.$$

  # 7) 保留最近 RETENTION 份
  local old
  old="$(ls -1t "$BACKUP_DIR"/ssq-backup-*.tar.gz 2>/dev/null | tail -n +$((RETENTION + 1)) || true)"
  if [[ -n "$old" ]]; then
    while IFS= read -r x; do
      rm -f "$x"
      log "清理旧归档：$x"
    done <<<"$old"
  fi

  local n
  n="$(ls -1 "$BACKUP_DIR"/ssq-backup-*.tar.gz 2>/dev/null | wc -l)"
  log "现存备份份数：$n（保留上限 $RETENTION）"
}

# ---------------------------------------------------------------------------
# list / verify / restore
# ---------------------------------------------------------------------------
latest_archive() {
  ls -1t "$BACKUP_DIR"/ssq-backup-*.tar.gz 2>/dev/null | head -1
}

cmd_list() {
  prepare_log
  if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "备份目录不存在：$BACKUP_DIR"
    return 0
  fi
  ls -lh "$BACKUP_DIR"/ssq-backup-*.tar.gz 2>/dev/null || echo "（还没有任何备份）"
}

cmd_verify() {
  require_env
  prepare_log
  local target="${1:-}"
  local tmp=""
  if [[ -z "$target" ]]; then
    target="$(latest_archive)"
    [[ -n "$target" ]] || die "备份目录里没有任何 ssq-backup-*.tar.gz"
  fi
  if [[ -f "$target" ]]; then
    tmp="$(mktemp -d)"
    tar -xzf "$target" -C "$tmp" || die "解包失败：$target"
    target="$tmp"
  fi
  [[ -d "$target" ]] || die "目标既不是归档也不是目录：$target"
  local rc=0
  verify_dir "$target" || rc=$?
  [[ -n "$tmp" ]] && rm -rf "$tmp"
  if [[ $rc -eq 0 ]]; then
    log "校验通过：$target"
  else
    log "ERROR: 校验失败：$target"
  fi
  return $rc
}

cmd_restore() {
  require_env
  prepare_log
  local archive="${1:-}" dest="${2:-}"
  [[ -n "$archive" && -n "$dest" ]] || die "用法：ssq_backup.sh restore <归档> <目标目录>"
  [[ -f "$archive" ]] || die "归档不存在：$archive"
  # 安全护栏：绝不允许把恢复结果直接盖回生产 data/
  local dest_abs data_abs
  dest_abs="$(readlink -f "$dest" 2>/dev/null || echo "$dest")"
  data_abs="$(readlink -f "$DATA_DIR")"
  if [[ "$dest_abs" == "$data_abs" ]]; then
    die "拒绝恢复到生产 data/ 目录；请先恢复到临时目录校验，再人工切换"
  fi
  mkdir -p "$dest"
  tar -xzf "$archive" -C "$dest"
  log "已解包 $archive → $dest"
  if verify_dir "$dest"; then
    log "恢复演练校验通过：$dest"
  else
    log "ERROR: 恢复演练校验失败：$dest"
    return 2
  fi
}

main() {
  case "${1:-backup}" in
    backup)  cmd_backup ;;
    list)    cmd_list ;;
    verify)  shift; cmd_verify "${1:-}" ;;
    restore) shift; cmd_restore "${1:-}" "${2:-}" ;;
    -h|--help|help)
      sed -n '2,30p' "$0"
      ;;
    *) die "未知子命令：$1（可用：backup / list / verify / restore）" ;;
  esac
}

main "$@"
