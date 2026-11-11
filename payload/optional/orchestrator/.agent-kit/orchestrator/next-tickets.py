#!/usr/bin/env python3
"""next-tickets.py — deterministic scanner for the next ready tickets.

Reads $TICKETS_DIR (default vault/tickets)/*.md frontmatter (status / depends_on / series / files), falls back to
old-format "- **Depends on:** X, Y" body lines, and prints the tickets that are ready to be
worked on. No LLM involved: the orchestrator agent only decides *which* of these to run and
writes missing task files.

Usage (from the repo root):
    python3 next-tickets.py              # one ticket id per line, ordered
    python3 next-tickets.py --json       # one JSON object per line (id, series, files, …)

Env:
    SERIES=design-language,supplements   # only these series (ignored when TICKETS is set)
    TICKETS=DL-12,S-06                   # only these ids (dependency checks still apply)
    TICKETS_DIR=vault/tickets            # where the ticket markdown files live (repo-relative)

Excluded from the ready set (already in flight / done elsewhere):
    - tickets parked by the supervisor        (.agent-kit/agents/state/parked-<id>.md)
    - tickets with a running agent            (.agent-kit/agents/state/running-*.json)
    - tickets already finished by an agent    (.agent-kit/agents/state/done-<id>.md)
    - tickets whose branch already exists locally or on origin
    - tickets referenced by an open PR (id in the PR title/body/head branch)
"""
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.getcwd()
TICKETS_DIR = os.path.join(ROOT, os.environ.get("TICKETS_DIR", "vault/tickets"))
AGENTS_DIR = os.path.join(ROOT, ".agent-kit", "agents")
STATE_DIR = os.path.join(AGENTS_DIR, "state")

LIST_KEYS = {"depends_on", "blocks", "files", "assertions"}
PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


def fm_block(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    return m.group(1) if m else None


def parse_frontmatter(fm):
    """Parse the flat subset of YAML the tickets use (scalars + inline/block string lists)."""
    data = {}
    lines = fm.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m or line.startswith((" ", "\t")):
            continue
        key, val = m.group(1), m.group(2).strip()
        if key in LIST_KEYS:
            items = []
            if val:
                for part in val.strip("[]").split(","):
                    part = part.strip().strip("'\"")
                    if part:
                        items.append(part)
            else:
                while i < len(lines) and re.match(r"^\s+-\s+", lines[i]):
                    items.append(re.sub(r"^\s+-\s+", "", lines[i]).strip().strip("'\""))
                    i += 1
            data[key] = items
        else:
            data[key] = val.strip("'\"")
    return data


def body_deps(text, self_id):
    """Old-format tickets declare deps in the body: '- **Depends on:** T-01, T-02'."""
    for line in text.split("\n")[:100]:
        if re.match(r"^\s*[-*>#\s]*\*{0,2}depends on\*{0,2}\s*:?\s*", line, re.I):
            return [d for d in re.findall(r"\b[A-Z][A-Z0-9]{0,5}-\d+\b", line) if d != self_id]
    return []


def load_tickets():
    tickets = {}
    for path in glob.glob(os.path.join(TICKETS_DIR, "*.md")):
        text = open(path, encoding="utf-8").read()
        fm = fm_block(text)
        if not fm:
            continue
        data = parse_frontmatter(fm)
        if data.get("type") == "sprint-plan":
            continue
        tid = data.get("id") or os.path.basename(path)[:-3]
        if not re.match(r"^[A-Z][A-Z0-9]*-\d+$", tid):
            continue
        data["_path"] = path
        data["_deps"] = data.get("depends_on", []) or body_deps(text, tid)
        tickets[tid] = data
    return tickets


def state_files(prefix):
    out = {}
    for path in glob.glob(os.path.join(STATE_DIR, f"{prefix}-*")):
        base = os.path.basename(path)
        out[base[len(prefix) + 1:].rsplit(".", 1)[0].upper()] = path
    return out


def running_tickets():
    ids = set()
    for path in glob.glob(os.path.join(STATE_DIR, "running-*.json")):
        try:
            ids.add(json.load(open(path)).get("ticket", "").upper())
        except (ValueError, OSError):
            pass
    return ids


def task_file_for(tid):
    """Task files are named after the *agent* name, not the ticket id, and the mapping is not
    uniform: F-04 → fin-f04-task.txt, S-02 → s-02-task.txt, PH-04 → ph-04-task.txt. Exact id
    match first, then any '<prefix>-<id>-task.txt' compared with punctuation stripped."""
    low = tid.lower()
    for cand in (os.path.join(AGENTS_DIR, f"{low}-task.txt"),
                 os.path.join(AGENTS_DIR, "tasks", f"{low}-task.txt")):
        if os.path.exists(cand):
            return cand
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s)
    matches = [p for p in glob.glob(os.path.join(AGENTS_DIR, "*-task.txt")) +
               glob.glob(os.path.join(AGENTS_DIR, "tasks", "*-task.txt"))
               if norm(os.path.basename(p)[:-len("-task.txt")]).endswith(norm(low))]
    return sorted(matches, key=len)[0] if matches else None


