"""The agent loop."""

from __future__ import annotations

import itertools
import os
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, plan, safety, tools, ui


REPEAT_LIMIT = 3   # same call this many times in the last REPEAT_WINDOW calls = stuck
REPEAT_WINDOW = 8  # wide enough that read, edit, read again does not trip it
FAIL_LIMIT = 4     # this many failed calls in a row = stuck
# Non-LLM drift detection, all free (the harness already sees every call). The real failure was
# ~12 edits to two files. EDIT_LIMIT was 4, but that tripped on honest multi-part edits to one file
# (2026-11-25), so it is 8 now, in a 16-call window so read/edit/run interleaving can still reach it.
EDIT_WINDOW, EDIT_LIMIT, READ_LIMIT = 16, 8, 5
CHANT_WINDOW, CHANT_LIMIT, CHANT_SIM = 4, 2, 0.8  # near-duplicate narration lines = spinning
STALL_LIMIT = 8    # turns of ONLY pure-read tools and no answer = make progress or report
READONLY = {"read_file", "web_search", "web_fetch", "see_image"}  # run_shell may mutate, so it resets the stall streak

# Re-stated to the model every ANCHOR_EVERY turns as a user message that stays in the history
# (append-only, so the server's prompt cache keeps extending). Fights the task getting buried
# under tool output. ~35 tokens each time.
ANCHOR = ("<system-reminder>Task: {task}\nProgress: {progress}. A passing tool call is not task "
          "completion. If the task is answered, state the answer and stop; otherwise take the next "
          "step toward it.</system-reminder>")

NUDGE = ("Loop check: {why}. Stop repeating. In 3 short lines: what you know, what you assumed, "
         "and a different approach. If only the user can unblock you, ask them and end your turn.")
CUT_OFF = ("ERROR: your reply hit max_tokens ({n} tokens) inside this tool call, so it was not run and "
           "nothing was written. Split the work: write_file the first part, then write_file append=true for the rest, "
           "each part well under half that size.")
CUT_OFF_TEXT = ("Your last reply hit max_tokens ({n} tokens) before it finished, so nothing was run. "
                "Think less and act in smaller steps: write long files in parts with write_file append=true.")
REPEATING = ("Your last reply was stopped because it wrote the same text over and over. Do not continue it. "
             "Take the next concrete step with a tool call, or give the answer in a few lines.")
REPEATED_CALL = "Not run: your reply was stopped because it wrote the same text over and over."
REPEATED_REPLY = "[reply stopped: it was repeating itself]"
COMPACT = ("Context is nearly full. Summarise the work so far in at most 10 bullets: the task, what is done, "
           "key facts and file paths, what failed, what is next. No tool calls.")
# LOCKED IN mode (config locked_in): long tasks lost queued messages and past failures to compaction (first and last
# ask kept, 10 bullets). The harness keeps those itself in the ledger, so the summary can spend its room on the work.
COMPACT_LOCKED = ("Context is nearly full. Summarise the work so far so you can carry on without re-doing anything, "
                  "in at most 25 bullets: what is done (file paths, commands that worked), what is half-done, every "
                  "problem hit and how it was solved or why it was not, key facts learned, and the exact next step. "
                  "The user's messages and the failed calls are kept separately; do not repeat them. No tool calls.")
LEDGER = "[LOCKED IN ledger: kept by the harness, survives compaction]"
LOCKED_COMPACT_AT = 0.7  # compact later than the default, so it happens less often
LEDGER_ASK = 600         # chars of each ask kept
LEDGER_ERRORS = 15       # newest failed calls kept
# config narrate = true: one readable line per turn, shown as "● ..." in the terminal (the dashboard still reads ". >_ ...") (Qwen Code does the same with the
# text sent alongside tool calls). Costs ~15-25 output tokens a turn, kept in the history.
NARRATE = " With each batch of calls, write one short plain line saying what you are doing and why."

# INVESTIGATE mode (replaced WE NEED TO TALK, 2026-11-12: that mode ran whole long tasks without asking once):
# appended to each task, not the system prompt, so switching never invalidates the prompt cache. The rule is backed
# by tools.dispatch refusing writes, so the model cannot carry on into an edit even if it ignores the words.
INVESTIGATE = ("[INVESTIGATE mode] The user wants answers, not changes. Read files, search, and run commands that "
               "only look; then reply with what you found and stop. Writing files is switched off, and a command that "
               "changes something needs the user's yes. If a change would help, describe it in your reply as a "
               "suggestion; do not try to make it.")
INVESTIGATE_ANCHOR = " INVESTIGATE mode is on: read only; reply with your findings and stop."
# Approval menu e: the model says why it wants a call. A side request, never kept in the history.
EXPLAIN = ("[harness: the user has not approved this yet and asks why first: {why}. In 2-4 plain sentences say why "
           "you want it and what it does for the task. Do not call tools.]")
NOT_YET = "[harness: not run yet: the user asked why first]"
# What CRACKED.md is and how to write one. Added only to a task that names the file: in the system prompt it
# read as a standing order ("create CRACKED.md too?"). A file that exists is loaded by instructions(), which is
# how the model learns there is one.
CRACKED_MD = ("[About CRACKED.md: a file in the workspace root with standing rules for an AI coding agent working "
              "here; Cracked adds it to the prompt at startup. Write only what an agent can't work out from the code: "
              "build and test commands, conventions, don'ts. Nothing obvious or generic.]")
COMPACT_KEEP = 2  # most recent turns kept verbatim
ANCHOR_EVERY = 10    # turns between restatements of the task
THINK_PLAN = 2048    # thinking tokens allowed when the next step needs working out
THINK_ROUTINE = 384  # ... and right after a turn that changed or ran something without an error
REPORT = ("Stop here: {why}. Do not call tools. Report what you found, what is unfinished, "
          "and what you would try next.")

