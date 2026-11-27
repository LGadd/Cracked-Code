import os
import threading
import time
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from cracked_code import safety, tools


class FileTools(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.ctx = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws, assume_yes=True))

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read(self):
        self.assertEqual(
            tools.write_file(self.ctx, "notes.md", "one\ntwo\nthree\n"),
            f"wrote 14 chars to {self.ws / 'notes.md'}",
        )
        out = tools.read_file(self.ctx, "notes.md")
        self.assertIn("one\ntwo\nthree", out)
        self.assertIn("lines 1-3 of 3", out)

    def test_write_creates_parents(self):
        tools.write_file(self.ctx, "a/b/c.txt", "deep")
        self.assertEqual((self.ws / "a/b/c.txt").read_text(), "deep")

    def test_one_line_longer_than_the_budget_is_cut(self):
        (self.ws / "min.js").write_text("x" * (tools.READ_BUDGET * 3) + "\nend\n")
        out = tools.read_file(self.ctx, "min.js")
        self.assertLess(len(out), tools.READ_BUDGET + 300)
        self.assertIn("line 1 cut at", out)
        self.assertIn("[more: read_file offset=1]", out)

    def test_read_offset_limit(self):
        tools.write_file(self.ctx, "n.txt", "".join(f"l{i}\n" for i in range(10)))
        out = tools.read_file(self.ctx, "n.txt", offset=3, limit=2)
        self.assertIn("l3\nl4", out)
        self.assertNotIn("l2\n", out)
        self.assertIn("lines 4-5 of 10", out)

    def test_read_missing_file_is_error_text(self):
        self.assertTrue(tools.read_file(self.ctx, "nope.txt").startswith("ERROR:"))

    def test_edit_unique(self):
        tools.write_file(self.ctx, "x.py", "def a():\n    return 1\n")
        out = tools.edit_file(self.ctx, "x.py", "return 1", "return 2")
        self.assertIn("1 replacement", out)
        self.assertIn("return 2", (self.ws / "x.py").read_text())

    def test_edit_not_found(self):
        tools.write_file(self.ctx, "x.py", "hello")
        self.assertIn("not found", tools.edit_file(self.ctx, "x.py", "goodbye", "x"))

    def test_edit_ambiguous(self):
        tools.write_file(self.ctx, "x.py", "dup\ndup\n")
        self.assertIn("appears 2 times", tools.edit_file(self.ctx, "x.py", "dup", "z"))

    def test_delete(self):
        tools.write_file(self.ctx, "gone.txt", "x")
        with mock.patch("builtins.input", return_value="y") as asked:
            self.assertIn("deleted", tools.delete_file(self.ctx, "gone.txt"))
        asked.assert_called_once()  # asked even though this ctx is in Fuck It Mode
        self.assertFalse((self.ws / "gone.txt").exists())

    def test_delete_directory_needs_shell(self):
        (self.ws / "adir").mkdir()
        with mock.patch("builtins.input", return_value="y"):
            self.assertIn("is a directory", tools.delete_file(self.ctx, "adir"))

    def test_read_and_write_outside_never_ask(self):
        manual = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws))
        with tempfile.TemporaryDirectory() as other, \
                mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertIn("wrote", tools.write_file(manual, f"{other}/o.txt", "hi"))
            self.assertIn("hi", tools.read_file(manual, f"{other}/o.txt"))

    def test_edit_asks_only_in_manual_mode(self):
        tools.write_file(self.ctx, "e.txt", "old")
        manual = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws))
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(tools.edit_file(manual, "e.txt", "old", "new").startswith("DENIED"))
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertIn("1 replacement", tools.edit_file(self.ctx, "e.txt", "old", "new"))

    def test_overwrite_asks_in_manual_mode_new_file_does_not(self):
        manual = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws))
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            self.assertIn("wrote", tools.write_file(manual, "n.txt", "v1"))
            self.assertIn("appended", tools.write_file(manual, "n.txt", "+", append=True))
        with mock.patch("builtins.input", return_value=""):
            self.assertTrue(tools.write_file(manual, "n.txt", "v2").startswith("DENIED"))
        self.assertEqual((self.ws / "n.txt").read_text(), "v1+")

    def test_append(self):
        tools.write_file(self.ctx, "p.txt", "part1\n")
        self.assertIn("appended", tools.write_file(self.ctx, "p.txt", "part2\n", append=True))
        self.assertEqual((self.ws / "p.txt").read_text(), "part1\npart2\n")

    def test_run_shell_cwd_and_exit_code(self):
        # Git Bash: pwd prints /tmp/...; pwd -W prints the Windows path (C:/Users/.../Temp/...)
        out = tools.run_shell(self.ctx, ("pwd -W" if tools.BASH else "pwd") + " && echo hi").replace("/", os.sep)
        self.assertIn("[exit 0]", out)
        self.assertIn(str(self.ws), out)
        self.assertIn("hi", out)

    def test_run_shell_reports_stderr(self):
        out = tools.run_shell(self.ctx, "echo bad >&2; exit 3")
        self.assertIn("[exit 3]", out)
        self.assertIn("[stderr]", out)

    def test_run_shell_timeout(self):
        self.assertIn("timed out", tools.run_shell(self.ctx, "sleep 5", timeout=1))

    def test_run_shell_background_job_returns_at_once(self):
        # A backgrounded server used to hold the output pipe open until the timeout.
        start = time.monotonic()
        out = tools.run_shell(self.ctx, "sleep 30 & echo started", timeout=10)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("started", out)

    def test_big_output_spills_to_file(self):
        out = tools.call(self.ctx, "run_shell", '{"command": "seq 1 5000"}')
        self.assertLess(len(out), 2 * tools.PREVIEW + 200)
        self.assertIn("full output: .cracked/out/1-run_shell.txt", out)
        full = (self.ws / ".cracked/out/1-run_shell.txt").read_text()
        self.assertIn("\n2500\n", full)
        self.assertEqual((self.ws / ".cracked/.gitignore").read_text(), "*\n")

    def test_small_output_stays_inline(self):
        self.assertEqual(tools.call(self.ctx, "run_shell", '{"command": "echo hi"}'), "[exit 0]\nhi")
        self.assertFalse((self.ws / ".cracked").exists())

    def test_read_pages_at_line_boundaries(self):
        tools.write_file(self.ctx, "big.txt", "".join(f"{'x' * 99}\n" for _ in range(300)))
        out = tools.call(self.ctx, "read_file", '{"path": "big.txt"}')
        self.assertIn("lines 1-80 of 300", out)
        self.assertIn("[more: read_file offset=80]", out)
        self.assertFalse((self.ws / ".cracked").exists())  # read_file never spills

    def test_call_unknown_tool(self):
        self.assertIn("unknown tool", tools.call(self.ctx, "frobnicate", "{}"))

    def test_call_bad_json(self):
        tools.write_file(self.ctx, "x.txt", "a")
        self.assertIn("not valid JSON", tools.call(self.ctx, "read_file", '{"path": '))

    def test_call_bad_arguments(self):
        out = tools.call(self.ctx, "read_file", '{"nope": 1}')
        self.assertIn("bad arguments", out)


