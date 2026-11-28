"""The interactive REPL: input (multi-line, paste, history, Tab paths), slash commands,
saved sessions and /undo. Nothing here adds to the model's context: commands talk to the
harness, and only typed tasks go to the model.
"""

from __future__ import annotations

import base64
import datetime
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import agent, config, images, llm, plan, safety, tools, ui

HELP = """\
Keys
  Enter          sends · end a line with \\ to keep typing · pasted text keeps its newlines
  Alt+Enter      new line · Ctrl+X Ctrl+E edit in $EDITOR · Ctrl+C clears the line · Right takes the grey suggestion
  / and @        open a menu as you type (@ finds files anywhere: @pars) · @file attaches it
  Up/Down        past tasks; Ctrl+R searches them
  Shift+Tab      mode: manual / Fuck It / INVESTIGATE (read only)
  Ctrl+O         details: reasoning, turns, diffs · Esc stops a task
  Ctrl+B         while a shell command runs: move it to the background and get the prompt back
  Mid-task       type and press Enter to steer it; it reads your message before its next step
  Ctrl-D         quits (the session is saved: cracked -c picks it up)
Commands
/undo    put the files back as they were before the last task, and forget that task
/diff    everything changed this session (files, as a diff)
/clear   start a new conversation (the old one stays saved)
/resume  load the most recent saved session for this folder
/context how full the context is, and the biggest messages in it
/model   list models, or /model <id> to switch for this session
/copy    copy the last answer to the clipboard
/export  save this conversation as markdown
/turns N max model round trips per task (-1 = no limit)
/incognito web search and fetch off and out of every request (saves ~150 tokens each); again to turn back on
/lockedin LOCKED IN for long tasks: every ask, failure and changed file survives compaction. on/off, or alone to switch
/oneshot ONE SHOT: the model makes the change and stops; you test it. on/off, or alone to switch
/autochecklist a long task with no checklist gets one, walked step by step. on/off, or alone to switch
/jobs    shell commands running now; /jobs kill N (or all) stops one. Also works mid-task
/btw Q   a quick side question: answered from the conversation, no tools, not kept
/init    draft CRACKED.md (project rules) from this repo; /init <hint> to steer it
!cmd     run a shell command yourself; the output is not sent to the model
/help    this list
Plans
/plan    /plan <goal> researches and writes a plan (read-only), then asks what to do with it:
         y auto · m manual (stop after each step) · e edit (say what to change) · n not now
         /plan shows it and asks again · /plan edit (in $EDITOR) · /plan clear
         /plan skip N · /plan back (un-tick the last step)
         Esc pauses a running plan; mid-task, type "+ something" and Enter to add it as a plan step"""
COMMANDS = ["/undo", "/diff", "/clear", "/resume", "/context", "/model", "/copy", "/export",
            "/turns", "/incognito", "/lockedin", "/oneshot", "/autochecklist", "/jobs", "/plan", "/btw", "/init", "/help", "/quit"]
SWITCHES = {"/lockedin": "locked_in", "/oneshot": "one_shot", "/autochecklist": "auto_checklist"}  # command -> config key
DESCRIBE = dict(ln.split(None, 1) for ln in HELP.splitlines() if ln.startswith("/")) | {"/quit": "quit (the session is saved)"}
BTW = ("[Side question between tasks. Answer briefly from this conversation and what you know. "
       "No tools. This exchange is not kept.]\n\n{q}")
INIT = ("Write CRACKED.md in the workspace root. Read the README, build files and CI config first. "
        "If CRACKED.md or AGENTS.md exists, improve it instead of starting over.")  # user_message adds agent.CRACKED_MD
KEEP_SESSIONS = 20
HISTORY = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "cracked" / "history"


MENTION = re.compile(r"(?<!\S)@(\S+)")  # @path at a word start, so me@host stays text
ATTACH_LINES, ATTACH_CHARS, ATTACH_MAX = 400, 40_000, 1024 * 1024


def attach(text: str, ctx) -> tuple[str, list[str]]:
    """@path in a task: a text file goes in after the task, an image comes back for user_message.

    shortcut: paths with spaces are not matched; add quoting if users ask for it."""
    blocks, imgs, seen = [], [], set()
    for m in MENTION.finditer(text):
        raw = m.group(1).rstrip(".,;:!?)\"'")
        p = ctx.guard.resolve(raw)
        if p in seen or not p.is_file():  # @someone, a typo: left as plain text
            continue
        seen.add(p)
        try:
            size = p.stat().st_size
            with open(p, "rb") as f:
                is_image = True
                try:
                    images.kind(f.read(16))
                except images.Unsupported:
                    is_image = False
            data = b"" if is_image or size > ATTACH_MAX else p.read_bytes()
        except OSError as e:
            blocks.append(f"[@{raw} not attached: {e.strerror or e}]")
            continue
        if is_image and ctx.vision:
            imgs.append(raw)  # user_message attaches it
            ui.out(ui.line(f". @{raw} attached (image)"))
            continue
        if not data or b"\0" in data[:8192]:
            why = ("an image and this model has no vision" if is_image else f"{ui.human(size)}, too big"
                   if size > ATTACH_MAX else "binary" if data else "empty")
            note = f"@{raw} not attached: {why}"
            blocks.append(f"[{note}]")
        else:
            lines = data.decode("utf-8", "replace").splitlines(keepends=True)
            end, chars = 0, 0
            while end < min(ATTACH_LINES, len(lines)) and chars + len(lines[end]) <= ATTACH_CHARS:
                chars += len(lines[end])
                end += 1
            more = f"\n[more: read_file {raw} offset={end}]" if end < len(lines) else ""
            blocks.append(f"[@{raw} lines 1-{end} of {len(lines)}]\n" + "".join(lines[:end]).rstrip("\n") + more)
            note = f"@{raw} attached ({end} of {len(lines)} lines)"
        ui.out(ui.line(". " + note))
    return (text + "\n\n" + "\n\n".join(blocks) if blocks else text), imgs


