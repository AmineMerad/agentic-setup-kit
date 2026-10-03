# Agentic Setup Kit

Guardrails for repos where **AI agents write most of the code** — one shared implementation, mirrored in
**Claude Code** and **opencode**.

When several agents work in one checkout, the same things go wrong: code is declared "done" without
being typechecked or tested, agents `git stash` / `reset --hard` away each other's work, and the
instruction files each harness reads quietly drift apart. This kit makes those failure modes
mechanical instead of advisory.

## What you get

| Guardrail | How it works | Claude Code | opencode |
|---|---|---|---|
| **Verify before "done"** | Runs the checks you configure for the files the agent changed; failures go back to the agent | `Stop` hook | plugin on `session.idle` |
| **No destructive git** | Denies `stash`, `reset --hard`, `clean`, `checkout --`, force-push | `permissions.deny` | `permission.bash` |
| **One instruction source** | `AGENTS.md` holds the facts; `CLAUDE.md` is a 10-line file that imports it | `@AGENTS.md` | reads `AGENTS.md` natively |
| **Terse, safe global rules** *(opt-in)* | Search-before-read, smallest diff, narrow tests first, shared-checkout rules | `~/.claude/CLAUDE.md` | `~/.config/opencode/AGENTS.md` |
| **Portable Obsidian MCP** *(opt-in)* | Launcher resolves the vault from its own path — no hard-coded absolute paths | `.mcp.json` | `opencode.json` |

The verification script is stack-agnostic: it only knows about **globs and shell commands** from
`.agent-kit/verify.json`.

## Install

Requires `git` and `python3` (stdlib only). Node is only needed for the opencode plugin to run.

```bash
git clone <this repo> agentic-setup-kit
python3 agentic-setup-kit/install.py /path/to/your/repo --dry-run   # see what would change
python3 agentic-setup-kit/install.py /path/to/your/repo             # install
```

Flags: `--dry-run`, `--force` (overwrite files; JSON configs are always merged, never replaced),
`--with-obsidian`, `--global-rules`.

The installer is idempotent and conservative: it never overwrites an existing file without `--force`,
**merges** into an existing `.claude/settings.json` / `opencode.json` (your values win), refuses to touch
a JSON file it can't parse, and keeps your existing opencode `permission.bash` rules first with its deny
rules appended last. Then restart Claude Code / opencode so the hook, plugin and permissions load.

## Configure the checks — `.agent-kit/verify.json`

```json
{
  "checks": [
    { "name": "typecheck",     "when": ["**/*.ts", "**/*.tsx"], "run": "pnpm run -s typecheck" },
    { "name": "related tests", "when": ["**/*.ts", "**/*.tsx"], "run": "pnpm run -s test:related -- {files}" }
  ]
}
```

| Field | Meaning |
|---|---|
| `name` | Label in the failure report. |
| `when` | Glob or list of globs (`**` supported). The check runs only if a changed file matches. Default: everything. |
| `run` | Shell command. Non-zero exit = failure. |
| `dir` | Directory to run in, relative to the repo root (default `.`). |
| `{files}` | Optional placeholder in `run`: the changed files that matched `when`, **relative to `dir`**, shell-quoted. Deleted files are excluded; if nothing is left, the check is skipped. |
| `timeout` | Seconds (default 300). A timeout counts as a failure. |

`install.py` auto-detects a root `package.json` (`typecheck`, `test:related` scripts; npm/pnpm/yarn by
lockfile). Anything else gets an empty config — fill it in. Examples (adapt to your tooling):

```jsonc
// monorepo
{ "name": "api typecheck", "dir": "api", "when": "api/**/*.ts", "run": "npm run -s typecheck" },
{ "name": "api tests",     "dir": "api", "when": "api/**/*.ts", "run": "npm run -s test:related -- {files}" }

// python
{ "name": "ruff",   "when": "**/*.py", "run": "ruff check {files}" },
{ "name": "pytest", "when": "**/*.py", "run": "python3 -m pytest -q -x" }

// go
{ "name": "vet",  "when": "**/*.go", "run": "go vet ./..." },
{ "name": "test", "when": "**/*.go", "run": "go test ./..." }
```

Keep the checks **fast** (seconds). They run when an agent finishes a turn that changed matching files.

## How it behaves

- **Cheap.** After a pass it stores a content hash (in the system temp dir, not your repo); turns that
  changed nothing relevant skip everything (~0.15 s).
- **Never loops.** The Claude hook honours `stop_hook_active`. The opencode plugin sends an identical
  failure only once, caps feedback at 3 per session, and ignores subagent sessions.
- **Fails open.** No config, not a git repo, or an internal error ⇒ the agent is never blocked
  (exit 1 is non-blocking for Claude Code).
- **Honest about shared checkouts.** The failure text tells the agent that a failure in a file it did
  not change is another agent's in-flight work: report it, don't "fix" it.

Exit codes of `scripts/agent-verify.sh`: `0` pass/nothing to do · `2` check failed (report on stderr) · `1` internal error.

### Gating a headless runner

`opencode run` exits the instant the session goes idle — before an async plugin check can finish — so
the plugin cannot gate headless runs. Call the script from whatever step *publishes* the work instead
(a finish script, a CI job, a wrapper around `git commit`):

```bash
printf '{"cwd":"%s"}' "$PWD" | scripts/agent-verify.sh || { echo "fix and retry"; exit 1; }
```

## Optional modules

- `--with-obsidian` — `scripts/mcp-obsidian.sh` resolves `<repo>/vault` from its own location (the
  `obsidian-mcp` server requires an absolute path, which is why configs hard-code one and then break on
  every other machine), and both harness configs call it. It also creates `vault/.obsidian/` if missing
  (the server refuses a vault without it). `scripts/mcp-check.sh` launches each config's declared command
  and completes a real MCP handshake.
- `--global-rules` — writes `payload/global-rules.md` to `~/.claude/CLAUDE.md` and
  `~/.config/opencode/AGENTS.md` **only if they don't exist**. Edit one, copy to the other.

## Limits (read these)

- The opencode plugin can only react **after** an assistant turn ends; it cannot veto it (Claude Code's
  Stop hook can).
- The check is repo-wide: in a *shared* checkout, one agent's broken file can fail another agent's
  turn. Use git worktrees for parallel agents; the failure message already tells agents not to fix
  files they didn't touch.
- Permission patterns are best-effort guardrails, not a security boundary.
- Auto-detection covers only a root-level Node project; everything else needs a hand-written config.
- Not included (yet): a worktree-per-agent launcher, PR/finish automation, a dev-server-per-worktree helper.

## More

- [`docs/DESIGN.md`](docs/DESIGN.md) — why it is built this way.
- [`docs/TEST-SUITE-TIPS.md`](docs/TEST-SUITE-TIPS.md) — making the test suite cheap enough that agents actually run it.

## Develop / test

```bash
python3 -m unittest discover -s tests -v   # verify script, installer merges, opencode plugin
```

## License

Not chosen yet — add a `LICENSE` file before publishing or selling.
