"""The `>_` prompt, on prompt_toolkit (Linux, macOS and Windows): a menu of /commands and
@files (fuzzy, whole repo) as you type, /commands and @files coloured, grey suggestions from
history (Right takes one), Ctrl+R, whole pastes, Alt+Enter for a new line, Ctrl+X Ctrl+E for
$EDITOR, Ctrl+C to clear the line, a status bar at the bottom, Shift+Tab for the mode and
Ctrl+O for details. Only used in a colour terminal with a keyboard; pipes and
the dashboard's pty keep plain input(). What to offer and colour is in repl.suggest and repl.marks.
"""

from __future__ import annotations

import shutil
from prompt_toolkit import PromptSession
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, Layout, Window
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style

from . import repl, ui


def _hex(rgb: str) -> str:
    return "#" + "".join(f"{int(v):02x}" for v in rgb.split(";"))


def style() -> Style:
    """Built when the prompt starts, after config theme has set the colours."""
    bg = "bg:#0d1f15" if ui.THEME == "dark" else "bg:#e3efe7"
    return Style.from_dict({
        "cmd": f"{_hex(ui.NEON)} bold", "bad": _hex(ui.CRIT),
        "file": f"{_hex(ui.TOOL)} underline", "missing": _hex(ui.AMBER),
        "bottom-toolbar": f"noreverse {bg}",
        "completion-menu.completion": f"{bg} {_hex(ui.INK2)}",
        "completion-menu.completion.current": f"bg:{_hex(ui.MUTED)} #ffffff bold",
        "completion-menu.meta.completion": f"{bg} {_hex(ui.MUTED)}",
        "completion-menu.meta.completion.current": f"bg:{_hex(ui.MUTED)} #ffffff",
    })


class Menu(Completer):
    def __init__(self, ws):
        self.ws = ws

    def get_completions(self, document, event):
        for text, start, meta in repl.suggest(document.text_before_cursor, self.ws, event.completion_requested):
            yield Completion(text, start, display_meta=meta)


class Colour(Lexer):
    def __init__(self, ws):
        self.ws = ws

    def lex_document(self, document):
        lines = document.lines
        return lambda i: [(f"class:{k}" if k else "", t) for k, t in repl.marks(lines[i], self.ws, i == 0)]


def history():
    """Same history as the readline prompt had: its file is copied over once."""
    new = repl.HISTORY.with_name("prompt-history")
    if not new.exists() and repl.HISTORY.exists():
        try:
            old = repl.HISTORY.read_text(errors="ignore", encoding="utf-8").splitlines()
            new.write_text("".join(f"\n+{ln}\n" for ln in old if ln.strip()), encoding="utf-8")
        except OSError:
            pass
    new.parent.mkdir(parents=True, exist_ok=True)
    return FileHistory(str(new))


def screen_line(e, end: bool) -> None:
    """Home/End go to the start/end of the row on screen, not of the whole wrapped line (the prompt)."""
    b, info = e.current_buffer, e.app.layout.current_window.render_info
    doc = b.document
    row, col = doc.cursor_position_row, doc.cursor_position_col
    text = doc.lines[row]
    spots = info._rowcol_to_yx if info else {}  # (row, col) of each character drawn -> (y, x) on screen
    y = (spots.get((row, col)) or spots.get((row, col - 1)) or (None,))[0]
    cols = sorted(c for (r, c), (yy, _) in spots.items() if r == row and yy == y and c < len(text))
    if not cols:  # nothing drawn to go by (empty line, not rendered yet): the whole line
        new = len(text) if end else 0
    elif end:
        new = len(text) if cols[-1] == len(text) - 1 else cols[-1]  # a wrapped row ends before its last character
    else:
        new = cols[0]
    b.cursor_position += new - col


