"""Terminal colours in the dashboard palette. Off for pipes, NO_COLOR and TERM=dumb (the dashboard's pty)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import threading
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ON = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
if sys.platform == "win32" and ON:  # Windows port (2026-11-05)
    with contextlib.suppress(Exception):  # classic conhost needs VT mode for the colour codes; Windows Terminal has it
        import ctypes
        _k, _mode = ctypes.windll.kernel32, ctypes.c_uint32()
        if _k.GetConsoleMode(_h := _k.GetStdHandle(-11), ctypes.byref(_mode)):
            _k.SetConsoleMode(_h, _mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
if sys.platform == "win32":  # pipes too: a ✓ or ☐ printed to a cp1252 pipe (cracked -p > log) crashed (CI, 2026-11-27)
    for _s in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            _s.reconfigure(encoding="utf-8", errors="replace")

# 24-bit colour where the terminal says so (COLORTERM); otherwise the nearest of the 256 (tmux, Terminal.app)
TRUE = (os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or "direct" in os.environ.get("TERM", "")
        or bool(os.environ.get("WT_SESSION")) or sys.platform == "win32")
THEMES = {  # config theme: NEON, INK2, AMBER, TOOL, MUTED, CRIT
    "dark": ("43;255;136", "143;201;163", "255;180;59", "240;98;184", "79;122;93", "255;59;78"),
    "light": ("0;135;62", "35;75;50", "176;96;0", "176;30;120", "105;125;110", "200;30;45"),
}
NEON, INK2, AMBER, TOOL, MUTED, CRIT = THEMES["dark"]
THEME = "dark"


def set_theme(name: str) -> None:
    """config theme = "dark" | "light". Call before anything is drawn: the colours are read when used."""
    global NEON, INK2, AMBER, TOOL, MUTED, CRIT, THEME
    THEME = name if name in THEMES else "dark"
    NEON, INK2, AMBER, TOOL, MUTED, CRIT = THEMES[THEME]


def _256(rgb: str) -> int:
    """Nearest xterm-256 colour: the 6x6x6 cube or the grey ramp, whichever is closer."""
    r, g, b = (int(v) for v in rgb.split(";"))
    steps = (0, 95, 135, 175, 215, 255)
    near = lambda v: min(range(6), key=lambda i: abs(steps[i] - v))
    cube = (near(r), near(g), near(b))
    grey = max(0, min(23, round((r + g + b) / 3 - 8) // 10))
    dist = lambda c: sum((x - y) ** 2 for x, y in zip(c, (r, g, b)))
    if dist((8 + 10 * grey,) * 3) < dist(tuple(steps[i] for i in cube)):
        return 232 + grey
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


def sgr(rgb: str) -> str:
    """The escape-code part that sets this foreground colour."""
    return f"38;2;{rgb}" if TRUE else f"38;5;{_256(rgb)}"

PLAN = ""            # plan progress for the live line ("plan 3/9 · title", or "☐ 3/7 · title" for the model's own)
EXPANDED = False      # Ctrl+O (at the prompt or mid-task): show reasoning, turn and snapshot lines in full
BELL = True           # config bell: ring when a task ends or an approval waits
TELEGRAM = None       # (token, chat) when config telegram is on and TELEGRAM_FILE has both; None = no messages
TELEGRAM_FILE = Path.home() / ".config" / "cracked-code" / "telegram.env"  # TG_TOKEN=... and TG_CHAT=..., outside the repo
WHERE = ""            # the workspace's name, at the start of every message
WAIT_PING = 15.0      # seconds a question may wait unanswered before it is sent to Telegram
ASK = None           # harness_input's line reader; the REPL swaps in prompt.ask (Shift+Tab, Ctrl+O)
CHOOSE = None        # choose()'s arrow-key menu; the REPL swaps in prompt.choose
PROMPT = ">_ "        # where you type: the REPL prompt and the box mid-task (was "you> " until 2026-11-12)
MODES = None          # () -> {"LOCKED IN": bool, "ONE SHOT": bool, "AUTO CHECKLIST": bool}: the REPL sets it
MODE_CMDS: dict = {}  # "/lockedin" -> fn(arg) -> message: the switches, also typed mid-task
LIVE = None           # live.Live while a task runs in a colour terminal: the spinner line and mid-task keys
# Everything the colour terminal printed this session, as (kind, text): "all" always shows, "detail"
# only expanded, "compact" only collapsed. Ctrl+O clears the screen and replays it in the other view.
TRANSCRIPT: list[tuple[str, str]] = []
TRANSCRIPT_MAX = 5000

WARN = (". truncated", ". loop check", ". compacting", ". stopping", ". snapshot failed", ". nothing old")


def paint(text: str, rgb: str, bold: bool = False, prompt: bool = False) -> str:
    if not ON:
        return text
    on, off = f"\033[{'1;' if bold else ''}{sgr(rgb)}m", "\033[0m"
    if prompt:  # readline must not count escape codes as cursor width
        on, off = f"\001{on}\002", f"\001{off}\002"
    return f"{on}{text}{off}"


def chip(text: str, rgb: str, prompt: bool = False) -> str:
    """A label on a solid block of colour (reverse video, so it suits dark and light terminals); [text] without colour."""
    if not ON:
        return f"[{text}]"
    on, off = f"\033[1;7;{sgr(rgb)}m", "\033[0m"
    if prompt:
        on, off = f"\001{on}\002", f"\001{off}\002"
    return f"{on} {text} {off}"


def mode(guard, prompt: bool = True) -> str:
    """The current mode as a chip: the prompt, the live footer and the bottom bar all use this one."""
    if guard.assume_yes:
        return chip("FUCK IT MODE", CRIT, prompt) + " "
    return chip("INVESTIGATE", AMBER, prompt) + " " if guard.investigate else ""


def modes_text(modes: dict) -> str:
    """The three task modes, every one shown so on and off are both visible: ● NAME when on (neon), ○ name when off
    (muted). The shapes differ too, so it reads without colour."""
    if not ON:
        return " · ".join(f"{name} {'on' if on else 'off'}" for name, on in modes.items())
    return "  ".join(paint(f"● {name}", NEON, bold=True) if on else paint(f"○ {name.lower()}", MUTED)
                     for name, on in modes.items())


def modes_on() -> list[str]:
    """The task modes that are on now (none without a REPL)."""
    return [name for name, on in MODES().items() if on] if MODES else []


def mode_line(guard) -> str:
    if guard.assume_yes:
        return paint("FUCK IT MODE on: everything is auto-approved", CRIT, bold=True)
    if guard.investigate:
        return paint("INVESTIGATE mode on: read only, it reports what it found and stops", AMBER, bold=True)
    return paint("manual mode: approvals ask first", NEON)


def next_mode(guard) -> str:
    """Shift+Tab: manual -> Fuck It -> INVESTIGATE -> manual."""
    if guard.assume_yes:
        guard.assume_yes, guard.investigate = False, True
    elif guard.investigate:
        guard.investigate = False
    else:
        guard.assume_yes = True
    return mode_line(guard)


def thinking(text: str) -> str:
    """The model's reasoning, dim and indented under the turn line."""
    return "\n".join(f"\033[3;{sgr(MUTED)}m  │ {ln}\033[0m" for ln in text.strip().splitlines())


