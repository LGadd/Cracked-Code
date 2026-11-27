"""The tool set: JSON schemas and handlers in one registry."""

from __future__ import annotations

import ast
import atexit
import contextlib
import difflib
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import images, safety, web
from . import plan as plan_mod  # "plan" is also a Ctx field

READ_BUDGET = 8000  # chars per read_file page; the model pages on with offset
SPILL_AT = 6000     # longer tool output goes to a file; only a preview enters context (2000 cost a read-back turn)
PREVIEW = 600       # chars kept from each end of a spilled output
SHELL_TIMEOUT_MAX = 3600  # config `shell_timeout_max`: longest run_shell timeout in seconds; 0 = no limit


def spill(ctx: "Ctx", text: str, tool: str) -> str:
    """Keep big output out of the conversation: save it, return head + tail + path."""
    if len(text) <= SPILL_AT:
        return text
    out = ctx.workspace / ".cracked" / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out.parent / ".gitignore").write_text("*\n", encoding="utf-8")  # keep spills out of the snapshots
    last = max((int(n) for f in out.iterdir() if (n := f.name.split("-")[0]).isdigit()), default=0)
    path = out / f"{last + 1}-{tool}.txt"  # past the highest number, so a deleted file's number is never reused
    path.write_text(text, encoding="utf-8")
    rel = path.relative_to(ctx.workspace).as_posix()  # the model's shell is bash, Git Bash on Windows
    return (f"{text[:PREVIEW]}\n... [{len(text) - 2 * PREVIEW} chars not shown; full output: {rel} "
            f"- grep it or read_file with offset] ...\n{text[-PREVIEW:]}")


@dataclass
class Ctx:
    workspace: Path
    guard: safety.Guard
    # Images the model asked to look at this turn: (info dict, image part). tools.call
    # appends them to the tool result message so the next request carries them; see
    # attach_pending(). Empty unless a vision-capable model is configured.
    pending_images: list = field(default_factory=list)
    vision: bool = False
    # The last write/edit as (added lines, removed lines, unified diff), for the terminal only:
    # the model gets the usual one-line result. The loop reads it and clears it.
    change: tuple | None = None
    usage: int = 0  # prompt tokens of the latest request, for /context and the status line
    anchor: bool = True  # re-state the task to the model every few turns; off only in tests
    think_auto: bool = False  # the agent loop sets the thinking cap turn by turn (config reasoning_budget = 0)
    fold: bool = False         # a finished task collapses to ask -> answer (on in the CLI; off here so tests see raw history)
    fold_keep: int = 0         # chars of touched files a fold keeps (0 = none; the CLI sets agent.FOLD_KEEP)
    fold_above: int = 0        # fold only once the prompt is this many tokens (0 = always; the CLI sets agent.FOLD_ABOVE)
    incognito: bool = False    # /incognito: web tools left out of requests and refused
    locked: bool = False       # LOCKED IN mode (config locked_in): the ledger below is restated and survives compaction
    config_mtime: float | None = None  # the config file's mtime when last read; None = don't follow it (tests)
    # Every ask (tasks and messages queued mid-task), failed calls and changed files this session; agent.ledger() renders it.
    # shortcut: not saved with the session, so /resume starts it empty; save it if resumed long tasks need it.
    ledger: dict = field(default_factory=lambda: {"asks": [], "errors": [], "files": []})
    # Plan mode (cracked_code/plan.py, docs/plan-mode.md): the plan the harness walks the model through, if any.
    plan: "plan_mod.Plan | None" = None
    researching: bool = False   # /plan <goal>: outside Fuck It, writes refused and shell that looks like a write asks
    plan_handoff: bool = False  # todo_write or auto_checklist made a checklist: agent.drive hands it to run_plan
    plan_ask: bool = False      # the plan waits for the user: the REPL shows the plan menu (y auto, m manual, e edit, n no)
    auto_checklist: bool = False  # config auto_checklist: ask for a checklist when a long task has none
    task: str = ""              # first line of the user's latest task (a model checklist's goal)
    stopped: str = ""           # why agent.run stopped early (stuck, out of turns, empty); "" = the model finished
    snapped: bool = False       # the REPL has just snapshotted: the loop's first turn skips its own
    backgrounded: int | None = None  # set by run_shell when the user pressed Ctrl+B: the loop hands the turn back


