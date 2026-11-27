"""The terminal while a task runs: one spinner line saying what the model is doing, and under it
your own input box between two rules, so what you type never shares a line with the status.
The REPL's keys (Ctrl+O, Shift+Tab) work mid-task. Enter queues what you typed for the model:
it reads it before its next step (agent.run drains `queue`). Colour terminals only (ui.ON):
pipes keep the plain lines.

Costs no context: everything shown comes from the stream and the tool calls already made.
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import shutil
import signal
import sys
import threading
import time

try:
    import termios
    import tty
except ImportError:  # Windows: msvcrt reads keys one by one, the console needs no cbreak
    import _thread
    import msvcrt
    termios = tty = None

from . import tools, ui

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
CTRL_B, CTRL_O, CTRL_U, SHIFT_TAB, ESC, BACKSPACE = b"\x02", b"\x0f", b"\x15", b"\x1b[Z", b"\x1b", (b"\x7f", b"\x08")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
PROMPT = ui.PROMPT
INPUT_ROWS = 6  # most rows the input box shows; a longer draft shows its end
HIDE, SHOW = "\033[?25l", "\033[?25h"  # the real cursor: the box draws its own caret


def width() -> int:
    return shutil.get_terminal_size((100, 20)).columns - 1


class Live:
    def __init__(self, guard, out=sys.stdout, hold: bool = False):
        self.guard, self.out = guard, out
        self.hold = hold    # a task: the caller prints `summary` after the answer, as the rule that closes the task
        self.summary = ""
        self.files: dict[str, list[int]] = {}  # path -> [added, removed] lines this task, for the summary
        self.titled = ""    # the tab title now, so it is only rewritten when it changes
        self.waited = 0.0   # seconds spent paused on a question: not part of a tool call's time
        self.phase, self.detail = "reading", ""
        self.turn = self.calls = self.tokens = 0
        self.ctx = None
        self.rline = ""     # reasoning not yet printed as a line
        self.rtext = ""     # this reply's reasoning so far (tail), for the collapsed sentence
        self.streamed = False  # reasoning arrived live this reply: the loop must not print it again
        self.typed = ""     # typed ahead mid-task: becomes the next prompt's draft
        self.queue: list[str] = []  # Enter pressed mid-task: the model reads these before its next step
        self.t0 = time.monotonic()
        self.lock = threading.RLock()       # one writer at a time: printed lines and the spinner
        self.reading = threading.Lock()     # held while the key thread may read stdin
        self.stop = threading.Event()
        self.paused = False
        self.frame = 0
        self.drawn = 0  # rows of the footer (status + input box) on screen now; the cursor sits at the end of the last

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self):
        self.fd = sys.stdin.fileno()
        if termios:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)  # keys arrive one by one, no echo; Ctrl+C still interrupts
        ui.LIVE = self
        self.out.write(HIDE)
        self.threads = [threading.Thread(target=f, daemon=True) for f in (self._spin, self._keys)]
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc):
        self.flush_reasoning()
        self.stop.set()
        for t in self.threads:
            t.join()
        ui.LIVE = None
        if termios:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        bits = [ui.took(time.monotonic() - self.t0), f"{self.turn} turn{'s' if self.turn != 1 else ''}",
                f"{self.calls} tool call{'s' if self.calls != 1 else ''}"]
        if self.ctx is not None:
            bits.append(f"ctx {self.ctx}%")
        if self.files:
            n = len(self.files)
            bits.append(f"{n} file{'s' if n != 1 else ''} +{sum(a for a, _ in self.files.values())} "
                        f"−{sum(d for _, d in self.files.values())}")
        self.out.write(self._erase() + SHOW)
        self._title("✗ stopped" if exc[0] else "✓ done")
        self.summary = ui.rule(("interrupted · " if exc[0] else "done · ") + " · ".join(bits))
        if not self.hold:
            ui.out(self.summary)
        return False

    def changed(self, path: str, added: int, removed: int) -> None:
        """A write or edit landed: counted for the summary."""
        a = self.files.setdefault(path, [0, 0])
        a[0], a[1] = a[0] + added, a[1] + removed

    def _title(self, state: str) -> None:
        text = f"{state} · cracked" + (f" · {ui.WHERE}" if ui.WHERE else "")
        if text != self.titled:
            self.titled = text
            self.out.write(f"\033]0;{text}\007")

    # ------------------------------------------------------------------ what it says
    def set(self, phase: str, detail: str = "", **kw) -> None:
        with self.lock:
            if phase != "thinking":
                self.flush_reasoning()
            self.phase, self.detail = phase, detail
            for k, v in kw.items():
                setattr(self, k, v)
            if phase == "reading":
                self.tokens, self.rtext, self.streamed = 0, "", False

    def delta(self, kind: str, text: str) -> None:
        with self.lock:
            self.tokens += 1  # one streamed chunk is about one token
            if kind == "reasoning":
                self.phase, self.streamed = "thinking", True
                self.rtext = (self.rtext + text)[-2000:]
                self.rline += text
                self._reasoning_lines()
                return
            self.flush_reasoning()
            if kind == "content":
                self.phase, self.detail = "writing", ""
            else:
                self.phase, self.detail = "calling", ui.VERBS.get(text, text)

    def _reasoning_lines(self) -> None:
        """Whole lines of reasoning, wrapped to the terminal: shown expanded, kept for Ctrl+O."""
        room = width() - 4
        while True:
            nl = self.rline.find("\n")
            if 0 <= nl <= room:
                cut, skip = nl, 1
            elif len(self.rline) > room:
                cut = self.rline.rfind(" ", 0, room)
                cut, skip = (cut, 1) if cut > room // 2 else (room, 0)
            else:
                return
            self._reason(self.rline[:cut])
            self.rline = self.rline[cut + skip:]

    def flush_reasoning(self) -> None:
        with self.lock:
            if self.rline.strip():
                self._reason(self.rline)
            self.rline = ""

    def _reason(self, text: str) -> None:
        if text.strip():
            ui.out(ui.thinking(text), "detail")

    def sentence(self) -> str:
        """Collapsed view: the last finished sentence of the reasoning, so the line changes once
        per sentence instead of sliding with every token."""
        parts = [p for p in SENTENCE_END.split(" ".join(self.rtext.split())) if p]
        if not parts:
            return ""
        if parts[-1][-1:] in ".!?":
            return parts[-1]
        return parts[-2] if len(parts) > 1 else ""

    def print(self, text: str) -> None:
        with self.lock:
            self.out.write(self._erase() + text + "\n")
            self._draw()

    def redraw(self) -> None:
        with self.lock:
            ui.redraw()
            self.drawn = 0  # the screen was cleared
            self._draw()

    def _erase(self) -> str:
        """Back to the footer's first row, clearing it and everything under it."""
        up, self.drawn = self.drawn - 1, 0
        return "\r" + (f"\033[{up}A" if up > 0 else "") + "\033[J"

    def _box(self, w: int) -> list[str]:
        """The input box: a rule, what you have typed (wrapped, with a caret), a rule."""
        rule = ui.paint("─" * w, ui.MUTED)
        room = max(2, w - len(PROMPT) - 1)  # -1: the caret
        rows, rest = [], self.typed
        while rest:  # by columns, so wide characters (CJK, emoji) never push a row past the edge
            row = ui.cut(rest, room) or rest[0]
            rows.append(row)
            rest = rest[len(row):]
        if not rows or ui.cells(rows[-1]) == room:
            rows.append("")  # the caret wraps to a row of its own
        rows = rows[-INPUT_ROWS:]
        hint = "" if self.typed else ui.paint(" type to steer the model, Enter sends it", ui.MUTED)
        body = [ui.paint(PROMPT if i == 0 else " " * len(PROMPT), ui.NEON, bold=True) + r
                + (ui.paint("▌", ui.NEON) + hint if i == len(rows) - 1 else "") for i, r in enumerate(rows)]
        return [rule, *body, rule]

    def _draw(self) -> None:
        if self.paused:
            return
        # two rows: what the model is doing (its text changes length all the time), then the numbers and keys on a
        # row of their own, numbers first in a fixed order, so they never move sideways with the text above
        w = width()
        head, phase = FRAMES[self.frame % len(FRAMES)] + " ", self.phase + " "
        detail = self.detail
        if self.phase == "thinking":  # expanded: the reasoning prints above as lines
            detail = "" if ui.EXPANDED else self.sentence()
        room = w - len(head) - len(phase)
        if ui.cells(detail) > room:  # cut the end, never slide the start: the line stays readable
            detail = ui.cut(detail, room - 1) + "…" if room > 1 else ""
        # the phase in its own colour, so a glance says which: thinking dim, writing neon, a tool pink, upkeep amber
        rgb = (ui.INK2 if self.phase in ("thinking", "reading") else ui.NEON if self.phase in ("writing", "working")
               else ui.AMBER if self.phase in ("compacting", "reporting") else ui.TOOL)
        status = ui.paint(head, rgb, bold=True) + ui.paint(phase, rgb, bold=True) + ui.paint(detail, ui.INK2)
        self._title(f"● {self.phase}")
        info = [ui.took(time.monotonic() - self.t0), f"{self.tokens} tok"]  # tok stays between requests: no jumping
        if self.ctx is not None:
            info.append(f"ctx {self.ctx}%")
        if ui.PLAN:
            info.append(ui.PLAN)
        info.append("^O less" if ui.EXPANDED else "^O details")
        if tools.foreground():
            info.append("^B background")
        info.append("esc stop")
        if self.queue:
            info.append(f"queued {len(self.queue)}")
        mode = ui.mode(self.guard, prompt=False)
        tail = ui.cut(" · ".join(info), max(0, w - ui.cells(ui.CODES.sub("", mode))))  # narrow: cut, never wrap
        numbers = mode + ui.paint(tail, ui.MUTED)
        # shortcut: a resize mid-task can leave a stray row until the next printed line; redraw on SIGWINCH if it bites
        rows = [status, numbers, *self._box(w)]
        self.out.write(self._erase() + "\n".join(rows))
        self.drawn = len(rows)
        self.out.flush()

    def _spin(self) -> None:
        while not self.stop.wait(0.1):
            with self.lock:
                self.frame += 1
                self._draw()

    # ------------------------------------------------------------------ keys
    def _keys(self) -> None:
        while not self.stop.is_set():
            if self.paused:  # input() owns stdin
                time.sleep(0.05)
                continue
            with self.reading:
                if termios is None:
                    data = self._win_key()
                    if data is None:
                        continue
                elif self.paused or not select.select([self.fd], [], [], 0.1)[0]:
                    continue
                else:
                    data = os.read(self.fd, 64)
            self._key(data)

    def _win_key(self) -> bytes | None:
        """One Windows console key as the bytes a POSIX terminal sends (Shift+Tab is NUL + 0x0f there)."""
        if self.paused or not msvcrt.kbhit():
            time.sleep(0.05)
            return None
        ch = msvcrt.getwch()
        if ch in "\x00\xe0":  # arrows, F-keys, Shift+Tab: a prefix and a scan code
            return SHIFT_TAB if msvcrt.getwch() == "\x0f" else b"\x1b["
        return ch.encode(errors="ignore")

    def _key(self, data: bytes) -> None:
        if data == CTRL_B:  # run_shell sees it within 0.1 s, returns, and the loop hands the turn back
            job = tools.foreground()
            if job:
                tools.DETACH.set()
            ui.out(ui.paint(f"  job {job.n} goes to the background: /jobs lists it" if job else
                            "  Ctrl+B moves a running shell command to the background; none is running", ui.INK2))
        elif data == CTRL_O:  # replays the whole session in the other view, not just what comes next
            with self.lock:
                ui.EXPANDED = not ui.EXPANDED
                self.redraw()
        elif data == SHIFT_TAB:  # into investigate mode mid-task: the loop adds its rule before the next turn
            ui.out(ui.next_mode(self.guard))
        elif data == ESC:  # stop the task like Ctrl+C; the loop repairs the history and the session carries on
            if termios:
                os.kill(os.getpid(), signal.SIGINT)
            else:  # os.kill(SIGINT) on Windows terminates the process outright
                _thread.interrupt_main()
        elif data in BACKSPACE:
            self.typed = self.typed[:-1]
        elif data == CTRL_U:
            self.typed = ""
        elif not data.startswith(b"\x1b"):  # arrows and other escape sequences are ignored
            for ch in data.decode(errors="ignore"):
                if ch in "\r\n":
                    self.steer()
                elif ch.isprintable():
                    self.typed += ch
        else:
            return
        with self.lock:  # show the keystroke now, not on the next spinner tick
            self._draw()

    def steer(self) -> None:
        """Enter mid-task: queue the typed line; the model reads it before its next step."""
        if self.typed.strip().split()[:1] == ["/jobs"]:  # answered now, not queued: a stuck command blocks the queue
            ui.out(ui.paint(tools.jobs_command(self.typed.strip()[5:]), ui.INK2))
        elif self.typed.strip():
            self.queue.append(self.typed.strip())
            ui.out(ui.paint(f"  queued for the model: {self.typed.strip()}", ui.INK2))
        self.typed = ""

    @contextlib.contextmanager
    def pause(self):
        """input() owns the terminal: line mode, echo, no spinner, no key thread."""
        with self.lock:
            self.paused = True
            self._title("⏸ waiting for you")
            self.out.write(self._erase() + SHOW)
            self.out.flush()
        with self.reading:  # the key thread is out of select/read once we hold this
            if termios:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.waited += time.monotonic() - t0
            if termios:
                tty.setcbreak(self.fd)
            with self.lock:
                self.out.write(HIDE)
                self.paused = False