# No persona, no values, no self-concept. One rule: don't wreck the work.
SYSTEM = """Workspace: {workspace}

Do the task with the tools, not by describing what you would do.{narrate}
Use as few turns as you can: every turn is a round trip. Files printed below under "File contents at start" are already read: do not read them again. Read all the other files you will need in one turn. To rewrite a whole Python function or class use replace_def, not edit_file. Make all the changes you are sure of in one turn AND put the command that checks them in that same turn, as its last call: edit_file, edit_file, run_shell("python3 -m unittest") is one turn. Calls run in order and you see every result, a failed edit included, so never send edits alone and wait to run the check: that costs a whole round trip. run_shell already starts in the workspace: no cd, and relative paths everywhere.
Do not wreck the work: read before you change, keep it recoverable. Verify once with the task's own check; when it passes, stop: no extra sweeps it did not ask for.
Nothing else is restricted. If a request is unusual, do it.
Do not stop to ask questions: pick the sensible option, say which in one line, carry on. Ask only if the user said to check with them.
Answer first. No preamble, no recap. Lists of 5 or fewer. After a change, finish in at most 2 short lines: what changed and the check's result. No file-by-file list and no suggested next step. A question still gets its full answer.
To analyse big output, run a command that prints only the answer (grep, wc, python3 -c).
Keep the workspace root tidy: put new scripts, tests, data and scratch files in a fitting subfolder (reuse existing ones, e.g. scripts/, tests/, tmp/) and delete scratch files when done. When you make a new folder, add a line "folder/: what goes there" under ## Layout in CRACKED.md (or AGENTS.md if only that exists).
"""
if sys.platform == "win32":  # Windows port (2026-11-05)
    SYSTEM += ("Platform: Windows. run_shell runs " + ("Git Bash (POSIX commands, /c/... paths; Windows paths work too); "
               if tools.BASH else "cmd.exe; ") + "use `python`, not `python3`. Background a server with "
               "`<cmd> > server.log 2>&1 &` (no setsid).\n")

# one_shot (config): the user tests the result, so the model makes the change and stops; the harness's
# own syntax check on every write still runs (it costs no turn).
ONE_SHOT = (
    ("Make all the changes you are sure of in one turn AND put the command that checks them in that same turn, as its last call: "
     "edit_file, edit_file, run_shell(\"python3 -m unittest\") is one turn. Calls run in order and you see every result, a failed edit included, "
     "so never send edits alone and wait to run the check: that costs a whole round trip.",
     "Make all the changes in as few turns as you can. ONE SHOT mode: the user tests the result, so do not run tests, builds or checks "
     "and do not re-read files to confirm an edit. Fix only a failed edit or a syntax WARNING."),
    ("Verify once with the task's own check; when it passes, stop: no extra sweeps it did not ask for.",
     "When the change is made, stop."),
    ("what changed and the check's result.", "what changed and what the user should test."),
)


def one_shot(system: str, on: bool = True) -> str:
    """The system prompt in ONE SHOT mode: every self-check rule swapped for "make it and stop".
    on=False swaps them back (/oneshot off mid-session). Already in that state: returned as it is."""
    if is_one_shot(system) == on:
        return system
    for old, new in ONE_SHOT:
        old, new = (old, new) if on else (new, old)
        assert old in system, old[:40]  # the SYSTEM text was edited without updating ONE_SHOT
        system = system.replace(old, new)
    return system


def is_one_shot(system: str) -> bool:
    """Whether this system prompt is in ONE SHOT mode: the prompt itself is the truth, also after /resume."""
    return all(new in system for _, new in ONE_SHOT)


MAP_MAX = 150  # files listed in the startup map; more and the model is told to look for itself
MAP_SKIP = tools.SKIP_DIRS


def workspace_map(workspace: Path) -> str:
    """The workspace's files at startup, so the model's first turn can read instead of `find` (saves a round trip)."""
    files: list[str] = []
    for root, dirs, names in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in MAP_SKIP and not d.startswith("."))
        rel = os.path.relpath(root, workspace)
        files += [n if rel == "." else f"{rel}/{n}" for n in sorted(names)]
        if len(files) > MAP_MAX:
            break
    if not files:
        return "\n## Workspace files at start\n(empty)\n"
    more = "\n(more files not listed: search with grep or find)" if len(files) > MAP_MAX else ""
    return "\n## Workspace files at start\n" + "\n".join(files[:MAP_MAX]) + more + "\n"


INLINE_TOTAL = 16_000  # chars of file contents put in the first prompt (about 4,000 tokens, read once, then cached)
INLINE_FILE = 6_000    # a bigger file is left for read_file


def workspace_files(workspace: Path) -> str:
    """A small workspace's text files, whole, so the first turn can act instead of reading (saves a round trip).
    All or nothing: a partly shown project would be read again anyway."""
    parts, total = [], 0
    for root, dirs, names in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in MAP_SKIP and not d.startswith("."))
        for n in sorted(names):
            p = Path(root) / n
            try:
                if p.stat().st_size > INLINE_FILE:
                    return ""
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return ""
            total += len(text)
            if total > INLINE_TOTAL or "\0" in text:
                return ""
            parts.append(f"### {p.relative_to(workspace).as_posix()}\n```\n{text.rstrip()}\n```")
    if not parts:
        return ""
    return "\n## File contents at start (whole files; they change when you edit them)\n" + "\n".join(parts) + "\n"


INSTRUCTIONS_MAX = 20_000  # chars per file; a huge file would quietly eat the context window


def instructions(workspace: Path, config_dir: Path) -> tuple[str, list[str]]:
    """User + project instruction files for the end of the system prompt, and the names loaded.

    ~/.config/cracked/CRACKED.md applies everywhere; the workspace's CRACKED.md (or cracked.md,
    else AGENTS.md) applies to this project and comes last, so it wins a conflict."""
    found = [config_dir / "CRACKED.md"]
    found += [next((p for n in ("CRACKED.md", "cracked.md", "AGENTS.md") if (p := workspace / n).is_file()), workspace / "CRACKED.md")]
    text, names = "", []
    for p in found:
        if not p.is_file():
            continue
        body = p.read_text(errors="replace", encoding="utf-8").strip()
        if len(body) > INSTRUCTIONS_MAX:
            body = body[:INSTRUCTIONS_MAX] + f"\n[cut: {p.name} is over {INSTRUCTIONS_MAX} chars]"
        label = f"~/{p.relative_to(Path.home())}" if p.is_relative_to(Path.home()) and p.parent != workspace else p.name
        text += f"\n## Instructions from {label}\n{body}\n"
        names.append(f"{label} ({body.count(chr(10)) + 1} lines)")
    return text, names


