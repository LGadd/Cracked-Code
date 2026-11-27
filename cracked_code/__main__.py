"""Command line: REPL by default, one task with -p."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
from pathlib import Path

from . import __version__, agent, config, live, llm, plan, repl, safety, tools, ui


# Same logo as the dashboard boot screen (dashboard .ascii block).
LOGO = """\
 ▄████▄  ██▀███   ▄▄▄       ▄████▄   ██ ▄█▀▓█████ ▓█████▄
▒██▀ ▀█ ▓██ ▒ ██▒▒████▄    ▒██▀ ▀█   ██▄█▒ ▓█   ▀ ▒██▀ ██▌
▒▓█    ▄▓██ ░▄█ ▒▒██  ▀█▄  ▒▓█    ▄ ▓███▄░ ▒███   ░██   █▌   ▄▄▄ ▄▄▄ ▄▄█ ▄▄▄
▒▓▓▄ ▄██▒██▀▀█▄  ░██▄▄▄▄██ ▒▓▓▄ ▄██▒▓██ █▄ ▒▓█  ▄ ░▓█▄   ▌   █   █ █ █ █ █▄█
▒ ▓███▀ ░██▓ ▒██▒ ▓█   ▓██▒▒ ▓███▀ ░▒██▒ █▄░▒████▒░▒████▓    █▄▄ █▄█ █▄█ █▄▄
░ ░▒ ▒  ░ ▒▓ ░▒▓░ ▒▒   ▓▒█░░ ░▒ ▒  ░▒ ▒▒ ▓▒░░ ▒░ ░ ▒▒▓  ▒    ░▒▒ ░▒░ ░▒░ ░▒▒"""


FALLBACK_CONTEXT = 262144  # budget when neither the config nor the server gives a window (Qwen3.8's)


def banner(rows: list[tuple[str, str]]) -> str:
    """The logo (one line when the terminal is too narrow for it, about 84 columns, or under 40 rows), then label/value rows lined up:
    the first two values (model, workspace) bright, the rest in the body colour, labels muted."""
    size = shutil.get_terminal_size((100, 50))
    wide = max(len(ln) for ln in LOGO.splitlines()) + len(__version__) + 2  # the art plus " 0.1.0", without wrapping
    logo = (f"{LOGO} {__version__}" if size.columns >= wide and size.lines >= 40 else f"▓▒░ CRACKED code {__version__}")
    if not ui.ON:
        return logo + "\n\n" + "\n".join(f"{k:<10} {v}" for k, v in rows)
    return (ui.paint(logo, ui.NEON, bold=True) + "\n\n"
            + "\n".join(ui.paint(f"{k:<10} ", ui.MUTED) + ui.paint(v, ui.NEON if i < 2 else ui.INK2, bold=i < 2)
                        for i, (k, v) in enumerate(rows)))


def main(argv: list[str] | None = None) -> int:
    try:
        cfg, src = config.load()
    except ValueError as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    if cfg["theme"] not in ui.THEMES:
        print(f"theme = {cfg['theme']!r}: use {' or '.join(ui.THEMES)}", file=sys.stderr)
        return 2
    ui.set_theme(cfg["theme"])
    ap = argparse.ArgumentParser(
        prog="cracked",
        description="Cracked Code: a small, fast coding-agent harness for Qwen3.8 on Strata (other OpenAI-compatible servers: best effort). "
                    f"Defaults come from {config.path()} (see --show-config).",
    )
    ap.add_argument("-p", "--prompt", action="append",
                    help="Run one task and exit. Repeat it to run several tasks in turn in one session.")
    ap.add_argument("-c", "--continue", dest="resume", action="store_true",
                    help="Carry on with the most recent saved session in this workspace.")
    ap.add_argument("--workspace", default=os.environ.get("HARNESS_WORKSPACE", os.getcwd()),
                    help="Folder the agent works in (default: cwd).")
    ap.add_argument("--base-url", default=cfg["base_url"])
    ap.add_argument("--model", default=cfg["model"])
    ap.add_argument("--max-turns", type=int, default=cfg["max_turns"],
                    help="Model round trips per task before it stops and reports; -1 is no limit. "
                         "In the REPL, /turns N changes it.")
    ap.add_argument("--max-tokens", type=int, default=cfg["max_tokens"])
    ap.add_argument("--context", type=int, default=cfg["context"],
                    help="Context window the harness budgets for; 0 = what the server reports.")
    ap.add_argument("--compact-at", type=float, default=cfg["compact_at"],
                    help="Auto-compact when a turn uses this share of --context.")
    ap.add_argument("--warn-mb", type=int, default=cfg["warn_mb"],
                    help="Ask before starting in a workspace bigger than this many MB; 0 never asks.")
    ap.add_argument("--yes", action="store_true", help="Start in Fuck It Mode: answer every approval with yes.")
    ap.add_argument("--image", action="append", default=[], metavar="PATH",
                    help="Attach an image to the task (repeatable).")
    ap.add_argument("--show-config", action="store_true",
                    help="Print every setting and where it came from (flag, env, file, default), then exit.")
    args = ap.parse_args(argv)

    flags = {k for k in config.DEFAULTS if getattr(args, k, cfg[k]) != cfg[k]}
    server = llm.probe(args.base_url, args.model)  # model id, window and image input, from the server itself
    asked = {}
    if not args.model and server["model"]:
        args.model, asked["model"] = server["model"], True
    if server["context"] and not 0 < args.context <= server["context"]:
        args.context, asked["context"] = server["context"], True
    args.context = args.context or FALLBACK_CONTEXT
    if args.show_config:
        print(f"# config file: {config.path()}" + ("" if config.path().exists() else " (not created yet)"))
        for k in config.DEFAULTS:
            print(f"{k} = {getattr(args, k, cfg[k])!r}  ({'server' if k in asked else 'flag' if k in flags else src[k]})")
        print(f"# images: {'on' if server['vision'] else 'off'} (decided by the server's model list)")
        return 0
    if not args.model:
        ids = ", ".join(server["ids"]) or "none listed: is it running?"
        print(f'no model set and {args.base_url} does not serve exactly one (models there: {ids}). '
              f'Add model = "<id>" to {config.path()}, set HARNESS_MODEL, or pass --model.', file=sys.stderr)
        return 2

    ws = Path(args.workspace).resolve()
    safety.SNAPSHOT, safety.MAX_MB, safety.SKIP = cfg["snapshot"], cfg["snapshot_max_mb"], cfg["snapshot_skip"]
    tools.SHELL_TIMEOUT_MAX = cfg["shell_timeout_max"]
    home_warn = safety.home_warning(ws)
    ws.mkdir(parents=True, exist_ok=True)
    if home_warn:  # measuring ~ takes seconds; say why it is quiet
        print(ui.paint(f"measuring {ws} …", ui.MUTED), file=sys.stderr, flush=True)
    size, nfiles = safety.workspace_size(ws)
    big = 0 < args.warn_mb * 1024 * 1024 < size  # warn_mb 0: the dashboard checks the workspace itself
    if big or home_warn:
        warn = (f"{home_warn} " if home_warn else "") + (
                f"This workspace is {ui.human(size)} ({nfiles} files). Every turn starts with a git "
                f"snapshot of it, and the first one can take a while.")
        if sys.stdin.isatty() and not args.yes:
            try:
                ok = input(ui.paint(warn + " Are you sure you would like to proceed? "
                                    "[y]es / [n]o / [s]tart without snapshots (no /undo or /diff) [y/N/s] ", ui.AMBER))
            except (EOFError, KeyboardInterrupt):
                ok = ""
            if ok.strip().lower() in ("s", "skip"):
                safety.SNAPSHOT = big = home_warn = False  # this session only; nothing to snapshot, so no "snapshotting…" line
            elif ok.strip().lower() not in ("y", "yes"):
                print("not started.", file=sys.stderr)
                return 1
        elif home_warn and not args.yes:  # no terminal to ask on: ~ or / needs an explicit --yes
            print(f"{warn} Not started: no terminal to ask on; pass --yes to proceed.", file=sys.stderr)
            return 2
        else:  # --yes, or a big project with no terminal to ask on: say so and carry on
            print(f"warning: {warn}", file=sys.stderr)
    vision = server["vision"]  # images only when the server says the model takes them
    if args.image and not vision:
        print(f"warning: --image given but {args.base_url} does not list image input for {args.model}; "
              f"the files will be attached anyway and the server may reject them.", file=sys.stderr)
    ui.BELL = cfg["bell"]
    ui.TELEGRAM, ui.WHERE = (ui.telegram_load() if cfg["telegram"] else None), ws.name
    ctx = tools.Ctx(workspace=ws, guard=safety.Guard(ws, assume_yes=args.yes), vision=vision, fold=not cfg["locked_in"], fold_keep=agent.FOLD_KEEP, fold_above=agent.FOLD_ABOVE,
                    think_auto=cfg["auto_think"] and cfg["reasoning_budget"] == 0, locked=cfg["locked_in"],
                    config_mtime=config.path().stat().st_mtime if config.path().exists() else 0.0,
                    auto_checklist=cfg["auto_checklist"])
    try:
        ctx.plan = plan.load(ws)  # an unfinished plan from an earlier session: /plan carries on
    except plan.PlanError:
        ctx.plan = None
    if cfg["reasoning_effort"] not in ("", "none", "low", "medium", "high"):
        print(f"reasoning_effort = {cfg['reasoning_effort']!r}: use none, low, medium or high (or \"\" to not send it)", file=sys.stderr)
        return 2
    api_key = os.environ.get(cfg["api_key_env"], "") if cfg["api_key_env"] else ""
    if cfg["api_key_env"] and not api_key:
        print(f"api_key_env = {cfg['api_key_env']!r}, but that environment variable is not set (fish: set -Ux {cfg['api_key_env']} <key>)", file=sys.stderr)
        return 2
    model = llm.LLM(args.base_url, args.model, max_tokens=args.max_tokens, api_key=api_key,
                    temperature=float(cfg["temperature"]), reasoning_effort=cfg["reasoning_effort"] or ("medium" if cfg["auto_think"] else ""),
                    reasoning_budget=cfg["reasoning_budget"],
                    on_delta=ui.activity if ui.ON else None)

    def watching(hold: bool = False):
        """The live spinner and mid-task keys, in a colour terminal with a keyboard. hold: the caller prints the
        done line (Live.summary) itself, after the answer."""
        return live.Live(ctx.guard, hold=hold) if ui.ON and sys.stdin.isatty() else contextlib.nullcontext()
    extra, loaded = agent.instructions(ws, config.path().parent)
    system = agent.SYSTEM.format(workspace=ws, narrate=agent.NARRATE if cfg["narrate"] else "")
    messages: list[dict] = [{"role": "system", "content": (agent.one_shot(system) if cfg["one_shot"] else system)
                                           + (agent.workspace_map(ws) + agent.workspace_files(ws) if cfg["map"] else "") + extra}]
    if loaded:
        ui.out(ui.line(". instructions: " + ", ".join(loaded)))

    # Take the first (slow) snapshot now, visibly, rather than silently after the first task.
    if big or home_warn:
        ui.out(ui.line(f". snapshotting {nfiles} files…"))
    try:
        sha, snap_err = safety.snapshot(ws, "harness: session start")
    except KeyboardInterrupt:
        print("\nsnapshot interrupted: not started.", file=sys.stderr)
        return 130
    if snap_err:
        ui.out(ui.line(f". snapshot failed: {snap_err}"))
    elif sha and ui.ON:
        ui.out(ui.line(f". snapshot {sha} (session start)"), "detail")

    session = repl.Repl(args, cfg, ctx, model, messages, watching, safety.head(ws))
    if not args.prompt:
        home = str(Path.home())
        where = "~" + str(ws)[len(home):] if str(ws) == home or str(ws).startswith(home + os.sep) else str(ws)
        ui.out(banner([("model", f"{args.model} @ {args.base_url}"), ("workspace", where),
                       ("keys", "type a task · /help · Shift+Tab mode · Ctrl+O details · Esc stops · Ctrl-D quits"),
                       ("turns", f"{'no limit' if args.max_turns < 1 else args.max_turns} per task (/turns N to change)"),
                       *([("locked in", "every ask and failure survives compaction (config locked_in)")] if ctx.locked else []),
                       *([("one shot", "the model makes the change and stops, you test it (config one_shot)")]
                         if cfg["one_shot"] else [])]) + "\n")
    if args.resume:
        if path := repl.latest_session(ws):
            session.resume(path)
        else:
            ui.out(ui.paint("no saved session in this folder: starting a new one", ui.MUTED))

    for n, prompt in enumerate(args.prompt or []):
        messages.append(repl.user_message(prompt, args.image if n == 0 else [], ctx))
        try:
            with watching(hold=True) as watch:
                answer = agent.drive(model, messages, ctx, args.max_turns, context=args.context, compact_at=args.compact_at)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        except OSError as e:
            print(llm.failure(args.base_url, e), file=sys.stderr)
            return 1
        finally:
            session.save()  # cracked -c carries on from a one-shot too
        if answer:
            ui.out(ui.answer(answer) if ui.ON else answer)
        if getattr(watch, "summary", ""):
            ui.out(watch.summary)
    if args.prompt:
        return 0

    return session.loop()


if __name__ == "__main__":
    raise SystemExit(main())
