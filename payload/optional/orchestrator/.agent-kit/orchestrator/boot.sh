#!/usr/bin/env bash
# boot.sh — entrypoint for the agent-orchestrator container.
#
#   1. env checks: GH_TOKEN present, gh accepts it, opencode model answers (fail fast)
#   2. clone (first boot) or fetch + hard-reset (restarts) the repo into the /workspace volume
#   3. git identity + gh credential helper (git push/pull over https)
#   4. dependency install — warms the shared store so agent worktree installs are fast
#   5. reconcile stale agent state left by a previous container run
#   6. print the ready-ticket set, then hand over to supervisor.sh (DRY_RUN=1 stops after the scan)
#
# Env (all optional unless noted):
#   GH_TOKEN        required — PAT with contents + pull requests on the repo
#   REPO_SLUG       required unless REPO_URL is set — owner/name on GitHub
#   REPO_URL        default https://github.com/$REPO_SLUG.git
#   WORKSPACE       default /workspace (the volume mount)
#   BASE            default main — the PR base branch
#   DRY_RUN         =1 → scan ready tickets and exit
#   HEALTH_TIMEOUT  default 120 (seconds for the opencode ping)
#   INSTALL_CMD     dependency install run in the clone (default: auto-detect from the lockfile; "" skips)
set -euo pipefail

REPO_SLUG="${REPO_SLUG:-}"
REPO_URL="${REPO_URL:-}"
if [ -z "$REPO_URL" ] && [ -n "$REPO_SLUG" ]; then REPO_URL="https://github.com/${REPO_SLUG}.git"; fi
if [ -z "$REPO_URL" ]; then
  printf '\033[31m✗ set REPO_SLUG (owner/name) or REPO_URL in .env.agents\033[0m\n' >&2
  exit 1
fi
WORKSPACE="${WORKSPACE:-/workspace}"
BASE="${BASE:-main}"
DRY_RUN="${DRY_RUN:-0}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
AGENTS_DIR="$WORKSPACE/.agent-kit/agents"

