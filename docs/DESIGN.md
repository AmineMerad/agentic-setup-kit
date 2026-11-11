# Design notes

## One implementation, thin adapters
The checks live in one script (`agent-verify.py`) configured by one file (`.agent-kit/verify.json`). Each
harness gets a few lines of adapter: a Claude Code `Stop` hook, an opencode plugin. Policy changes in one
place; the harnesses cannot drift. The same rule applies to permissions (one list, two syntaxes) and to
instructions (`AGENTS.md` is the source, `CLAUDE.md` imports it).

Why: in practice the two instruction files disagreed within days, and a stale `CLAUDE.md` misled every
Claude session. A 10-line importer cannot go stale.

## Check once at "stop", not on every edit
A per-edit hook (typecheck after each `Edit`) costs seconds per edit and mostly reports half-finished
states. Checking when the agent says it is done is where a failure is actionable. To keep that cheap:

- the script stores a hash of the matched files after a pass and skips when unchanged, so read-only turns
  cost ~0;
- checks are scoped by glob, so editing a markdown file triggers nothing.

## Never trap an agent
- Claude Code passes `stop_hook_active` when the hook already blocked this turn; the script returns 0
  immediately, so a failing check cannot create an endless loop.
- The opencode plugin dedupes identical failures per session and caps re-prompts at 3.
- No config, not a git repo, or an exception in the script ⇒ the agent is not blocked.

## Why a plugin can't gate headless opencode
`opencode run` terminates when the session goes idle. A plugin that reacts to `session.idle` and starts an
async check races the exit and loses (observed: the event arrives, the process is gone before the check
finishes). In TUI/server mode the process stays alive and the plugin works. For headless runs, put the
check in the step that publishes the work.

## Shared checkouts
Several agents editing one working tree is the norm. Two consequences baked in:

1. `git stash` / `reset --hard` / `clean` / `checkout --` / force-push are denied. "Compare against a clean
   tree" tricks silently destroy other agents' uncommitted work; use a worktree.
2. A verification failure may belong to someone else. The failure text says so explicitly and tells the
   agent to report rather than fix. Isolated worktrees don't have this problem, which is the argument for
   one worktree per agent.

## Permission ordering in opencode
`permission.bash` rules are order-sensitive (later rules win). The installer therefore keeps a user's
existing rules first and appends the deny rules last, and never adds a catch-all `"*": "allow"` on top of
rules the user already wrote — doing so would override their denies.

## The MCP launcher
`obsidian-mcp` requires an **absolute** vault path. Hard-coding it breaks every other machine and every git
worktree. A launcher script that derives `<repo>/vault` from `$0` makes both harness configs path-free, and
inside a worktree it serves that worktree's own vault. Personal/authenticated MCP servers belong in user-level
config, not the repo.

## Judgement vs determinism in the ticket runner
Launching, monitoring, retrying and PR repair are pure bash/python (`supervisor.sh`, `next-tickets.py`,
`state.py`): they must not hallucinate and are unit-testable. An LLM planner is consulted only for two
judgement calls — writing a missing task file, and triaging a parked ticket — through a fixed reply contract
(`TASKFILES_WRITTEN:` / `NOTHING_TO_DO`). Success is detected from the worker log (`✅ finished:` + `PR: http…`),
the same contract a human-launched agent produces, so manual and unattended runs share one code path.

## Dependencies unblock on merge, not on PR
A ticket's `depends_on` must be `status: done` on the *base branch*. Workers flip `status: done` in their own
PR, so a dependent ticket cannot start until that PR is merged. Slower than chaining branches, but each agent
starts from code a human has already accepted.

## Everything under `.agent-kit/`
The ported harness originally lived in `.opencode/agents` and `scripts/orchestrator`. The kit keeps all of its
files under `.agent-kit/` (plus `scripts/agent-verify.*`) so it is one directory to review, update or delete,
and nothing collides with a repo's own `scripts/` or opencode config. Paths are resolved relative to the
script's own location or the repo root, never hard-coded to a project.