def session(ws, guard, status) -> PromptSession:
    """The REPL's prompt. `status()` is the bar under it, redrawn as you type."""
    kb = KeyBindings()

    @kb.add("s-tab", filter=~has_completions)  # with the menu open, Shift+Tab goes back up it
    def _mode(_e):
        line = ui.next_mode(guard)
        run_in_terminal(lambda: ui.out(line))

    @kb.add("c-o")
    def _details(_e):
        ui.EXPANDED = not ui.EXPANDED
        run_in_terminal(ui.redraw)

    @kb.add("c-c")
    def _clear(e):  # a line to clear: clear it; an empty one: quit, as before
        if e.current_buffer.text:
            e.current_buffer.reset()
        else:
            e.app.exit(exception=KeyboardInterrupt)

    @kb.add("home")
    def _home(e):
        screen_line(e, end=False)

    @kb.add("end")
    def _end(e):
        screen_line(e, end=True)

    @kb.add("escape", "enter")  # Alt+Enter: a new line (terminals can't tell Shift+Enter apart)
    def _newline(e):
        e.current_buffer.insert_text("\n")

    @kb.add("enter")
    def _send(e):
        b = e.current_buffer
        if b.document.text_before_cursor.endswith("\\"):  # a line ending in \ keeps typing
            b.delete_before_cursor()
            b.insert_text("\n")
            return
        st = b.complete_state
        if st and b.text.startswith("/") and " " not in b.text:  # /cl + Enter runs /clear
            pick = st.current_completion or (st.completions[0] if len(st.completions) == 1 else None)
            if pick:
                b.apply_completion(pick)
        b.validate_and_handle()

    return PromptSession(
        # a rule above the input, as mid-task (live.Live._box), so the prompt reads as its own box in both states
        lambda: ANSI(ui.paint("─" * (shutil.get_terminal_size((100, 20)).columns - 1), ui.MUTED) + "\n"
                     + ui.mode(guard, prompt=False) + ui.paint(ui.PROMPT, ui.NEON, bold=True)),
        bottom_toolbar=lambda: ANSI(status()), auto_suggest=AutoSuggestFromHistory(),
        enable_open_in_editor=True, history=history(), completer=Menu(ws), complete_while_typing=True,
        lexer=Colour(ws), style=style(), key_bindings=kb, reserve_space_for_menu=8)


def ask(text: str) -> str:
    """ui.harness_input's prompt (approvals, the stuck prompt): Shift+Tab and Ctrl+O answer
    /mode and /expand, which harness_input handles and never sends to the model."""
    kb = KeyBindings()
    kb.add("s-tab")(lambda e: e.app.exit(result="/mode"))
    kb.add("c-o")(lambda e: e.app.exit(result="/expand"))
    return pt_prompt(ANSI(text.replace("\001", "").replace("\002", "")), key_bindings=kb)


def choose(question: str, options: list[tuple[str, str]], start: int = 0, tag: str = "") -> str:
    """ui.choose's menu: Up/Down and Enter, or press an option's key. Esc and Ctrl+C pick the last option (no).
    Shift+Tab and Ctrl+O answer /mode and /expand, as at ask()."""
    at = [start]
    kb = KeyBindings()
    kb.add("s-tab")(lambda e: e.app.exit(result="/mode"))
    kb.add("c-o")(lambda e: e.app.exit(result="/expand"))
    kb.add("up")(lambda e: at.__setitem__(0, (at[0] - 1) % len(options)))
    kb.add("down")(lambda e: at.__setitem__(0, (at[0] + 1) % len(options)))
    kb.add("enter")(lambda e: e.app.exit(result=options[at[0]][0]))
    for key in ("escape", "c-c"):
        kb.add(key)(lambda e: e.app.exit(result=options[-1][0]))
    for k, _ in options:
        kb.add(k)(lambda e, k=k: e.app.exit(result=k))

    def lines():
        rows = [("bold reverse " + _hex(ui.AMBER), f" {tag} "), ("", " ")] if tag else []
        rows.append(("bold " + _hex(ui.NEON), question + "\n"))
        for n, (k, label) in enumerate(options):
            here = n == at[0]
            rows.append((f"bg:{_hex(ui.MUTED)} #ffffff bold" if here else _hex(ui.INK2),
                         f"{'❯' if here else ' '} {k}  {label}"))
            rows.append(("", "\n"))
        rows.append((_hex(ui.MUTED), "↑/↓ and Enter, or press the letter"))
        return rows

    control = FormattedTextControl(lines, show_cursor=False)
    return Application(layout=Layout(Window(control, height=len(options) + 2)), key_bindings=kb).run()
