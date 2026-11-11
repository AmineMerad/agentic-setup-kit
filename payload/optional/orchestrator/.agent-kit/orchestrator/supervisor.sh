#!/usr/bin/env bash
# supervisor.sh — the deterministic loop around the agents. It never writes code and never
# makes judgement calls: it reaps finished agents, repairs/retries failures, launches the next
# ready tickets through agentctl.sh, and asks the orchestrator session (an LLM) only when a
# ready ticket has no task file or a ticket is parked after MAX_ATTEMPTS.
#
#   reap  → classify log tail:  success (✅ finished + PR url)  |  PR-create failure (repair)  |  crash
#   retry → cleanup + relaunch with the failure tail appended to the prompt, then park
#   launch→ next-tickets.py --json, file-overlap guard, agentctl up -f <task> -n <name> BASE=main
#   ask   → orchestrator session writes the missing task files (cooldown per ticket)
#
# Env (all optional):
#   MAX_PARALLEL (2) MAX_ATTEMPTS (3) TICK_SECONDS (15) STALL_MINUTES (20)
#   ORCH_TIMEOUT (900) ORCH_COOLDOWN (600) DRY_RUN (0)
set -uo pipefail

ROOT="${WORKSPACE:-/workspace}"
cd "$ROOT" || exit 1
BASE="${BASE:-main}"
AGENTS_DIR="$ROOT/.agent-kit/agents"
STATE_DIR="$AGENTS_DIR/state"
BOOT_DIR="/boot"

SCAN="$ROOT/.agent-kit/orchestrator/next-tickets.py"; [ -f "$SCAN" ] || SCAN="$BOOT_DIR/next-tickets.py"
STATE="$ROOT/.agent-kit/orchestrator/state.py";        [ -f "$STATE" ] || STATE="$BOOT_DIR/state.py"
PROMPT_FILE="$ROOT/.agent-kit/orchestrator/planner-prompt.md"; [ -f "$PROMPT_FILE" ] || PROMPT_FILE="$BOOT_DIR/planner-prompt.md"
AGENTCTL="$AGENTS_DIR/agentctl.sh"

MAX_PARALLEL="${MAX_PARALLEL:-2}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
TICK_SECONDS="${TICK_SECONDS:-15}"
STALL_MINUTES="${STALL_MINUTES:-20}"
ORCH_TIMEOUT="${ORCH_TIMEOUT:-900}"
ORCH_COOLDOWN="${ORCH_COOLDOWN:-600}"
DRY_RUN="${DRY_RUN:-0}"

mkdir -p "$STATE_DIR"
log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }

st() { python3 "$STATE" "$@"; }

# ── dry run: show what would be picked, launch nothing ───────────────────────
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN=1 — ready candidates:"
  python3 "$SCAN" --json || exit 1
  exit 0
fi

# ── helpers ──────────────────────────────────────────────────────────────────

log_tail() { tail -n 60 "$1" 2>/dev/null || true; }