def _sound(arguments: str) -> str:
    """Arguments safe to keep in the history: broken JSON (a cut-off or malformed call) becomes {}.
    The history is re-sent every turn, and a chat template that parses past calls can reject it all."""
    try:
        return arguments if isinstance(json.loads(arguments), dict) else "{}"
    except ValueError:
        return "{}"


def assistant_message(reply) -> dict:
    """The reply as a history message, thinking included: the chat template then renders the reply as the
    server generated it, so the next prompt extends the server's cache and the reply is not read again.
    Folding the task drops it (run() keeps only ask and answer)."""
    think = {"reasoning_content": reply.reasoning} if reply.reasoning else {}
    if reply.calls:
        return {
            **think,
            "role": "assistant",
            "content": reply.text or None,
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": _sound(c.arguments)}}
                for c in reply.calls
            ],
        }
    return {**think, "role": "assistant", "content": reply.text or ""}


def run(llm, messages: list[dict], ctx: tools.Ctx, max_turns: int = -1, out=None,
        context: int = 262144, compact_at: float = 0.4) -> str:
    """Send messages, execute every tool call the model asks for, repeat until it stops.

    max_turns < 1 means no limit: the model finishing, loop detection or Esc / Ctrl-C ends the task.
    However it ends, the history stays valid, so the next task carries on from here.
    """
    at = _task_index(messages)
    task = messages[at]["content"] if at is not None else ""
    stats = {"task_at": at, "task": _first_line(task), "edits": [], "seen": [], "calls": 0, "checklist_asked": False}
    ctx.stopped = ""
    if at is not None and not _text(task).startswith(plan.HARNESS):  # plan steps are the harness's, not the user's
        _ledger_ask(ctx, _text(task).split(INVESTIGATE)[0].split(plan.RESEARCH_HEAD)[0])
        ctx.task = stats["task"]
    try:
        answer = _loop(llm, messages, ctx, max_turns, out, context, compact_at, stats)
    except BaseException as e:
        repair(messages, by_user=isinstance(e, KeyboardInterrupt))
        raise
    at = stats["task_at"]
    if ctx.fold and not ctx.locked and not ctx.plan_handoff and at is not None and ctx.usage >= ctx.fold_above:
        # The task's working turns collapse to ask -> answer (+ what changed).
        files = ", ".join(dict.fromkeys(stats["edits"]))
        del messages[at + 1:]
        for m in messages:  # only the newest fold keeps file copies: newer ones supersede them
            if m["role"] == "assistant" and isinstance(m.get("content"), str) and KEPT in m["content"]:
                m["content"] = m["content"].split(KEPT)[0].rstrip()
        kept = _kept_files(ctx, stats["edits"], stats["seen"])
        messages.append({"role": "assistant", "content": answer + (f"\n\n[harness: this task changed {files}]" if files else "") + kept})
    return answer


# ---------------------------------------------------------------- plan mode (docs/plan-mode.md)
PLAN_HANDOFF = "[harness: a checklist was made; the harness walks the model through it]"
AUTO_CHECKLIST_AT = 8  # turns into a task with no checklist before auto_checklist asks for one
STEP_TURNS = 25        # model round trips a plan step gets before it counts as stuck
CHECK_TRIES = 3        # failed checks before a step is blocked
CHECK_TIMEOUT = 600    # seconds a step's check may run


def drive(llm, messages: list[dict], ctx: tools.Ctx, max_turns: int = -1, out=None,
          context: int = 262144, compact_at: float = 0.4) -> str:
    """run(), then the checklist if the model made one (todo_write, auto_checklist) on the way."""
    answer = run(llm, messages, ctx, max_turns, out, context, compact_at)
    if not ctx.plan_handoff:
        return answer
    ctx.plan_handoff = False
    return run_plan(llm, messages, ctx, out, context, compact_at)


