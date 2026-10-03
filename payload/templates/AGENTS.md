# AGENTS.md — __REPO_NAME__

> Single source of truth for AI agents (and humans): stack, commands, conventions. Keep it true;
> `CLAUDE.md` imports this file, opencode reads it natively. Replace every TODO.

## Overview
TODO: one paragraph — what the product is and who uses it.

## Stack
TODO: languages, frameworks, datastore.

## Commands
```bash
# TODO: the real commands, copied from the repo (install, dev, build, typecheck, test, lint)
```

## Testing
- Run only the tests related to what you changed; the full suite is the last gate of a large PR.
- TODO: how to run a single test file, and any quirks (services needed, env vars, ports).

## Conventions
TODO: naming, error handling, response shapes, directory layout — whatever agents get wrong.

## Verification (automatic)
`.agent-kit/verify.json` lists the checks run for changed files (Claude Code Stop hook, opencode
plugin). Edit it when commands or package layout change.

## Parallel agents
Agents sharing one checkout must not stash/reset/clean. Use `git worktree add` for parallel work.