def user_message(text: str, paths: list[str], ctx) -> dict:
    """A user turn, with any --image files attached as OpenAI image parts.

    Images ride in the first message so the model can look at them without spending a
    turn on see_image. Anything that fails is reported in the text, never raised: a
    missing file must not stop the run.
    """
    if "cracked.md" in text.lower():
        text = f"{text}\n\n{agent.CRACKED_MD}"
    text, found = attach(text, ctx)
    paths = list(paths) + found
    if not paths:
        return {"role": "user", "content": text}
    parts: list[dict] = [{"type": "text", "text": text}]
    notes = []
    for raw in paths:
        try:
            part, info = images.prepare(ctx.guard.resolve(raw))
        except (images.Unsupported, OSError) as e:
            notes.append(f"[could not attach {raw}: {e}]")
            continue
        parts.append(part)
        notes.append(f"[attached {raw}: {images.describe(info)}]")
    if notes:
        parts[0]["text"] = text + "\n\n" + "\n".join(notes)
    return {"role": "user", "content": parts}


# --------------------------------------------------------------------------- sessions
def sessions_dir(ws: Path) -> Path:
    d = ws / ".cracked" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (ws / ".cracked" / ".gitignore").write_text("*\n", encoding="utf-8")  # never in the snapshots
    return d


def new_session(ws: Path) -> Path:
    return sessions_dir(ws) / f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}.json"


def save_session(path: Path, model: str, messages: list[dict], undo: list, since: str | None) -> None:
    """Written after every task (and on interrupt), so a crash or Ctrl-D loses nothing."""
    if len(messages) < 2:  # only the system prompt: nothing to keep
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"model": model, "saved": datetime.datetime.now().isoformat(timespec="seconds"),
                               "since": since, "undo": undo, "messages": messages}), encoding="utf-8")
    tmp.replace(path)
    for old in sorted(path.parent.glob("*.json"))[:-KEEP_SESSIONS]:
        old.unlink(missing_ok=True)


def latest_session(ws: Path, exclude: Path | None = None) -> Path | None:
    found = [p for p in sorted(sessions_dir(ws).glob("*.json")) if p != exclude]
    return found[-1] if found else None


def load_session(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def text_of(m: dict) -> str:
    return agent._text(m.get("content"))


def paths(ws: Path, text: str) -> list[str]:
    """Names in the workspace (or an absolute folder) starting with `text`; folders end in /."""
    d, part = os.path.split(os.path.expanduser(text))
    root = Path(d) if os.path.isabs(d) else ws / d
    try:
        names = sorted(n for n in os.listdir(root) if n.startswith(part)
                       and (part.startswith(".") or not n.startswith(".")))
    except OSError:
        names = []
    # "/" on every platform: the model's shell is bash (Git Bash on Windows)
    return [(f"{d}/{n}" if d and not d.endswith(("/", os.sep)) else d + n) + ("/" if (root / n).is_dir() else "") for n in names]


_FILES: dict[Path, tuple[float, list[str]]] = {}


def files(ws: Path) -> list[str]:
    """Every file in the workspace for the @ menu: git's list (tracked and new, not ignored), else a walk.

    shortcut: cached 3 s per workspace so typing doesn't run git per key; a file made since shows up after that."""
    at, names = _FILES.get(ws, (0.0, []))
    if time.monotonic() - at < 3:
        return names
    try:
        r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ws, capture_output=True,
                           text=True, timeout=3, stdin=subprocess.DEVNULL)
        names = r.stdout.splitlines() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        names = None
    if names is None:  # not a git repo: walk it, skipping hidden and build folders, up to 5000 files
        names = []
        for root, dirs, found in os.walk(ws):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in tools.SKIP_DIRS]
            names += [Path(os.path.relpath(os.path.join(root, n), ws)).as_posix() for n in found if not n.startswith(".")]
            if len(names) > 5000:
                break
    _FILES[ws] = (time.monotonic(), names)
    return names


