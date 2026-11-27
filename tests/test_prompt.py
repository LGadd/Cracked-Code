"""The prompt_toolkit prompt, driven with keys from a pipe instead of a keyboard."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.output import DummyOutput
    from cracked_code import prompt
except ImportError:  # prompt_toolkit not installed: `pip install -e .` brings it
    prompt = None

from cracked_code import repl, ui


@unittest.skipIf(prompt is None, "prompt_toolkit not installed")
class Prompt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "src").mkdir()
        (self.ws / "src" / "main.py").write_text("")
        self.guard = SimpleNamespace(assume_yes=False, investigate=False)
        hist = mock.patch.object(repl, "HISTORY", self.ws / "state" / "history")
        hist.start()
        self.addCleanup(hist.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def type(self, keys: str) -> str:
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):  # no console needed (Windows over ssh)
            inp.send_text(keys)
            return prompt.session(self.ws, self.guard, lambda: "status").prompt()

    def test_menu_lists_commands_with_descriptions(self):
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document
        got = list(prompt.Menu(self.ws).get_completions(Document("/cl"), CompleteEvent()))
        self.assertEqual([(c.text, c.start_position, c.display_meta_text) for c in got],
                         [("/clear", -3, repl.DESCRIBE["/clear"])])

    def test_backslash_enter_keeps_typing(self):
        self.assertEqual(self.type("one\\\rtwo\r"), "one\ntwo")

    def test_alt_enter_is_a_new_line(self):
        self.assertEqual(self.type("one\x1b\rtwo\r"), "one\ntwo")

    def test_ctrl_c_clears_the_line_then_quits(self):
        self.assertEqual(self.type("oops\x03hi\r"), "hi")
        with self.assertRaises(KeyboardInterrupt):
            self.type("\x03")

    def test_shift_tab_cycles_the_mode(self):
        with mock.patch.object(ui, "out"):
            self.assertEqual(self.type("\x1b[Zhi\r"), "hi")
        self.assertTrue(self.guard.assume_yes)


@unittest.skipIf(prompt is None, "prompt_toolkit not installed")
class HomeEnd(unittest.TestCase):
    """A 159-char line drawn 80 wide after a 3-char prompt: rows hold chars 0-76, 77-156 and 157-158."""

    def press(self, cursor, end):
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.document import Document
        text = " ".join(f"w{i:02d}" for i in range(40))
        b = Buffer(document=Document(text, cursor))
        spots = {(0, c): (0 if c < 77 else 1 if c < 157 else 2, 0) for c in range(len(text))}
        info = SimpleNamespace(_rowcol_to_yx=spots)
        e = SimpleNamespace(current_buffer=b, app=SimpleNamespace(layout=SimpleNamespace(current_window=SimpleNamespace(render_info=info))))
        prompt.screen_line(e, end)
        return b.cursor_position

    def test_home_and_end_stay_on_the_screen_row(self):
        self.assertEqual(self.press(100, end=False), 77)
        self.assertEqual(self.press(100, end=True), 156)   # before the row's last character, so it stays on the row
        self.assertEqual(self.press(159, end=False), 157)  # from the very end
        self.assertEqual(self.press(5, end=True), 76)
        self.assertEqual(self.press(158, end=True), 159)   # the last row ends at the end of the text