RESEARCH_ONLY = ("ERROR: plan mode: research only. Nothing can be written until the user approves the plan; "
                 "finish with the plan as your reply.")
INVESTIGATE_ONLY = ("ERROR: investigate mode: read only. Nothing can be written; put the change you would make in "
                    "your reply as a suggestion, and finish with what you found.")
WRITERS = ("write_file", "edit_file", "replace_def", "delete_file")


def todo_write(ctx: Ctx, items: list) -> str:
    """The model's own checklist: saved as a plan (source model) that the harness then walks it through."""
    if ctx.researching or ctx.guard.investigate:
        return RESEARCH_ONLY if ctx.researching else INVESTIGATE_ONLY
    if ctx.plan is not None and ctx.plan.source == "user" and ctx.plan.state != "done":
        return "ERROR: the user's plan is in charge; do the step you were given."
    if not isinstance(items, list):
        return "ERROR: items must be a list of steps."
    steps = []
    for it in items:
        text = it.get("text", "") if isinstance(it, dict) else it if isinstance(it, str) else ""
        check = it.get("check", "") if isinstance(it, dict) else ""
        if " ".join(str(text).split()):
            steps.append(plan_mod.Step(text=" ".join(str(text).split()), check=str(check or "").strip()))
    if len(steps) < 3:
        return "ERROR: a checklist needs 3 or more steps; for fewer, just do them."
    old = ctx.plan if ctx.plan is not None and ctx.plan.state != "done" else None
    if old:  # a revision: done steps keep their tick, undone ones it left out are shown as dropped
        done = {s.text: s for s in old.steps if s.mark in ("x", "-")}
        for s in steps:
            if s.text in done:
                s.mark, s.result = done[s.text].mark, done[s.text].result
        kept = {s.text for s in steps}
        steps += [plan_mod.Step(text=f"{s.text} (dropped by the model)", mark="-") for s in old.steps
                  if s.mark not in ("x", "-") and s.text not in kept]
    ctx.plan = plan_mod.Plan(goal=ctx.task or steps[0].text, steps=steps, source="model", state="running")
    plan_mod.save(ctx.workspace, ctx.plan)
    ctx.plan_handoff = True
    return f"Checklist saved ({len(steps)} steps). Stop here: the harness gives you one step at a time from now on."


# Approvals: read never asks (a refused read only pushed the model to `cat`). Write asks in
# manual mode only when it would overwrite an existing file. Edit asks in manual mode.
# Delete always asks, even in Fuck It Mode.


def _diff(ctx: Ctx, p: Path, old: str, new: str) -> tuple[int, int, str]:
    try:
        rel = str(p.relative_to(ctx.workspace))
    except ValueError:
        rel = str(p)
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm="", n=2))
    plus = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    minus = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return plus, minus, "\n".join(lines)


def read_file(ctx: Ctx, path: str, offset: int = 0, limit: int = 400) -> str:
    try:
        p = ctx.guard.resolve(path)
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"ERROR: cannot read {path}: {e.strerror or e}"
    lines = text.splitlines(keepends=True)
    start = int(offset)
    end, size = start, 0
    while end < min(start + int(limit), len(lines)) and (size + len(lines[end]) <= READ_BUDGET or end == start):
        size += len(lines[end])
        end += 1
    head = f"[{p} lines {start + 1}-{end} of {len(lines)}]\n"
    more = f"\n[more: read_file offset={end}]" if end < len(lines) else ""
    body = "".join(lines[start:end])
    if size > READ_BUDGET:  # one line longer than the budget (minified file)
        body = body[:READ_BUDGET] + f"\n[line {start + 1} cut at {READ_BUDGET} of {size} chars; use run_shell with grep or cut]"
    return head + body + more


