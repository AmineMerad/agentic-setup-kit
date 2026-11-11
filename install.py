#!/usr/bin/env python3
"""Install the agentic setup kit into a git repository.

    python3 install.py [TARGET] [--dry-run] [--force] [--with-obsidian] [--global-rules]
                       [--with-agents] [--with-orchestrator]

Idempotent. Never overwrites an existing file unless --force; JSON config files are *merged*
(your existing settings win; missing keys are added). Stdlib only.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys

KIT = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.join(KIT, "payload")
DENY_MARK = "agent-verify.sh"

GREEN, YELLOW, RED, NC = "\033[0;32m", "\033[0;33m", "\033[0;31m", "\033[0m"


class Ctx:
    def __init__(self, target, dry, force):
        self.target, self.dry, self.force = target, dry, force
        self.changed = []

    def say(self, msg, color=""):
        print("%s%s%s" % (color, msg, NC if color else ""))

    def did(self, verb, rel):
        self.changed.append(rel)
        self.say("%s %s" % (("would " + verb) if self.dry else verb, rel), GREEN)

    def skip(self, rel, why="exists, skipping (use --force)"):
        self.say("skip  %s — %s" % (rel, why), YELLOW)


def ensure_dir(ctx, path):
    if not ctx.dry:
        os.makedirs(path, exist_ok=True)


def copy_file(ctx, src_abs, dst_rel, executable=False):
    dst = os.path.join(ctx.target, dst_rel)
    if os.path.exists(dst) and not ctx.force:
        ctx.skip(dst_rel)
        return
    ctx.did("install", dst_rel)
    if ctx.dry:
        return
    ensure_dir(ctx, os.path.dirname(dst))
    shutil.copyfile(src_abs, dst)
    if executable:
        os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_text(ctx, dst_rel, text):
    dst = os.path.join(ctx.target, dst_rel)
    if os.path.exists(dst) and not ctx.force:
        ctx.skip(dst_rel)
        return False
    ctx.did("create ", dst_rel)
    if not ctx.dry:
        ensure_dir(ctx, os.path.dirname(dst))
        with open(dst, "w") as f:
            f.write(text)
    return True


# ── JSON merging ────────────────────────────────────────────────────────────────────────
def deep_merge(existing, fragment):
    """Existing values win; missing keys are added; lists are unioned (order preserved)."""
    if isinstance(existing, dict) and isinstance(fragment, dict):
        out = dict(existing)
        for k, v in fragment.items():
            out[k] = deep_merge(existing[k], v) if k in existing else v
        return out
    if isinstance(existing, list) and isinstance(fragment, list):
        out = list(existing)
        for item in fragment:
            if item not in out:
                out.append(item)
        return out
    return existing  # scalar or type mismatch: keep what the user has


def has_stop_command(settings, needle):
    for entry in settings.get("hooks", {}).get("Stop", []):
        for h in entry.get("hooks", []):
            if needle in h.get("command", ""):
                return True
    return False


def merge_opencode_bash(existing, fragment):
    """permission.bash is order-sensitive (later rules win). Keep the user's rules first, append
    our denies after, and never add our catch-all "*" on top of rules the user already has."""
    ebash = existing.get("permission", {}).get("bash") if isinstance(existing.get("permission"), dict) else None
    if not isinstance(ebash, dict):
        return existing, fragment  # absent -> plain merge; string value -> keep theirs
    frag = json.loads(json.dumps(fragment))
    fbash = frag["permission"]["bash"]
    fbash.pop("*", None)
    out = json.loads(json.dumps(existing))
    for k, v in fbash.items():
        out["permission"]["bash"].pop(k, None)  # re-append so denies sit last
        out["permission"]["bash"][k] = v
    del frag["permission"]
    return out, frag


def merge_json(ctx, dst_rel, fragment_path, kind=None):
    dst = os.path.join(ctx.target, dst_rel)
    with open(fragment_path) as f:
        fragment = json.load(f)
    existing = {}
    if os.path.exists(dst):
        try:
            with open(dst) as f:
                existing = json.load(f)
        except ValueError:
            ctx.skip(dst_rel, "not valid JSON — merge it by hand from %s" % os.path.relpath(fragment_path, KIT))
            return
    base = json.loads(json.dumps(existing))
    if kind == "claude" and has_stop_command(existing, DENY_MARK):
        fragment.get("hooks", {}).pop("Stop", None)
    if kind == "opencode":
        existing, fragment = merge_opencode_bash(existing, fragment)
    merged = deep_merge(existing, fragment)
    if merged == base:
        ctx.say("ok    %s — already configured" % dst_rel)
        return
    ctx.did("merge  " if os.path.exists(dst) else "create ", dst_rel)
    if not ctx.dry:
        ensure_dir(ctx, os.path.dirname(dst))
        with open(dst, "w") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")


# ── agents / orchestrator modules ───────────────────────────────────────────────────────
GITIGNORE_MARK = "# agentic-setup-kit: agents"
GITIGNORE_BLOCK = """%s
.worktrees/
.env.agents
.agent-kit/agents/state/
.agent-kit/agents/*.log
.agent-kit/agents/*.pid
.agent-kit/agents/*-attempt*-task.txt
""" % GITIGNORE_MARK
MAKE_INCLUDE = "-include .agent-kit/agents.mk"


def install_tree(ctx, src_root, exclude=()):
    """Copy every file under src_root into the target at the same relative path. Scripts get +x."""
    for dirpath, _dirs, files in os.walk(src_root):
        for fn in sorted(files):
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, src_root)
            if rel in exclude or fn == ".DS_Store" or fn.endswith(".pyc"):
                continue
            copy_file(ctx, src, rel, executable=fn.endswith((".sh", ".py")))


def append_block(ctx, rel, marker, text):
    """Append text to rel unless marker is already in it (creates the file when absent)."""
    dst = os.path.join(ctx.target, rel)
    old = open(dst).read() if os.path.exists(dst) else ""
    if marker in old:
        ctx.say("ok    %s — already has the kit block" % rel)
        return
    ctx.did("append " if old else "create ", rel)
    if not ctx.dry:
        with open(dst, "w") as f:
            f.write(old + ("" if not old or old.endswith("\n") else "\n") + ("\n" if old else "") + text)


def github_slug(target):
    r = subprocess.run(["git", "-C", target, "remote", "get-url", "origin"], capture_output=True, text=True)
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$", r.stdout) if r.returncode == 0 else None
    return m.group(1) if m else None


def install_agents(ctx):
    install_tree(ctx, os.path.join(PAYLOAD, "optional/agents"))
    append_block(ctx, ".gitignore", GITIGNORE_MARK, GITIGNORE_BLOCK)
    mk = os.path.join(ctx.target, "Makefile")
    if os.path.exists(mk):
        append_block(ctx, "Makefile", "agents.mk", MAKE_INCLUDE + "\n")
    else:
        write_text(ctx, "Makefile", ".DEFAULT_GOAL := agents-list\n" + MAKE_INCLUDE + "\n")


def install_orchestrator(ctx):
    opt = os.path.join(PAYLOAD, "optional/orchestrator")
    install_tree(ctx, opt, exclude=("templates/ticket.md", ".env.agents.example"))
    ticket_dir = os.path.join("vault", "tickets")
    copy_file(ctx, os.path.join(opt, "templates/ticket.md"), os.path.join(ticket_dir, "_TEMPLATE.md"))
    slug = github_slug(ctx.target)
    text = open(os.path.join(opt, ".env.agents.example")).read().replace("__REPO_SLUG__", slug or "owner/name")
    write_text(ctx, ".env.agents.example", text)
    if not slug:
        ctx.say("      no GitHub origin found — set REPO_SLUG in .env.agents.example", YELLOW)


# ── verify.json generation ──────────────────────────────────────────────────────────────
def detect_checks(root):
    pkg = os.path.join(root, "package.json")
    if not os.path.isfile(pkg):
        return []
    try:
        scripts = json.load(open(pkg)).get("scripts", {})
    except ValueError:
        return []
    if os.path.exists(os.path.join(root, "pnpm-lock.yaml")):
        pm = "pnpm run -s"
    elif os.path.exists(os.path.join(root, "yarn.lock")):
        pm = "yarn run"
    else:
        pm = "npm run -s"
    checks = []
    if "typecheck" in scripts:
        checks.append({"name": "typecheck", "when": ["**/*.ts", "**/*.tsx"], "run": pm + " typecheck"})
    if "test:related" in scripts:
        checks.append({"name": "related tests", "when": ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"],
                       "run": pm + " test:related -- {files}"})
    return checks


def install_verify_config(ctx):
    rel = os.path.join(".agent-kit", "verify.json")
    checks = detect_checks(ctx.target)
    cfg = {"checks": checks}
    if write_text(ctx, rel, json.dumps(cfg, indent=2) + "\n"):
        if checks:
            ctx.say("      detected %d check(s) from package.json — review %s" % (len(checks), rel))
        else:
            ctx.say("      no checks auto-detected: edit %s (see README for monorepo/python/go examples)" % rel, YELLOW)


# ── main ────────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default=".")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite existing files (not JSON merges)")
    ap.add_argument("--with-obsidian", action="store_true", help="also wire a portable Obsidian MCP server")
    ap.add_argument("--with-agents", action="store_true",
                    help="add the worktree-per-agent launcher (agentctl.sh), agent-finish.sh (verify→commit→PR) and agents.mk")
    ap.add_argument("--with-orchestrator", action="store_true",
                    help="add the Docker ticket orchestrator (implies --with-agents)")
    ap.add_argument("--global-rules", action="store_true",
                    help="install the terse global rules into ~/.claude/CLAUDE.md and ~/.config/opencode/AGENTS.md if absent")
    a = ap.parse_args()

    target = os.path.abspath(a.target)
    if subprocess.run(["git", "-C", target, "rev-parse", "--git-dir"], capture_output=True).returncode != 0:
        print("%s✗ %s is not a git repository%s" % (RED, target, NC))
        return 1
    ctx = Ctx(target, a.dry_run, a.force)
    name = os.path.basename(target)
    ctx.say("%s agent kit → %s" % ("Dry run:" if a.dry_run else "Installing", target))

    copy_file(ctx, os.path.join(PAYLOAD, "scripts/agent-verify.sh"), "scripts/agent-verify.sh", True)
    copy_file(ctx, os.path.join(PAYLOAD, "scripts/agent-verify.py"), "scripts/agent-verify.py", True)
    copy_file(ctx, os.path.join(PAYLOAD, ".opencode/plugin/agent-verify.ts"), ".opencode/plugin/agent-verify.ts")
    install_verify_config(ctx)

    merge_json(ctx, ".claude/settings.json", os.path.join(PAYLOAD, "merge/claude-settings.json"), "claude")
    merge_json(ctx, "opencode.json", os.path.join(PAYLOAD, "merge/opencode.json"), "opencode")

    for tpl in ("AGENTS.md", "CLAUDE.md"):
        text = open(os.path.join(PAYLOAD, "templates", tpl)).read().replace("__REPO_NAME__", name)
        write_text(ctx, tpl, text)

    if a.with_obsidian:
        opt = os.path.join(PAYLOAD, "optional/obsidian")
        copy_file(ctx, os.path.join(opt, "scripts/mcp-obsidian.sh"), "scripts/mcp-obsidian.sh", True)
        copy_file(ctx, os.path.join(opt, "scripts/mcp-check.sh"), "scripts/mcp-check.sh", True)
        merge_json(ctx, ".mcp.json", os.path.join(opt, "mcp.json"))
        merge_json(ctx, "opencode.json", os.path.join(opt, "opencode.json"))
        merge_json(ctx, ".claude/settings.json", os.path.join(opt, "claude-settings.json"))
        keep = os.path.join(target, "vault", ".obsidian", ".gitkeep")
        if not os.path.exists(os.path.join(target, "vault", ".obsidian")):
            ctx.did("create ", "vault/.obsidian/.gitkeep")
            if not ctx.dry:
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                open(keep, "w").close()

    if a.with_agents or a.with_orchestrator:
        install_agents(ctx)
    if a.with_orchestrator:
        install_orchestrator(ctx)

    if a.global_rules:
        rules = open(os.path.join(PAYLOAD, "global-rules.md")).read()
        for dest in ("~/.claude/CLAUDE.md", "~/.config/opencode/AGENTS.md"):
            path = os.path.expanduser(dest)
            if os.path.exists(path):
                ctx.skip(dest, "exists — merge by hand from payload/global-rules.md")
            else:
                ctx.did("create ", dest)
                if not ctx.dry:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as f:
                        f.write(rules)

    ctx.say("")
    if a.dry_run:
        ctx.say("Dry run — nothing written.", YELLOW)
    else:
        ctx.say("Done. Next:", GREEN)
        print("  1. Review .agent-kit/verify.json (the checks run for changed files).")
        print("  2. Fill the TODOs in AGENTS.md.")
        print("  3. Restart Claude Code / opencode so the hook, plugin and permissions load.")
        print("  4. Try it: edit a file, break the typecheck, let the agent finish — it should be sent back to fix it.")
        if a.with_agents or a.with_orchestrator:
            print("  5. COMMIT the kit files before launching agents — worktrees branch from a commit, not your working tree.")
            print("     Then: make agents-up FILE=.agent-kit/agents/tasks/<task>.txt NAME=<name> BASE=main")
        if a.with_orchestrator:
            print("  6. cp .env.agents.example .env.agents (set GH_TOKEN), make agents-image, make agents-orchestrator DRY_RUN=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
