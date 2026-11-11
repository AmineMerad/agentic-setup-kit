#!/usr/bin/env python3
"""state.py — small state store for the agent supervisor.

Files live in .agent-kit/agents/state/ (gitignored):
    running-<name>.json      ticket, pid, files, taskfile, title, started   (live agents)
    attempts-<TICKET>.count  failed-launch counter                          (retry policy)
    done-<TICKET>.md         PR url + timestamp                             (finished)
    parked-<TICKET>.md       reason + log path after MAX_ATTEMPTS           (needs triage)
    ask-<TICKET>.ts          timestamp of the last orchestrator ask         (cooldown)
    orchestrator.session     opencode session id for the planner            (memory)
    triaged-<TICKET>.md      parked ticket already handed to the planner    (one-shot)

All ticket ids are normalised to upper case. Run from the repo root.
"""
import glob
import json
import os
import sys
import time

STATE_DIR = os.path.join(os.getcwd(), ".agent-kit", "agents", "state")


def p(*parts):
    return os.path.join(STATE_DIR, *parts)


def ensure():
    os.makedirs(STATE_DIR, exist_ok=True)


def clean(s):
    return s.replace("\t", " ").replace("\n", " ")


def running_files():
    return sorted(glob.glob(p("running-*.json")))


def cmd_set_running(argv):
    name, ticket, pid, files, taskfile, title = argv
    ensure()
    json.dump({
        "ticket": ticket.upper(), "pid": int(pid),
        "files": [f for f in files.split(",") if f],
        "taskfile": taskfile, "title": title, "started": int(time.time()),
    }, open(p(f"running-{name}.json"), "w"))


def cmd_clear_running(argv):
    for f in running_files():
        if os.path.basename(f) == f"running-{argv[0]}.json":
            os.remove(f)


def cmd_running(_argv):
    ensure()
    for f in running_files():
        try:
            d = json.load(open(f))
        except (ValueError, OSError):
            continue
        name = os.path.basename(f)[len("running-"):-len(".json")]
        print("\t".join([name, d.get("ticket", ""), str(d.get("pid", "")),
                         ",".join(d.get("files", [])), d.get("taskfile", ""),
                         clean(d.get("title", "")), str(d.get("started", ""))]))


def cmd_bump_attempts(argv):
    ensure()
    ticket = argv[0].upper()
    f = p(f"attempts-{ticket}.count")
    n = 0
    if os.path.exists(f):
        try:
            n = int(open(f).read().strip() or 0)
        except ValueError:
            n = 0
    n += 1
    open(f, "w").write(str(n))
    print(n)


def cmd_get_attempts(argv):
    f = p(f"attempts-{argv[0].upper()}.count")
    print(open(f).read().strip() if os.path.exists(f) else "0")


def cmd_done(argv):
    ensure()
    ticket, url = argv[0].upper(), argv[1]
    open(p(f"done-{ticket}.md"), "w").write(
        f"# {ticket} done\n\npr: {url}\nat: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")


def cmd_park(argv):
    ensure()
    ticket, reason = argv[0].upper(), argv[1]
    open(p(f"parked-{ticket}.md"), "w").write(
        f"# {ticket} parked\n\nat: {time.strftime('%Y-%m-%d %H:%M:%S')}\nreason: {reason}\n"
        "Remove this file to let the supervisor try the ticket again.\n")


def cmd_asked(argv):
    """asked <TICKET> <cooldown-seconds> → '1' when asked recently."""
    f = p(f"ask-{argv[0].upper()}.ts")
    cooldown = int(argv[1])
    fresh = os.path.exists(f) and (time.time() - os.path.getmtime(f)) < cooldown
    print("1" if fresh else "0")


def cmd_mark_asked(argv):
    ensure()
    open(p(f"ask-{argv[0].upper()}.ts"), "w").write(str(int(time.time())))


def cmd_triaged(argv):
    """triaged <TICKET> → '1' when this park was already handed to the planner."""
    print("1" if os.path.exists(p(f"triaged-{argv[0].upper()}.md")) else "0")


def cmd_mark_triaged(argv):
    ensure()
    open(p(f"triaged-{argv[0].upper()}.md"), "w").write(str(int(time.time())))


def cmd_parked(_argv):
    ensure()
    for f in sorted(glob.glob(p("parked-*.md"))):
        print(os.path.basename(f)[len("parked-"):-len(".md")])


def cmd_session(argv):
    f = p("orchestrator.session")
    if argv and argv[0] == "get":
        print(open(f).read().strip() if os.path.exists(f) else "")
    elif argv and argv[0] == "set":
        ensure()
        open(f, "w").write(argv[1].strip())


COMMANDS = {
    "set-running": cmd_set_running, "clear-running": cmd_clear_running, "running": cmd_running,
    "bump-attempts": cmd_bump_attempts, "get-attempts": cmd_get_attempts,
    "done": cmd_done, "park": cmd_park, "parked": cmd_parked,
    "asked": cmd_asked, "mark-asked": cmd_mark_asked,
    "triaged": cmd_triaged, "mark-triaged": cmd_mark_triaged, "session": cmd_session,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"usage: state.py <{'|'.join(COMMANDS)}> [args…]", file=sys.stderr)
        sys.exit(2)
    COMMANDS[sys.argv[1]](sys.argv[2:])