def write_file(ctx: Ctx, path: str, content: str, append: bool = False) -> str:
    try:
        p = ctx.guard.resolve(path)
        old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        change = _diff(ctx, p, old, old + content if append else content)
        if not append and p.exists() and not ctx.guard.allow(f"edit:{p}", f"overwrite {p}", preview=change[2]):
            return f"DENIED: user declined to overwrite {p}"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"ERROR: cannot write {path}: {e.strerror or e}"
    ctx.change = change
    # An appended part is unfinished by design, so only a whole-file write is checked.
    return f"{'appended' if append else 'wrote'} {len(content)} chars to {p}" + ("" if append else _syntax(p, content))


CLOSEST_MAX = 40  # lines of the file shown back after a failed edit


def _closest(text: str, old: str) -> str:
    """The lines of the file most like `old`, so a failed edit is fixed without a read_file turn."""
    lines = text.splitlines()
    want = [ln for ln in old.splitlines() if ln.strip()]
    if not want or not lines:
        return ""
    n = min(len(old.splitlines()), CLOSEST_MAX)
    stripped = [ln.strip() for ln in lines]
    best, at = 0.0, -1
    for hit in difflib.get_close_matches(want[0].strip(), set(stripped), n=3, cutoff=0.6):
        for i in (i for i, ln in enumerate(stripped) if ln == hit):
            score = difflib.SequenceMatcher(None, old, "\n".join(lines[i:i + n])).ratio()
            if score > best:
                best, at = score, i
    if best < 0.5:
        return ""
    return (f" The closest text is lines {at + 1}-{min(at + n, len(lines))}; copy old_string from it exactly:\n"
            + "\n".join(lines[at:at + n]))


def _syntax(p: Path, text: str) -> str:
    """A warning to add to a write or edit result when the file no longer parses, else ""."""
    why = ""
    try:
        if p.suffix == ".py":
            compile(text, str(p), "exec", dont_inherit=True)
        elif p.suffix == ".json":
            json.loads(text)
        elif p.suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
            done = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True, timeout=10)
            why = "\n".join(done.stderr.strip().splitlines()[:5]) if done.returncode else ""
    except SyntaxError as e:
        why = f"line {e.lineno}: {e.msg}"
    except ValueError as e:  # json.JSONDecodeError, or a null byte in Python source
        why = str(e)
    except (OSError, subprocess.SubprocessError):
        pass
    return f"\nWARNING: {p.name} now has a syntax error ({why}). Fix it before going on." if why else ""


def edit_file(ctx: Ctx, path: str, old_string: str = "", new_string: str = "", edits: list | None = None) -> str:
    try:
        p = ctx.guard.resolve(path)
        with open(p, encoding="utf-8", newline="") as f:  # newline="": line endings come back as they are on disk
            text = f.read()
    except UnicodeDecodeError:
        return f"ERROR: {path} is not UTF-8 text, so edit_file would damage it. Change it with a command instead."
    except OSError as e:
        return f"ERROR: cannot read {path}: {e.strerror or e}"
    pairs = [(old_string, new_string)] if old_string else []
    for e in edits or []:
        if not isinstance(e, dict) or not e.get("old_string"):
            return "ERROR: each item of edits needs old_string and new_string. Nothing was changed."
        pairs.append((e["old_string"], e.get("new_string", "")))
    if not pairs:
        return "ERROR: give old_string and new_string, or a list of edits."
    # Every replacement is checked before asking, so the approval shows the real change and a
    # bad list changes nothing. Each one is matched against the text the earlier ones left.
    new = text
    for k, (old, repl) in enumerate(pairs, 1):
        which = f"edit {k} of {len(pairs)}: " if len(pairs) > 1 else ""
        undone = " Nothing was changed." if len(pairs) > 1 else ""
        if "\r\n" in text and "\r\n" not in old:  # a CRLF file, read by the model as LF: match and keep its endings
            old, repl = (s.replace("\n", "\r\n") for s in (old, repl))
        hits = new.count(old)
        if hits == 0:
            return f"ERROR: {which}old_string not found in {path}.{undone}" + (
                _closest(new, old) or " Read the file and copy the exact text.")
        if hits > 1:
            return (f"ERROR: {which}old_string appears {hits} times in {path}.{undone} "
                    "Include more surrounding lines so it is unique.")
        new = new.replace(old, repl, 1)
    change = _diff(ctx, p, text, new)
    if not ctx.guard.allow(f"edit:{p}", f"edit {p}", preview=change[2]):
        return f"DENIED: user declined to edit {p}"
    try:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(new)
    except OSError as e:
        return f"ERROR: cannot write {path}: {e.strerror or e}"
    ctx.change = change
    return f"edited {p} ({len(pairs)} replacement{'s' if len(pairs) > 1 else ''})" + _syntax(p, new)


