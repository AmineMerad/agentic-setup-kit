# __REPO_NAME__

`AGENTS.md` is the single source of truth for stack, commands and conventions. Do not duplicate
its content here — Claude Code reads this file, and the import below pulls in the rest.

@AGENTS.md

## How to work here
- Several agents may share this checkout. Do not `git stash` / `git reset --hard` / `git clean`
  (denied in `.claude/settings.json`). Use a git worktree for parallel work.
- A Stop hook (`scripts/agent-verify.sh`) runs the checks in `.agent-kit/verify.json` for the
  files you changed and sends failures back to you. A failure in a file you did not touch is
  another agent's in-flight work: report it, do not fix it.
