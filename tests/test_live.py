"""Streaming replies, the compact tool line, and the live terminal view."""

import io
import json
import re
import urllib.error
import unittest
from types import SimpleNamespace
from unittest import mock

from cracked_code import live, llm, tools, ui
from tests import stubserver as stub


class Streaming(unittest.TestCase):
    def ask(self, stream_reply: bool, reply: dict):
        st = stub.Stub([reply])
        st.stream = stream_reply
        srv, port, _ = stub.serve(st)
        seen = []
        try:
            model = llm.LLM(f"http://127.0.0.1:{port}/v1", "m", on_delta=lambda k, t: seen.append(k))
            out = model.chat([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "x"}}])
        finally:
            srv.shutdown()
            srv.server_close()
        return out, seen, st.requests[0]

    def test_stream_rebuilds_the_reply(self):
        r = stub.reply(text="two words", calls=[("c1", "run_shell", {"command": "ls -la /tmp"})],
                       finish="tool_calls", in_tokens=321)
        r["reasoning"] = "look first"
        out, seen, sent = self.ask(True, r)
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        self.assertEqual((out.text, out.reasoning, out.finish, out.prompt_tokens), ("twowords", "lookfirst", "tool_calls", 321))
        self.assertEqual([(c.id, c.name, c.arguments) for c in out.calls],
                         [("c1", "run_shell", '{"command": "ls -la /tmp"}')])
        self.assertEqual(seen[:2], ["reasoning", "reasoning"])
        self.assertIn("tool", seen)

    def test_server_that_ignores_stream_still_works(self):
        out, seen, _ = self.ask(False, stub.reply(text="plain"))
        self.assertEqual((out.text, seen), ("plain", []))

    def test_sampling_fields_sent_only_when_set(self):
        def sent(**kw):
            st = stub.Stub([stub.reply(text="ok")])
            srv, port, _ = stub.serve(st)
            try:
                llm.LLM(f"http://127.0.0.1:{port}/v1", "m", **kw).chat([{"role": "user", "content": "hi"}], [])
            finally:
                srv.shutdown()
                srv.server_close()
            return st.requests[0]
        default = sent()
        self.assertEqual(default["temperature"], 0.2)  # unchanged for every model that sets nothing
        self.assertNotIn("reasoning_effort", default)
        self.assertNotIn("reasoning_budget_tokens", default)
        tuned = sent(temperature=0.0, reasoning_effort="low", reasoning_budget=2048)
        self.assertEqual((tuned["temperature"], tuned["reasoning_effort"], tuned["reasoning_budget_tokens"]), (0.0, "low", 2048))
        self.assertNotIn("presence_penalty", tuned)  # sampling beyond temperature is the server's