def fuzzy(q: str, names: list[str], limit: int = 30) -> list[str]:
    """Names matching q, best first: file name starts with it, contains it, path contains it, then
    its letters in order (@mnpy finds main.py). Shorter paths first within each."""
    q, scored = q.lower(), []
    for n in names:
        low = n.lower()
        base = re.split(r"[\\/]", low)[-1]
        it = iter(low)
        rank = (0 if base.startswith(q) else 1 if q in base else 2 if q in low
                else 3 if all(c in it for c in q) else None)
        if rank is not None:
            scored.append((rank, len(n), n))
    return [n for *_, n in sorted(scored)[:limit]]


def suggest(before: str, ws: Path, tab: bool) -> list[tuple[str, int, str]]:
    """The prompt's menu for the text left of the cursor, as (text, start offset, description):
    /commands at the start of the line and @paths as you type, plain paths only on Tab."""
    if before.startswith("/") and not any(c.isspace() for c in before):
        return [(c, -len(before), DESCRIBE.get(c, "")) for c in COMMANDS if c.startswith(before)]
    word = re.split(r"\s", before)[-1]
    if word.startswith("@"):
        q = word[1:]
        found = paths(ws, q)  # what is right here first (folders too), then files anywhere in the repo
        if q and "/" not in q and os.sep not in q:
            found += [n for n in fuzzy(q, files(ws)) if n not in found]
        return [("@" + p, -len(word), "folder" if p.endswith("/") else "attach") for p in found]
    return [(p, -len(word), "") for p in paths(ws, word)] if tab and word else []


def marks(line: str, ws: Path, first: bool) -> list[tuple[str, str]]:
    """One prompt line as (colour, text): a known /command, an unknown one (bad), an @file that
    will attach, an @path that won't (missing). The first line only can hold a command."""
    spans = []
    if first and (m := re.match(r"/[a-z]+(?=\s|$)", line)):
        spans.append((m.start(), m.end(), "cmd" if m.group() in COMMANDS or m.group() in ("/q", "/exit") else "bad"))
    for m in MENTION.finditer(line):
        p = Path(m.group(1)).expanduser()
        spans.append((m.start(), m.end(), "file" if (p if p.is_absolute() else ws / p).is_file() else "missing"))
    out, i = [], 0
    for a, b, kind in spans:
        out += [("", line[i:a]), (kind, line[a:b])]
        i = b
    out.append(("", line[i:]))
    return [x for x in out if x[1]]