def run_check(ctx: tools.Ctx, command: str) -> tuple[bool, str]:
    """A step's check, in the workspace: (passed, the last 40 lines of output)."""
    if why := ctx.guard.shell_denied(command):
        return False, why
    try:
        args, use_shell = tools.shell_argv(command)
        r = subprocess.run(args, shell=use_shell, cwd=ctx.workspace, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=CHECK_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {CHECK_TIMEOUT} s"
    lines = (r.stdout + r.stderr).rstrip("\n").splitlines()
    return r.returncode == 0, f"[exit {r.returncode}]\n" + "\n".join(lines[-40:])


def run_plan(llm, messages: list[dict], ctx: tools.Ctx, out=None, context: int = 262144, compact_at: float = 0.4) -> str:
    """Walk the model through ctx.plan one step at a time: the end of its turn means the step is done, the step's
    check (if any) decides, the harness ticks it and sends the next. A blocked step (or the model's PLAN WRONG) gets
    one replan; a second block, or a replan outside Fuck It mode, pauses the plan for the user. Manual mode pauses
    after every step. A pause sets ctx.plan_ask, so the REPL shows the plan menu."""
    out = out or sys.stdout
    tty = out is sys.stdout and ui.ON

    def say(line: str) -> None:
        ui.out(ui.line(line)) if tty else print(line, file=out, flush=True)

    p = ctx.plan
    p.state = "running"
    try:
        while (i := p.current()) is not None:
            s = p.steps[i]
            s.mark = "~"
            plan.save(ctx.workspace, p)
            ui.PLAN = p.label()
            say(f". plan step {i + 1}/{len(p.steps)}: {s.text}")
            messages.append({"role": "user", "content": plan.step_message(p, i, ledger(ctx) if ctx.locked else "")})
            answer, why = "", ""
            for attempt in range(1, CHECK_TRIES + 1):
                answer = run(llm, messages, ctx, STEP_TURNS, out, context, compact_at)
                if ctx.plan_handoff or ctx.backgrounded is not None or answer.startswith(HANDED_BACK_PREFIX):
                    break
                if ctx.stopped:
                    why = ctx.stopped
                    break
                if answer.lstrip().upper().startswith(plan.WRONG):
                    why = "the model says the plan no longer fits: " + _first_line(answer.lstrip()[len(plan.WRONG):])
                    break
                if not s.check:
                    break
                ok, output = run_check(ctx, s.check)
                if output.startswith("[exit 127]"):  # command not found: the check is broken, not the step
                    say(f". plan check could not run (command not found), dropped: {s.check}")
                    s.check = ""
                    break
                say(f". plan check {'passed' if ok else 'failed'}: {s.check}")
                if ok:
                    break
                why = f"its check `{s.check}` failed {attempt} time{'s' if attempt > 1 else ''}: {output.splitlines()[-1][:120] if output else ''}"
                if attempt < CHECK_TRIES:
                    code = output.split("]")[0].removeprefix("[exit ")
                    messages.append({"role": "user", "content": plan.CHECK_FAILED.format(
                        check=s.check, code=code, output=output.split("\n", 1)[-1], n=i + 1)})
            if ctx.plan_handoff:  # the model rewrote its own checklist mid-step (todo_write): follow the new one
                ctx.plan_handoff, p = False, ctx.plan
                if p.state != "running":
                    return _pause(ctx, p, f"The checklist is waiting for you:\n{plan.checklist(p)}")
                continue
            if answer.startswith(HANDED_BACK_PREFIX):  # Ctrl+B: the user took the turn back
                return answer
            if why:
                s.mark, s.result = "!", why[:160]
                plan.save(ctx.workspace, p)
                say(f". plan step {i + 1} blocked: {why}")
                if (i + 1) in p.replanned or not _replan(llm, messages, ctx, p, i, why, say):
                    return _pause(ctx, p, f"Plan paused: step {i + 1} is blocked ({why}).\n{plan.checklist(p)}\n"
                                          f"Fix it yourself, /plan skip {i + 1} or /plan edit; or pick below.")
                if not ctx.guard.assume_yes:
                    return _pause(ctx, p, f"Step {i + 1} was blocked ({why}), so the model rewrote the rest of the "
                                          f"plan:\n{plan.checklist(p)}")
                say(f". plan: rewrote the steps from {i + 1} (Fuck It mode: carrying on)")
                continue
            s.mark, s.result = "x", _first_line(answer)[:160] or "done"
            plan.save(ctx.workspace, p)
            say(f". plan step {i + 1}/{len(p.steps)} done")
            if p.mode == "manual" and p.current() is not None:
                return _pause(ctx, p, f"Step {i + 1} done: {s.result}\n{plan.checklist(p)}")
        checks = list(dict.fromkeys(t.check for t in p.steps if t.check and t.mark == "x"))
        results = [f"`{c}` {'passed' if run_check(ctx, c)[0] else 'FAILED'}" for c in checks]
        p.state = "done"
        plan.save(ctx.workspace, p)
        ui.PLAN = ""
        say(f". plan complete: {len(p.steps)} steps")
        messages.append({"role": "user", "content": plan.FINAL.format(
            total=len(p.steps), checks=", ".join(results) or "none", goal=p.goal)})
        return run(llm, messages, ctx, STEP_TURNS, out, context, compact_at)
    finally:
        if p.state != "done":
            plan.save(ctx.workspace, p)  # Esc mid-step: the step stays in progress; /plan carries on
        ui.PLAN = ""


def _pause(ctx: tools.Ctx, p, text: str) -> str:
    """Stop the run here and hand the plan to the user: the REPL shows the plan menu next."""
    p.state = "draft"
    plan.save(ctx.workspace, p)
    ctx.plan_ask = True
    return text


def _replan(llm, messages, ctx, p, i: int, why: str, say) -> bool:
    """One text-only request to rewrite the steps from i on; True if a usable plan came back."""
    reply = text_only(llm, messages + [{"role": "user", "content": plan.REPLAN.format(
        n=i + 1, why=why, plan=plan.checklist(p))}], tools.schemas(ctx.vision, web=not ctx.incognito))
    try:
        new = plan.parse(reply.text or "", p.goal).steps
    except plan.PlanError as e:
        say(f". plan: the rewrite could not be used ({e})")
        return False
    for s in new:
        s.mark, s.result = " ", ""
    p.steps = p.steps[:i] + new
    p.replanned.append(i + 1)
    plan.save(ctx.workspace, p)
    return True


def _checklist_free(ctx) -> bool:
    """No plan in charge: not researching, no plan running, no user plan waiting in the plan menu."""
    p = ctx.plan
    return not ctx.researching and not (p and (p.state == "running" or (p.source == "user" and p.state != "done")))


def _auto_checklist(llm, messages, ctx, toolset, say) -> bool:
    """auto_checklist: ask the model to list the remaining steps; True if a checklist of 3+ steps was made."""
    say(". auto checklist: asking for the remaining steps")
    reply = text_only(llm, messages + [{"role": "user", "content": plan.CHECKLIST}], list(toolset))
    if (reply.text or "").strip().upper().startswith("NONE"):
        say(". auto checklist: not needed, carrying on without one")
        return False
    try:
        steps = plan.parse(reply.text or "", ctx.task).steps
    except plan.PlanError as e:
        say(f". auto checklist: none made ({e})")
        return False
    if len(steps) < 3:
        say(". auto checklist: fewer than 3 steps left, carrying on without one")
        return False
    ctx.plan = plan.Plan(goal=ctx.task or steps[0].text, steps=steps, source="model", state="running")
    plan.save(ctx.workspace, ctx.plan)
    ctx.plan_handoff = True
    say(f". checklist: {len(steps)} steps (made for the model) · /plan to see it")
    return True


KEPT = "\n\n[harness: files as they are now"
FOLD_KEEP = 16000  # chars of touched files a folded task keeps, so the next ask skips re-reading them
# A fold rewrites the history after the system prompt, so the next ask re-reads it all (1,450-2,400 tokens, 1.2-1.5 s an ask in
# the 2026-11-11 task 13 runs). Below this size the history is left as it is and the next ask only adds to the server's cache.
# shortcut: tasks finished below the line stay unfolded for good (compaction is the backstop); fold them too if long sessions bloat.
FOLD_ABOVE = 24_000  # prompt tokens; under Strata's 32,768-token resident KV window
WRITERS = tools.WRITERS


def _kept_files(ctx, edits: list[str], seen: list[str]) -> str:
    """Current text of the files a folded task changed (first) or read, up to ctx.fold_keep chars,
    so the next task can use them without spending a turn re-reading."""
    room, parts = ctx.fold_keep, []
    for path in dict.fromkeys(list(reversed(edits)) + list(reversed(seen))):
        try:
            p = ctx.guard.resolve(path)
            if ".cracked" in p.parts:  # spilled tool output, not project files
                continue
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # deleted, a folder, or binary
        if len(text) > room:
            continue
        room -= len(text)
        parts.append(f"--- {path} ({text.count(chr(10)) + 1} lines)\n{text.rstrip()}")
    return (KEPT + "; use them instead of reading again]\n" + "\n".join(parts)) if parts else ""


INTERRUPTED_CALL = "Not run: the user interrupted the task before this call."
INTERRUPTED_REPLY = "[interrupted by the user]"
FAILED_CALL = "Not run: the task stopped on an error before this call."
FAILED_REPLY = "[no reply: the model server failed]"
# Ctrl+B (live.Live, tools.run_shell): the command runs on and the turn ends, so the user gets the prompt back.
HANDED_BACK = "Job {n} is running in the background. I'll look at its result when it ends."
HANDED_BACK_PREFIX = HANDED_BACK.split("{n}")[0]
NOT_RUN = "[not run: the user moved an earlier command to the background and took the turn back]"
EMPTY = "[the model returned an empty reply twice: no answer and no tool call]"
# Typed and sent with Enter while the task ran (live.Live.steer): read before the next step.
# The text comes first: the keep-on-task anchor shows a user message's first line.
STEERED = "{text}\n\n[The user sent this while you were working. Take it into account from now on.]"


def repair(messages: list[dict], by_user: bool = True) -> None:
    """After an interrupt or a server failure: every tool call gets a result, and the history never
    ends on the user's own message (two user turns in a row confuse some chat templates)."""
    last = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "assistant"), None)
    if last is not None:
        answered = {m.get("tool_call_id") for m in messages[last + 1:] if m["role"] == "tool"}
        for c in messages[last].get("tool_calls") or []:
            if c["id"] not in answered:
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": INTERRUPTED_CALL if by_user else FAILED_CALL})
    if messages and messages[-1]["role"] == "user":
        messages.append({"role": "assistant", "content": INTERRUPTED_REPLY if by_user else FAILED_REPLY})


