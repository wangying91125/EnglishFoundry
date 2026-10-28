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
RUN_LOCK="$STATE_DIR/cloud-run.lock"
INTERVAL="${CLOUD_SYNC_INTERVAL:-30}"
mkdir -p "$STATE_DIR"

acquire_lock(){
  exec 9>"$LOCK_FILE"
  if ! flock -w "${CLOUD_SYNC_LOCK_WAIT:-60}" 9; then
    printf '%s timed out waiting for the cloud sync lock\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
    return 1
  fi
}

refresh_under_lock(){
  local branch="$1" remote_branch
  if ! remote_branch="$(git -C "$ROOT" ls-remote --heads origin "$branch" 2>> "$LOG_FILE")"; then
    printf '%s unable to check remote branch; local files were kept\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
    return 1
  fi
  if [[ -n "$remote_branch" ]] && ! git -C "$ROOT" pull --rebase --autostash origin "$branch" >> "$LOG_FILE" 2>&1; then
    printf '%s remote update conflict; resolve it before autosave can continue\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
    return 1
  fi
}

refresh_latest(){
  local branch
  branch="$(git -C "$ROOT" branch --show-current)"
  [[ -n "$branch" ]] || return 1
  acquire_lock || return 1
  refresh_under_lock "$branch"
}

sync_once(){
  local branch
  branch="$(git -C "$ROOT" branch --show-current)"
  [[ -n "$branch" ]] || return 1
<<<<<<< Updated upstream
  acquire_lock || return 1
  refresh_under_lock "$branch" || return 1
=======
  exec 9>"$LOCK_FILE"
  flock -n 9 || return 0

  exec 8>"$RUN_LOCK"
  if flock -s -n 8; then
    if git -C "$ROOT" ls-remote --heads origin "refs/heads/$branch" >> "$LOG_FILE" 2>&1; then
      if ! git -C "$ROOT" pull --rebase --autostash origin "$branch" >> "$LOG_FILE" 2>&1; then
        printf '%s remote update conflict; resolve it before autosave can continue\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
        return 1
      fi
    fi
  else
    printf '%s run in progress; defer remote pull, save local changes only\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
  fi
  flock -u 8
>>>>>>> Stashed changes

  git -C "$ROOT" add -A -- . ':!.vscode/mcp.json'
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

run_latest(){
  local branch
  branch="$(git -C "$ROOT" branch --show-current)"
  [[ -n "$branch" ]] || return 1
  exec 8>"$RUN_LOCK"
  flock -n 8 || { printf '错误：已有云端程序正在运行。\n' >&2; return 1; }
  exec 9>"$LOCK_FILE"
  flock 9
  if ! git -C "$ROOT" ls-remote --heads origin "refs/heads/$branch" >> "$LOG_FILE" 2>&1; then
    printf '错误：无法连接云端，拒绝使用可能过期的脚本运行。\n' >&2
    return 1
  fi
  if ! git -C "$ROOT" pull --rebase --autostash origin "$branch" >> "$LOG_FILE" 2>&1; then
    printf '错误：无法合并云端最新版；请查看 %s\n' "$LOG_FILE" >&2
    return 1
  fi
  git -C "$ROOT" add -A -- . ':!.vscode/mcp.json'
  if ! git -C "$ROOT" diff --cached --quiet; then
    git -C "$ROOT" commit -m "chore: cloud autosave before run $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE" 2>&1 || return 1
  fi
  if ! git -C "$ROOT" push origin HEAD >> "$LOG_FILE" 2>&1; then
    printf '%s latest fetched; push deferred and will retry\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
  fi
  flock -u 9
  exec 9>&-
  exec env FOUNDRY_CLOUD_LATEST=1 bash "$ROOT/deepseek_bash_20261028_162950.sh" "$@"
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
<<<<<<< Updated upstream
  refresh)
    refresh_latest
=======
  run-latest)
    shift
    run_latest "$@"
>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
    printf '用法：bash scripts/cloud-sync.sh [start|stop|status|refresh|sync-once]\n' >&2
=======
    printf '用法：bash scripts/cloud-sync.sh [start|stop|status|sync-once|run-latest]\n' >&2
>>>>>>> Stashed changes
    exit 2
    ;;
esac