# Remove the agent's worktree, branches (unless a PR references the branch) and pid file.
cleanup_agent() {
  local name="$1"
  local wt="$ROOT/.worktrees/$name"
  [ -d "$wt" ] && { git -C "$ROOT" worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"; }
  git -C "$ROOT" worktree prune 2>/dev/null || true
  if git -C "$ROOT" show-ref --verify --quiet "refs/heads/$name"; then
    if git -C "$ROOT" ls-remote --exit-code --heads origin "$name" >/dev/null 2>&1 \
       && ! gh pr view "$name" --json state -q .state >/dev/null 2>&1; then
      git -C "$ROOT" push --quiet origin --delete "$name" >/dev/null 2>&1 || true
    fi
    git -C "$ROOT" branch -D "$name" >/dev/null 2>&1 || true
  fi
  rm -f "$AGENTS_DIR/$name.pid" "$AGENTS_DIR/$name.log.tmp"
}

# Success: branch pushed and a PR url printed by agent-finish.sh.
finish_success() {
  local name="$1" ticket="$2" url="$3"
  st done "$ticket" "$url"
  st clear-running "$name"
  rm -f "$AGENTS_DIR/$name.pid"
  log "✅ $ticket — PR opened: $url"
}

# The branch is already pushed but the PR step failed → retry only `gh pr create`.
repair_pr() {
  local name="$1" ticket="$2" title="$3" body
  if gh pr view "$name" --json url -q .url >/dev/null 2>&1; then
    finish_success "$name" "$ticket" "$(gh pr view "$name" --json url -q .url)"
    return 0
  fi
  body="Auto-PR from agent \`$name\` (branch \`$name\`, base \`$BASE\`) for ticket $ticket.
Opened by the orchestrator supervisor after agent-finish.sh failed to create it."
  if gh pr create --base "$BASE" --head "$name" --title "$title" --body "$body" >/dev/null 2>&1; then
    log "🔧 $ticket — PR created by repair path"
    finish_success "$name" "$ticket" "$(gh pr view "$name" --json url -q .url)"
    return 0
  fi
  log "✗ $ticket — PR repair failed"
  return 1
}

# Failure: bump the attempt counter; relaunch with the failure tail, or park the ticket.
handle_failure() {
  local name="$1" ticket="$2" reason="$3" tail_text="$4" taskfile="$5" files="$6" title="$7"
  local attempts
  attempts="$(st bump-attempts "$ticket")"
  mkdir -p "$STATE_DIR"
  log_tail "$AGENTS_DIR/$name.log" > "$STATE_DIR/$name.attempt$attempts.log" 2>/dev/null || true
  cleanup_agent "$name"
  st clear-running "$name"

  if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
    st park "$ticket" "$reason (attempt $attempts/$MAX_ATTEMPTS; log: state/$name.attempt$attempts.log)"
    log "⛔ $ticket parked after $attempts attempts — $reason"
    return 0
  fi

  # Retry with the failure context appended so the agent does not repeat the same mistake.
  local retry_file="$AGENTS_DIR/$name-attempt$attempts-task.txt"
  {
    cat "$taskfile"
    printf '\n\n---\n\n## Previous attempt #%s failed (%s)\n\n' "$attempts" "$reason"
    printf 'The supervisor re-launched you from the same base. Log tail of the failed attempt:\n\n'
    printf '```\n%s\n```\n' "$tail_text"
    printf '\nDo not repeat the failing approach. If the failure is environmental, say so and stop.\n'
  } > "$retry_file"
  log "🔁 $ticket — attempt $attempts/$MAX_ATTEMPTS failed ($reason); relaunching"
  launch_agent "$retry_file" "$name" "$ticket" "$files" "$title"
}

launch_agent() {
  local taskfile="$1" name="$2" ticket="$3" files="$4" title="$5"
  # agentctl refuses to launch when a log/pid for the name exists — rotate them away.
  if [ -f "$AGENTS_DIR/$name.log" ]; then
    mv "$AGENTS_DIR/$name.log" "$STATE_DIR/$name.$(date +%s).log"
  fi
  rm -f "$AGENTS_DIR/$name.pid"
  if ! (cd "$ROOT" && BASE="$BASE" bash "$AGENTCTL" up -f "$taskfile" -n "$name" >/dev/null 2>&1); then
    log "✗ $ticket — agentctl could not launch $name"
    return 1
  fi
  local pid
  pid="$(cat "$AGENTS_DIR/$name.pid" 2>/dev/null || echo '')"
  st set-running "$name" "$ticket" "${pid:-0}" "$files" "$taskfile" "$title"
  log "🚀 $ticket — launched agent '$name' (pid ${pid:-?}) base=$BASE"
}

# Extract the conventional-commit title from the task file's finish line, else a default.
title_for() {
  local taskfile="$1" name="$2" ticket="$3"
  local t
  t="$(grep -oE 'agent-finish\.sh[[:space:]]+'"$name"'[[:space:]]+[^[:space:]]+[[:space:]]+"[^"]+"' "$taskfile" 2>/dev/null | head -1 | sed -E 's/.*"([^"]+)"$/\1/')"
  printf '%s' "${t:-chore($name): agent work for $ticket}"
}