def _defs(tree: ast.AST, prefix: str = "") -> dict:
    """Dotted name -> node for every function and class, methods as Class.method."""
    found = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[prefix + node.name] = node
            if isinstance(node, ast.ClassDef):
                found.update(_defs(node, f"{prefix}{node.name}."))
    return found


def replace_def(ctx: Ctx, path: str, name: str, code: str) -> str:
    try:
        p = ctx.guard.resolve(path)
        with open(p, encoding="utf-8", newline="") as f:  # keep CRLF as it is
            text = f.read()
        defs = _defs(ast.parse(text))
    except (UnicodeDecodeError, SyntaxError, ValueError) as e:
        return f"ERROR: cannot parse {path} as Python ({e}). Use edit_file."
    except OSError as e:
        return f"ERROR: cannot read {path}: {e.strerror or e}"
    node = defs.get(name)
    if node is None:
        return f"ERROR: no function or class named {name!r} in {path}. It has: {', '.join(defs) or 'none'}. To add new code use edit_file."
    lines = text.splitlines(keepends=True)
    sent = textwrap.dedent(code).strip("\n")
    # New code with its own decorators replaces the old ones; code starting at def/class keeps them.
    start = (min([node.lineno] + [d.lineno for d in node.decorator_list]) if sent.startswith("@") else node.lineno) - 1
    indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
    eol = "\r\n" if lines[start].endswith("\r\n") else "\n"
    body = textwrap.indent(sent, indent).replace("\r\n", "\n").replace("\n", eol) + eol  # at whatever depth it was sent
    new = "".join(lines[:start]) + body + "".join(lines[node.end_lineno:])
    change = _diff(ctx, p, text, new)
    if not ctx.guard.allow(f"edit:{p}", f"edit {p}", preview=change[2]):
        return f"DENIED: user declined to edit {p}"
    try:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(new)
    except OSError as e:
        return f"ERROR: cannot write {path}: {e.strerror or e}"
    ctx.change = change
    return f"replaced {name} in {p} (lines {start + 1}-{node.end_lineno})" + _syntax(p, new)


def delete_file(ctx: Ctx, path: str) -> str:
    p = ctx.guard.resolve(path)
    if not ctx.guard.allow(f"delete:{p}", f"delete {p}", always_ask=True):
        return f"DENIED: user declined to delete {p}"
    try:
        if p.is_dir():
            return f"ERROR: {p} is a directory. Use run_shell with rm -r if you mean it."
        p.unlink()
    except OSError as e:
        return f"ERROR: cannot delete {path}: {e.strerror or e}"
    return f"deleted {p}"


@dataclass
class Job:
    """One run_shell command. Foreground until the user presses Ctrl+B; then it runs on as a background job."""
    n: int
    command: str
    proc: subprocess.Popen
    out: Path
    err: Path
    t0: float
    background: bool = False
    cancelled: bool = False  # /jobs kill


JOBS: list[Job] = []          # running, plus background ones whose result the model has not had yet
DETACH = threading.Event()    # Ctrl+B: move the foreground command to the background
_numbers = itertools.count(1)
BACKGROUNDED = ("The user moved this command to the background as job {n}; it is still running. You will be given "
                "its result when it ends. Do not check on it, wait for it or run it again.")


