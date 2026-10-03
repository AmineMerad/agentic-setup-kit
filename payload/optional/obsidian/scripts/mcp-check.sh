#!/usr/bin/env bash
# Verify the project MCP servers actually start and speak MCP, using each harness's OWN config:
#   .mcp.json (Claude Code)  and  opencode.json (opencode)
# Launches the declared command from the repo root, does initialize + tools/list over stdio.
#   scripts/mcp-check.sh            # run from anywhere
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec python3 - "$ROOT" <<'PY'
import json, subprocess, sys, os, threading, queue

root = sys.argv[1]
def servers():
    for name, s in json.load(open(".mcp.json"))["mcpServers"].items():
        yield ("claude-code .mcp.json", name, [s["command"], *s.get("args", [])])
    for name, s in json.load(open("opencode.json")).get("mcp", {}).items():
        if s.get("type") == "local" and s.get("enabled", True):
            yield ("opencode opencode.json", name, s["command"])

def rpc(proc, q, msg, want_id=None, timeout=60):
    proc.stdin.write((json.dumps(msg) + "\n").encode()); proc.stdin.flush()
    if want_id is None: return None
    while True:
        try: line = q.get(timeout=timeout)
        except queue.Empty: raise TimeoutError(f"no reply to id={want_id}")
        if line is None: raise RuntimeError("server exited")
        try: m = json.loads(line)
        except ValueError: continue
        if m.get("id") == want_id: return m

failed = 0
for harness, name, cmd in servers():
    p = subprocess.Popen(cmd, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    q = queue.Queue()
    threading.Thread(target=lambda: ([q.put(l) for l in p.stdout], q.put(None)), daemon=True).start()
    try:
        init = rpc(p, q, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mcp-check", "version": "0"}}}, 1)
        info = init["result"]["serverInfo"]
        rpc(p, q, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = rpc(p, q, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 2)["result"]["tools"]
        print(f"OK    {harness:24} {name}: {info['name']} {info.get('version','')} — {len(tools)} tools")
    except Exception as e:
        failed += 1
        err = p.stderr.read1(2000).decode(errors="replace") if p.poll() is not None else ""
        print(f"FAIL  {harness:24} {name}: {e} {err}")
    finally:
        p.kill()
sys.exit(1 if failed else 0)
PY