def visible(kind: str) -> bool:
    return kind == "all" or (kind == "detail") == EXPANDED


def record(text: str, kind: str = "all") -> None:
    """Remember a line for Ctrl+O replays without printing it (input() already showed it)."""
    if ON:
        TRANSCRIPT.append((kind, text))
        del TRANSCRIPT[:-TRANSCRIPT_MAX]


def out(text: str, kind: str = "all") -> None:
    """Print a line the user sees (if this view shows its kind) and keep it for replays."""
    record(text, kind)
    if not ON or visible(kind):
        emit(text)


def redraw() -> None:
    """Ctrl+O: clear the screen and scrollback, then replay the session in the current view."""
    if not ON:  # nothing to redraw on a pipe or the dashboard's pty; don't emit escape codes
        return
    cols = shutil.get_terminal_size((100, 20)).columns - 1
    body = "".join(wrap(t, cols) + "\n" for k, t in TRANSCRIPT if visible(k))
    sys.stdout.write("\033[H\033[2J\033[3J" + body)
    sys.stdout.flush()


def telegram_load(file: Path | None = None) -> tuple[str, str] | None:
    """(token, chat) from a KEY=VALUE file, or None when it is missing or lacks either."""
    try:
        lines = (file or TELEGRAM_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    env = dict(ln.split("=", 1) for ln in (l.strip() for l in lines) if "=" in ln and not ln.startswith("#"))
    token, chat = env.get("TG_TOKEN", "").strip(), env.get("TG_CHAT", "").strip()
    return (token, chat) if token and chat else None


def _telegram_send(html: str) -> None:
    token, chat = TELEGRAM
    data = urllib.parse.urlencode({"chat_id": chat, "parse_mode": "HTML", "disable_web_page_preview": "true",
                                   "text": f"<b>{_esc(WHERE)}</b> · {html}"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10).close()
    except OSError:
        pass  # shortcut: a failed send is silent (printing from a thread breaks the live line); add a /telegram test if it bites


def _esc(text: str, limit: int = 1500) -> str:
    """Plain text made safe for Telegram HTML: colours gone, cut to limit (so the message stays under 4096)."""
    text = re.sub(r"\033\[[0-9;]*m", "", text).strip()
    text = text if len(text) <= limit else text[:limit].rstrip() + " …"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify(html: str) -> None:
    """Send a Telegram HTML message in the background; never blocks, never raises. Escape parts with _esc."""
    if TELEGRAM:
        threading.Thread(target=_telegram_send, args=(html,), daemon=True).start()


def took(seconds: float) -> str:
    """A duration the same way everywhere: 42s, then 3m 5s."""
    return f"{int(seconds // 60)}m {int(seconds % 60)}s" if seconds >= 60 else f"{seconds:.0f}s"


def rule(text: str = "", rgb: str = "") -> str:
    """── text ──────── across the terminal: closes a task, so the scrollback splits into tasks."""
    if not ON:
        return text
    w = shutil.get_terminal_size((100, 20)).columns - 1
    head = f"── {text} " if text else ""
    return paint(head + "─" * max(0, w - cells(head)), rgb or MUTED)


def answer(text: str) -> str:
    """The model's answer, formatted, with a neon bar down its left edge so it stands apart from the tool lines."""
    body = pretty(text)
    if not ON:
        return body
    body = wrap(body, shutil.get_terminal_size((100, 20)).columns - 3)
    return "\n".join(paint("│ " if ln else "│", NEON) + ln for ln in body.split("\n"))


def waiting_message(question: str, detail: str = "") -> str:
    """What a question left unanswered says: the question, and the diff or detail behind it (20 lines at most)."""
    lines = re.sub(r"\033\[[0-9;]*m", "", detail).strip().splitlines()
    more = f"\n… {len(lines) - 20} more lines" if len(lines) > 20 else ""
    return (f"⏸ <b>waiting for you</b>\n{_esc(question, 500)}"
            + (f"\n<pre>{_esc(chr(10).join(lines[:20]) + more, 1800)}</pre>" if lines else ""))


def done_message(task: str, answer: str, seconds: float, numstat: str = "") -> str:
    """What a finished task says: what was asked, how long, which files changed (git --numstat), the closing reply."""
    rows = [ln.split("\t") for ln in numstat.strip().splitlines() if ln.count("\t") == 2]
    files = ", ".join(f"{Path(f).name} +{a} −{d}" if a != "-" else f"{Path(f).name} (binary)" for a, d, f in rows[:5])
    files += f" (+{len(rows) - 5} more)" if len(rows) > 5 else ""
    asked = (task.strip().splitlines() or [""])[0]
    return (f"✅ <b>finished</b> · {took(seconds)}\n<b>asked:</b> {_esc(asked, 200)}"
            + (f"\n<b>files:</b> {_esc(files, 400)}" if files else "")
            + f"\n\n{_esc(answer)}")


@contextlib.contextmanager
def waiting(question: str, detail: str = ""):
    """Around a question to the user: if it is still open after WAIT_PING seconds, it goes to Telegram."""
    timer = threading.Timer(WAIT_PING, notify, [waiting_message(question, detail)]) if TELEGRAM else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer:
            timer.cancel()


def harness_input(prompt: str, guard=None, ping: str = "", detail: str = "") -> str:
    """input() for the harness's own prompts (the stuck prompt, the approval prompt). The REPL's
    view/mode slash commands work here too and NEVER reach the model: /expand (or Ctrl+O) toggles
    detail and re-asks, /mode cycles the mode and re-asks, /q|/quit|/exit raises EOFError (the
    caller treats that as stop/deny). Anything else is returned as the user's own answer.
    """
    global EXPANDED
    with paused(), waiting(ping or prompt, detail):  # ping/detail: what Telegram gets if it waits  # stop the live spinner (if any) while the question is open
        while True:
            line = (ASK or input)(prompt)
            s = line.strip().lower()
            if s in ("/q", "/quit", "/exit"):
                raise EOFError
            if s in ("/expand", "/e"):
                EXPANDED = not EXPANDED
                redraw()
                continue
            if s == "/mode" and guard is not None:
                print(next_mode(guard))
                continue
            return line


def choose(question: str, options: list[tuple[str, str]], default: str = "", detail: str = "", mode: bool = False,
           tag: str = "") -> str:
    """One of options' keys: (key, label) pairs. Arrow keys + Enter, or the key itself, in a colour terminal;
    elsewhere (pipes, plain terminals) type the key. Enter alone (and the first highlight) takes default, else the
    first; /q or end of input = the last. /expand, /e or Ctrl+O toggles detail and asks again. mode=True returns
    "/mode" for Shift+Tab (or typed /mode); otherwise it asks again. detail goes to Telegram if the question waits.
    tag: a short label shown as an amber chip before the question (approvals: "ALLOW?")."""
    global EXPANDED
    keys = [k for k, _ in options]
    default = default if default in keys else keys[0]
    show = True
    with paused(), waiting(f"{tag} {question}".strip(), detail):
        while True:
            if CHOOSE:
                key = CHOOSE(question, options, keys.index(default), tag=tag)
            else:
                if show:
                    print((chip(tag, AMBER) + " " if tag else "") + paint(question, NEON, bold=True))
                    for k, label in options:
                        print(f"  {paint(k, NEON, bold=True)}  {label}")
                    show = False
                try:
                    answer = (ASK or input)(paint(f"[{'/'.join(keys)}] ", AMBER, prompt=True)).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = "/q"
                key = (default if not answer else keys[-1] if answer in ("/q", "/quit", "/exit")
                       else "/expand" if answer in ("/expand", "/e") else "/mode" if answer.startswith("/mode") else answer[:1])
            if key == "/expand":
                EXPANDED, show = not EXPANDED, True
                redraw()
            elif key in keys or (mode and key == "/mode"):
                break
        record((chip(tag, AMBER) + " " if tag else "") + paint(question, NEON, bold=True) + " " + dict(options).get(key, key))
        return key


CODES = re.compile(r"\033\[[0-9;?]*[A-Za-z]|[\001\002]")  # colour codes: no width on screen
HANG = re.compile(r"\s*(?:(?:[-*•·▸✓!–]|\d+[.)])\s+(?:\[.\]\s+\d+\.\s+)?)?")  # a list marker: wrapped lines line up after it


def cells(text: str) -> int:
    """Columns on screen: wide characters (CJK, most emoji) take two, combining marks none. Colour codes count."""
    return sum(0 if unicodedata.combining(c) or c == "\ufe0f" else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def cut(text: str, room: int) -> str:
    """The start of plain text that fits in room columns."""
    used = 0
    for i, c in enumerate(text):
        used += cells(c)
        if used > room:
            return text[:i]
    return text


def wrap(text: str, width: int) -> str:
    """Word-wrap for the terminal, which otherwise cuts words at the edge. Colour codes take no room, wide
    characters two; a wrapped list item lines up under its text."""
    out = []
    for line in text.split("\n"):
        plain = CODES.sub("", line)
        if cells(plain) <= width:
            out.append(line)
            continue
        hang = " " * min(len(HANG.match(plain).group()), width // 2)
        row, used = "", 0
        for word in re.split(r"(?<= )(?=[^ ])", line):  # words keep the spaces after them
            w = cells(CODES.sub("", word.rstrip(" ")))
            if used and used + w > width:
                out.append(row.rstrip(" "))
                row, used = hang, len(hang)
            row += word
            used += cells(CODES.sub("", word))
        out.append(row.rstrip(" "))
    return "\n".join(out)


def _inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: paint(m.group(1), INK2, bold=True), text)
    return re.sub(r"`([^`]+)`", lambda m: paint(m.group(1), NEON), text)


TABLE_RULE = re.compile(r"\s*:?-{2,}:?\s*")


def _table(lines: list[str]) -> list[str]:
    """A markdown table with its columns lined up; the header row bold. Too wide for the terminal: left as typed."""
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    head = len(rows) > 1 and all(TABLE_RULE.fullmatch(c) for c in rows[1])
    rows = [r for i, r in enumerate(rows) if not (head and i == 1)]
    shown = [[_inline(c) for c in r] for r in rows]
    n = max(len(r) for r in rows)
    widths = [max((cells(CODES.sub("", r[i])) for r in shown if i < len(r)), default=0) for i in range(n)]
    if sum(widths) + 3 * n > shutil.get_terminal_size((100, 20)).columns - 3:
        return lines
    pad = lambda c, w: c + " " * (w - cells(CODES.sub("", c)))
    bar = paint(" │ ", MUTED)
    out = []
    for i, r in enumerate(shown):
        cols = [pad(r[j] if j < len(r) else "", widths[j]) for j in range(n)]
        if head and i == 0:
            cols = [paint(CODES.sub("", c), NEON, bold=True) for c in cols]
        out.append(bar.join(cols).rstrip())
        if head and i == 0:
            out.append(paint("─┼─".join("─" * w for w in widths), MUTED))
    return out


def _code(lines: list[str], lang: str) -> list[str]:
    """A fenced code block in syntax colours (pygments, when installed and the fence names a language it knows)."""
    try:
        from pygments import highlight
        from pygments.formatters import Terminal256Formatter, TerminalTrueColorFormatter
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except ImportError:
        return lines
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return lines
    style = "monokai" if THEME == "dark" else "friendly"
    fmt = TerminalTrueColorFormatter(style=style) if TRUE else Terminal256Formatter(style=style)
    return highlight("\n".join(lines), lexer, fmt).rstrip("\n").split("\n")


def pretty(text: str) -> str:
    """The model's answers and plans, made easy to scan: headings coloured with a gap above, numbered steps
    spaced apart with the number in amber, bullets marked, a step's `| files:` / `| check:` on lines of
    their own. Code blocks are left as they are. Plain text without colour."""
    if not ON:
        return text
    out, fence, code, lang, table = [], False, [], "", []
    gap = lambda: out and out[-1] != "" and out.append("")
    for line in text.split("\n"):
        s = line.strip()
        if table and not (s.startswith("|") and not fence):
            out += _table(table)
            table = []
        if s.startswith("```"):
            if fence:
                out += _code(code, lang)
                code = []
            fence, lang = not fence, s[3:].strip().split(" ")[0]
            out.append(paint(line, MUTED))
            continue
        if fence:
            code.append(line)
        elif s.startswith("|") and s.endswith("|") and s.count("|") > 1 or table and s.startswith("|"):
            table.append(line)
        elif m := re.match(r"#{1,6}\s+(.*)", s):
            gap()
            out.append(paint(m.group(1).strip("*"), NEON, bold=True))
        elif re.fullmatch(r"\*\*[^*]+\*\*:?", s) or (s.endswith(":") and len(s) <= 60 and not HANG.match(s).group()):
            gap()
            out.append(paint(s.replace("**", ""), AMBER, bold=True))
        elif m := re.match(r"(?:-\s+\[(.)\]\s+|([·▸✓!–])\s+)?(\d+)[.)]\s+(.*)", line):
            box, sign, n, rest = m.groups()
            parts = re.split(r"\s+\|\s+(?=(?:files|check):)", rest)
            result = re.split(r"\s+→\s+", parts[0], maxsplit=1)
            mark = {"x": "✓", "~": "▸", " ": "·", "!": "!", "-": "–"}.get(box, sign) if box or sign else ""
            gap()
            out.append(paint(f"{mark + ' ' if mark else ''}{n}.", AMBER, bold=True) + " " + _inline(result[0]))
            pad = " " * (len(mark) + len(n) + (3 if mark else 2))
            out += [pad + paint(p, MUTED) for p in parts[1:]]
            if len(result) > 1:
                out.append(pad + paint("→ " + result[1], INK2))
        elif m := re.match(r"(\s*)[-*•]\s+(.*)", line):
            out.append(m.group(1) + paint("•", NEON) + " " + _inline(m.group(2)))
        elif not s:
            if out and out[-1] != "":
                out.append("")
        else:
            out.append(_inline(line))
    out += _table(table) if table else code  # an unclosed fence keeps its lines
    return "\n".join(out).strip("\n")


def emit(text: str) -> None:
    """Print one finished line; with a live spinner, above it."""
    if ON:
        text = wrap(text, shutil.get_terminal_size((100, 20)).columns - 1)
    if LIVE:
        LIVE.print(text)
    else:
        print(text, flush=True)


def status(phase: str, detail: str = "", **kw) -> None:
    """What the spinner line says now: reading, thinking, shell ... (no-op without a live line)."""
    if LIVE:
        LIVE.set(phase, detail, **kw)


def steered() -> list[str]:
    """Messages queued with Enter mid-task, taken off the queue (none without a live line)."""
    if not LIVE:
        return []
    with LIVE.lock:
        texts, LIVE.queue[:] = LIVE.queue[:], []
    return texts


def activity(kind: str, text: str) -> None:
    """llm.LLM's on_delta while a reply streams in."""
    if LIVE:
        LIVE.delta(kind, text)


@contextlib.contextmanager
def paused():
    """Around input(): the spinner stops and the terminal is back in line mode."""
    if LIVE:
        with LIVE.pause():
            yield
    else:
        yield


VERBS = {"todo_write": "checklist", "read_file": "read", "write_file": "write", "edit_file": "edit", "replace_def": "edit", "delete_file": "delete",
         "run_shell": "shell", "web_search": "search", "web_fetch": "fetch", "see_image": "look"}


def target(name: str, arguments: str) -> str:
    """The one argument a person wants to see: the path, command, query or URL."""
    try:
        a = json.loads(arguments)
    except ValueError:
        a = {}
    if not isinstance(a, dict):
        a = {}
    t = a.get("path") or a.get("command") or a.get("query") or a.get("url") or ("" if "items" in a else arguments)
    return " ".join(str(t).split())


def narration(text: str) -> str:
    """The model's own line about what it is doing (sent alongside its tool calls), after a neon ●:
    not `>_`, which is where you type, so the scrollback never mixes up who said what."""
    lines = text.strip().splitlines() or [""]
    return "\n".join(paint("● " if i == 0 else "  ", NEON, bold=True) + paint(ln, INK2) for i, ln in enumerate(lines))


def bell() -> None:
    if ON and BELL:
        sys.stdout.write("\a")
        sys.stdout.flush()


HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)")


def diff(text: str, max_lines: int = 40) -> str:
    """A unified diff in colour: added green, removed red, hunks muted, each line with its number in the
    file (the old file's for a removed line, the new file's otherwise); long ones are cut."""
    lines = text.splitlines()
    shown, old, new = [], 0, 0
    for ln in lines[:max_lines]:
        rgb = (INK2 if ln.startswith(("+++", "---")) else NEON if ln.startswith("+") else CRIT if ln.startswith("-")
               else MUTED if ln.startswith("@@") else INK2)
        num = ""
        if m := HUNK.match(ln):
            old, new = int(m.group(1)), int(m.group(2))
        elif not ln.startswith(("+++", "---", "\\")):
            num = str(old if ln.startswith("-") else new)
            old, new = old + (not ln.startswith("+")), new + (not ln.startswith("-"))
        shown.append(paint(f"{num:>5} ", MUTED) + paint(ln, rgb))
    if len(lines) > max_lines:
        shown.append("      " + paint(f"… {len(lines) - max_lines} more lines (/diff shows everything)", MUTED))
    return "\n".join(shown)


def call_line(name: str, arguments: str, result: str, width: int = 100, workspace: str = "",
              change: tuple | None = None, seconds: float = 0.0) -> str:
    """Collapsed mode: one line per tool call, after it ran. Workspace paths are shown relative; a shell
    command that exits 0 says only how much it printed; a call of a second or more says how long it took."""
    lines = result.splitlines()
    first = lines[0] if lines else ""
    bad = bool(re.match(r"(ERROR|DENIED|\[exit [1-9])", first))
    if change and not bad:  # write/edit: what changed, not "wrote 504 chars to ..."
        outcome = f"+{change[0]} −{change[1]}"
    elif bad or name not in ("run_shell", "read_file"):
        outcome = first
    else:
        n = len(lines) - (name == "run_shell")
        outcome = f"{n} line{'s' if n != 1 else ''}" if n or name != "run_shell" else "no output"
        if name == "run_shell" and not first.startswith("[exit 0]"):  # a job, a timeout: still worth reading
            outcome = first + " · " + outcome
    if seconds >= 1:
        outcome += f" · {seconds:.1f}s" if seconds < 10 else f" · {took(seconds)}"
    verb, tgt = VERBS.get(name, name), target(name, arguments)
    if workspace:
        tgt, outcome = (t.replace(workspace + "/", "").replace(workspace + os.sep, "").replace(workspace, ".")
                        for t in (tgt, outcome))
    room = max(20, width - len(verb) - 12 - min(len(outcome), 50))
    tgt = tgt if len(tgt) <= room else tgt[:room - 1] + "…"
    outcome = outcome if len(outcome) <= 50 else outcome[:49] + "…"
    return (paint("  ✗ " if bad else "  ✓ ", CRIT if bad else NEON, bold=True) + paint(f"{verb:<7}", TOOL, bold=True)
            + paint(tgt, INK2) + "  " + paint(outcome, CRIT if bad else MUTED))


def human(n: int) -> str:
    """Bytes as e.g. 512 MB / 3.4 GB."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit == "GB" else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n} GB"


def line(text: str) -> str:
    """Colour one agent status line by its prefix."""
    if text.startswith("-> "):
        name, _, args = text[3:].partition(" ")
        return paint("-> " + name, TOOL, bold=True) + " " + paint(args, MUTED)
    if text.startswith("   "):
        failed = re.match(r"(ERROR|DENIED|\[exit [1-9])", text.lstrip())
        return paint(text, CRIT if failed else INK2)
    if text.startswith(WARN):
        return paint(text, AMBER)
    if text.startswith(". "):
        return paint(text, MUTED)
    return text