# ------------------------------------------------------------------------- the REPL
class Repl:
    def __init__(self, args, cfg, ctx, model, messages, watching, since: str | None):
        self.args, self.cfg, self.ctx, self.model, self.messages = args, cfg, ctx, model, messages
        self.watching = watching
        self.ws = ctx.workspace
        self.since = since              # snapshot at session start: /diff compares against it
        self.undo: list[list] = []      # [len(messages) before the task, snapshot sha before it]
        self.session = new_session(self.ws)
        self.last_answer = ""
        self.started = datetime.datetime.now()
        self.prompt = None              # prompt.session() in a colour terminal; else plain input()
        self.draft = ""                 # text the next prompt starts with
        ctx.guard.explain = lambda why: agent.explain(self.model, self.messages,  # /clear and /model swap these
                                                      tools.schemas(ctx.vision, web=not ctx.incognito), why)
        ui.MODES = self.modes  # the status bar, the banner and the live footer show these
        ui.MODE_CMDS = {cmd: (lambda arg, key=key: self.switch(key, arg)) for cmd, key in SWITCHES.items()}

    # ---------------------------------------------------------------- terminal set-up
    def setup_prompt(self) -> None:
        if ui.ON and sys.stdin.isatty():
            try:
                from . import prompt  # prompt_toolkit only where there is a keyboard to type on
            except ImportError:
                return self.say("prompt_toolkit is not installed (pip install prompt_toolkit): "
                                "no / and @ menus this session", ui.AMBER)
            self.prompt = prompt.session(self.ws, self.ctx.guard, self.status)
            ui.ASK = prompt.ask
            ui.CHOOSE = prompt.choose

    def put_draft(self, draft: str) -> None:
        """Pre-fill the next prompt with text the user already typed."""
        self.draft = draft

    def modes(self) -> dict:
        """The task modes as they are now in this session (the dashboard's switches reach ctx at the next turn)."""
        return {"LOCKED IN": self.ctx.locked, "ONE SHOT": agent.is_one_shot(self.messages[0]["content"]),
                "AUTO CHECKLIST": self.ctx.auto_checklist}

    def switch(self, key: str, arg: str) -> str:
        """/lockedin, /oneshot, /autochecklist: on, off, or (no argument) the other way. Applies to this session at
        once and is saved to the config file like the dashboard switch, so new sessions and the dashboard agree."""
        name = {"locked_in": "LOCKED IN", "one_shot": "ONE SHOT", "auto_checklist": "AUTO CHECKLIST"}[key]
        cmd = next(c for c, k in SWITCHES.items() if k == key)
        a = arg.strip().lower()
        if a not in ("", "on", "off"):
            return ui.paint(f"{cmd} on | off, or {cmd} alone to switch it", ui.AMBER)
        want = (not self.modes()[name]) if not a else a == "on"
        notes, saved = [], {key: want}
        if key == "locked_in":
            self.ctx.locked = want
            if want and not self.ctx.auto_checklist:  # as the dashboard's LOCKED IN switch does
                self.ctx.auto_checklist = saved["auto_checklist"] = True
                notes.append("auto checklist is on too")
        elif key == "auto_checklist":
            self.ctx.auto_checklist = want
        else:
            before = self.messages[0]["content"]
            self.messages[0]["content"] = agent.one_shot(before, want)
            if self.messages[0]["content"] != before:
                notes.append("the system prompt changed, so the next request reads the whole prompt once")
        try:
            for k, v in saved.items():
                config.set_key(k, v)
            self.ctx.config_mtime = config.path().stat().st_mtime  # our own write: the follower stays quiet
            notes.append("saved to the config file")
        except (OSError, ValueError) as e:
            notes.append(f"this session only: the config file was not changed ({e})")
        return (ui.paint(f"{name} {'on' if want else 'off'}: " + "; ".join(notes), ui.NEON if want else ui.INK2)
                + "\n" + ui.modes_text(self.modes()))

    def cmd_lockedin(self, arg: str) -> None:
        ui.out(self.switch("locked_in", arg))

    def cmd_oneshot(self, arg: str) -> None:
        ui.out(self.switch("one_shot", arg))

    def cmd_autochecklist(self, arg: str) -> None:
        ui.out(self.switch("auto_checklist", arg))

    def status(self) -> str:
        """The line under `>_`: the task modes (each on or off), then model, context, tasks, session age."""
        mins = int((datetime.datetime.now() - self.started).total_seconds() // 60)
        tasks = sum(1 for m in self.messages if m["role"] == "user")
        p = self.ctx.plan
        shown = "" if p is None or p.state == "done" else f"{p.label()} · " if p.state == "running" else (
            f"{'plan' if p.source == 'user' else '☐'} waiting: /plan · ")
        return ui.modes_text(self.modes()) + ui.paint(
            f"  │  {shown}{self.model.model} · ctx {100 * self.ctx.usage // self.args.context}% · "
            f"{tasks} task{'s' if tasks != 1 else ''} · {mins}m · /help", ui.MUTED)

    def read(self) -> str:
        """One task. A line ending in \\ continues on the next; a paste keeps its newlines."""
        draft, self.draft = self.draft, ""
        if self.prompt:
            return self.prompt.prompt(default=draft).strip()
        mode = ui.mode(self.ctx.guard)
        status = self.status() + "\n" if ui.ON else ""
        text = input(status + mode + ui.paint(ui.PROMPT, ui.NEON, bold=True, prompt=True))
        while text.endswith("\\"):
            text = text[:-1] + "\n" + input(ui.paint(" ... ", ui.MUTED, prompt=True))
        return text.strip()

    # ---------------------------------------------------------------- the loop
    def loop(self) -> int:
        self.setup_prompt()
        p = self.ctx.plan
        if p is not None and p.state != "done" and (i := p.current()) is not None:
            self.say(f"unfinished {'plan' if p.source == 'user' else 'checklist'}: step {i + 1} of {len(p.steps)}, "
                     f"{p.steps[i].text[:60]} (/plan carries on)", ui.AMBER)
        while True:
            try:
                prefix = ui.mode(self.ctx.guard, prompt=False) + ui.paint(ui.PROMPT, ui.NEON, bold=True)
                task = self.read()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            ui.record(prefix + task)  # input() echoed it; replays need it too
            if not task:
                continue
            word = task.split()[0]
            if word in ("/q", "/quit", "/exit"):
                break
            try:
                if task.startswith("!"):
                    self.shell(task[1:].strip())
                elif word in COMMANDS:
                    self.command(task)
                elif word == "/":
                    self.cmd_help("")
                elif word[0] == "/" and word[1:].isalpha():  # a typo'd command, not a path like /etc/hosts
                    near = difflib.get_close_matches(word, COMMANDS, 1)
                    self.say(f"unknown command {word}: " + (f"did you mean {near[0]}? " if near else "") + "/help lists them", ui.CRIT)
                else:
                    self.run_task(task)
                    p = self.ctx.plan
                    if self.ctx.plan_ask:
                        self.plan_menu()
                    elif p is not None and p.source == "user" and p.state != "done" and (i := p.current()) is not None:
                        self.say(f"the plan waits at step {i + 1} of {len(p.steps)}: /plan carries on", ui.MUTED)
            except KeyboardInterrupt:
                ui.out(ui.paint("interrupted", ui.AMBER))
        self.save()
        return 0

    def run_task(self, task: str | None) -> str:
        """One task; None runs (or carries on) ctx.plan instead. Returns the answer."""
        safety.snapshot(self.ws, f"harness: before task {len(self.undo) + 1}")
        self.ctx.snapped = True
        self.undo.append([len(self.messages), safety.head(self.ws)])
        p = self.ctx.plan
        if task is not None and p is not None and p.source == "model" and p.state == "running":
            p.state = "draft"  # a new ask pauses the model's own checklist; /plan picks it up again
            plan.save(self.ws, p)
        if task is not None:
            if done := tools.finished_jobs(self.ctx):  # after the task: the anchor restates a message's first line
                task = f"{task}\n\n{done}"
            self.messages.append(user_message(f"{task}\n\n{agent.INVESTIGATE}" if self.ctx.guard.investigate else task, [], self.ctx))
        watch = self.watching(hold=True)  # its done line closes the task, under the answer
        answer, started = "", time.monotonic()
        try:
            with watch:
                kw = dict(context=self.args.context, compact_at=self.args.compact_at)
                answer = (agent.drive(self.model, self.messages, self.ctx, self.args.max_turns, **kw) if task is not None
                          else agent.run_plan(self.model, self.messages, self.ctx, **kw))
        except KeyboardInterrupt:
            ui.out(ui.paint("stopped. The conversation is kept: type to carry on, or /undo to roll the files back."
                            + (" /plan carries on with the plan." if self.ctx.plan is not None and self.ctx.plan.state == "running" else ""),
                            ui.AMBER))
        except OSError as e:
            ui.out(ui.paint(f"{llm.failure(self.args.base_url, e)}. The conversation is kept; try again.", ui.CRIT))
            ui.notify(f"❌ <b>failed</b>: {ui._esc(llm.failure(self.args.base_url, e))}")
        finally:
            # typed while it worked: the next prompt starts with it, and with anything queued it never read
            self.put_draft("\n".join([*getattr(watch, "queue", []), getattr(watch, "typed", "")]).strip())
            self.save()
            ui.bell()
        if answer:  # finished pings at once; Ctrl+C means you are at the keyboard, so no ping
            self.ping_done(task, answer, time.monotonic() - started)
            self.last_answer = answer
            ui.out(f"\n{ui.answer(answer)}\n")
        if getattr(watch, "summary", ""):
            ui.out(watch.summary + "\n")  # a gap before the prompt's own rule
        return answer

    def ping_done(self, task: str | None, answer: str, seconds: float) -> None:
        """Telegram: what was asked, how long, files changed since the task's own snapshot, the closing reply."""
        if not ui.TELEGRAM:
            return
        numstat, before = "", self.undo[-1][1] if self.undo else None
        if before:
            safety.snapshot(self.ws, "harness: after task")  # the changes are in the store, so git can count them
            numstat = safety.git(self.ws, "diff", "--numstat", before, "HEAD").stdout
        p = self.ctx.plan
        ui.notify(ui.done_message(task if task is not None else f"the plan: {p.goal}" if p is not None else "the plan",
                                  answer, seconds, numstat))

    def save(self) -> None:
        try:
            save_session(self.session, self.model.model, self.messages, self.undo, self.since)
        except OSError as e:
            ui.out(ui.paint(f"could not save the session: {e}", ui.CRIT))

    # ---------------------------------------------------------------- commands
    def say(self, text: str, rgb: str = "") -> None:
        ui.out(ui.paint(text, rgb or ui.INK2))  # read now, not at import: config theme changes the colours

    def shell(self, cmd: str) -> None:
        """!cmd: the user's own command. Output shown and kept in the replay, never sent to the model."""
        if not cmd:
            return self.say("!<command> runs it in the workspace, e.g. !git status", ui.MUTED)
        args, use_shell = tools.shell_argv(cmd)
        r = subprocess.run(args, shell=use_shell, cwd=self.ws, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           encoding="utf-8", errors="replace")
        for ln in (r.stdout + r.stderr).rstrip("\n").splitlines():
            ui.out(ln)
        self.say(f"[exit {r.returncode}] (not sent to the model)", ui.MUTED if r.returncode == 0 else ui.CRIT)

    def command(self, line: str) -> None:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        getattr(self, "cmd_" + cmd[1:])(arg)

    def cmd_help(self, _arg: str) -> None:
        """HELP in colour: section headings amber, commands and keys neon, what they do in the body colour."""
        width = shutil.get_terminal_size((100, 20)).columns - 1

        def show(head: str, body: str) -> None:  # a long body wraps under itself, not under the key
            rows = ui.wrap(body, max(20, width - len(head))).split("\n")
            ui.out(ui.paint(head, ui.NEON, bold=True) + ui.paint(rows[0], ui.INK2))
            for r in rows[1:]:
                ui.out(" " * len(head) + ui.paint(r, ui.INK2))

        for ln in HELP.splitlines():
            if not ln.startswith((" ", "/", "!")):
                ui.out(ui.paint(ln, ui.AMBER, bold=True))
            elif ln.startswith("         "):  # a command's next line
                show(" " * 9, ln.strip())
            elif ln.startswith("  "):  # a key: name, then what it does
                show(ln[:17], ln[17:])
            else:  # /command (with its argument: /btw Q, /turns N), then what it does
                cmd = re.match(r"\S+(?: [A-Z]\b)?", ln).group()
                show(cmd.ljust(9) if len(cmd) < 9 else cmd + " ", ln[len(cmd):].strip())

    def cmd_turns(self, arg: str) -> None:
        try:
            self.args.max_turns = int(arg) if arg else self.args.max_turns
        except ValueError:
            return self.say(f"/turns takes a whole number, not {arg!r}", ui.CRIT)
        self.say("max turns per task: " + ("no limit" if self.args.max_turns < 1 else str(self.args.max_turns))
                 + "   (/turns N to change; -1 = no limit)", ui.NEON)

    def cmd_incognito(self, _arg: str) -> None:
        self.ctx.incognito = not self.ctx.incognito
        self.say("incognito: web_search and web_fetch are off and left out of every request (/incognito again to undo)"
                 if self.ctx.incognito else "incognito off: web tools are back", ui.NEON)

    def cmd_undo(self, _arg: str) -> None:
        if not self.undo:
            return self.say("nothing to undo in this session", ui.MUTED)
        index, sha = self.undo.pop()
        task = text_of(self.messages[index]) if index < len(self.messages) else ""
        if not sha:
            return self.say("cannot undo: snapshots are off (snapshot = false in the config, or [s] at the size warning)" if not safety.SNAPSHOT else
                            "cannot undo: there was no snapshot before that task (see `. snapshot failed`)", ui.CRIT)
        result = safety.restore(self.ws, sha)
        del self.messages[index:]
        self.save()
        self.say(f"undone: {task.splitlines()[0][:80] if task else 'the last task'}", ui.NEON)
        self.say(f"files: {result}. The model has forgotten that task. "
                 "Changes outside the workspace (installs, services) are not undone.", ui.MUTED)

    def cmd_diff(self, _arg: str) -> None:
        if not self.since:
            return self.say("snapshots are off (snapshot = false in the config, or [s] at the size warning)" if not safety.SNAPSHOT else
                            "no session-start snapshot to compare with", ui.CRIT)
        stat, patch = safety.diff(self.ws, self.since)
        if not stat:
            return self.say("no file changes this session", ui.MUTED)
        ui.out(stat)
        ui.out(ui.diff(patch, max_lines=400))

    def cmd_clear(self, _arg: str) -> None:
        self.save()
        del self.messages[1:]
        self.undo.clear()
        self.session = new_session(self.ws)
        self.ctx.usage = 0
        ui.TRANSCRIPT.clear()
        ui.redraw()
        self.say("new conversation (the last one is saved; /resume brings it back)", ui.NEON)

    def cmd_resume(self, _arg: str) -> None:
        self.save()
        path = latest_session(self.ws, exclude=self.session)
        if not path:
            return self.say("no saved session in this folder", ui.MUTED)
        self.resume(path)

    def resume(self, path: Path) -> None:
        data = load_session(path)
        self.messages[1:] = data["messages"][1:]  # keep today's system prompt
        self.undo = data.get("undo") or []
        self.since = data.get("since") or self.since
        self.session = path
        tasks = [text_of(m) for m in self.messages if m["role"] == "user"]
        answers = [text_of(m) for m in self.messages if m["role"] == "assistant" and not m.get("tool_calls")]
        self.say(f"resumed {path.name}: {len(tasks)} task{'s' if len(tasks) != 1 else ''}, saved {data.get('saved', '?')}", ui.NEON)
        if tasks:
            self.say("last task: " + tasks[-1].splitlines()[0][:100], ui.MUTED)
        if answers:
            self.last_answer = answers[-1]
            self.say("last answer: " + " ".join(answers[-1].split())[:160], ui.MUTED)

    def cmd_jobs(self, arg: str) -> None:
        self.say(tools.jobs_command(arg))

    def cmd_plan(self, arg: str) -> None:
        """/plan <goal> researches a plan; /plan [edit|skip N|back|clear] works on the one saved; /plan alone shows
        it and, with steps left, the plan menu."""
        word, _, rest = arg.partition(" ")
        if word not in ("", "edit", "skip", "back", "clear"):
            return self.plan_research(arg)
        try:
            p = plan.load(self.ws)
        except plan.PlanError as e:
            return self.say(f"{plan.path(self.ws)} does not parse: {e}. /plan edit to fix it", ui.CRIT)
        if word == "edit":
            return self.plan_edit()
        if p is None:
            return self.say("no plan. /plan <goal> researches one (read-only), then asks how to run it", ui.MUTED)
        self.ctx.plan = p
        if word == "skip" or word == "back":
            n = int(rest) if rest.strip().isdigit() else None
            if word == "skip" and not (n and 1 <= n <= len(p.steps)):
                return self.say(f"/plan skip N, with N from 1 to {len(p.steps)}", ui.CRIT)
            if word == "back":
                done = [k for k, s in enumerate(p.steps) if s.mark == "x"]
                if not done:
                    return self.say("no ticked step to take back", ui.MUTED)
                n = done[-1] + 1
            p.steps[n - 1].mark = "-" if word == "skip" else " "
            p.steps[n - 1].result = p.steps[n - 1].result if word == "skip" else ""
            if p.state == "done":
                p.state = "draft"
            plan.save(self.ws, p)
            self.say(f"step {n} {'skipped' if word == 'skip' else 'un-ticked (its files stay as they are; /undo rolls files back)'}", ui.NEON)
        if word == "clear":
            plan.path(self.ws).unlink(missing_ok=True)
            self.ctx.plan = None
            return self.say("plan cleared", ui.NEON)
        self.plan_show(p)
        if not word:
            self.plan_menu()

    PLAN_MENU = [("y", "Yes, auto: every step on its own; stops only if the plan has to change"),
                 ("m", "Manual: one step, then ask again"),
                 ("e", "Edit: say what to change, and the plan is rewritten"),
                 ("n", "No: keep it for later (/plan asks again)")]

    def plan_menu(self) -> None:
        """y/m/e/n until the plan is done, the user says no, or a run stops without asking (Esc, Ctrl+B)."""
        while (p := self.ctx.plan) is not None and (i := p.current()) is not None:
            self.ctx.plan_ask = False
            key = ui.choose(f"Run the plan from step {i + 1} of {len(p.steps)}?", self.PLAN_MENU)
            if key == "n":
                return self.say("plan kept: /plan asks again", ui.MUTED)
            if key == "e":
                self.plan_rewrite(p)
                continue
            p.mode = "auto" if key == "y" else "manual"
            if self.ctx.guard.investigate:  # running a plan makes changes, which investigate mode refuses
                self.ctx.guard.investigate = False
                self.say("investigate mode off: the plan makes changes", ui.AMBER)
            self.run_task(None)
            if not self.ctx.plan_ask:
                return

    def plan_rewrite(self, p) -> None:
        """Menu e: the user says what to change; the model rewrites the plan (no tools), and both stay in the history."""
        try:
            ask = ui.harness_input(ui.paint("What should change? ", ui.NEON, bold=True, prompt=True)).strip()
        except (EOFError, KeyboardInterrupt):
            ask = ""
        if not ask:
            return self.say("no change asked: the plan stays as it is", ui.MUTED)
        request = {"role": "user", "content": plan.EDIT.format(ask=ask, plan=plan.render(p))}
        try:
            with self.watching():
                reply = agent.text_only(self.model, self.messages + [request],
                                        tools.schemas(self.ctx.vision, web=not self.ctx.incognito))
            new = plan.parse(reply.text or "", p.goal)
        except (KeyboardInterrupt, OSError, plan.PlanError) as e:
            return self.say(f"the plan was not changed: {e or 'stopped'}", ui.CRIT)
        new.source, new.state, new.mode = p.source, "draft", p.mode
        self.messages += [request, {"role": "assistant", "content": reply.text}]
        self.ctx.plan = new
        plan.save(self.ws, new)
        self.plan_show(new)

    def plan_show(self, p) -> None:
        state = {"draft": "waiting", "running": "paused", "done": "done"}[p.state]
        self.say(f"{'plan' if p.source == 'user' else 'the model’s checklist'}: {p.goal} · {p.done()}/{len(p.steps)} · {state}", ui.NEON)
        ui.out(ui.pretty(plan.checklist(p)))
        self.say(f"file: {plan.path(self.ws)} · /plan edit changes it", ui.MUTED)

    def plan_research(self, goal: str) -> None:
        """Phase 1: a read-only task that ends in a plan; the harness parses and saves it (one retry if it can't)."""
        p = self.ctx.plan
        if p is not None and p.state != "done" and p.current() is not None and p.source == "user":
            return self.say("a plan is unfinished: /plan carries on, /plan clear drops it", ui.AMBER)
        self.ctx.researching = True
        try:
            answer = self.run_task(f"{goal}\n\n" + plan.research(self.ctx.guard.assume_yes))
        finally:
            self.ctx.researching = False
        if not answer:
            return
        try:
            new = plan.parse(answer, goal)
        except plan.PlanError as e:
            self.say(f"the plan could not be used ({e}): asking once more", ui.AMBER)
            try:
                with self.watching():
                    reply = agent.text_only(self.model, self.messages + [{"role": "user", "content": plan.FIX.format(why=e)}],
                                            tools.schemas(self.ctx.vision, web=not self.ctx.incognito))
            except (KeyboardInterrupt, OSError) as err:
                return self.say(f"no plan: {err or 'stopped'}", ui.CRIT)
            try:
                new = plan.parse(reply.text or "", goal)
            except plan.PlanError as e2:
                plan.path(self.ws).parent.mkdir(parents=True, exist_ok=True)
                plan.path(self.ws).write_text(f"# Plan: {goal}\n\n{reply.text or answer}\n", encoding="utf-8")
                return self.say(f"still not a usable plan ({e2}). Its text is in {plan.path(self.ws)}: "
                                "/plan edit to fix it, then /plan", ui.CRIT)
        new.source, new.state = "user", "draft"
        self.ctx.plan = new
        plan.save(self.ws, new)
        self.plan_show(new)
        self.plan_menu()

    def plan_edit(self) -> None:
        path = plan.path(self.ws)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Plan: <goal>\n\n- [ ] 1. <one action> | files: <a, b> | check: <command>\n", encoding="utf-8")
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        with ui.paused():
            subprocess.call([*shlex.split(editor), str(path)])
        try:
            self.ctx.plan = plan.load(self.ws)
        except plan.PlanError as e:
            return self.say(f"the plan does not parse: {e}. /plan edit again", ui.CRIT)
        self.plan_show(self.ctx.plan)

    def cmd_btw(self, arg: str) -> None:
        """Ask on the side: the whole history goes along, the question and answer are not kept."""
        if not arg:
            return self.say("/btw <question>: a quick side question; neither it nor the answer is kept", ui.MUTED)
        side = self.messages + [{"role": "user", "content": BTW.format(q=arg)}]
        # Pin the history (as the anchor does) so Strata keeps its cache point and the next task stays fast.
        self.model.extra = {"strata_prefix": {"messages": len(self.messages)}}
        try:
            with self.watching():
                reply = agent.text_only(self.model, side, tools.schemas(self.ctx.vision, web=not self.ctx.incognito))
        except KeyboardInterrupt:
            return self.say("stopped", ui.AMBER)
        except OSError as e:
            return self.say(llm.failure(self.args.base_url, e), ui.CRIT)
        finally:
            self.model.extra = {}
        ui.out(f"\n{(reply.text or '').strip() or '(no answer)'}\n")
        self.say("(side answer: not kept in the conversation)", ui.MUTED)

    def cmd_init(self, arg: str) -> None:
        """A normal task (so /undo works) with a canned prompt."""
        self.run_task(INIT + (f"\n\nAlso: {arg}" if arg else ""))
        self.say("restart cracked to load the new CRACKED.md", ui.MUTED)  # the system prompt is built at startup

    def cmd_context(self, _arg: str) -> None:
        a = self.args
        self.say(f"context: {self.ctx.usage} of {a.context} tokens ({100 * self.ctx.usage // a.context}%) at the "
                 f"last request · auto-compact at {a.compact_at:.0%} · {len(self.messages)} messages", ui.NEON)
        sized = sorted(((len(json.dumps(m)) // 4, i, m) for i, m in enumerate(self.messages)), reverse=True)[:5]
        for tokens, i, m in sized:
            label = m["role"] + (f" {m['tool_calls'][0]['function']['name']}" if m.get("tool_calls") else "")
            self.say(f"  ~{tokens:>6} tok  #{i:<3} {label:<16} {' '.join(text_of(m).split())[:60]}", ui.MUTED)

    def cmd_model(self, arg: str) -> None:
        if arg:
            self.model.model = arg
            self.ctx.vision = llm.probe(self.args.base_url, arg)["vision"]
            return self.say(f"model: {arg} for this session (vision {'on' if self.ctx.vision else 'off'}); "
                            f"set model in the config file to keep it", ui.NEON)
        try:
            with urllib.request.urlopen(self.args.base_url.rstrip("/") + "/models", timeout=8) as r:
                ids = [m.get("id", "?") for m in json.load(r).get("data") or []]
        except Exception as e:
            return self.say(f"cannot list models on {self.args.base_url}: {e}", ui.CRIT)
        for i in ids:
            self.say(("* " if i == self.model.model else "  ") + i, ui.NEON if i == self.model.model else ui.INK2)
        self.say("/model <id> switches", ui.MUTED)

    def cmd_copy(self, _arg: str) -> None:
        if not self.last_answer:
            return self.say("no answer to copy yet", ui.MUTED)
        self.say(f"copied the last answer ({copy(self.last_answer)})", ui.NEON)

    def cmd_export(self, arg: str) -> None:
        path = Path(arg).expanduser() if arg else self.ws / ".cracked" / "exports" / f"{self.session.stem}.md"
        path = path if path.is_absolute() else self.ws / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown(self.messages), encoding="utf-8")
        self.say(f"exported to {path}", ui.NEON)


def markdown(messages: list[dict]) -> str:
    out = []
    for m in messages[1:]:
        text = text_of(m).strip()
        if m["role"] == "user":
            out.append(f"## you\n\n{text}")
        elif m["role"] == "assistant":
            calls = "".join(f"\n\n`{c['function']['name']}` `{c['function']['arguments'][:300]}`"
                            for c in m.get("tool_calls") or [])
            out.append(f"## cracked\n\n{text}{calls}".rstrip())
        else:
            lines = text.splitlines()
            more = f"\n… {len(lines) - 20} more lines" if len(lines) > 20 else ""
            out.append("```\n" + "\n".join(lines[:20]) + more + "\n```")
    return "\n\n".join(out) + "\n"


def copy(text: str) -> str:
    """Clipboard via the first tool found; else OSC 52, which most terminals (and SSH) pass on."""
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, timeout=5, check=True)
                return cmd[0]
            except (subprocess.SubprocessError, OSError):
                continue
    sys.stdout.write(f"\033]52;c;{base64.b64encode(text.encode()).decode()}\a")
    sys.stdout.flush()
    return "terminal clipboard, OSC 52"