# True when any candidate file overlaps any running agent's files (prefix match, both ways).
files_overlap() {
  python3 - "$1" "$2" <<'PY'
import sys
def split(s): return [x for x in s.split(",") if x]
a, b = split(sys.argv[1]), split(sys.argv[2])
for x in a:
    for y in b:
        if x.startswith(y) or y.startswith(x):
            sys.exit(0)
sys.exit(1)
PY
}

# Ask the orchestrator session to write the missing task files (and triage parked tickets).
ask_orchestrator() {
  local need_task="$1" parked="$2" body session out
  local prompt; prompt="$(cat "$PROMPT_FILE" 2>/dev/null || echo 'You are the agent orchestrator.')"
  body="$prompt"$'\n\n'
  if [ -n "$need_task" ]; then
    body+="NEED TASK FILES FOR (ready tickets, no task file yet): $need_task"$'\n\n'
  fi
  if [ -n "$parked" ]; then
    body+="PARKED TICKETS TO TRIAGE: $parked"$'\n\n'
  fi
  body+="Candidate records (next-tickets.py --json), one per line:"$'\n'
  body+="$(SERIES="${SERIES:-}" TICKETS="${TICKETS:-}" python3 "$SCAN" --json 2>/dev/null)"

  local -a args=(run --auto --format json)
  session="$(st session get)"
  [ -n "$session" ] && args+=(-s "$session")
  log "🧠 asking orchestrator (need-task: ${need_task:-none}; parked: ${parked:-none})"
  out="$(cd "$ROOT" && timeout "$ORCH_TIMEOUT" opencode "${args[@]}" "$body" 2>&1)"
  printf '%s\n' "$out" >> "$STATE_DIR/orchestrator.log"
  local new_session
  new_session="$(printf '%s' "$out" | grep -oE '"sessionID"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 | sed -E 's/.*"([^"]+)"$/\1/')"
  if [ -n "$new_session" ]; then st session set "$new_session"; fi
  if printf '%s' "$out" | grep -q 'TASKFILES_WRITTEN'; then
    log "🧠 orchestrator wrote task files"
  elif printf '%s' "$out" | grep -q 'NOTHING_TO_DO'; then
    log "🧠 orchestrator: nothing to do"
  else
    log "🧠 orchestrator returned (see state/orchestrator.log)"
  fi
}

# ── the loop ─────────────────────────────────────────────────────────────────
log "supervisor up — base=$BASE max_parallel=$MAX_PARALLEL max_attempts=$MAX_ATTEMPTS"
trap 'log "supervisor stopping"; exit 0' TERM INT
TICKS=0

while true; do
  TICKS=$((TICKS + 1))

  # 1) reap
  while IFS=$'\t' read -r name ticket pid files taskfile title started; do
    [ -n "${name:-}" ] || continue
    LOG_FILE="$AGENTS_DIR/$name.log"
    if [ -n "$pid" ] && [ "$pid" != "0" ] && kill -0 "$pid" 2>/dev/null; then
      # stall detection: no log growth for STALL_MINUTES
      if [ -f "$LOG_FILE" ]; then
        AGE=$(( $(date +%s) - $(stat -c %Y "$LOG_FILE" 2>/dev/null || echo "$(date +%s)") ))
        if [ "$AGE" -gt $((STALL_MINUTES * 60)) ]; then
          log "⏱  $ticket — agent '$name' stalled ($((AGE / 60))m without log output); killing"
          kill "$pid" 2>/dev/null || true
          handle_failure "$name" "$ticket" "stalled $((AGE / 60))m" "$(log_tail "$LOG_FILE")" "$taskfile" "$files" "$title"
        fi
      fi
      continue
    fi

    # process gone → classify
    if grep -q '✅ finished:' "$LOG_FILE" 2>/dev/null && grep -qE '^[[:space:]]+PR: http' "$LOG_FILE" 2>/dev/null; then
      finish_success "$name" "$ticket" "$(grep -m1 -E '^[[:space:]]+PR: http' "$LOG_FILE" | awk '{print $2}')"
    elif grep -q '✗ PR create failed' "$LOG_FILE" 2>/dev/null; then
      repair_pr "$name" "$ticket" "$title" \
        || handle_failure "$name" "$ticket" "PR repair failed" "$(log_tail "$LOG_FILE")" "$taskfile" "$files" "$title"
    else
      handle_failure "$name" "$ticket" "exited without opening a PR" "$(log_tail "$LOG_FILE")" "$taskfile" "$files" "$title"
    fi
  done < <(st running)

  # 2) launch while there are free slots
  RUNNING_COUNT="$(st running | grep -c . || true)"
  SLOTS=$((MAX_PARALLEL - RUNNING_COUNT))
  NEED_TASK=""
  if [ "$SLOTS" -gt 0 ]; then
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      [ "$SLOTS" -gt 0 ] || break
      read -r cid cname cfiles ctask <<<"$(printf '%s' "$line" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(d["id"], d["name"], ",".join(d.get("files") or []), d.get("task_file") or "")')"
      # skip anything already running (state is authoritative)
      st running | cut -f2 | grep -qx "$cid" && continue
      # file-overlap guard: never run two agents touching the same paths
      if st running | cut -f4 | grep -q . ; then
        OVERLAP=0
        while IFS=$'\t' read -r rname rticket rpid rfiles rtask rtitle rstarted; do
          [ -n "${rfiles:-}" ] || continue
          if [ -n "$cfiles" ] && files_overlap "$cfiles" "$rfiles"; then OVERLAP=1; fi
        done < <(st running)
        [ "$OVERLAP" = "1" ] && continue
      fi
      if [ -z "$ctask" ] || [ ! -f "$ctask" ]; then
        if [ "$(st asked "$cid" "$ORCH_COOLDOWN")" = "1" ]; then continue; fi
        NEED_TASK="${NEED_TASK:+$NEED_TASK,}$cid"
        continue
      fi
      title="$(title_for "$ctask" "$cname" "$cid")"
      launch_agent "$ctask" "$cname" "$cid" "$cfiles" "$title" && SLOTS=$((SLOTS - 1))
    done < <(SERIES="${SERIES:-}" TICKETS="${TICKETS:-}" python3 "$SCAN" --json 2>/dev/null)
  fi

  # 3) orchestrator: missing task files / parked triage
  PARKED_NEW=""
  for tid in $(st parked); do
    if [ "$(st triaged "$tid")" = "0" ]; then PARKED_NEW="${PARKED_NEW:+$PARKED_NEW,}$tid"; fi
  done
  if [ -n "$NEED_TASK" ] || [ -n "$PARKED_NEW" ]; then
    ask_orchestrator "$NEED_TASK" "$PARKED_NEW"
    for tid in ${NEED_TASK//,/ }; do st mark-asked "$tid"; done
    for tid in ${PARKED_NEW//,/ }; do st mark-triaged "$tid"; done
  fi

  # 4) heartbeat every ~2 minutes; transitions are logged as they happen
  if [ $((TICKS % 8)) -eq 0 ]; then
    log "tick: running=$(st running | grep -c . || true) parked=$(st parked | grep -c . || true) ready=$(SERIES="${SERIES:-}" TICKETS="${TICKETS:-}" python3 "$SCAN" 2>/dev/null | grep -c . || true)"
  fi
  sleep "$TICK_SECONDS"
done
