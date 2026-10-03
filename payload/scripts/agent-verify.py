#!/usr/bin/env python3
"""Verify what an AI agent changed before it declares "done".

Driven by .agent-kit/verify.json (see README). For every check whose `when` globs match a
changed file (modified, added, renamed or untracked per `git status`), runs `run` in `dir`.

Contract (used by the Claude Code Stop hook, the opencode plugin and finish scripts):
  stdin   optional JSON: {"cwd": "...", "stop_hook_active": bool}
  exit 0  pass, nothing to check, or nothing changed since the last pass
  exit 2  a check failed; the report is on stderr (feed it back to the agent)
  exit 1  internal error in this script (non-blocking for Claude Code)

Design notes:
  * A content hash of the matched files is stored (outside the repo) after a pass, so turns
    that changed nothing cost ~0 s.
  * `stop_hook_active` => exit 0 immediately: a hook must never loop.
  * Fails open when there is no config / not a git repo.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

CONFIG_REL = os.path.join(".agent-kit", "verify.json")
TAIL_LINES = 40


def glob_to_regex(pattern):
    """Glob with ** support -> anchored regex. `**/` matches zero or more directories."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)


def changed_files(root):
    """-> (existing_paths, deleted_paths) relative to the repo root."""
    res = subprocess.run(
        ["git", "-C", root, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True, text=True,
    )
    entries = res.stdout.split("\0")
    existing, deleted, i = [], [], 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        xy, path = e[:2], e[3:]
        if "R" in xy or "C" in xy:
            i += 1  # the following entry is the rename/copy source
        (deleted if "D" in xy else existing).append(path)
    return existing, deleted


def file_digest(path):
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        h.update(b"<unreadable>")
    return h.hexdigest()


def main():
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if payload.get("stop_hook_active"):
        return 0

    start = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    top = git(start, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return 0
    root = top.stdout.strip()

    cfg_path = os.path.join(root, CONFIG_REL)
    if not os.path.isfile(cfg_path):
        return 0
    with open(cfg_path) as f:
        cfg_text = f.read()
    checks = json.loads(cfg_text).get("checks", [])

    existing, deleted = changed_files(root)
    everything = existing + deleted
    if not everything:
        return 0

    plan = []  # (check, matched_existing_files, any_match)
    for chk in checks:
        globs = chk.get("when", ["**/*"])
        globs = [globs] if isinstance(globs, str) else globs
        regexes = [glob_to_regex(g) for g in globs]
        any_match = any(r.match(p) for p in everything for r in regexes)
        files = [p for p in existing if any(r.match(p) for r in regexes)]
        if any_match:
            plan.append((chk, files))
    if not plan:
        return 0

    # Skip when nothing relevant changed since the last pass.
    digest = hashlib.sha1(cfg_text.encode())
    for p in sorted({p for _, files in plan for p in files}):
        digest.update(p.encode() + b"\0" + file_digest(os.path.join(root, p)).encode())
    digest.update(("\0".join(sorted(deleted))).encode())
    state_dir = os.path.join(tempfile.gettempdir(), "agent-kit-verify")
    os.makedirs(state_dir, exist_ok=True)
    state_file = os.path.join(state_dir, hashlib.sha1(root.encode()).hexdigest()[:12])
    try:
        with open(state_file) as f:
            if f.read() == digest.hexdigest():
                return 0
    except OSError:
        pass

    failures = []
    for chk, files in plan:
        sub = chk.get("dir", ".")
        workdir = os.path.normpath(os.path.join(root, sub))
        cmd = chk["run"]
        if "{files}" in cmd:
            prefix = "" if sub in (".", "") else sub.rstrip("/") + "/"
            rel = [p[len(prefix):] for p in files if p.startswith(prefix)]
            if not rel:
                continue  # only deletions matched: nothing to pass to the command
            cmd = cmd.replace("{files}", " ".join(shlex.quote(p) for p in rel))
        try:
            r = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True, text=True,
                               timeout=chk.get("timeout", 300))
            ok, out = r.returncode == 0, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            ok, out = False, "timed out after %ss" % chk.get("timeout", 300)
        if not ok:
            tail = "\n".join(out.strip().splitlines()[-TAIL_LINES:])
            failures.append("── %s FAILED ──\n%s\n" % (chk.get("name", cmd), tail))

    if failures:
        sys.stderr.write(
            "Verification failed — fix before finishing.\n"
            "If a failure is in a file you did NOT change, another agent's in-flight work is the "
            "cause: say so in your reply and do not edit that file.\n\n" + "\n".join(failures)
        )
        return 2

    with open(state_file, "w") as f:
        f.write(digest.hexdigest())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let a bug here block an agent
        sys.stderr.write("agent-verify internal error: %r\n" % (exc,))
        sys.exit(1)