say() { printf '\033[36m◈\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 1. auth ──────────────────────────────────────────────────────────────────
[ -n "${GH_TOKEN:-}" ] || die "GH_TOKEN is not set — create a PAT (contents + pull requests) and put it in .env.agents"
export GH_TOKEN

gh auth status >/dev/null 2>&1 || die "gh rejected GH_TOKEN (gh auth status failed) — check the token scopes/expiry"
gh auth setup-git >/dev/null 2>&1 || die "gh auth setup-git failed"
say "gh authenticated as $(gh api user -q .login 2>/dev/null || echo '<unknown>')"

# ── 2. model health ──────────────────────────────────────────────────────────
say "checking model access (up to ${HEALTH_TIMEOUT}s)..."
HEALTH_OUT=""
if ! HEALTH_OUT="$(cd /tmp && timeout "$HEALTH_TIMEOUT" opencode run --auto 'Reply with exactly: ok' 2>&1)"; then
  die "opencode health check failed. Output:
$HEALTH_OUT
If you moved off the keyless provider, pass OPENAI_API_KEY / ANTHROPIC_API_KEY / OPENROUTER_API_KEY via .env.agents."
fi
say "model OK"

# ── 3. repo ──────────────────────────────────────────────────────────────────
if [ ! -d "$WORKSPACE/.git" ]; then
  say "cloning $REPO_URL into $WORKSPACE"
  git clone --quiet "$REPO_URL" "$WORKSPACE" || die "clone failed (token access to $REPO_SLUG?)"
else
  say "updating workspace clone"
  git -C "$WORKSPACE" remote set-url origin "$REPO_URL"
  git -C "$WORKSPACE" fetch --quiet origin --prune || die "git fetch failed"
  git -C "$WORKSPACE" checkout --quiet "$BASE" 2>/dev/null \
    || git -C "$WORKSPACE" checkout --quiet -B "$BASE" "origin/$BASE"
  git -C "$WORKSPACE" reset --hard --quiet "origin/$BASE" || die "cannot reset to origin/$BASE"
fi
git -C "$WORKSPACE" config user.name "${GIT_NAME:-Agent Orchestrator}"
git -C "$WORKSPACE" config user.email "${GIT_EMAIL:-agents@users.noreply.local}"
say "workspace at $(git -C "$WORKSPACE" rev-parse --short HEAD) ($BASE)"

# ── 4. dependencies (also warms the shared package store) ───────────────────────
if [ -z "${INSTALL_CMD+x}" ]; then
  if   [ -f "$WORKSPACE/pnpm-lock.yaml" ];    then INSTALL_CMD="pnpm install --frozen-lockfile --prefer-offline"
  elif [ -f "$WORKSPACE/yarn.lock" ];         then INSTALL_CMD="yarn install --frozen-lockfile"
  elif [ -f "$WORKSPACE/package-lock.json" ]; then INSTALL_CMD="npm ci"
  else INSTALL_CMD=""; fi
fi
if [ -n "$INSTALL_CMD" ]; then
  say "install: $INSTALL_CMD (warms the store for agent worktrees)"
  (cd "$WORKSPACE" && bash -c "$INSTALL_CMD" >/dev/null) || die "dependency install failed: $INSTALL_CMD"
else
  say "no INSTALL_CMD / lockfile detected — skipping dependency install"
fi

# ── 5. reconcile stale state from a previous run ──────────────────────────────
# A previous container's opencode processes never survive a restart, so any pid file whose
# process is not a live opencode (or whose pid is gone) is stale: drop its worktree, delete
# its branch when no PR references it, and clear its running-state record.
if [ -d "$AGENTS_DIR" ]; then
  say "reconciling stale agent state"
  rm -f "$AGENTS_DIR"/state/running-*.json 2>/dev/null || true
  for PID_FILE in "$AGENTS_DIR"/*.pid; do
    [ -e "$PID_FILE" ] || continue
    NAME="$(basename "$PID_FILE" .pid)"
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null \
       && ps -p "$PID" -o args= 2>/dev/null | grep -q opencode; then
      say "agent '$NAME' still running (pid $PID) — leaving it alone"
      continue
    fi
    rm -f "$PID_FILE"
    if [ -d "$WORKSPACE/.worktrees/$NAME" ]; then
      git -C "$WORKSPACE" worktree remove --force "$WORKSPACE/.worktrees/$NAME" 2>/dev/null \
        || rm -rf "$WORKSPACE/.worktrees/$NAME"
    fi
    git -C "$WORKSPACE" worktree prune 2>/dev/null || true
    if git -C "$WORKSPACE" show-ref --verify --quiet "refs/heads/$NAME"; then
      if git -C "$WORKSPACE" ls-remote --exit-code --heads origin "$NAME" >/dev/null 2>&1 \
         && ! gh pr view "$NAME" --json state -q .state >/dev/null 2>&1; then
        say "  deleting pushed branch '$NAME' (no PR references it)"
        git -C "$WORKSPACE" push --quiet origin --delete "$NAME" >/dev/null 2>&1 || true
      fi
      git -C "$WORKSPACE" branch -D "$NAME" >/dev/null 2>&1 || true
    fi
    say "  cleaned up stale agent '$NAME'"
  done
fi

# ── 6. ready scan, then hand over ────────────────────────────────────────────
say "ready tickets${SERIES:+ (series=$SERIES)}${TICKETS:+ (tickets=$TICKETS)}:"
SCAN="$WORKSPACE/.agent-kit/orchestrator/next-tickets.py"
[ -f "$SCAN" ] || SCAN=/boot/next-tickets.py
READY="$(cd "$WORKSPACE" && python3 "$SCAN")" || die "ticket scan failed"
printf '%s\n' "$READY"
READY_COUNT="$(printf '%s\n' "$READY" | grep -c . || true)"
say "$READY_COUNT ready"

if [ "$DRY_RUN" = "1" ]; then
  say "DRY_RUN=1 — exiting before the supervisor"
  exit 0
fi

SUPERVISOR="$WORKSPACE/.agent-kit/orchestrator/supervisor.sh"
[ -f "$SUPERVISOR" ] || SUPERVISOR=/boot/supervisor.sh
say "starting supervisor (MAX_PARALLEL=${MAX_PARALLEL:-2}, MAX_ATTEMPTS=${MAX_ATTEMPTS:-3})"
exec bash "$SUPERVISOR"