def _loop(llm, messages, ctx, max_turns, out, context, compact_at, stats) -> str:
    out = out or sys.stdout
    tty = out is sys.stdout and ui.ON  # colour terminal: compact lines + live spinner; else the plain lines the dashboard parses

    def say(line: str) -> None:
        if not tty:
            print(line, file=out, flush=True)
            return
        # auto checklist asks and misses are detail (Ctrl+O); a checklist that is made shows as ". checklist: ..."
        detail = line.startswith(("-> ", "   ", ". turn ", ". auto checklist")) or (
            line.startswith(". snapshot ") and not line.startswith(". snapshot failed"))
        ui.out(ui.line(line), "detail" if detail else "all")

    ncalls = 0

    recent: list[str] = []
    touches: list[tuple[str, str]] = []   # (edit|read, path) for the edit/read-loop detectors
    narrations: list[str] = []            # recent assistant text, for the chanting detector
    readonly_streak = 0                   # turns in a row with no file change and no answer
    fails = 0
    empties = 0
    nudged = False
    calm = False  # the last turn changed or ran something and nothing failed: the next one needs little thinking
    stop_why = f"out of turns (max_turns={max_turns})"
    told = ctx.guard.investigate  # repl.run_task added INVESTIGATE to the task when the mode was on at the start
    anchor_now = False  # LOCKED IN: restate the ledger on the turn after a compaction
    snap, ctx.snapped = not ctx.snapped, False

    for turn in itertools.count(1) if max_turns < 1 else range(1, max_turns + 1):
        if snap:
            sha, snap_err = safety.snapshot(ctx.workspace, f"harness: pre-turn {turn}")
            if sha:
                say(f". snapshot {sha}")
            elif snap_err:
                say(f". snapshot failed: {snap_err}")

        _follow_config(ctx, say)
        for text in ui.steered():
            if text.startswith("+") and ctx.plan is not None and ctx.plan.state == "running" and text[1:].strip():
                ctx.plan.steps.append(plan.Step(text=f"{text[1:].strip()} (you)"))
                plan.save(ctx.workspace, ctx.plan)
                _ledger_ask(ctx, text[1:])
                say(f". added to the plan as step {len(ctx.plan.steps)}: {text[1:].strip()}")
                continue
            messages.append({"role": "user", "content": STEERED.format(text=text)})
            _ledger_ask(ctx, text)
            say(f". steered: {text}")
        if done := tools.finished_jobs(ctx):  # a job the user backgrounded ended while this task ran
            messages.append({"role": "user", "content": done})
            say(f". {done.splitlines()[0]}")
        ui.status("reading", "", turn=turn)  # until the first token: the server is reading the prompt
        # The task is restated every ANCHOR_EVERY turns and stays in the history. A different anchor at the
        # end of every prompt (the old way) sat between the history and the reply the server had just
        # generated, so each turn re-read that reply (about a quarter of a run's time, 2026-11-11).
        if ctx.guard.investigate and not told:  # Shift+Tab into investigate mode mid-task: this task gets the rule now
            messages.append({"role": "user", "content": INVESTIGATE})
            say(". INVESTIGATE mode on for the rest of this task")
        told = told or ctx.guard.investigate
        due = ctx.anchor and (anchor_now or turn > 1 and turn % ANCHOR_EVERY == 1)
        anchor = _anchor(messages, turn, ncalls, touches, ctx.guard.investigate, ledger(ctx) if ctx.locked else "") if due else None
        anchor_now = False
        if anchor:
            messages.append(anchor)
        if ctx.think_auto:  # the cap is applied by the server, outside the prompt, so changing it keeps the cache
            llm.reasoning_budget = THINK_ROUTINE if calm and not ctx.locked else THINK_PLAN
        toolset = tools.schemas(ctx.vision, web=not ctx.incognito)
        reply = llm.chat(messages, toolset)
        used = ctx.usage = reply.prompt_tokens  # what was sent; completion includes reasoning, which is never kept
        say(f". turn {turn}: {reply.prompt_tokens} in / {reply.completion_tokens} out / {reply.finish}"
            f" / ctx {100 * used // context}%" + (f" / anchor ~{len(anchor['content']) // 4}t" if anchor else "") + (f" / think<={llm.reasoning_budget}" if ctx.think_auto else "") + _speed(reply.timings))
        ui.status("working", "", ctx=100 * used // context)
        # Terminal only (the dashboard and logs stay lean); skipped when it already streamed in live.
        if reply.reasoning and tty and not (ui.LIVE and ui.LIVE.streamed):
            ui.out(ui.thinking(reply.reasoning), "detail")
        if reply.finish == "length":
            say(". truncated by max_tokens: the answer or the last call may be cut off")
        if reply.finish == "repeat":  # llm.repeating() cut the stream; the loop is not kept, or the model would carry it on
            say(". stopped a reply that was repeating itself")
            reply.reasoning, reply.text = "", REPEATED_REPLY
        if reply.calls and (reply.text or "").strip():  # what it says it is doing: shown in every view
            if tty:
                ui.out(ui.narration(reply.text))
            else:  # the dashboard reads ". " lines as notes
                print(f". >_ {' '.join(reply.text.split())}", file=out, flush=True)

        messages.append(assistant_message(reply))

        if not reply.calls and reply.finish == "repeat":
            messages.append({"role": "user", "content": REPEATING})
            fails += 1
            continue
        if not reply.calls and reply.finish == "length":
            # Cut off before any call or answer was complete (often in the reasoning): say so and go on.
            messages.append({"role": "user", "content": CUT_OFF_TEXT.format(n=reply.completion_tokens)})
            fails += 1
            continue
        if not reply.calls:
            if ui.LIVE and ui.LIVE.queue:  # it finished while a message waited: show the answer, then read that
                ui.out(f"\n{ui.answer(reply.text or '')}\n")
                continue
            if not (reply.text or "").strip():  # nothing at all came back: once is a glitch, twice is said out loud
                messages.pop()
                empties += 1
                if empties < 2:
                    say(". empty reply: asking again")
                    continue
                messages.append({"role": "assistant", "content": EMPTY})
                ctx.stopped = "the model returned nothing twice"
                return EMPTY
            return reply.text or ""

        if reply.finish in ("length", "repeat"):
            # Cut off mid-call: the JSON is incomplete, so running it would only say "not valid JSON".
            for c in reply.calls:
                say(f"-> {c.name} (cut off, not run)")
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": REPEATED_CALL if reply.finish == "repeat" else CUT_OFF.format(n=reply.completion_tokens)})
            fails += len(reply.calls)
            continue

        for c in reply.calls:
            if ctx.backgrounded:  # Ctrl+B on an earlier call of this batch: the user wants the prompt back
                messages.append({"role": "tool", "tool_call_id": c.id, "content": NOT_RUN})
                continue
            say(f"-> {c.name} {_preview(c.arguments)}")
            ui.status(ui.VERBS.get(c.name, c.name), ui.target(c.name, c.arguments))
            ctx.change = None
            t0, w0 = time.monotonic(), ui.LIVE.waited if ui.LIVE else 0.0
            result = tools.call(ctx, c.name, c.arguments)
            took = time.monotonic() - t0 - ((ui.LIVE.waited if ui.LIVE else 0.0) - w0)  # an approval is your time, not the tool's
            first = result.splitlines()[0] if result else ""
            say(f"   {first[:200]}")
            ncalls += 1
            ui.status("working", "", calls=ncalls)
            if tty:
                ui.out(ui.call_line(c.name, c.arguments, result, shutil.get_terminal_size((100, 20)).columns - 1,
                                   str(ctx.workspace), ctx.change, took), "compact")
                if ui.LIVE and ctx.change and not _failed(result):
                    ui.LIVE.changed(_arg(c.arguments, "path") or c.name, ctx.change[0], ctx.change[1])
                if ctx.change and ctx.change[2]:
                    ui.out(ui.diff(ctx.change[2]), "detail")
            messages.append(tools.attach_pending(ctx, result, c.id))
            stats["calls"] += 1
            if c.name == "read_file" and not _failed(result) and (p := _arg(c.arguments, "path")):
                stats["seen"].append(p)
            if c.name in WRITERS and not _failed(result) and (p := _arg(c.arguments, "path")):
                stats["edits"].append(p)
                ctx.ledger["files"] = [f for f in ctx.ledger["files"] if f != p] + [p]
            if _failed(result):
                ctx.ledger["errors"] = (ctx.ledger["errors"] + [f"{c.name} {_preview(c.arguments, 100)} -> {first[:160]}"])[-LEDGER_ERRORS:]
            recent = (recent + [_key(c.name, c.arguments)])[-REPEAT_WINDOW:]
            touches = (touches + _touch(c.name, c.arguments))[-EDIT_WINDOW:]
            fails = fails + 1 if _failed(result) else 0

        if ctx.backgrounded:
            n, ctx.backgrounded = ctx.backgrounded, None
            say(f". job {n} moved to the background; handing back to the user")
            messages.append({"role": "assistant", "content": HANDED_BACK.format(n=n)})
            return HANDED_BACK.format(n=n)
        if ctx.plan_handoff:  # todo_write made a checklist: agent.drive walks the model through it from here
            say(f". checklist: {len(ctx.plan.steps)} steps (the model's own) · /plan to see it")
            return PLAN_HANDOFF

        calm = fails == 0 and any(c.name in WRITERS or c.name == "run_shell" for c in reply.calls)
        if (reply.text or "").strip():
            narrations = (narrations + [reply.text])[-CHANT_WINDOW:]
        readonly_streak = readonly_streak + 1 if all(c.name in READONLY for c in reply.calls) else 0
        # shortcut: a file the user edits by hand mid-task waits for the next writing turn's snapshot
        snap = not all(c.name in READONLY for c in reply.calls) or any(j.proc.poll() is None for j in tools.JOBS)

        compacted = False
        limit = max(compact_at, LOCKED_COMPACT_AT) if ctx.locked else compact_at
        if used >= limit * context:
            say(f". compacting: {used} tokens >= {limit:.0%} of {context}")
            ui.status("compacting", "summarising older turns")
            say(f". {compact(llm, messages, toolset, ledger(ctx) if ctx.locked else '')}")
            stats["task_at"] = None  # the index is gone; the summary holds the task
            anchor_now, compacted = ctx.locked, True
        # auto_checklist: a long task with no checklist gets one, once, at turn AUTO_CHECKLIST_AT or right after a compaction
        if (ctx.auto_checklist and not ctx.guard.investigate and not stats["checklist_asked"] and (compacted or turn >= AUTO_CHECKLIST_AT)
                and _checklist_free(ctx)):
            stats["checklist_asked"] = True
            if _auto_checklist(llm, messages, ctx, toolset, say):
                return PLAN_HANDOFF

        # Soft nudges (stall, chanting): the model is still working, just drifting — remind it once
        # per trip, never stop it. Hard loops (repeat, repeated failure, edit/read-loop) escalate.
        soft = _soft(narrations, readonly_streak)
        if soft:
            say(f". loop check: {soft}")
            narrations, readonly_streak = [], 0
            messages.append({"role": "user", "content": NUDGE.format(why=soft)})
            continue
        why = _stuck(recent, fails, touches)
        if not why:
            continue
        say(f". loop check: {why}")
        recent, fails, touches = [], 0, []
        if not nudged:
            nudged = True
            messages.append({"role": "user", "content": NUDGE.format(why=why)})
            continue
        hint = _ask_user(ctx, why)
        if not hint:
            stop_why = f"still stuck after a nudge ({why})"
            break
        nudged = False
        messages.append({"role": "user", "content": hint})

    ctx.stopped = stop_why
    say(f". stopping: {stop_why}; asking for a final report")
    messages.append({"role": "user", "content": REPORT.format(why=stop_why)})
    ui.status("reporting", "final report")
    report = text_only(llm, messages, tools.schemas(ctx.vision, web=not ctx.incognito)).text or ""
    messages.append({"role": "assistant", "content": report})
    return report


def explain(llm, messages: list[dict], toolset: list[dict], why: str) -> str:
    """Approval menu e: the model says why it wants the call waiting for approval. Nothing is kept in the history.
    Calls in the last reply that have no result yet get a placeholder: the chat format wants every call answered."""
    side = list(messages)
    last = next((i for i in range(len(side) - 1, -1, -1) if side[i]["role"] == "assistant"), None)
    if last is not None:
        answered = {m.get("tool_call_id") for m in side[last + 1:]}
        side += [{"role": "tool", "tool_call_id": c["id"], "content": NOT_YET}
                 for c in side[last].get("tool_calls") or [] if c["id"] not in answered]
    reply = text_only(llm, side + [{"role": "user", "content": EXPLAIN.format(why=why)}], toolset)
    return (reply.text or "").strip() or "(no answer)"


def text_only(llm, messages: list[dict], toolset: list[dict]):
    """A request that wants words, not calls (final report, summary, /btw). The tool list still goes
    along: Qwen's template writes it at the top of the prompt, so leaving it out changes the prompt
    from its first tokens and Strata re-reads the whole history. Any call in the reply is dropped."""
    reply = llm.chat(messages, toolset)
    reply.calls = []
    return reply


def _speed(timings: dict) -> str:
    """Strata's own numbers for the turn line: tokens read vs taken from its cache, and decode speed."""
    if not timings or timings.get("prompt_n") is None:
        return ""
    tps = timings.get("predicted_per_second")
    return f" / read {timings['prompt_n']} new, {timings.get('cache_n', 0)} cached" + (f" / {tps:.0f} tok/s" if tps else "")


def _text(content) -> str:
    """A message's text, whether its content is a string or a list of parts (text + images)."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def compact(llm, messages: list[dict], toolset: list[dict] = (), ledger: str = "") -> str:
    """Swap everything but the system prompt, the current task and the last turns for a summary.

    Runs rarely on purpose: rewriting history makes the server re-read the whole prompt once.
    With a ledger (LOCKED IN mode) the note is the ledger, which holds every ask, plus a longer summary.
    """
    turns = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
    if len(turns) <= COMPACT_KEEP:
        return "nothing old enough to compact"
    cut = turns[-COMPACT_KEEP]  # starts at an assistant message, so no tool reply is orphaned
    tasks = [t for m in messages[1:cut]
             if m["role"] == "user" and not (t := _text(m["content"])).startswith(HARNESS_LINES)]  # the user's own asks only
    reply = text_only(llm, messages + [{"role": "user", "content": COMPACT_LOCKED if ledger else COMPACT}], list(toolset))
    keep = [t for t in (tasks[:1] + tasks[-1:]) if t]  # the original task and the latest, deduped
    head = ledger + "\n\n" if ledger else ("\n\n".join(dict.fromkeys(keep)) + "\n\n" if keep else "")
    note = head + f"[earlier work, auto-compacted]\n{reply.text or ''}"
    dropped = cut - 1
    messages[1:cut] = [{"role": "user", "content": note}]
    return f"compacted {dropped} messages into a {reply.completion_tokens}-token summary"


def _key(name: str, arguments: str) -> str:
    try:
        arguments = json.dumps(json.loads(arguments), sort_keys=True)
    except ValueError:
        pass
    return name + " " + " ".join(arguments.split())


def _arg(arguments: str, name: str):
    try:
        return json.loads(arguments).get(name)
    except (ValueError, AttributeError):
        return None


def _touch(name: str, arguments: str) -> list[tuple[str, str]]:
    """What this call did to a file, for the edit/read-loop detectors. append=true writes are
    NOT edits — the prompt tells the model to write long files in parts that way."""
    path = _arg(arguments, "path")
    if not path:
        return []
    if name in ("edit_file", "replace_def") or (name == "write_file" and not _arg(arguments, "append")):
        return [("edit", path)]
    if name == "read_file":
        return [("read", path)]
    return []


def _follow_config(ctx, say) -> None:
    """Dashboard switches reach a running session: re-read locked_in, auto_checklist and telegram when the config file changes."""
    if ctx.config_mtime is None:
        return
    try:
        mtime = config.path().stat().st_mtime
    except OSError:
        return
    if mtime == ctx.config_mtime:
        return
    ctx.config_mtime = mtime
    try:
        values = config.load()[0]
    except ValueError:
        return  # a half-written or broken file: keep the mode as it is
    locked, checklist = values["locked_in"], values["auto_checklist"]
    ui.TELEGRAM = ui.telegram_load() if values["telegram"] else None
    if locked != ctx.locked:
        ctx.locked = locked
        say(f". LOCKED IN mode {'on' if locked else 'off'} (config changed)")
    if checklist != ctx.auto_checklist:
        ctx.auto_checklist = checklist
        say(f". auto checklist {'on' if checklist else 'off'} (config changed)")


def _ledger_ask(ctx, text: str) -> None:
    """Record a user ask (a task or a message queued mid-task) once, for the LOCKED IN ledger."""
    text = text.strip()
    text = text[:LEDGER_ASK] + ("…" if len(text) > LEDGER_ASK else "")
    if text and text not in ctx.ledger["asks"]:
        ctx.ledger["asks"].append(text)


def ledger(ctx) -> str:
    """LOCKED IN mode's record of the session: every ask, the files changed and the calls that failed."""
    led = ctx.ledger
    parts = [LEDGER, "Everything the user asked, oldest first (each must be done or answered):"]
    parts += [f"{i}. {a}" for i, a in enumerate(led["asks"], 1)] or ["(none yet)"]
    if led["files"]:
        parts.append("Files changed so far: " + ", ".join(led["files"][-30:]))
    if led["errors"]:
        parts.append("Calls that already failed (do not repeat them unchanged):")
        parts += [f"- {e}" for e in led["errors"]]
    if ctx.plan is not None and ctx.plan.state == "running":
        parts += [f"The plan ({ctx.plan.goal}):", plan.checklist(ctx.plan)]
    return "\n".join(parts)


def _anchor(messages: list[dict], turn: int, ncalls: int, touches: list[tuple[str, str]], investigate: bool = False,
            ledger: str = "") -> dict:
    """The task reminder (kept in the history): re-states the latest user line and shows the model its own edit histogram.
    With a ledger (LOCKED IN mode) it restates every ask and past failure instead of the latest line."""
    edits = [p for kind, p in touches if kind == "edit"]
    counts = {p: edits.count(p) for p in dict.fromkeys(edits)}
    hot = ", ".join(f"{_short(p)} x{n}" for p, n in counts.items())
    progress = f"turn {turn}, {ncalls} calls" + (f"; edits so far: {hot}" if hot else "")
    text = ANCHOR.format(task=ledger or _task_line(messages), progress=progress)
    if investigate:
        text = text.replace("</system-reminder>", INVESTIGATE_ANCHOR + "</system-reminder>")
    return {"role": "user", "content": text}


HARNESS_LINES = ("[INVESTIGATE mode]", tools.JOB_DONE, "Your last reply was stopped", "Loop check", "Stop here", "<system-reminder>", "Your last reply hit", "Context is nearly full", "[earlier work, auto-compacted]", LEDGER)


def _task_index(messages: list[dict]) -> int | None:
    """Index of the latest real task (skip the harness's own messages)."""
    for i in range(len(messages) - 1, -1, -1):
        c = messages[i].get("content")
        if messages[i]["role"] == "user" and isinstance(c, (str, list)) and not _text(c).startswith(HARNESS_LINES):
            return i
    return None


def _first_line(text) -> str:
    first = text.strip().splitlines()[0] if isinstance(text, str) and text.strip() else ""
    return first[:120] + ("…" if len(first) > 120 else "")


def _task_line(messages: list[dict]) -> str:
    """First line of the latest real task, truncated."""
    i = _task_index(messages)
    return _first_line(_text(messages[i]["content"])) if i is not None else "(unknown)"


def _short(path: str) -> str:
    return path.rsplit("/", 1)[-1] or path


def _chanting(narrations: list[str]) -> bool:
    """Near-duplicate narration lines = the model spinning ('correcting my correction')."""
    sets = [set(s.lower().split()) for s in narrations if len(s.split()) >= 6]
    hits = sum(1 for i in range(len(sets)) for j in range(i + 1, len(sets))
               if sets[i] and sets[j] and len(sets[i] & sets[j]) / len(sets[i] | sets[j]) >= CHANT_SIM)
    return hits >= CHANT_LIMIT - 1


def _failed(result: str) -> bool:
    return result.startswith(("ERROR:", "DENIED:")) or bool(re.match(r"\[exit -?[1-9]", result))


def _stuck(recent: list[str], fails: int, touches: list[tuple[str, str]] = ()) -> str:
    """Hard loops: a repeated identical call, repeated failures, or editing/re-reading one file
    over and over. These escalate (nudge, then ask the user, then stop)."""
    if fails >= FAIL_LIMIT:
        return f"{fails} failed tool calls in a row"
    for k in set(recent):
        if recent.count(k) >= REPEAT_LIMIT:
            return f"same call {recent.count(k)}x: {_preview(k, 80)}"
    edits = [p for kind, p in touches if kind == "edit"]
    for p in set(edits):
        if edits.count(p) >= EDIT_LIMIT:
            return f"edited {_short(p)} {edits.count(p)}x; stop editing and verify or report"
    reads = [p for kind, p in touches if kind == "read"]
    for p in set(reads):
        if reads.count(p) >= READ_LIMIT:
            return f"re-read {_short(p)} {reads.count(p)}x; act on what you have"
    return ""


def _soft(narrations: list[str] = (), readonly_streak: int = 0) -> str:
    """Drift that only warrants a reminder, never a stop: the model is still making calls, just
    spinning its wheels (repeating itself) or reading without acting/answering."""
    if _chanting(list(narrations)):
        return "repeating yourself; change approach or report"
    if readonly_streak >= STALL_LIMIT:
        return (f"{readonly_streak} turns of only reading and no answer; if the task only needs "
                "an answer, give it and stop, otherwise make the change")
    return ""


def _ask_user(ctx: tools.Ctx, why: str) -> str:
    """A hint from the user, or "" to stop. Nobody is there to ask under --yes."""
    if ctx.guard.assume_yes:
        return ""
    prompt = f"[harness] paused — {why}. Type guidance, Enter to stop, /expand for detail, /q to end: "
    try:
        hint = ui.harness_input(prompt, ctx.guard).strip()
        ui.record(prompt + hint)
        return hint
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return ""


def _preview(arguments: str, width: int = 160) -> str:
    flat = " ".join(arguments.split())
    return flat if len(flat) <= width else flat[:width] + "..."
