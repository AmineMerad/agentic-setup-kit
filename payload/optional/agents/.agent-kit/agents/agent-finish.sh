#!/usr/bin/env bash
# agent-finish.sh — finalize an isolated-worktree agent session.
# Commits the worktree's own changes, pushes the agent branch, opens/updates a PR
# against the base branch, removes the worktree from the main clone, and prints the
# PR URL. Run FROM INSIDE the agent's worktree. The session ends after this prints.
#
# Usage: bash .agent-kit/agents/agent-finish.sh <name> <base> [commit-title]
set -u

NAME="${1:-}"
BASE="${2:-}"
TITLE="${3:-feat: $NAME}"
if [ -z "$NAME" ] || [ -z "$BASE" ]; then
  echo "usage: agent-finish.sh <name> <base-branch> [commit-title]"
  exit 1
fi

# Sanity: we must be inside a git worktree (git-common-dir differs from the worktree toplevel).
BRANCH="$NAME"
WT_REL=".worktrees/$NAME"
WT_TOP="$(git rev-parse --show-toplevel 2>/dev/null)"
COMMON="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
MAIN_ROOT="$(dirname "$COMMON" 2>/dev/null)"
if [ -z "$WT_TOP" ] || [ -z "$MAIN_ROOT" ] || [ "$WT_TOP/_" = "$MAIN_ROOT/_" ]; then
  echo "✗ agent-finish must run inside the agent's worktree (.worktrees/$NAME)"
  exit 1
fi

# 0) Verify before anything is committed/pushed/removed (same check as the Claude Code Stop hook
#    and the opencode agent-verify plugin). A worktree is isolated, so a failure here is this
#    agent's own: fix it and re-run. Skipped for worktrees whose base predates the script.
#    Human override for emergencies: SKIP_VERIFY=1.
if [ "${SKIP_VERIFY:-0}" != "1" ] && [ -f "$WT_TOP/scripts/agent-verify.sh" ]; then
  VERIFY_ERR="$(printf '{"cwd":"%s","stop_hook_active":false}' "$WT_TOP" | CLAUDE_PROJECT_DIR="$WT_TOP" bash "$WT_TOP/scripts/agent-verify.sh" 2>&1 >/dev/null)"
  if [ $? -ne 0 ]; then
    echo "✗ verification failed — nothing committed, pushed or removed. Fix, then re-run agent-finish.sh."
    printf '%s\n' "$VERIFY_ERR"
    exit 1
  fi
fi

# 1) Commit only this worktree's changes.
git add -A
if git diff --cached --quiet; then
  echo "(nothing to commit — no changes staged in this worktree)"
else
  git commit -q -m "$TITLE" || { echo "✗ commit failed"; exit 1; }
fi

# 2) Push the agent branch.
git push -q -u origin "$BRANCH" || { echo "✗ push failed for branch $BRANCH"; exit 1; }

# 3) Create or update the PR against BASE.
if gh pr view "$BRANCH" --json url -q .url >/dev/null 2>&1; then
  PR_URL="$(gh pr view "$BRANCH" --json url -q .url)"
  gh pr edit "$BRANCH" --base "$BASE" >/dev/null 2>&1 || true
else
  BODY="Auto-PR from isolated worktree agent \`$NAME\` (branch \`$BRANCH\`, base \`$BASE\`).
Finished via \`agent-finish.sh\` — worktree already deleted."
  PR_URL="$(gh pr create --base "$BASE" --head "$BRANCH" --title "$TITLE" --body "$BODY" 2>&1)" || PR_URL="✗ PR create failed: $PR_URL"
fi

# 4) Remove the worktree from the main clone (we are inside it, so worktree commands come from MAIN_ROOT).
git -C "$MAIN_ROOT" worktree remove --force "$WT_REL" 2>/dev/null
git -C "$MAIN_ROOT" worktree prune 2>/dev/null

echo "======================================================"
echo "✅ finished: agent=$NAME  branch=$BRANCH  base=$BASE"
echo "   PR: $PR_URL"
echo "   worktree removed: $WT_REL"
echo "   session ending — stop working now."
echo "======================================================"