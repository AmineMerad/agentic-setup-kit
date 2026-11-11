#!/usr/bin/env bash
# agentctl.sh — manage headless opencode background agents.
# Backward compatible with pre-naming launch (run-*.log, no pid file).
# Commands:
#   up   -f FILE [-n NAME]  launch an agent; NAME defaults to agent-<next free N>
#   launch ID [-b BASE]     resolve .agent-kit/agents/<ID>-task.txt (or tasks/<ID>-task.txt),
#                           derive NAME from its agent-finish.sh line, then launch it
#   log  [-n NAME | -a | -l] follow one agent's log, ALL logs (-a), or the latest (-l)
#   list                     enumerate agents: pid, state, task, last output line
#   stop [-n NAME]           stop one agent (by NAME) or all agents (incl. untracked)
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$DIR"
GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'

usage() {
  echo "usage: agentctl.sh {up|launch|log|list|stop} ..."
  echo "  up   -f FILE [-n NAME]    launch agent; NAME defaults to agent-<next free N>"
  echo "                            worktree mode (default): BASE=<ref> WT=0 to disable"
  echo "  launch ID [-b BASE]       launch the task file for ID (tasks/<ID>-task.txt or"
  echo "                            <ID>-task.txt); NAME derived from agent-finish.sh line"
  echo "  log  [-n NAME | -a | -l]  follow a specific agent's log, ALL (-a), or latest (-l)"
  echo "  list                      show every agent: pid, state, task, last line"
  echo "  stop [-n NAME]            stop one agent (by NAME; omit = stop everything)"
  exit 1
}

ACT="${1:-}"; shift || true
NAME=""; FILE=""; ALL=0; LATEST=0; POS=""
while [ $# -gt 0 ]; do
  case "$1" in
    -n) NAME="${2:-}"; shift 2;;
    -f) FILE="${2:-}"; shift 2;;
    -b) BASE="${2:-}"; shift 2;;
    -a) ALL=1; shift;;
    -l) LATEST=1; shift;;
    *) if [ -z "$POS" ]; then POS="$1"; shift; else usage; fi;;
  esac
done

next_name() {
  local n=1 cand
  while true; do
    cand="agent-$n"
    if [ ! -f "$DIR/$cand.log" ] && [ ! -f "$DIR/$cand.pid" ] && [ ! -f "$DIR/run-$cand.log" ]; then
      echo "$cand"; return
    fi
    n=$((n+1))
  done
}

