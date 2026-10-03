#!/usr/bin/env bash
# Obsidian MCP server for THIS repo's vault — the single definition both agent harnesses use:
#   Claude Code -> .mcp.json        opencode -> opencode.json
# obsidian-mcp needs an ABSOLUTE vault path, which is why configs used to hardcode one
# (/Users/<someone>/...). Resolving it from this script's own location makes the config portable
# across machines, and inside a git worktree it serves that worktree's own vault/.
set -euo pipefail

# GUI-launched editors often start with a minimal PATH; make node/npx findable.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT="$ROOT/vault"
[ -d "$VAULT" ] || { echo "mcp-obsidian: vault not found at $VAULT" >&2; exit 1; }

# stdout is the MCP channel — nothing else may print to it.
exec npx -y obsidian-mcp@2 serve --vault "vault=$VAULT"