class ServerErrors(unittest.TestCase):
    def chat(self, *responses):
        """Each response is an exception to raise or a body to return."""
        answers = list(responses)

        def urlopen(req, timeout=None):
            r = answers.pop(0)
            if isinstance(r, Exception):
                raise r
            return io.BytesIO(json.dumps(r).encode())
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch.object(llm, "RETRY_WAIT", 0):
            return llm.LLM("http://x/v1", "m").chat([], []), answers

    def http(self, code, body):
        return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))

    def test_error_body_is_shown_not_cannot_reach(self):
        with self.assertRaises(llm.ServerError) as e:
            self.chat(self.http(500, {"error": {"message": "context length exceeded"}}))
        self.assertIn("context length exceeded", str(e.exception))
        self.assertEqual(llm.failure("http://x/v1", e.exception), str(e.exception))
        self.assertIn("cannot reach", llm.failure("http://x/v1", ConnectionRefusedError("refused")))

    def test_rejected_thinking_level_is_dropped_and_the_request_sent_again(self):
        ok = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        sent = []

        def urlopen(req, timeout=None):
            sent.append(json.loads(req.data))
            if len(sent) == 1:
                raise self.http(400, {"error": {"message": 'Invalid reasoning effort: medium, should be one of "low", "high", "max"'}})
            return io.BytesIO(json.dumps(ok).encode())
        model = llm.LLM("http://x/v1", "m", reasoning_effort="medium")
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(model.chat([], []).text, "hi")
        self.assertEqual(sent[0]["reasoning_effort"], "medium")
        self.assertNotIn("reasoning_effort", sent[1])
        self.assertEqual(model.reasoning_effort, "")

    def test_rejected_thinking_budget_is_dropped_and_the_key_is_sent(self):
        ok = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        sent = []

        def urlopen(req, timeout=None):
            sent.append((json.loads(req.data), req.get_header("Authorization")))
            if len(sent) == 1:  # NVIDIA's API, 2026-11-13
                raise self.http(400, {"message": "Validation: Unsupported parameter(s): `reasoning_budget_tokens`"})
            return io.BytesIO(json.dumps(ok).encode())
        model = llm.LLM("http://x/v1", "m", reasoning_budget=2048, api_key="k")
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(model.chat([], []).text, "hi")
            model.reasoning_budget = 384  # auto-think sets it every turn: it must stay unsent
            model.chat([], [])
        self.assertEqual(sent[0][0]["reasoning_budget_tokens"], 2048)
        self.assertNotIn("reasoning_budget_tokens", sent[1][0])
        self.assertNotIn("reasoning_budget_tokens", sent[2][0])
        self.assertEqual({a for _, a in sent}, {"Bearer k"})
        with mock.patch("urllib.request.urlopen", urlopen):
            llm.LLM("http://x/v1", "m").chat([], [])  # no key: no header
        self.assertIsNone(sent[3][1])

    def test_busy_server_and_dropped_connection_get_one_retry(self):
        ok = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        self.assertEqual(self.chat(self.http(503, {"error": "busy"}), ok)[0].text, "hi")
        self.assertEqual(self.chat(ConnectionResetError("reset"), ok)[0].text, "hi")
        with self.assertRaises(llm.ServerError):
            self.chat(self.http(503, {"error": "busy"}), self.http(503, {"error": "busy"}))

    def test_error_in_the_stream_is_raised(self):
        model = llm.LLM("http://x/v1", "m")
        with self.assertRaises(llm.ServerError) as e:
            model._read_stream([b'data: {"error": {"message": "engine crashed"}}\n'])
        self.assertIn("engine crashed", str(e.exception))
        with self.assertRaises(llm.ServerError):
            self.chat({"object": "error"})  # no choices at all

    def test_a_stream_that_repeats_itself_is_cut(self):
        model = llm.LLM("http://x/v1", "m")
        block = "I should check the file again before I change anything in it. "  # 62 chars
        chunk = lambda kind, s: ("data: " + json.dumps({"choices": [{"delta": {kind: s}}]}) + "\n").encode()  # noqa: E731
        stream = iter([chunk("reasoning_content", block)] * 500)
        data = model._read_stream(stream)
        self.assertEqual(data["choices"][0]["finish_reason"], "repeat")
        self.assertGreater(len(list(stream)), 480)  # stopped after about 10 blocks, not at the end
        prose = [chunk("content", f"Step {i}: edit file number {i} and run its test.\n") for i in range(200)]
        self.assertEqual(model._read_stream(iter(prose))["choices"][0]["finish_reason"], "")  # similar lines, not a loop
        self.assertFalse(llm.repeating(("=" * 79 + "\n") * 30))  # a ruler is not a loop