case "$ACT" in
  launch)
    ID="$POS"
    if [ -z "$ID" ]; then
      echo "${YELLOW}usage: agentctl.sh launch ID [-b BASE]${NC}"
      echo "Available task ids:"
      ls -1 "$DIR"/tasks/*-task.txt "$DIR"/*-task.txt 2>/dev/null | sed 's#.*/##; s#-task.txt$##' | sort -u | sed 's/^/   /'
      exit 0
    fi
    if [ -f "$DIR/tasks/$ID-task.txt" ]; then FILE="$DIR/tasks/$ID-task.txt"
    elif [ -f "$DIR/$ID-task.txt" ]; then FILE="$DIR/$ID-task.txt"
    else
      echo "${RED}✗ no task file for '$ID'. Available ids:${NC}"
      ls -1 "$DIR"/tasks/*-task.txt "$DIR"/*-task.txt 2>/dev/null | sed 's#.*/##; s#-task.txt$##' | sort -u | sed 's/^/   /'
      exit 1
    fi
    [ -n "$NAME" ] || NAME="$(grep -oE 'agent-finish\.sh [A-Za-z0-9._-]+' "$FILE" | head -1 | awk '{print $2}')"
    [ -n "$NAME" ] || NAME="$ID"
    echo "${YELLOW}◈ launching task '$ID' as agent '$NAME'  ($FILE)${NC}"
    export BASE
    exec "$0" up -f "$FILE" -n "$NAME"
    ;;
  up)
    [ -n "$FILE" ] || { echo "${RED}✗ -f FILE is required${NC}"; usage; }
    [ -f "$FILE" ] || { echo "${RED}✗ FILE not found: $FILE${NC}"; exit 1; }
    [ -n "$NAME" ] || NAME="$(next_name)"
    if [ -f "$DIR/$NAME.log" ] || [ -f "$DIR/$NAME.pid" ]; then
      echo "${RED}✗ agent '$NAME' already exists in $DIR — use a different -n NAME${NC}"; exit 1
    fi
    m="$(cat "$FILE")"
    [ -n "$m" ] || { echo "${RED}✗ FILE is empty: $FILE${NC}"; exit 1; }
    WT_MODE=0
    if [ "${WT:-1}" != "0" ] && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      BASE="${BASE:-$(git branch --show-current 2>/dev/null)}"
      BASE="${BASE:-$(git rev-parse --abbrev-ref HEAD)}"
      WT_DIR=".worktrees/$NAME"
      if [[ "$NAME" == agent-* ]]; then echo "${RED}✗ worktree mode needs an explicit -n NAME (default name 'agent-N' would collide with branches)${NC}"; exit 1; fi
      if [ -e "$WT_DIR" ]; then echo "${RED}✗ worktree path exists: $WT_DIR — clean it up first${NC}"; exit 1; fi
      if git rev-parse --verify --quiet "refs/heads/$NAME" >/dev/null; then
        echo "${RED}✗ branch '$NAME' already exists — use a different -n NAME or delete the branch${NC}"; exit 1
      fi
      git worktree add -b "$NAME" "$WT_DIR" "$BASE" >/dev/null 2>&1 || {
        echo "${RED}✗ could not create worktree from base '$BASE' (branch/commit must exist)${NC}"; exit 1; }
      wt_hdr=$(printf '# ═══ WORKTREE SESSION (auto-managed) ═══
# You run INSIDE an isolated git worktree: .worktrees/%s  (branch %s, based on %s).
# - Edit ONLY files inside this worktree. Do not touch the main clone.
# - The main clone'\''s uncommitted changes are NOT available here — rely on committed state at %s.
# - When your task is done and verified in this worktree, finalize with:
#       bash .agent-kit/agents/agent-finish.sh %s %s "<conventional commit title>"
#   That commits, pushes %s, opens/updates the PR against %s, removes this worktree,
#   and prints the PR URL. Then stop and report. Do NOT run git add/commit/push/PR yourself.'
        "$NAME" "$NAME" "$BASE" "$BASE" "$NAME" "$BASE" "$NAME" "$BASE")
      m="$wt_hdr