if __name__ == "__main__":
    unittest.main()


class Incognito(unittest.TestCase):
    def test_web_tools_left_out_and_refused(self):
        ctx = tools.Ctx(workspace=Path("."), guard=safety.Guard(Path("."), assume_yes=True), incognito=True)
        names = [s["function"]["name"] for s in tools.schemas(web=False)]
        self.assertNotIn("web_search", names)
        self.assertNotIn("web_fetch", names)
        self.assertIn("web_search", [s["function"]["name"] for s in tools.schemas()])
        self.assertTrue(tools.call(ctx, "web_search", '{"query": "x"}').startswith("ERROR: incognito"))


class Fixes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.ctx = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws, assume_yes=True))

    def tearDown(self):
        self._tmp.cleanup()

    def test_edit_keeps_crlf_line_endings(self):
        (self.ws / "win.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
        self.assertIn("edited", tools.edit_file(self.ctx, "win.txt", "two\nthree", "2\n3"))
        self.assertEqual((self.ws / "win.txt").read_bytes(), b"one\r\n2\r\n3\r\n")

    def test_edit_refuses_a_file_that_is_not_utf8(self):
        (self.ws / "latin.txt").write_bytes(b"caf\xe9 one\n")
        self.assertIn("not UTF-8", tools.edit_file(self.ctx, "latin.txt", "one", "two"))
        self.assertEqual((self.ws / "latin.txt").read_bytes(), b"caf\xe9 one\n")

    def test_spill_never_reuses_a_number(self):
        big = "x" * (tools.SPILL_AT + 1)
        tools.spill(self.ctx, big, "a"), tools.spill(self.ctx, big, "b")
        (self.ws / ".cracked/out/1-a.txt").unlink()
        tools.spill(self.ctx, big, "c")
        self.assertEqual(sorted(f.name for f in (self.ws / ".cracked/out").iterdir()), ["2-b.txt", "3-c.txt"])


class EditHelp(Fixes):
    """A failed edit shows the nearest text, a broken file is flagged, several edits go in one call."""

    def test_failed_edit_shows_the_closest_lines(self):
        (self.ws / "a.py").write_text("x = 1\n\ndef total(items):\n    return sum(items)\n\ny = 2\n")
        out = tools.edit_file(self.ctx, "a.py", "def total(items):\n  return sum(items)", "pass")
        self.assertIn("closest text is lines 3-4", out)
        self.assertIn("    return sum(items)", out)
        self.assertNotIn("y = 2", out)

    def test_failed_edit_with_nothing_close_says_read_the_file(self):
        (self.ws / "a.py").write_text("x = 1\n")
        self.assertIn("Read the file", tools.edit_file(self.ctx, "a.py", "completely different", "y"))

    def test_broken_python_and_json_are_flagged(self):
        (self.ws / "a.py").write_text("def f():\n    return 1\n")
        out = tools.edit_file(self.ctx, "a.py", "return 1", "return (1")
        self.assertIn("edited", out)
        self.assertIn("WARNING: a.py now has a syntax error (line", out)
        self.assertNotIn("WARNING", tools.edit_file(self.ctx, "a.py", "return (1", "return 2"))
        self.assertIn("WARNING", tools.write_file(self.ctx, "c.json", '{"a": 1,}'))
        self.assertNotIn("WARNING", tools.write_file(self.ctx, "part.py", "def f(:", append=True))

    def test_several_edits_in_one_call(self):
        (self.ws / "a.txt").write_text("one\ntwo\nthree\n")
        edits = [{"old_string": "one", "new_string": "1"}, {"old_string": "three", "new_string": "3"}]
        self.assertIn("2 replacements", tools.edit_file(self.ctx, "a.txt", edits=edits))
        self.assertEqual((self.ws / "a.txt").read_text(), "1\ntwo\n3\n")

    def test_one_bad_edit_changes_nothing(self):
        (self.ws / "a.txt").write_text("one\ntwo\nthree\n")
        edits = [{"old_string": "one", "new_string": "1"}, {"old_string": "four", "new_string": "4"}]
        out = tools.edit_file(self.ctx, "a.txt", edits=edits)
        self.assertIn("edit 2 of 2", out)
        self.assertIn("Nothing was changed", out)
        self.assertEqual((self.ws / "a.txt").read_text(), "one\ntwo\nthree\n")
        self.assertIn("ERROR", tools.edit_file(self.ctx, "a.txt"))


class ReplaceDef(Fixes):
    SRC = "import os\n\n\nclass A:\n    @staticmethod\n    def f(x):\n        return x\n\n    def g(self):\n        return 1\n\n\ndef top():\n    return 0\n"

    def test_replaces_a_method_with_its_decorator_at_the_right_depth(self):
        (self.ws / "m.py").write_text(self.SRC)
        out = tools.replace_def(self.ctx, "m.py", "A.f", "def f(x):\n    return x * 2\n")
        self.assertIn("replaced A.f", out)
        self.assertNotIn("WARNING", out)
        # code sent from `def` keeps the decorators it had
        self.assertEqual((self.ws / "m.py").read_text(), self.SRC.replace("        return x\n", "        return x * 2\n", 1))

    def test_code_with_its_own_decorators_replaces_the_old_ones(self):
        (self.ws / "m.py").write_text(self.SRC)
        tools.replace_def(self.ctx, "m.py", "A.f", "@classmethod\ndef f(cls, x):\n    return x\n")
        self.assertIn("    @classmethod\n    def f(cls, x):", (self.ws / "m.py").read_text())
        self.assertNotIn("staticmethod", (self.ws / "m.py").read_text())

    def test_crlf_file_stays_crlf(self):
        (self.ws / "m.py").write_bytes(self.SRC.replace("\n", "\r\n").encode())
        tools.replace_def(self.ctx, "m.py", "top", "def top():\n    return 5\n")
        data = (self.ws / "m.py").read_bytes()
        self.assertIn(b"def top():\r\n    return 5\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_replaces_a_top_level_function_sent_indented(self):
        (self.ws / "m.py").write_text(self.SRC)
        tools.replace_def(self.ctx, "m.py", "top", "    def top():\n        return 5")
        self.assertTrue((self.ws / "m.py").read_text().endswith("def top():\n    return 5\n"))

    def test_unknown_name_lists_what_is_there_and_changes_nothing(self):
        (self.ws / "m.py").write_text(self.SRC)
        out = tools.replace_def(self.ctx, "m.py", "missing", "def missing(): pass")
        self.assertIn("A.f, A.g, top", out)
        self.assertEqual((self.ws / "m.py").read_text(), self.SRC)
        (self.ws / "n.txt").write_text("not python (")
        self.assertIn("Use edit_file", tools.replace_def(self.ctx, "n.txt", "x", "def x(): pass"))


class ShellJobs(unittest.TestCase):
    """Ctrl+B moves a running command to the background; /jobs lists and kills."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.ctx = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws, assume_yes=True))

    def tearDown(self):
        tools.jobs_command("kill all")
        tools.JOBS.clear()
        self._tmp.cleanup()

    def later(self, fn, delay=0.3):
        t = threading.Timer(delay, fn)
        t.start()
        return t

    def test_ctrl_b_backgrounds_and_the_result_comes_once(self):
        self.later(tools.DETACH.set)
        out = tools.run_shell(self.ctx, "sleep 1; echo finished-now")
        self.assertIn("background as job", out)
        n = self.ctx.backgrounded
        self.assertEqual(tools.finished_jobs(self.ctx), "")  # still running
        self.assertIn("background  running", tools.jobs_command(""))
        tools.JOBS[0].proc.wait(5)
        note = tools.finished_jobs(self.ctx)
        self.assertTrue(note.startswith(f"{tools.JOB_DONE} {n} finished"))
        self.assertIn("finished-now", note)
        self.assertEqual(tools.finished_jobs(self.ctx), "")
        self.assertFalse((self.ws / ".cracked").exists())  # output lives outside the workspace

    def test_jobs_kill_stops_a_stuck_foreground_command_and_its_children(self):
        self.later(lambda: tools.jobs_command(f"kill {tools.foreground().n}"))
        start = time.monotonic()
        out = tools.run_shell(self.ctx, "sleep 30 & echo $! > child.pid; wait")
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("cancelled by the user", out)
        self.assertEqual(tools.JOBS, [])
        if os.name != "posix":
            return  # shortcut: $! is a Git Bash pid there, not a Windows one; checked on Linux only
        pid = int((self.ws / "child.pid").read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_timeout_keeps_partial_output(self):
        out = tools.run_shell(self.ctx, "echo early; sleep 5", timeout=1)
        self.assertIn("timed out", out)
        self.assertIn("early", out)

    def test_limit_follows_config(self):
        with mock.patch.object(tools, "SHELL_TIMEOUT_MAX", 1):
            self.assertIn("timed out after 1s", tools.run_shell(self.ctx, "sleep 5", timeout=600))

    def test_jobs_command_messages(self):
        self.assertEqual(tools.jobs_command(""), "no shell commands running")
        self.assertIn("nothing to kill", tools.jobs_command("kill 99"))
        self.assertIn("/jobs kill", tools.jobs_command("what"))