def find_bash() -> str | None:
    """Windows: Git for Windows' bash, so the model's POSIX commands (pipes, &&, grep, `cmd &`) work; cmd.exe broke
    them (Windows port, 2026-11-05). Never C:\\Windows\\System32\\bash.exe: that is the WSL launcher,
    on another filesystem. HARNESS_SHELL=<path> picks another shell; HARNESS_SHELL=cmd keeps cmd.exe."""
    forced = os.environ.get("HARNESS_SHELL")
    if forced:
        return None if forced.lower() == "cmd" else forced
    cands = []
    if git := shutil.which("git"):  # ...\Git\cmd\git.exe or ...\Git\mingw64\bin\git.exe
        g = Path(git).resolve()
        cands += [g.parent.parent / "bin" / "bash.exe", g.parent.parent.parent / "bin" / "bash.exe"]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [Path(base) / "Git" / "bin" / "bash.exe", Path(base) / "Programs" / "Git" / "bin" / "bash.exe"]
    return next((str(c) for c in cands if c.is_file()), None)


BASH = find_bash() if sys.platform == "win32" else None


def shell_argv(command: str) -> tuple[str | list[str], bool]:
    """(args, shell=) for subprocess: /bin/sh on Unix, Git Bash on Windows, cmd.exe as a last resort."""
    return ([BASH, "-c", command], False) if BASH else (command, True)