def agent_name_for(tid):
    """Prefer the name baked into the task file's finish line (e.g. fin-f04 for F-04)."""
    path = task_file_for(tid)
    if path:
        m = re.search(r"agent-finish\.sh\s+(\S+)\s+\S+", open(path, encoding="utf-8").read())
        if m:
            return m.group(1)
    return tid.lower()


def local_branch_exists(name):
    for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
        if subprocess.run(["git", "-C", ROOT, "show-ref", "--verify", "--quiet", ref]).returncode == 0:
            return True
    return False


def open_pr_text():
    """One gh call: title+body+head branch of every open PR, or '' if gh is unusable."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", "200",
             "--json", "title,body,headRefName"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        print(f"# warning: gh pr list failed ({err}) — skipping open-PR exclusion", file=sys.stderr)
        return ""
    if out.returncode != 0:
        print(f"# warning: gh pr list failed ({out.stderr.strip()}) — skipping open-PR exclusion",
              file=sys.stderr)
        return ""
    return out.stdout


def main():
    as_json = "--json" in sys.argv[1:]
    tickets = load_tickets()
    parked = state_files("parked")
    done = state_files("done")
    running = running_tickets()
    prs = open_pr_text()

    series_filter = [s.strip() for s in os.environ.get("SERIES", "").split(",") if s.strip()]
    id_filter = [t.strip().upper() for t in os.environ.get("TICKETS", "").split(",") if t.strip()]

    ready = []
    for tid, t in tickets.items():
        if t.get("status") != "todo":
            continue
        if id_filter and tid not in id_filter:
            continue
        if not id_filter and series_filter and t.get("series", "") not in series_filter:
            continue
        if tid in parked or tid in done or tid in running:
            continue
        deps = t.get("_deps") or []
        if any(d not in tickets or tickets[d].get("status") != "done" for d in deps):
            continue
        name = agent_name_for(tid)
        if local_branch_exists(name):
            continue
        if prs and re.search(rf"\b{re.escape(tid)}\b", prs):
            continue
        ready.append((t, name))

    ready.sort(key=lambda p: (PRIORITY_ORDER.get(p[0].get("priority", ""), 3),
                              int(p[0]["phase"]) if str(p[0].get("phase", "")).isdigit() else 99,
                              p[0]["id"]))
    for t, name in ready:
        if as_json:
            print(json.dumps({
                "id": t["id"], "name": name, "series": t.get("series", ""),
                "priority": t.get("priority", ""), "phase": t.get("phase", ""),
                "files": t.get("files", []), "depends_on": t.get("_deps", []),
                "task_file": task_file_for(t["id"]),
            }))
        else:
            print(t["id"])


if __name__ == "__main__":
    main()