$m"
      WT_MODE=1
      GIT_FLAG="$WT_DIR"
      echo "${YELLOW}◈ worktree: .worktrees/$NAME  branch: $NAME  base: $BASE${NC}"
    else
      GIT_FLAG="."
    fi
    { echo "# ═══ Agent task: $FILE (worktree mode: $([ "$WT_MODE" = 1 ] && echo yes || echo no)) ═══"; cat "$FILE"; echo; echo "# ═══ end of task — agent output below ═══"; } > "$DIR/$NAME.log"
    if [ "$WT_MODE" = 1 ]; then
      nohup opencode run --auto --dir "$GIT_FLAG" "$m" >> "$DIR/$NAME.log" 2>&1 &
    else
      nohup opencode run --auto "$m" >> "$DIR/$NAME.log" 2>&1 &
    fi
    PID=$!
    echo "$PID" > "$DIR/$NAME.pid"
    echo "${GREEN}✅ Agent '$NAME' launched in background (PID $PID).${NC}"
    echo "   Log:   .agent-kit/agents/$NAME.log"
    echo "   Track: make agents-log NAME=$NAME    Stop: make agents-stop NAME=$NAME"
    echo "   All:   make agents-log ALL=1         List: make agents-list"
    ;;
  log)
    if [ "$ALL" -eq 1 ]; then
      files="$(ls -t "$DIR"/*.log 2>/dev/null)"
      [ -n "$files" ] || { echo "${RED}✗ no agent logs yet${NC}"; exit 1; }
      exec tail -f $files
    elif [ "$LATEST" -eq 1 ] || [ -z "$NAME" ]; then
      latest="$(ls -t "$DIR"/*.log 2>/dev/null | head -1)"
      [ -n "$latest" ] || { echo "${RED}✗ no agent logs yet${NC}"; exit 1; }
      echo "==> $latest"
      exec tail -f "$latest"
    else
      [ -f "$DIR/$NAME.log" ] || { echo "${RED}✗ no log for agent '$NAME' (run make agents-list)${NC}"; exit 1; }
      echo "==> $DIR/$NAME.log"
      exec tail -f "$DIR/$NAME.log"
    fi
    ;;
  list)
    found=0
    for logf in "$DIR"/*.log; do
      [ -e "$logf" ] || continue
      name="$(basename "$logf" .log)"
      pidf="$DIR/$name.pid"
      pid=""; state="untracked"
      if [ -f "$pidf" ]; then
        pid="$(cat "$pidf")"
        if kill -0 "$pid" 2>/dev/null; then state="running"; else state="exited"; fi
      fi
      task="$(sed -n '2p' "$logf")"
      last="$(tail -n1 "$logf" 2>/dev/null)"
      printf 'agent: %-14s pid: %-7s state: %-9s task: %s\n' "$name" "${pid:--}" "$state" "$task"
      if [ -d "$PWD/.worktrees/$name" ]; then printf '        worktree: .worktrees/%s  branch: %s\n' "$name" "$name"; fi
      if [ -n "$last" ]; then printf '        last: %s\n' "${last:0:110}"; fi
      found=1
    done
    [ "$found" -eq 1 ] || echo "No agents yet — run: make agents-up FILE=.agent-kit/agents/<task-file>.txt"
    ;;
  stop)
    if [ -n "$NAME" ]; then
      pidf="$DIR/$NAME.pid"
      [ -f "$pidf" ] || { echo "${RED}✗ no agent '$NAME' (no $NAME.pid) — see make agents-list${NC}"; exit 1; }
      pid="$(cat "$pidf")"
      if kill "$pid" 2>/dev/null; then echo "${GREEN}✅ Stopped agent '$NAME' (PID $pid)${NC}"; else echo "${YELLOW}⚠ agent '$NAME' (PID $pid) was already stopped${NC}"; fi
      pkill -P "$pid" 2>/dev/null
      rm -f "$pidf"
      if [ -e "$DIR/../../.worktrees/$NAME" ]; then
        git -C "$DIR/../../" worktree remove --force ".worktrees/$NAME" 2>/dev/null
        git -C "$DIR/../../" worktree prune 2>/dev/null
        echo "   removed leftover worktree .worktrees/$NAME"
      fi
    else
      n=0
      for pidf in "$DIR"/*.pid; do
        [ -e "$pidf" ] || continue
        pid="$(cat "$pidf")"
        if kill "$pid" 2>/dev/null; then n=$((n+1)); fi
        pkill -P "$pid" 2>/dev/null
        rm -f "$pidf"
      done
      # catch any untracked pre-naming agents too
      leftover="$(pgrep -f "opencode run --auto" 2>/dev/null)"
      if [ -n "$leftover" ]; then pkill -f "opencode run --auto" 2>/dev/null; n=$((n+1)); fi
      # remove any leftover worktrees
      if [ -d "$DIR/../../.worktrees" ]; then
        for wt in "$DIR/../../.worktrees"/*; do
          [ -d "$wt" ] || continue
          git -C "$DIR/../../" worktree remove --force "${wt#"$DIR/../../"/}" 2>/dev/null
        done 2>/dev/null
        git -C "$DIR/../../" worktree prune 2>/dev/null
      fi
      [ "$n" -gt 0 ] && echo "${GREEN}✅ Stopped $n agent(s)${NC}" || echo "${YELLOW}⚠ No running agents${NC}"
    fi
    ;;
  *) usage;;
esac