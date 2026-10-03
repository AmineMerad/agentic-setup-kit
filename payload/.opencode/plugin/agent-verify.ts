// opencode counterpart of the Claude Code Stop hook (.claude/settings.json).
//
// Both run scripts/agent-verify.sh, so the checks (configured in .agent-kit/verify.json)
// are defined once. opencode has no "block the stop" hook, so on
// `session.idle` we run the script and, if it fails (exit 2), post its output back into the
// session as a new user message, which makes the agent continue and fix it.
//
// Safety rails: subagent sessions are skipped; an identical failure is sent only once; at most
// MAX_REPROMPTS failures are fed back per session before giving up (no endless loops).
import type { Plugin } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"
import { createHash } from "node:crypto"
import { join } from "node:path"

const MAX_REPROMPTS = 3
const TIMEOUT_MS = 240_000

type Result = { status: number | null; stderr: string }

const runVerify = (root: string): Promise<Result> =>
  new Promise((resolve) => {
    const child = spawn("bash", [join(root, "scripts", "agent-verify.sh")], {
      cwd: root,
      env: { ...process.env, CLAUDE_PROJECT_DIR: root },
    })
    let stderr = ""
    child.stderr.on("data", (d) => (stderr += d))
    child.stdin.end(JSON.stringify({ cwd: root, stop_hook_active: false }))
    const timer = setTimeout(() => child.kill(), TIMEOUT_MS)
    child.on("close", (status) => {
      clearTimeout(timer)
      resolve({ status, stderr })
    })
    child.on("error", () => {
      clearTimeout(timer)
      resolve({ status: 0, stderr: "" }) // script missing (e.g. old worktree): fail open
    })
  })

export const AgentVerify: Plugin = async ({ client, directory, worktree }) => {
  const root = worktree && worktree !== "/" ? worktree : directory
  const lastFailure = new Map<string, string>() // sessionID -> hash of last failure sent
  const reprompts = new Map<string, number>()
  const running = new Set<string>()

  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle") return
      const sessionID = event.properties.sessionID
      if (running.has(sessionID)) return

      running.add(sessionID)
      let result: Result
      try {
        // Subagent (child) sessions end mid-task; only verify the top-level session.
        const session = await client.session.get({ path: { id: sessionID } }).catch(() => undefined)
        if (session?.data?.parentID) return
        result = await runVerify(root)
      } finally {
        running.delete(sessionID)
      }

      if (result.status !== 2) {
        lastFailure.delete(sessionID)
        reprompts.delete(sessionID)
        return
      }

      const hash = createHash("sha1").update(result.stderr).digest("hex")
      const count = reprompts.get(sessionID) ?? 0
      if (lastFailure.get(sessionID) === hash || count >= MAX_REPROMPTS) return
      lastFailure.set(sessionID, hash)
      reprompts.set(sessionID, count + 1)

      await client.session
        .prompt({ path: { id: sessionID }, body: { parts: [{ type: "text", text: result.stderr }] } })
        .catch(() => undefined)
    },
  }
}
