#!/usr/bin/env bash
set -uo pipefail

ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)" || {
  printf '错误：当前目录不属于 Git 仓库。\n' >&2
  exit 1
}
STATE_DIR="$ROOT/.foundry-state"
PID_FILE="$STATE_DIR/cloud-sync.pid"
LOG_FILE="$STATE_DIR/cloud-sync.log"
LOCK_FILE="$STATE_DIR/cloud-sync.lock"
INTERVAL="${CLOUD_SYNC_INTERVAL:-30}"
mkdir -p "$STATE_DIR"

sync_once(){
  local branch
  branch="$(git -C "$ROOT" branch --show-current)"
  [[ -n "$branch" ]] || return 1
  exec 9>"$LOCK_FILE"
  flock -n 9 || return 0

  if git -C "$ROOT" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    if ! git -C "$ROOT" pull --rebase --autostash origin "$branch" >> "$LOG_FILE" 2>&1; then
      printf '%s remote update conflict; resolve it before autosave can continue\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
      return 1
    fi
  fi

  git -C "$ROOT" add -A
  if ! git -C "$ROOT" diff --cached --quiet; then
    if ! git -C "$ROOT" commit -m "chore: cloud autosave $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE" 2>&1; then
      printf '%s commit failed; check git identity and repository status\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
      return 1
    fi
  fi
  if git -C "$ROOT" push origin HEAD >> "$LOG_FILE" 2>&1; then
    printf '%s saved and pushed\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
  else
    printf '%s push failed; will retry\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
    return 1
  fi
}

worker(){
  while true; do
    sync_once || true
    sleep "$INTERVAL"
  done
}

case "${1:-status}" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(<"$PID_FILE")" 2>/dev/null; then
      printf '云端自动保存已在运行（PID %s）。\n' "$(<"$PID_FILE")"
      exit 0
    fi
    if ! git -C "$ROOT" remote get-url origin >/dev/null 2>&1; then
      printf '错误：未配置 origin 远端。\n' >&2
      exit 1
    fi
    if ! git -C "$ROOT" symbolic-ref -q HEAD >/dev/null; then
      printf '错误：当前处于 detached HEAD，无法自动推送。\n' >&2
      exit 1
    fi
    git -C "$ROOT" config user.name >/dev/null || git -C "$ROOT" config user.name "EnglishFoundry autosave"
    git -C "$ROOT" config user.email >/dev/null || git -C "$ROOT" config user.email "englishfoundry-bot@users.noreply.github.com"
    nohup env GIT_TERMINAL_PROMPT=0 bash "$ROOT/scripts/cloud-sync.sh" worker >> "$LOG_FILE" 2>&1 < /dev/null &
    echo "$!" > "$PID_FILE"
    printf '云端自动保存已启动，每 %s 秒检查一次；状态日志：.foundry-state/cloud-sync.log\n' "$INTERVAL"
    ;;
  sync-once)
    sync_once
    ;;
  worker)
    worker
    ;;
  stop)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(<"$PID_FILE")" 2>/dev/null; then
      kill "$(<"$PID_FILE")"
      rm -f "$PID_FILE"
      printf '云端自动保存已停止。\n'
    else
      rm -f "$PID_FILE"
      printf '云端自动保存当前未运行。\n'
    fi
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(<"$PID_FILE")" 2>/dev/null; then
      printf '云端自动保存正在运行（PID %s）。\n' "$(<"$PID_FILE")"
      tail -n 3 "$LOG_FILE" 2>/dev/null || true
    else
      printf '云端自动保存当前未运行。启动：bash scripts/cloud-sync.sh start\n'
    fi
    ;;
  *)
    printf '用法：bash scripts/cloud-sync.sh [start|stop|status|sync-once]\n' >&2
    exit 2
    ;;
esac
