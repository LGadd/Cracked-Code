"""Plan mode: the plan file, its parser, the messages that walk the model through it, and the
read-only shell check used while a plan is researched. Design: docs/plan-mode.md.

The harness owns the plan (the model never has to keep it): it lives in <workspace>/.cracked/plan.md,
which the user can edit, and agent.run_plan hands the model one step at a time.
"""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

MAX_STEPS = 30
MAX_FILES = 2
STATE = re.compile(r"<!-- cracked plan: source=(\w+) state=(\w+)(?: mode=(\w+))? -->")
STEP = re.compile(r"^\s*(?:[-*]\s*)?(?:\[(?P<box>[ xX~!\-])\]\s*)?(?P<n>\d+)[.)]\s+(?P<body>\S.*)$")
FIELD = re.compile(r"\s+\|\s+(?=(?:files|check)\s*:)", re.I)  # a check may hold its own pipes: split only before a key
RESULT = re.compile(r"^\s+(?:→|->)\s*(?P<text>.*)$")


class PlanError(ValueError):
    """The text is not a usable plan; the message says exactly why (sent back to the model as is)."""


@dataclass
class Step:
    text: str
    files: list[str] = field(default_factory=list)
    check: str = ""
    mark: str = " "
    result: str = ""


@dataclass
class Plan:
    goal: str
    steps: list[Step]
    source: str = "user"   # user: /plan, approved in the plan menu; model: todo_write or auto_checklist
    state: str = "draft"   # draft (waiting for the plan menu) | running | done
    mode: str = "auto"     # auto: every step in a row | manual: the plan menu after each step
    replanned: list[int] = field(default_factory=list)  # step numbers already replanned once: a second block stops

    def current(self) -> int | None:
        """Index of the step to work on: the one in progress, else the first not done or skipped."""
        for i, s in enumerate(self.steps):
            if s.mark in (" ", "~", "!"):
                return i
        return None

    def done(self) -> int:
        return sum(s.mark in ("x", "-") for s in self.steps)

    def label(self) -> str:
        """The status bar: `plan 3/9 · title` for the user's plan, a quieter `☐ 3/7 · title` for the model's own."""
        i = self.current()
        if i is None or self.state != "running":
            return ""
        head = "plan" if self.source == "user" else "☐"
        return f"{head} {i + 1}/{len(self.steps)} · {_short(self.steps[i].text, 40)}"


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def parse(text: str, goal: str = "") -> Plan:
    """A plan from markdown: `# Plan: goal`, then `- [ ] 1. step | files: a, b | check: cmd` lines (boxes,
    files and check optional; numbers are renumbered). Other lines are ignored, so a reply may talk around it."""
    steps: list[Step] = []
    source, state, mode = "user", "draft", "auto"
    for line in text.splitlines():
        if m := STATE.search(line):
            source, state, mode = m.group(1), m.group(2), m.group(3) or "auto"
        elif line.lstrip().lower().startswith("# plan:"):
            goal = line.split(":", 1)[1].strip() or goal
        elif m := STEP.match(line):
            parts = FIELD.split(m.group("body"))
            step = Step(text=parts[0].strip(), mark=(m.group("box") or " ").lower())
            for p in parts[1:]:
                key, _, value = p.partition(":")
                if key.strip().lower() == "files":
                    step.files = [f.strip(" `") for f in value.split(",") if f.strip(" `")]
                else:
                    step.check = value.strip().strip("`")
            steps.append(step)
        elif steps and (m := RESULT.match(line)):
            steps[-1].result = m.group("text").strip()
    problems = []
    if not steps:
        problems.append("no steps found: each step is a line like `- [ ] 1. <one action> | files: a.py | check: <command>`")
    if len(steps) > MAX_STEPS:
        problems.append(f"{len(steps)} steps: at most {MAX_STEPS}; merge small ones")
    for n, s in enumerate(steps, 1):
        if len(s.files) > MAX_FILES:
            problems.append(f"step {n} names {len(s.files)} files: at most {MAX_FILES}; split it")
        if s.check and s.mark not in ("x", "-") and (why := check_problem(s.check)):
            problems.append(f"step {n}'s check {why}")
    if problems:
        raise PlanError("; ".join(problems))
    return Plan(goal=goal or steps[0].text, steps=steps, source=source, state=state, mode=mode)


SHELL_WORDS = {"!", "[", "[[", "test", "for", "if", "while", "until", "case", "cd", "true", "false", "set", "export",
               "source", ".", "{", "(", "command", "builtin", "exit"}