class CallLine(unittest.TestCase):
    def test_shell_ok_and_failed(self):
        ok = ui.call_line("run_shell", '{"command": "ls\\n -la"}', "[exit 0]\na\nb")
        self.assertIn("shell", ok)
        self.assertIn("ls -la", ok)
        self.assertIn("ls -la  2 lines", ok)  # exit 0 is not repeated on every line
        self.assertNotIn("[exit 0]", ok)
        self.assertIn("no output", ui.call_line("run_shell", '{"command": "true"}', "[exit 0] (no output)"))
        self.assertIn("[exit 1]", ui.call_line("run_shell", '{"command": "x"}', "[exit 1]\nboom"))
        bad = ui.call_line("edit_file", '{"path": "x.py"}', "ERROR: old_string not found")
        self.assertIn("✗", bad)
        self.assertIn("ERROR: old_string", bad)

    def test_slow_calls_say_how_long(self):
        self.assertNotIn("s", ui.call_line("read_file", '{"path": "a"}', "x", seconds=0.2).split("1 line")[1])
        self.assertIn("1 line · 3.1s", ui.call_line("read_file", '{"path": "a"}', "x", seconds=3.14))
        self.assertIn("· 1m 5s", ui.call_line("run_shell", '{"command": "make"}', "[exit 0]\nok", seconds=65))

    def test_workspace_paths_are_relative(self):
        line = ui.call_line("run_shell", '{"command": "cd /w/p && ls /w/p/src"}', "[exit 0]", workspace="/w/p")
        self.assertIn("cd . && ls src", line)

    def test_long_target_is_cut_to_width(self):
        line = ui.call_line("run_shell", '{"command": "%s"}' % ("x" * 500), "[exit 0]", width=80)
        self.assertLess(len(line), 200)  # with colour codes; the visible text fits 80


class LiveView(unittest.TestCase):
    def setUp(self):
        self.guard = SimpleNamespace(assume_yes=False, investigate=False)
        self.out = io.StringIO()
        self.v = live.Live(self.guard, self.out)
        self.lines = []
        self._out = mock.patch.object(ui, "out", lambda text, kind="all": self.lines.append((kind, text)))
        self._out.start()
        self.expanded = ui.EXPANDED

    def tearDown(self):
        self._out.stop()
        ui.EXPANDED = self.expanded

    def test_ctrl_b_and_jobs_mid_task(self):
        job = SimpleNamespace(n=3)
        with mock.patch.object(tools, "foreground", return_value=job):
            self.v._draw()
            self.assertIn("^B background", self.out.getvalue())
            self.v._key(live.CTRL_B)
        self.assertTrue(tools.DETACH.is_set())
        tools.DETACH.clear()
        self.assertIn("job 3 goes to the background", self.lines[-1][1])
        with mock.patch.object(tools, "foreground", return_value=None):
            self.v._key(live.CTRL_B)
        self.assertFalse(tools.DETACH.is_set())  # nothing running: nothing to detach
        for ch in "/jobs\r":
            self.v._key(ch.encode())
        self.assertEqual(self.v.queue, [])  # answered now, never sent to the model
        self.assertIn("no shell commands running", self.lines[-1][1])

    def test_stream_sets_phase(self):
        self.v.delta("reasoning", "check the config")
        self.assertEqual(self.v.phase, "thinking")
        self.v.delta("tool", "run_shell")
        self.assertEqual((self.v.phase, self.v.detail), ("calling", "shell"))
        self.v._draw()
        self.assertIn("calling", self.out.getvalue())

    def test_reasoning_becomes_wrapped_detail_lines(self):
        with mock.patch.object(live, "width", return_value=40):
            for word in ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu".split()):
                self.v.delta("reasoning", word + " ")
            self.v.delta("reasoning", "end\nnext")
            self.v.flush_reasoning()
        kinds = {k for k, _ in self.lines}
        texts = [t for _, t in self.lines]
        self.assertEqual(kinds, {"detail"})
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(t) < 40 + 40 for t in texts))  # wrapped (colour codes add a little)
        self.assertIn("next", texts[-1])

    def test_collapsed_sentence_changes_per_sentence_not_per_token(self):
        self.v.delta("reasoning", "First I read the file. Then I")
        self.assertEqual(self.v.sentence(), "First I read the file.")
        self.v.delta("reasoning", " check the tests")
        self.assertEqual(self.v.sentence(), "First I read the file.")
        self.v.delta("reasoning", ". Done")
        self.assertEqual(self.v.sentence(), "Then I check the tests.")

    def test_keys_mid_task(self):
        ui.EXPANDED = False
        with mock.patch.object(ui, "redraw") as redraw:
            self.v._key(live.CTRL_O)
            self.assertTrue(ui.EXPANDED)
            self.v._key(live.CTRL_O)
            self.assertFalse(ui.EXPANDED)  # collapses again
            self.assertEqual(redraw.call_count, 2)  # each press replays the whole session
        self.v._key(live.SHIFT_TAB)
        self.assertTrue(self.guard.assume_yes)
        self.v._key(live.SHIFT_TAB)
        self.assertEqual((self.guard.assume_yes, self.guard.investigate), (False, True))
        self.assertIn("INVESTIGATE mode on", self.lines[-1][1])
        self.v._draw()
        self.assertIn("[INVESTIGATE]", self.out.getvalue())
        self.v._key(live.SHIFT_TAB)
        self.assertEqual((self.guard.assume_yes, self.guard.investigate), (False, False))
        for k in (b"f", b"i", b"x", b"\x7f", b"\x1b[A"):
            self.v._key(k)
        self.assertEqual(self.v.typed, "fi")
        self.v._key(b"\r")  # Enter: queued for the model, the line is free again
        self.assertEqual((self.v.queue, self.v.typed), (["fi"], ""))
        self.v._key(b"\r")  # nothing typed: nothing queued
        self.v._key(b"use tabs\rand")  # a paste with a newline in it
        self.assertEqual((self.v.queue, self.v.typed), (["fi", "use tabs"], "and"))
        self.assertEqual(ui.steered(), [])  # no live line in the tests: nothing to drain