def _kill(proc: subprocess.Popen) -> None:
    """The command and everything it started (its own process group), politely, then not."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        else:  # Windows: /T takes the whole tree, or a child holds the output files open and they can't be deleted
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        proc.wait(3)
    except (OSError, subprocess.TimeoutExpired):
        pass


_dir: list[Path] = []


def _job_dir() -> Path:
    """Output files live outside the workspace: no clutter, never snapshotted, and nothing for the model to poll."""
    if not _dir:
        _dir.append(Path(tempfile.mkdtemp(prefix="cracked-jobs-")))
    return _dir[0]


def _unlink(*files: Path) -> None:
    """Windows: a command backgrounded with `&` still holds the file open, so it can't be deleted yet; the job
    folder goes at exit instead (on Linux an open file unlinks fine)."""
    for f in files:
        with contextlib.suppress(OSError):
            f.unlink(missing_ok=True)


@atexit.register
def _cleanup() -> None:
    for j in JOBS:
        if j.proc.poll() is None:
            _kill(j.proc)
    for d in _dir:
        shutil.rmtree(d, ignore_errors=True)


def _output(job: Job) -> str:
    stdout, stderr = (p.read_bytes().decode(errors="replace") if p.exists() else "" for p in (job.out, job.err))
    out = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
    out = out.strip()
    rc = job.proc.returncode
    return f"[exit {rc}]\n{out}" if out else f"[exit {rc}] (no output)"


def foreground() -> Job | None:
    return next((j for j in JOBS if not j.background and j.proc.poll() is None), None)


def _read_only(ctx: Ctx) -> bool:
    """Manual plan research or investigate mode: writes are refused and shell writes ask (Fuck It research writes)."""
    return (ctx.researching or ctx.guard.investigate) and not ctx.guard.assume_yes


def run_shell(ctx: Ctx, command: str, timeout: int = 120) -> str:
    blocked = ctx.guard.shell_denied(command)
    if blocked:
        return f"DENIED: {blocked}"
    # manual research and investigate mode already asked for anything that looks like a write (a wider net than writes_outside)
    where = None if _read_only(ctx) else ctx.guard.writes_outside(command)
    if where and ctx.researching:  # Fuck It research: no approvals until the plan is made (deletes aside), so refuse
        return (f"DENIED: {where}. While the plan is being researched, nothing outside the workspace changes and sudo "
                "is not run. If the plan needs it, make it a step; the user approves it with the plan.")
    if where and not ctx.guard.allow(f"outside:{command}", f"{command!r} {where}", even_fuck_it=True):
        return (f"DENIED: the user said no ({where}). Keep changes inside the workspace; if the task needs this, "
                "say so in your reply and the user will run it.")
    cap = min(int(timeout), SHELL_TIMEOUT_MAX) if SHELL_TIMEOUT_MAX > 0 else int(timeout)
    # Files, not pipes: a backgrounded server (`http-server . &`) inherits a pipe and holds
    # it open, so waiting on the pipe waited out the whole timeout while the server ran.
    n = next(_numbers)
    out, err = _job_dir() / f"{n}.out", _job_dir() / f"{n}.err"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        args, use_shell = shell_argv(command)
        proc = subprocess.Popen(args, shell=use_shell, cwd=str(ctx.workspace), stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                start_new_session=os.name == "posix",  # its own group, so a kill takes its children too
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    job = Job(n, command, proc, out, err, time.monotonic())
    JOBS.append(job)
    DETACH.clear()
    try:
        while True:
            try:
                proc.wait(0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            if DETACH.is_set():
                DETACH.clear()
                job.background, ctx.backgrounded = True, n
                return BACKGROUNDED.format(n=n)
            if time.monotonic() - job.t0 > cap:
                _kill(proc)
                return (f"ERROR: {command!r} timed out after {cap}s. Narrow it or raise timeout.\n"
                        f"Output so far:\n{_output(job)}")
        result = _output(job)  # before the finally below deletes the files
    except BaseException:  # Esc / Ctrl+C: the command is in its own group, so the terminal's interrupt never reached it
        _kill(proc)
        raise
    finally:
        if not job.background:
            JOBS.remove(job)
            _unlink(out, err)
    return f"[cancelled by the user with /jobs after {time.monotonic() - job.t0:.0f}s]\n{result}" if job.cancelled else result


def finished_jobs(ctx: Ctx) -> str:
    """Background jobs that have ended, as one note for the model; each is reported once."""
    notes = []
    for j in [j for j in JOBS if j.background and j.proc.poll() is not None]:
        JOBS.remove(j)
        how = "was cancelled by the user" if j.cancelled else "finished"
        notes.append(f"{JOB_DONE} {j.n} {how}: `{j.command}`\n{spill(ctx, _output(j), 'run_shell')}")
        _unlink(j.out, j.err)
    return "\n\n".join(notes)


JOB_DONE = "[harness: background job"


def jobs_command(arg: str) -> str:
    """/jobs lists shell commands; /jobs kill N (or all) stops them. Works at the prompt and mid-task."""
    words = arg.split()
    if words and words[0] in ("kill", "cancel", "stop"):
        every = words[1:] == ["all"]
        targets = [j for j in JOBS if j.proc.poll() is None and (every or str(j.n) in words[1:])]
        if not targets:
            return "nothing to kill: /jobs kill <number> or /jobs kill all (/jobs lists them)"
        for j in targets:
            j.cancelled = True
            _kill(j.proc)
        return "killed job " + ", ".join(str(j.n) for j in targets)
    if words:
        return "/jobs lists shell commands · /jobs kill <number> or /jobs kill all stops them"
    if not JOBS:
        return "no shell commands running"
    rows = []
    for j in JOBS:
        rc = j.proc.poll()
        state = f"running {time.monotonic() - j.t0:.0f}s" if rc is None else f"ended, exit {rc} (sent with your next message)"
        rows.append(f"  {j.n:>3}  {'background' if j.background else 'foreground'}  {state}  {' '.join(j.command.split())[:80]}")
    return "\n".join(rows) + "\n/jobs kill <number> stops one"


def see_image(ctx: Ctx, path: str, detail: str = "auto") -> str:
    """Load an image for the vision model; the picture rides along with this tool result."""
    if not ctx.vision:
        return ("ERROR: this model has no vision encoder, so images cannot be shown to it. "
                "The server is text-only: use an image-capable --base-url/--model, or have a "
                "command describe the file instead (e.g. python3 tools/review.py for layout, "
                "ffprobe/exiftool for metadata).")
    try:
        p = ctx.guard.resolve(path)
        part, info = images.prepare(p)
    except images.Unsupported as e:
        return f"ERROR: {path}: {e}"
    except OSError as e:
        return f"ERROR: cannot read {path}: {e.strerror or e}"
    ctx.pending_images.append(part)
    return (f"image attached: {images.describe(info)} - it is in this message, after this text. "
            f"Look at it now and answer from what you see; do not call see_image again for it.")


def web_search(ctx: Ctx, query: str, max_results: int = 5) -> str:
    try:
        hits = web.search(query, int(max_results))
    except Exception as e:
        return f"ERROR: search failed: {type(e).__name__}: {e}"
    if not hits:
        return "no results"
    return "\n\n".join(
        f"{i}. {h['title']}\n   {h['url']}\n   {h['snippet']}".rstrip()
        for i, h in enumerate(hits, 1)
    )


def web_fetch(ctx: Ctx, url: str) -> str:
    try:
        page = web.get(url)
    except Exception as e:
        return f"ERROR: fetch failed: {type(e).__name__}: {e}"
    return web.to_text(page)


# Folders no search or map should walk: VCS, deps, caches, build output. Hidden folders are skipped too.
SKIP_DIRS = {".git", ".cracked", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".tox", "dist", "build", "target", ".next", ".cache"}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., str]


TOOLS: list[Tool] = [
    Tool(
        "read_file",
        "Read a text file. Returns at most `limit` lines starting at `offset` (0-based line numbers). "
        "Do not use it on images: use see_image for those.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, relative to the workspace or absolute."},
                "offset": {"type": "integer", "description": "First line to read, 0-based. Default 0."},
                "limit": {"type": "integer", "description": "How many lines to read. Default 400."},
            },
            "required": ["path"],
        },
        read_file,
    ),
    Tool(
        "write_file",
        "Write text to a file, creating parent directories. Overwrites the whole file unless append is true. "
        "For long files (300+ lines), write the first part, then add the rest in more calls with append=true.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "content": {"type": "string", "description": "The file contents, or the part to append."},
                "append": {"type": "boolean", "description": "Add to the end instead of overwriting. Default false."},
            },
            "required": ["path", "content"],
        },
        write_file,
    ),
    Tool(
        "edit_file",
        "Replace one exact occurrence of old_string with new_string. old_string must be unique in the file. "
        "For several changes to one file, send them all in `edits` in one call: all are applied, or none.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "old_string": {"type": "string", "description": "Exact text to replace, copied from the file."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "edits": {
                    "type": "array",
                    "description": "More than one replacement, applied in order. Use instead of old_string/new_string.",
                    "items": {
                        "type": "object",
                        "properties": {"old_string": {"type": "string"}, "new_string": {"type": "string"}},
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["path"],
        },
        edit_file,
    ),
    Tool(
        "replace_def",
        "Replace one whole Python function, method (Class.method) or class, found by name, with new code. "
        "No old text is needed, so it is the cheapest way to rewrite a function. Python files only.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "A .py file."},
                "name": {"type": "string", "description": "Function or class name; a method as Class.method."},
                "code": {"type": "string", "description": "The complete new definition, from its def/class line (or decorators)."},
            },
            "required": ["path", "name", "code"],
        },
        replace_def,
    ),
    Tool(
        "delete_file",
        "Delete one file. Always asks the user, even in auto-approve mode.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path."}},
            "required": ["path"],
        },
        delete_file,
    ),
    Tool(
        "see_image",
        "Look at an image file (PNG, JPEG, GIF, WEBP, BMP): screenshot, photo, chart, diagram, rendered "
        "frame, UI. The picture is attached to the result, so you can describe and judge what is in it. "
        "Large images are downscaled to 1024 px first. Requires a vision-capable model.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Image file, relative to the workspace or absolute."},
                "detail": {"type": "string", "description": "Hint only: 'auto' (default), 'layout' or 'text'.", "enum": ["auto", "layout", "text"]},
            },
            "required": ["path"],
        },
        see_image,
    ),
    Tool(
        "run_shell",
        "Run a shell command in the workspace and return exit code, stdout and stderr. "
        "Use it for anything without a dedicated tool: git, nmap, ncat, curl, ss, python, compilers. "
        "For a server that must keep running, background it: `setsid nohup <cmd> > server.log 2>&1 &` "
        "returns at once and survives the session.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line to run."},
                "timeout": {"type": "integer", "description": "Wall-clock seconds before it is killed. Default 120; raise it for long builds and test runs."},
            },
            "required": ["command"],
        },
        run_shell,
    ),
    Tool(
        "web_search",
        "Search the web. Returns titles, URLs and snippets.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "description": "How many results. Default 5."},
            },
            "required": ["query"],
        },
        web_search,
    ),
    Tool(
        "web_fetch",
        "Fetch a URL and return its text content. Long pages are saved to a file you can grep.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch."},
            },
            "required": ["url"],
        },
        web_fetch,
    ),
]

TOOLS.append(Tool(
    "todo_write",
    "For a task of 3 or more steps: write your checklist once, before starting. The harness then gives you one step "
    "at a time and ticks them. Call it again only to change the remaining steps.",
    {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string", "description": "One step, one short line."},
                "check": {"type": "string", "description": "Optional: a command that exits 0 when the step is right."}},
                "required": ["text"]}},
        },
        "required": ["items"],
    },
    todo_write,
))
BY_NAME = {t.name: t for t in TOOLS}
WEB = ("web_search", "web_fetch")  # what /incognito turns off


def attach_pending(ctx: Ctx, result: str, call_id: str = "") -> dict:
    """The tool message for one call, carrying any images it attached.

    Text-only result -> a plain string content, exactly as before. With images, the
    OpenAI shape is a content list: the text first, then the image parts, so a
    text-only server still gets a usable string and a vision server gets the picture.
    """
    if not ctx.pending_images:
        return {"role": "tool", "tool_call_id": call_id, "content": result}
    parts: list[dict] = [{"type": "text", "text": result}]
    parts.extend(ctx.pending_images)
    ctx.pending_images = []
    return {"role": "tool", "tool_call_id": call_id, "content": parts}


def schemas(vision: bool = False, web: bool = True) -> list[dict]:
    """Tool list for the request; see_image only for a model that takes images (it saves prompt tokens on every request)."""
    def schema(t: Tool) -> dict:
        return {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
    return [
        schema(t) for t in TOOLS
        if (vision or t.name != "see_image") and (web or t.name not in WEB)
    ]


def call(ctx: Ctx, name: str, arguments: str) -> str:
    """Run one tool call. Failures come back as text: the model recovers from those."""
    tool = BY_NAME.get(name)
    if tool is None:
        return f"ERROR: unknown tool {name!r}. Available: {', '.join(BY_NAME)}"
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return "ERROR: arguments were not valid JSON. Send a JSON object."
    if not isinstance(args, dict):
        return "ERROR: arguments must be a JSON object."
    # researching a plan: Fuck It reads and writes freely; manual reads freely, and writes ask or are refused.
    # investigate mode: the same as manual research.
    if _read_only(ctx) and name in WRITERS:
        return RESEARCH_ONLY if ctx.researching else INVESTIGATE_ONLY
    if _read_only(ctx) and name == "run_shell" and plan_mod.looks_like_write(cmd := str(args.get("command", ""))):
        who = "plan research" if ctx.researching else "investigate mode"
        if not ctx.guard.allow(f"plan-shell:{cmd}", f"{who} wants to run {cmd!r} (it looks like it changes "
                               "something)", always_ask=True):
            return ("DENIED: the user said no. " + ("While researching a plan, only read; changes wait for the user's "
                    "approval." if ctx.researching else "Investigate mode is read only; suggest the change in your reply."))
    if ctx.incognito and name in WEB:
        return "ERROR: incognito mode: web tools are off. Work from the workspace and what you know."
    try:
        result = tool.run(ctx, **args)
        # read_file already pages its output, and its source is a file on disk anyway.
        return result if name in ("read_file", "see_image") else spill(ctx, result, name)
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}. Expected {json.dumps(tool.parameters['properties'])}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
