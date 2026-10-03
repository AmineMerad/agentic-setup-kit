# Agent rules

## Talk
- Terse. No preamble, no recap, no restating the task.
- Explain only non-obvious decisions.
- Commit messages, PR bodies, and code comments: clear normal English.

## Navigate
- Search before reading: grep/rg for symbols, then read only the relevant line ranges.
- Never read large files (>500 lines) whole; use offset/limit.
- Batch independent searches/reads in one turn.
- Learn commands from the repo (README, Makefile, package.json, pyproject.toml, CI config) before guessing.

## Change
- Smallest diff that solves the task. No drive-by refactors, renames, or reformatting.
- Follow the conventions in surrounding code.
- No new dependencies, config changes, or public API changes without asking.
- Unclear requirement or multiple valid approaches: ask once, briefly, before coding.

## Verify
- Run the narrowest relevant tests first (single test/file, fail-fast); prefer the project's documented "related/changed" command.
- Run the project's linter/type-checker for what you changed.
- The full suite is the last gate of a large PR, not a per-change check. Run it once at the end; skip it for small PRs.
- Never say "done" without a green run; if you couldn't test, say so and why.

## Shared checkout
- Other agents may be editing the same checkout. Do not `git stash`, `git checkout`, or `git reset` to compare against a clean tree; use a separate worktree.
- A failure in a file you did not touch is someone else's in-flight work: report it, do not fix it.

## Finish
- ≤5 lines: files changed, commands run + results, risks/follow-ups.