def screen(raw: str) -> list[str]:
    """What a terminal shows after `raw`: enough of one for the footer (\\r, \\n, cursor up, clear below; colours dropped)."""
    rows, y, x = [""], 0, 0
    for tok in re.findall(r"\x1b\[\??[0-9;]*[A-Za-z]|\r|\n|[^\x1b\r\n]+", raw):
        if tok == "\r":
            x = 0
        elif tok == "\n":
            y, x = y + 1, 0
            rows += [""] * (y + 1 - len(rows))
        elif tok.startswith("\x1b["):
            if tok.endswith("A"):
                y -= int(tok[2:-1] or 1)
            elif tok.endswith("J"):
                rows[y] = rows[y][:x]
                del rows[y + 1:]
        else:
            rows[y] = rows[y][:x].ljust(x) + tok + rows[y][x + len(tok):]
            x += len(tok)
    return rows


class InputBox(unittest.TestCase):
    """2026-11-10: typing mid-task has its own box under the status line, so the status can't move your text."""
    def setUp(self):
        self.out = io.StringIO()
        self.v = live.Live(SimpleNamespace(assume_yes=False, investigate=False), self.out)
        self._w = mock.patch.object(live, "width", return_value=30)
        self._w.start()

    def tearDown(self):
        self._w.stop()

    def test_status_then_typed_text_between_two_rules(self):
        self.v._draw()
        self.v.print("-> read_file a.py")           # a finished line goes above the footer
        for k in (b"f", b"i", b"x"):
            self.v._key(k)
        self.v.set("thinking")                       # the status changes; the box must not move
        self.v._draw()
        rows = screen(self.out.getvalue())
        self.assertEqual(rows[0], "-> read_file a.py")
        self.assertTrue(rows[1].lstrip("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ ").startswith("thinking"))
        self.assertRegex(rows[2], r"^\d+s · 0 tok · ")  # the numbers have a row of their own
        self.assertEqual(rows[3:], ["─" * 30, ">_ fix▌", "─" * 30])
        self.assertNotIn("fix", rows[1])

    def test_numbers_row_stays_put_while_the_text_above_changes(self):
        seen = set()
        for detail in ("a", "a much longer line that fills the whole row and more", ""):
            self.v.set("reading", detail)
            self.v._draw()
            seen.add(screen(self.out.getvalue())[1].split("s · ", 1)[1])  # minus the clock
        self.assertEqual(len(seen), 1)

    def test_long_draft_wraps_inside_the_box_and_enter_empties_it(self):
        self.v._key(b"a" * 50)
        rows = screen(self.out.getvalue())
        self.assertEqual(rows[3:5], [">_ " + "a" * 26, "   " + "a" * 24 + "▌"])
        self.assertTrue(all(len(r) <= 30 for r in rows))
        with mock.patch.object(ui, "out"):
            self.v._key(b"\r")
        self.assertEqual(self.v.queue, ["a" * 50])
        self.assertIn(">_ ▌", screen(self.out.getvalue())[-2])  # back to one empty row


