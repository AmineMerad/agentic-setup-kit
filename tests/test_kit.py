"""Smoke + behaviour tests for the kit. Run:  python3 -m unittest discover -s tests -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(KIT, "install.py")
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=kw.pop("env", GIT_ENV), **kw)


def make_repo(files=None):
    d = tempfile.mkdtemp(prefix="kit-test-")
    sh(["git", "init", "-q", "-b", "main", "."], d)
    for rel, text in (files or {}).items():
        write(d, rel, text)
    sh(["git", "add", "-A"], d)
    sh(["git", "commit", "-q", "--allow-empty", "-m", "init"], d)
    return d


def rd(path):
    with open(path) as f:
        return f.read()


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def read_json(root, rel):
    with open(os.path.join(root, rel)) as f:
        return json.load(f)


def install(target, *flags, home=None):
    env = dict(GIT_ENV, HOME=home) if home else GIT_ENV
    return sh([sys.executable, INSTALL, target, *flags], KIT, env=env)


class VerifyScript(unittest.TestCase):
    """Runs the payload script directly against fixtures (no installer involved)."""

    def setUp(self):
        self.repo = make_repo({"README.md": "x\n"})
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.script = os.path.join(KIT, "payload/scripts/agent-verify.py")
        # state lives in the system temp dir keyed by repo path, so fresh repos start clean

    def config(self, checks):
        write(self.repo, ".agent-kit/verify.json", json.dumps({"checks": checks}))

    def run_verify(self, payload=None):
        return sh([sys.executable, self.script], self.repo,
                  input=json.dumps(payload if payload is not None else {"cwd": self.repo}))

    def test_no_config_fails_open(self):
        write(self.repo, "a.ts", "x")
        self.assertEqual(self.run_verify().returncode, 0)

    def test_stop_hook_active_never_runs(self):
        self.config([{"name": "boom", "run": "exit 1"}])
        write(self.repo, "a.ts", "x")
        self.assertEqual(self.run_verify({"cwd": self.repo, "stop_hook_active": True}).returncode, 0)

    def test_clean_tree_passes_without_running(self):
        self.config([{"name": "boom", "run": "exit 1"}])
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "commit", "-q", "-m", "cfg"], self.repo)
        self.assertEqual(self.run_verify().returncode, 0)

    def test_failure_exits_2_with_report(self):
        self.config([{"name": "typecheck", "when": "**/*.ts", "run": "echo 'a.ts: error TS1' >&2; exit 1"}])
        write(self.repo, "src/a.ts", "x")
        r = self.run_verify()
        self.assertEqual(r.returncode, 2)
        self.assertIn("typecheck FAILED", r.stderr)
        self.assertIn("error TS1", r.stderr)
        self.assertIn("did NOT change", r.stderr)

    def test_unmatched_files_do_not_trigger(self):
        self.config([{"name": "ts only", "when": ["**/*.ts"], "run": "exit 1"}])
        write(self.repo, "notes.md", "x")
        self.assertEqual(self.run_verify().returncode, 0)

    def test_pass_is_cached_until_files_change(self):
        counter = os.path.join(self.repo, "..", os.path.basename(self.repo) + ".count")
        self.addCleanup(lambda: os.path.exists(counter) and os.remove(counter))
        self.config([{"name": "count", "when": "**/*.ts", "run": "echo x >> '%s'" % counter}])
        write(self.repo, "a.ts", "1")
        for _ in range(3):
            self.assertEqual(self.run_verify().returncode, 0)
        self.assertEqual(rd(counter).count("x"), 1, "unchanged tree must not re-run checks")
        write(self.repo, "a.ts", "2")
        self.run_verify()
        self.assertEqual(rd(counter).count("x"), 2, "a content change must re-run")

    def test_failure_is_not_cached(self):
        self.config([{"name": "f", "when": "**/*.ts", "run": "exit 1"}])
        write(self.repo, "a.ts", "1")
        self.assertEqual(self.run_verify().returncode, 2)
        self.assertEqual(self.run_verify().returncode, 2)

    def test_files_placeholder_is_relative_to_dir_and_quoted(self):
        out = os.path.join(self.repo, "..", os.path.basename(self.repo) + ".args")
        self.addCleanup(lambda: os.path.exists(out) and os.remove(out))
        self.config([{"name": "args", "dir": "pkg", "when": "pkg/**/*.ts",
                      "run": "%s -c \"import sys; open(r'%s','w').write(repr(sys.argv[1:]))\" {files}" % (sys.executable, out)}])
        write(self.repo, "pkg/src/my file.ts", "x")
        write(self.repo, "other/b.ts", "x")  # outside `when`: must not be passed
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertEqual(eval(rd(out)), ["src/my file.ts"])

    def test_deleted_only_skips_files_check_but_runs_plain_check(self):
        write(self.repo, "gone.ts", "x")
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "commit", "-q", "-m", "add"], self.repo)
        os.remove(os.path.join(self.repo, "gone.ts"))
        self.config([{"name": "needs files", "when": "**/*.ts", "run": "exit 1 # {files}"},
                     {"name": "plain", "when": "**/*.ts", "run": "exit 1"}])
        r = self.run_verify()
        self.assertEqual(r.returncode, 2)
        self.assertIn("plain FAILED", r.stderr)
        self.assertNotIn("needs files FAILED", r.stderr)

    def test_rename_reports_new_path(self):
        write(self.repo, "old.ts", "x")
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "commit", "-q", "-m", "add"], self.repo)
        sh(["git", "mv", "old.ts", "new.ts"], self.repo)
        out = os.path.join(self.repo, "..", os.path.basename(self.repo) + ".args")
        self.addCleanup(lambda: os.path.exists(out) and os.remove(out))
        self.config([{"name": "args", "when": "**/*.ts",
                      "run": "%s -c \"import sys; open(r'%s','w').write(repr(sys.argv[1:]))\" {files}" % (sys.executable, out)}])
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertEqual(eval(rd(out)), ["new.ts"])

    def test_timeout_fails(self):
        self.config([{"name": "slow", "when": "**/*.ts", "run": "sleep 5", "timeout": 1}])
        write(self.repo, "a.ts", "x")
        r = self.run_verify()
        self.assertEqual(r.returncode, 2)
        self.assertIn("timed out", r.stderr)

    def test_broken_config_is_internal_error_not_a_block(self):
        write(self.repo, ".agent-kit/verify.json", "{not json")
        write(self.repo, "a.ts", "x")
        r = self.run_verify()
        self.assertEqual(r.returncode, 1)
        self.assertIn("internal error", r.stderr)

    def test_glob_semantics(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("av", self.script)
        av = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(av)
        m = lambda g, p: bool(av.glob_to_regex(g).match(p))
        self.assertTrue(m("**/*.ts", "a.ts"))
        self.assertTrue(m("**/*.ts", "x/y/a.ts"))
        self.assertFalse(m("*.ts", "x/a.ts"))
        self.assertTrue(m("backend/**/*.ts", "backend/src/a.ts"))
        self.assertFalse(m("backend/**/*.ts", "frontend/src/a.ts"))
        self.assertFalse(m("**/*.ts", "a.tsx"))
        self.assertTrue(m("src/**", "src/a/b.py"))


class Installer(unittest.TestCase):
    def fresh_node_repo(self):
        repo = make_repo({
            "package.json": json.dumps({"scripts": {"typecheck": "tsc --noEmit", "test:related": "jest --findRelatedTests"}}),
            "pnpm-lock.yaml": "lockfileVersion: 9\n",
        })
        self.addCleanup(shutil.rmtree, repo, True)
        return repo

    def test_fresh_install_creates_everything(self):
        repo = self.fresh_node_repo()
        r = install(repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel in ("scripts/agent-verify.sh", "scripts/agent-verify.py", ".opencode/plugin/agent-verify.ts",
                    ".agent-kit/verify.json", ".claude/settings.json", "opencode.json", "AGENTS.md", "CLAUDE.md"):
            self.assertTrue(os.path.exists(os.path.join(repo, rel)), rel)
        self.assertTrue(os.access(os.path.join(repo, "scripts/agent-verify.sh"), os.X_OK))
        cfg = read_json(repo, ".agent-kit/verify.json")
        self.assertEqual([c["run"] for c in cfg["checks"]],
                         ["pnpm run -s typecheck", "pnpm run -s test:related -- {files}"])
        s = read_json(repo, ".claude/settings.json")
        self.assertIn("Bash(git stash:*)", s["permissions"]["deny"])
        self.assertIn("agent-verify.sh", s["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertEqual(read_json(repo, "opencode.json")["permission"]["bash"]["git stash*"], "deny")
        self.assertIn("@AGENTS.md", rd(os.path.join(repo, "CLAUDE.md")))
        self.assertNotIn("__REPO_NAME__", rd(os.path.join(repo, "AGENTS.md")))

    def test_second_run_changes_nothing(self):
        repo = self.fresh_node_repo()
        install(repo)
        before = sh(["git", "status", "--porcelain"], repo).stdout
        snapshot = {rel: rd(os.path.join(repo, rel)) for rel in
                    (".claude/settings.json", "opencode.json", ".agent-kit/verify.json")}
        r = install(repo)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("install ", r.stdout.replace("\x1b[0;32m", ""))
        for rel, text in snapshot.items():
            self.assertEqual(rd(os.path.join(repo, rel)), text, rel)
        self.assertEqual(sh(["git", "status", "--porcelain"], repo).stdout, before)

    def test_dry_run_writes_nothing(self):
        repo = self.fresh_node_repo()
        r = install(repo, "--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(sh(["git", "status", "--porcelain"], repo).stdout.strip(), "")
        self.assertIn("would", r.stdout)

    def test_non_git_target_is_rejected(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(install(d).returncode, 1)

    def test_merge_preserves_existing_claude_settings(self):
        repo = self.fresh_node_repo()
        write(repo, ".claude/settings.json", json.dumps({
            "model": "opus",
            "permissions": {"allow": ["Bash(make:*)"], "deny": ["Read(./.env)"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}]},
        }))
        install(repo)
        s = read_json(repo, ".claude/settings.json")
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["permissions"]["allow"][0], "Bash(make:*)")
        self.assertIn("Read(./.env)", s["permissions"]["deny"])
        self.assertIn("Bash(git stash:*)", s["permissions"]["deny"])
        cmds = [h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]]
        self.assertEqual(cmds[0], "echo mine")
        self.assertTrue(any("agent-verify.sh" in c for c in cmds))
        self.assertIn("PreToolUse", s["hooks"])

    def test_existing_verify_hook_is_not_duplicated(self):
        repo = self.fresh_node_repo()
        write(repo, ".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "scripts/agent-verify.sh", "timeout": 5}]}]}}))
        install(repo)
        s = read_json(repo, ".claude/settings.json")
        self.assertEqual(len(s["hooks"]["Stop"]), 1)
        self.assertEqual(s["hooks"]["Stop"][0]["hooks"][0]["timeout"], 5)

    def test_opencode_bash_merge_keeps_user_rules_first_and_denies_last(self):
        repo = self.fresh_node_repo()
        write(repo, "opencode.json", json.dumps({"permission": {"bash": {"npm *": "allow", "rm *": "ask"}}, "theme": "x"}))
        install(repo)
        o = read_json(repo, "opencode.json")
        keys = list(o["permission"]["bash"])
        self.assertEqual(keys[:2], ["npm *", "rm *"])
        self.assertNotIn("*", keys, "must not add a catch-all allow on top of the user's rules")
        self.assertEqual(o["permission"]["bash"]["git stash*"], "deny")
        self.assertEqual(o["theme"], "x")

    def test_opencode_bash_string_value_is_left_alone(self):
        repo = self.fresh_node_repo()
        write(repo, "opencode.json", json.dumps({"permission": {"bash": "ask"}}))
        install(repo)
        self.assertEqual(read_json(repo, "opencode.json")["permission"]["bash"], "ask")

    def test_invalid_existing_json_is_never_overwritten(self):
        repo = self.fresh_node_repo()
        write(repo, "opencode.json", "{ // comment\n}")
        r = install(repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(rd(os.path.join(repo, "opencode.json")), "{ // comment\n}")
        self.assertIn("not valid JSON", r.stdout)

    def test_existing_agents_md_and_verify_config_are_kept(self):
        repo = self.fresh_node_repo()
        write(repo, "AGENTS.md", "mine\n")
        write(repo, ".agent-kit/verify.json", '{"checks": []}')
        install(repo)
        self.assertEqual(rd(os.path.join(repo, "AGENTS.md")), "mine\n")
        self.assertEqual(rd(os.path.join(repo, ".agent-kit/verify.json")), '{"checks": []}')
        self.assertIn("@AGENTS.md", rd(os.path.join(repo, "CLAUDE.md")))

    def test_no_package_json_gives_empty_checks(self):
        repo = make_repo({"main.py": "x"})
        self.addCleanup(shutil.rmtree, repo, True)
        r = install(repo)
        self.assertEqual(read_json(repo, ".agent-kit/verify.json"), {"checks": []})
        self.assertIn("no checks auto-detected", r.stdout)

    def test_with_obsidian(self):
        repo = self.fresh_node_repo()
        install(repo, "--with-obsidian")
        self.assertTrue(os.access(os.path.join(repo, "scripts/mcp-obsidian.sh"), os.X_OK))
        self.assertIn("obsidian", read_json(repo, ".mcp.json")["mcpServers"])
        self.assertIn("obsidian", read_json(repo, "opencode.json")["mcp"])
        self.assertEqual(read_json(repo, ".claude/settings.json")["enabledMcpjsonServers"], ["obsidian"])
        self.assertTrue(os.path.exists(os.path.join(repo, "vault/.obsidian/.gitkeep")))
        self.assertIn("permission", read_json(repo, "opencode.json"))  # base merge survived

    def test_global_rules_create_but_never_overwrite(self):
        repo = self.fresh_node_repo()
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        write(home, ".claude/CLAUDE.md", "personal\n")
        r = install(repo, "--global-rules", home=home)
        self.assertEqual(rd(os.path.join(home, ".claude/CLAUDE.md")), "personal\n")
        self.assertIn("Terse", rd(os.path.join(home, ".config/opencode/AGENTS.md")))
        self.assertIn("exists", r.stdout)

    def test_installed_hook_end_to_end(self):
        repo = make_repo({"package.json": json.dumps({"scripts": {"typecheck": "echo 'a.ts: error TS9' >&2; exit 1"}})})
        self.addCleanup(shutil.rmtree, repo, True)
        install(repo)
        write(repo, "a.ts", "x")
        r = sh([os.path.join(repo, "scripts/agent-verify.sh")], repo, input=json.dumps({"cwd": repo}))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("error TS9", r.stderr)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class OpencodePlugin(unittest.TestCase):
    def test_reprompts_once_dedupes_and_skips_subagents(self):
        repo = make_repo({"package.json": json.dumps({"scripts": {"typecheck": "echo 'a.ts: error TS7' >&2; exit 1"}})})
        self.addCleanup(shutil.rmtree, repo, True)
        install(repo)
        write(repo, "a.ts", "x")
        harness = textwrap.dedent("""
            import { AgentVerify } from %r
            const prompts = []
            let parent
            const client = { session: {
              get: async () => ({ data: { parentID: parent } }),
              prompt: async (o) => { prompts.push(o.body.parts[0].text) } } }
            const hooks = await AgentVerify({ client, directory: %r, worktree: %r })
            const idle = (id) => hooks.event({ event: { type: 'session.idle', properties: { sessionID: id } } })
            await idle('s1'); await idle('s1')
            parent = 'p'; await idle('child')
            await hooks.event({ event: { type: 'message.updated', properties: {} } })
            console.log(JSON.stringify({ n: prompts.length, text: prompts[0] }))
        """ % (os.path.join(repo, ".opencode/plugin/agent-verify.ts"), repo, repo))
        h = os.path.join(repo, "harness.mts")
        write(repo, "harness.mts", harness)
        r = subprocess.run(["node", h], capture_output=True, text=True, cwd=repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["n"], 1, "one prompt: repeat deduped, subagent skipped")
        self.assertIn("error TS7", out["text"])


if __name__ == "__main__":
    unittest.main()