def check_problem(check: str) -> str:
    """Why a check cannot be run as a command, or "" when it can. A sentence ("the audit lists X and exits 0") ran
    as shell, failed with 127 every time and blocked a finished step (2026-11-11), so it is refused up front.
    shortcut: a command the plan itself installs later (step 4 installs jq, step 5 checks with jq) is refused too;
    write it as a path or install it in an earlier session if that ever bites."""
    if "`" in check:
        return "holds a backtick: write only the command itself, e.g. `| check: ./audit.sh`"
    try:
        words = shlex.split(check)
    except ValueError:
        return "is not valid shell (an unclosed quote?)"
    first = words[0] if words else ""
    if not first or first in SHELL_WORDS or "/" in first or "=" in first or shutil.which(first):
        return ""
    return f"starts with {first!r}, which is not a command here: a check is a shell command that exits 0 when the step is right, not a sentence"


def render(plan: Plan) -> str:
    """The plan file. Parsing it gives the same plan back."""
    lines = [f"# Plan: {plan.goal}", f"<!-- cracked plan: source={plan.source} state={plan.state} mode={plan.mode} -->", ""]
    for n, s in enumerate(plan.steps, 1):
        extra = (f" | files: {', '.join(s.files)}" if s.files else "") + (f" | check: {s.check}" if s.check else "")
        lines.append(f"- [{s.mark}] {n}. {s.text}{extra}")
        if s.result:
            lines.append(f"  → {s.result}")
    return "\n".join(lines) + "\n"


def path(workspace: Path) -> Path:
    return workspace / ".cracked" / "plan.md"


def save(workspace: Path, plan: Plan) -> None:
    p = path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.parent / ".gitignore").write_text("*\n", encoding="utf-8")  # never in the snapshots, so /undo leaves the plan alone
    tmp = p.with_suffix(".tmp")
    tmp.write_text(render(plan), encoding="utf-8")
    tmp.replace(p)


def load(workspace: Path) -> Plan | None:
    """The saved plan, or None when there is none. Raises PlanError for a file that no longer parses."""
    p = path(workspace)
    return parse(p.read_text(encoding="utf-8")) if p.exists() else None


def checklist(plan: Plan) -> str:
    """The plan as the terminal and the ledger show it: one line a step."""
    sign = {" ": "·", "~": "▸", "x": "✓", "!": "!", "-": "–"}
    return "\n".join(f"{sign[s.mark]} {n}. {s.text}" + (f"  → {s.result}" if s.result else "")
                     for n, s in enumerate(plan.steps, 1))


# ---------------------------------------------------------------- what the model is sent
HARNESS = "[PLAN"  # how every plan message starts (not the user's words: kept out of the LOCKED IN ledger)
RESEARCH_HEAD = "[PLAN mode: research]"
READ_ONLY_RULES = ("Read anything you need: files, commands and scripts that only look, the web. Writing files is "
                   "refused until the user approves the plan, and commands that change things ask the user first.")
WRITE_RULES = ("Read anything you need, and write files if it helps to test an idea (probes, prototypes, scratch "
               "scripts), but do not carry out the plan itself: the user approves the plan before any step runs.")
RESEARCH = RESEARCH_HEAD + """ Research this goal, then write a plan for it. {rules}
Finish with the plan as your reply, in exactly this format:
# Plan: <the goal in one line>
- [ ] 1. <one action> | files: <at most 2 paths> | check: <a command that exits 0 when the step is right>
- [ ] 2. ...
Rules: 3 to 25 steps, in the order to do them; each step is one action small enough for about 10 tool calls; at \
most 2 files a step; give a check whenever a command can prove the step (tests, a build, running it) and leave out \
`| check:` when none can; a check is only the shell command, no backticks and no words around it (the harness runs \
it as is and the step passes when it exits 0); the last step checks the whole goal."""
FIX = "[PLAN] That plan could not be used: {why}. Send the whole plan again in the format asked for. No tool calls."
REPLAN = ("[PLAN] Step {n} is blocked: {why}. Rewrite the steps that are not done yet, starting with what replaces "
          "step {n}, in the same format (`- [ ] 1. ... | files: ... | check: ...`). Steps already ticked stay done: do not "
          "repeat them. No tool calls.\n\nThe plan now:\n{plan}")
EDIT = ("[PLAN edit] The user wants the plan changed: {ask}\nSend the whole plan again with that change, in the same format "
        "(`- [ ] 1. ... | files: ... | check: ...`), keeping `[x]` on steps already done. No tool calls.\n\nThe plan now:\n{plan}")
WRONG = "PLAN WRONG:"  # the model's way to stop the run when it finds the plan no longer fits
CHECK_FAILED = ("[PLAN check failed] `{check}` exited {code}:\n{output}\nStep {n} is not done yet. Fix it, then reply "
                "in one line saying what you did.")
FINAL = ("[PLAN complete] All {total} steps are ticked. Checks run again: {checks}. Goal: {goal}\nCompare the work against "
         "the goal. Fix anything small that is missing or failing, then give a short final report: what was done, and "
         "anything left.")