class Summary(unittest.TestCase):
    """2026-11-27: the done line closes a task (a rule under the answer) and counts the files changed."""
    def test_held_summary_counts_files_and_is_not_printed(self):
        out = io.StringIO()
        v = live.Live(SimpleNamespace(assume_yes=False, investigate=False), out, hold=True)
        v.threads = []  # not entered: no key thread reading the test's stdin
        v.changed("a.py", 3, 1)
        v.changed("a.py", 2, 0)
        v.changed("b.py", 1, 4)
        with mock.patch.object(live, "termios", None), mock.patch.object(ui, "out") as said:
            v.__exit__(None, None, None)
        said.assert_not_called()
        self.assertIn("done · ", v.summary)
        self.assertIn("2 files +6 −5", v.summary)
        self.assertIn("\033]0;✓ done · cracked", out.getvalue())  # the tab title


class Modes(unittest.TestCase):
    def test_prefixes_cycle(self):
        g = SimpleNamespace(assume_yes=False, investigate=False)
        self.assertEqual(ui.mode(g), "")  # not a colour tty under test: plain text
        ui.next_mode(g)
        self.assertEqual(ui.mode(g), "[FUCK IT MODE] ")
        ui.next_mode(g)
        self.assertEqual(ui.mode(g), "[INVESTIGATE] ")


if __name__ == "__main__":
    unittest.main()


class HarnessInput(unittest.TestCase):
    """Slash commands at the harness's own prompts must act, not leak to the model."""
    def setUp(self):
        self.on = ui.ON; self.exp = ui.EXPANDED
        ui.ON = True

    def tearDown(self):
        ui.ON = self.on; ui.EXPANDED = self.exp

    def feed(self, lines, guard=None):
        it = iter(lines)
        with mock.patch("builtins.input", lambda _p="": next(it)), mock.patch.object(ui, "redraw"):
            return ui.harness_input("> ", guard)

    def test_expand_toggles_and_reasks_then_returns_hint(self):
        ui.EXPANDED = False
        out = self.feed(["/expand", "focus on the TLS finding"])
        self.assertEqual(out, "focus on the TLS finding")
        self.assertTrue(ui.EXPANDED)   # toggled, not sent to the model

    def test_q_raises_eoferror(self):
        with self.assertRaises(EOFError):
            self.feed(["/q"])

    def test_mode_cycles_then_reasks(self):
        g = SimpleNamespace(assume_yes=False, investigate=False)
        with mock.patch.object(ui, "next_mode", return_value="mode x") as nm:
            out = self.feed(["/mode", "a hint"], g)
        self.assertEqual(out, "a hint")
        nm.assert_called_once_with(g)

    def test_plain_hint_passes_through(self):
        self.assertEqual(self.feed(["just do the thing"]), "just do the thing")