CHECKLIST = ("[harness] Before you go on: list the remaining steps of this task as a checklist, in order, one short line "
             "each, as `- [ ] 1. <step>` (add ` | check: <command>` when a command can prove the step). If fewer than 3 steps "
             "are left, reply only NONE. No tool calls.")


def step_message(plan: Plan, i: int, ledger: str = "") -> str:
    """One step at a time: the goal, what is done, the step to do now, and where to stop."""
    s, total = plan.steps[i], len(plan.steps)
    done = " · ".join(f"{n}. {_short(t.result or t.text, 70)}" for n, t in enumerate(plan.steps, 1)
                      if t.mark in ("x", "-") and n - 1 < i) or "nothing yet"
    files = f" (files: {', '.join(s.files)})" if s.files else ""
    finish = (f"`{s.check}` passes; the harness runs it after your reply." if s.check
              else "the step is done.")
    nxt = next((f" Do not start step {n} ({_short(t.text, 60)})." for n, t in enumerate(plan.steps, 1)
                if n - 1 > i and t.mark == " "), " This is the last step.")
    text = (f"[PLAN step {i + 1} of {total}] Goal: {plan.goal}\nDone: {done}\nNOW: {i + 1}. {s.text}{files}\n"
            f"Finish line: {finish} Then reply in one line saying what you did.{nxt} If you find the plan itself no "
            f"longer fits (not just this step being hard), reply starting with `{WRONG} <why>` instead.")
    if s.mark == "!":
        text += "\nThis step was tried before and got stuck; it may be partly done already: check first."
    return text + (f"\n{ledger}" if ledger else "")


def research(fuck_it: bool) -> str:
    """What /plan <goal> appends to the goal: Fuck It may write while researching, the other modes only read."""
    return RESEARCH.format(rules=WRITE_RULES if fuck_it else READ_ONLY_RULES)


# ---------------------------------------------------------------- shell that changes things, while researching
WRITES = {"rm", "rmdir", "mv", "cp", "touch", "mkdir", "tee", "ln", "chmod", "chown", "chgrp", "dd", "truncate", "shred",
          "unlink", "install", "patch", "sudo", "doas", "su", "mkfs", "mount", "umount", "kill", "pkill", "killall",
          "systemctl", "reboot", "shutdown", "rsync", "scp", "wget", "curl"}  # curl/wget: -o writes; ask, they rarely only read here
INSTALLERS = {"pip", "pip3", "pipx", "npm", "pnpm", "yarn", "uv", "pacman", "paru", "yay", "apt", "apt-get", "dnf",
              "cargo", "gem", "brew", "go"}
INSTALL_WORDS = {"install", "add", "remove", "uninstall", "rm", "sync", "update", "upgrade", "get", "publish"}
WRAPPERS = {"xargs", "env", "nohup", "time", "timeout", "nice", "exec", "command", "builtin", "stdbuf"}
GIT_SAFE = {"log", "status", "diff", "show", "ls-files", "grep", "blame", "rev-parse", "shortlog"}
BAD_ARGS = {"find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
            "sort": ("-o", "--output"), "tree": ("-o",), "git": ("--output", "-o"), "sed": ("-i", "--in-place"),
            "perl": ("-i",)}
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
REDIRECT = re.compile(r"(?<![0-9&])>(?!&)|[0-9]>(?!&)")  # > file, >> file, 2> file; not 2>&1
SPLIT = re.compile(r"&&|\|\||\$\(|[;|\n()`&]")


def looks_like_write(command: str) -> bool:
    """While researching in manual mode: True when a command looks like it changes something (a redirect into a file,
    rm/mv/cp/mkdir..., sed -i, git commit, a package install, sudo), so it asks; everything else runs unasked.
    shortcut: a program or script that writes on its own (python3 x.py, make, bash -c "...") runs unasked, which the
    user chose (option A, 2026-11-12); ask for interpreters too if research ever writes something it should not."""
    bare = QUOTED.sub("''", command)
    if REDIRECT.search(re.sub(r"[0-9]*>>?\s*/dev/null", "", bare)):
        return True
    for segment in SPLIT.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        while words and ("=" in words[0] or words[0] in WRAPPERS):
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", words[0])):  # timeout -s 9 5, xargs -0
                words = words[1:]
        if not words:
            continue
        cmd, args = words[0].rsplit("/", 1)[-1], words[1:]
        if cmd in WRITES:
            return True
        if cmd in INSTALLERS and any(a in INSTALL_WORDS or a.startswith(("-S", "-R", "-U")) for a in args):
            return True
        if cmd == "git" and next((a for a in args if not a.startswith("-")), "") not in GIT_SAFE | {""}:
            return True
        if any(a == bad or a.startswith(bad + "=") or (len(bad) == 2 and a.startswith(bad))  # -ofile, -i.bak too
               for a in args for bad in BAD_ARGS.get(cmd, ())):
            return True
    return False
