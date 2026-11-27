"""REPL features: interrupt repair, /undo, sessions, diffs, !cmd, /export, Tab completion."""

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cracked_code import agent, repl, safety, tools, ui


def call(i, name="run_shell"):
    return {"id": i, "type": "function", "function": {"name": name, "arguments": "{}"}}


class Repair(unittest.TestCase):
    def test_unanswered_calls_get_a_result(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"},
                {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]},
                {"role": "tool", "tool_call_id": "a", "content": "ok"}]
        agent.repair(msgs)
        self.assertEqual(msgs[-1], {"role": "tool", "tool_call_id": "b", "content": agent.INTERRUPTED_CALL})

    def test_interrupted_before_any_reply(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
        agent.repair(msgs)
        self.assertEqual(msgs[-1], {"role": "assistant", "content": agent.INTERRUPTED_REPLY})

    def test_complete_history_is_left_alone(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"},
                {"role": "assistant", "content": "done"}]
        agent.repair(msgs)
        self.assertEqual(len(msgs), 3)

    def test_run_repairs_on_interrupt(self):
        class Boom:
            def chat(self, *a):
                raise KeyboardInterrupt
        with tempfile.TemporaryDirectory() as d:
            ctx = tools.Ctx(workspace=Path(d), guard=safety.Guard(Path(d), assume_yes=True))
            msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
            with self.assertRaises(KeyboardInterrupt):
                agent.run(Boom(), msgs, ctx, out=open(os.devnull, "w"))
        self.assertEqual(msgs[-1]["role"], "assistant")


class Undo(unittest.TestCase):
    def test_restore_brings_files_back_and_removes_new_ones(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "keep.txt").write_text("v1")
            (ws / "gone.txt").write_text("x")
            safety.snapshot(ws, "start")
            before = safety.head(ws)
            (ws / "keep.txt").write_text("v2")
            (ws / "gone.txt").unlink()
            (ws / "new.txt").write_text("made by the task")
            (ws / "out" / "deep").mkdir(parents=True)
            (ws / "out" / "deep" / "x.txt").write_text("also made by the task")
            safety.snapshot(ws, "task")
            summary = safety.restore(ws, before)
            self.assertEqual((ws / "keep.txt").read_text(), "v1")
            self.assertTrue((ws / "gone.txt").exists())
            self.assertFalse((ws / "new.txt").exists())
            self.assertFalse((ws / "out").exists())  # the folders it made go too
            self.assertIn("changed", summary)
            log = safety.git(ws, "log", "--oneline").stdout
            self.assertIn("undo to", log)  # the undone work is still in git


class Sessions(unittest.TestCase):
    def test_save_load_latest_and_prune(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hello"}]
            paths = []
            for i in range(repl.KEEP_SESSIONS + 2):
                p = repl.sessions_dir(ws) / f"2026-11-04_0000{i:02d}.json"
                repl.save_session(p, "m", msgs, [[1, "abc"]], "abc")
                paths.append(p)
            self.assertEqual(len(list(repl.sessions_dir(ws).glob("*.json"))), repl.KEEP_SESSIONS)
            self.assertEqual(repl.latest_session(ws), paths[-1])
            self.assertEqual(repl.latest_session(ws, exclude=paths[-1]), paths[-2])
            data = repl.load_session(paths[-1])
            self.assertEqual((data["messages"], data["undo"], data["since"]), (msgs, [[1, "abc"]], "abc"))
            self.assertEqual((ws / ".cracked" / ".gitignore").read_text(), "*\n")

    def test_nothing_saved_without_a_task(self):
        with tempfile.TemporaryDirectory() as d:
            p = repl.sessions_dir(Path(d)) / "x.json"
            repl.save_session(p, "m", [{"role": "system", "content": "s"}], [], None)
            self.assertFalse(p.exists())


class Diffs(unittest.TestCase):
    def test_edit_previews_the_diff_and_records_the_change(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.py").write_text("x = 1\ny = 2\n")
            guard = safety.Guard(ws)
            seen = {}
            guard.allow = lambda key, why, always_ask=False, preview="": seen.setdefault("preview", preview) or True
            ctx = tools.Ctx(workspace=ws, guard=guard)
            tools.edit_file(ctx, "a.py", "y = 2", "y = 3")
            self.assertIn("-y = 2", seen["preview"])
            self.assertIn("+y = 3", seen["preview"])
            self.assertEqual(ctx.change[:2], (1, 1))
            self.assertIn("+1 −1", ui.call_line("edit_file", '{"path": "a.py"}', "edited", change=ctx.change))

    def test_bad_edit_asks_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.py").write_text("x = 1\n")
            guard = safety.Guard(ws)
            guard.allow = mock.Mock(return_value=True)
            out = tools.edit_file(tools.Ctx(workspace=ws, guard=guard), "a.py", "nope", "z")
            self.assertTrue(out.startswith("ERROR"))
            guard.allow.assert_not_called()


class Commands(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "src").mkdir()
        (self.ws / "src" / "main.py").write_text("")
        ctx = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws))
        args = SimpleNamespace(context=1000, compact_at=0.5, max_turns=-1, base_url="http://127.0.0.1:9/v1")
        self.msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "fix it"},
                     {"role": "assistant", "content": "fixed"}]
        self.r = repl.Repl(args, {"vision": "auto"}, ctx, SimpleNamespace(model="m"), self.msgs, None, None)
        self.said = []
        self.r.say = lambda text, rgb=None: self.said.append(text)

    def tearDown(self):
        self._tmp.cleanup()

    def test_shell_output_is_not_added_to_the_conversation(self):
        with mock.patch.object(ui, "out", lambda text, kind="all": self.said.append(text)):
            self.r.shell("echo hi")
        self.assertIn("hi", self.said)
        self.assertEqual(len(self.msgs), 3)

    def test_plan_menu_runs_until_done_or_no(self):
        from cracked_code import plan
        p = plan.parse("# Plan: g\n1. a\n2. b")
        self.r.ctx.plan = p
        runs = []

        def run(task):  # stands in for agent.run_plan: manual stops after a step, auto finishes
            runs.append(p.mode)
            i = p.current()
            p.steps[i].mark = "x"
            self.r.ctx.plan_ask = p.mode == "manual" and p.current() is not None
            return ""

        with mock.patch.object(ui, "choose", side_effect=["m", "y"]), mock.patch.object(self.r, "run_task", run):
            self.r.plan_menu()
        self.assertEqual(runs, ["manual", "auto"])
        p.steps[1].mark = " "
        with mock.patch.object(ui, "choose", return_value="n"), mock.patch.object(self.r, "run_task", run):
            self.r.plan_menu()
        self.assertEqual(len(runs), 2)
        self.assertIn("plan kept: /plan asks again", self.said)

    def test_plan_menu_edit_rewrites_the_plan(self):
        from cracked_code import agent, llm, plan
        self.r.ctx.plan = plan.parse("# Plan: g\n- [x] 1. a\n- [ ] 2. b")
        reply = llm.Reply(text="- [x] 1. a\n- [ ] 2. b, with tests\n- [ ] 3. c", calls=[], finish="stop")
        self.r.watching = contextlib.nullcontext
        with mock.patch.object(ui, "choose", side_effect=["e", "n"]), \
                mock.patch.object(ui, "harness_input", return_value="add tests"), \
                mock.patch.object(agent, "text_only", return_value=reply) as model:
            self.r.plan_menu()
        self.assertIn("add tests", model.call_args.args[1][-1]["content"])
        self.assertEqual([s.text for s in self.r.ctx.plan.steps], ["a", "b, with tests", "c"])
        self.assertEqual(plan.load(self.ws).steps[2].text, "c")
        self.assertEqual(self.msgs[-1]["content"], reply.text)  # the edit stays in the history

    def test_research_in_fuck_it_still_waits_for_the_menu(self):
        from cracked_code import agent, plan
        self.r.ctx.guard.assume_yes = True
        sent = []
        with mock.patch.object(self.r, "run_task", side_effect=lambda t: sent.append(t) or "# Plan: g\n1. a\n2. b"), \
                mock.patch.object(ui, "choose", return_value="n") as menu, \
                mock.patch.object(agent, "run_plan") as runner:
            self.r.plan_research("g")
        menu.assert_called_once()
        runner.assert_not_called()
        self.assertIn(plan.WRITE_RULES, sent[0])
        self.assertEqual(self.r.ctx.plan.state, "draft")

    def test_export_markdown(self):
        self.r.cmd_export("notes/out.md")
        text = (self.ws / "notes" / "out.md").read_text()
        self.assertIn("## you\n\nfix it", text)
        self.assertIn("## cracked\n\nfixed", text)

    def test_menu_offers_commands_and_paths(self):
        self.assertEqual(repl.suggest("/un", self.ws, False), [("/undo", -3, repl.DESCRIBE["/undo"])])
        self.assertIn("/clear", [t for t, _, _ in repl.suggest("/", self.ws, False)])
        self.assertEqual(repl.suggest("/undo now", self.ws, False), [])  # past the command word
        self.assertEqual(repl.suggest("fix @sr", self.ws, False),  # @ as you type: here first, then anywhere
                         [("@src/", -3, "folder"), ("@src/main.py", -3, "attach")])
        self.assertEqual(repl.suggest("@mai", self.ws, False), [("@src/main.py", -4, "attach")])  # no folder typed
        self.assertEqual(repl.suggest("look @src/ma", self.ws, False), [("@src/main.py", -7, "attach")])
        self.assertEqual(repl.suggest("fix sr", self.ws, False), [])  # plain words: only on Tab
        self.assertEqual(repl.suggest("fix sr", self.ws, True), [("src/", -2, "")])

    def test_fuzzy_ranks_file_name_matches_first(self):
        names = ["docs/parser-notes.md", "src/utils/parse.py", "tests/test_parse.py", "a/p/a/r/s/e.txt", "README.md"]
        self.assertEqual(repl.fuzzy("parse", names),
                         ["src/utils/parse.py", "docs/parser-notes.md", "tests/test_parse.py", "a/p/a/r/s/e.txt"])
        self.assertEqual(repl.fuzzy("rdme", names), ["README.md"])  # letters in order

    def test_files_skips_hidden_and_build_folders(self):
        for d in (".git-like", "node_modules"):
            (self.ws / d).mkdir()
            (self.ws / d / "x.py").write_text("")
        repl._FILES.clear()
        self.assertEqual(repl.files(self.ws), ["src/main.py"])  # "/" on Windows too

    def test_marks_colour_commands_and_mentions(self):
        self.assertEqual(repl.marks("/clear", self.ws, True), [("cmd", "/clear")])
        self.assertEqual(repl.marks("/clr", self.ws, True), [("bad", "/clr")])
        self.assertEqual(repl.marks("cat /etc/hosts", self.ws, True), [("", "cat /etc/hosts")])
        self.assertEqual(repl.marks("see @src/main.py and @nope.py", self.ws, True),
                         [("", "see "), ("file", "@src/main.py"), ("", " and "), ("missing", "@nope.py")])
        self.assertEqual(repl.marks("/clear", self.ws, False), [("", "/clear")])  # not the first line

    def test_unknown_command_suggests_the_close_one(self):
        with mock.patch.object(self.r, "read", side_effect=["/claer", EOFError]), \
                mock.patch.object(self.r, "save"), mock.patch.object(self.r, "setup_prompt"):
            self.r.loop()
        self.assertTrue(any("did you mean /clear?" in s for s in self.said))

    def test_undo_with_nothing_to_undo(self):
        self.r.cmd_undo("")
        self.assertIn("nothing to undo", self.said[-1])


if __name__ == "__main__":
    unittest.main()


class Mentions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.ctx = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws, assume_yes=True))

    def tearDown(self):
        self._tmp.cleanup()

    def test_text_file_is_attached_and_others_stay_text(self):
        (self.ws / "skills").mkdir()
        (self.ws / "skills" / "video.md").write_text("step one\nstep two\n", newline="\n")
        (self.ws / "long.txt").write_text("x\n" * 1000, newline="\n")
        (self.ws / "blob.bin").write_bytes(b"\0\1\2")
        msg = repl.user_message("make it @skills/video.md, also @long.txt @blob.bin mail me@host @nope", [], self.ctx)
        text = msg["content"]
        self.assertTrue(text.startswith("make it @skills/video.md,"))  # the task line is untouched
        self.assertIn("[@skills/video.md lines 1-2 of 2]\nstep one\nstep two", text)
        self.assertIn("[@long.txt lines 1-400 of 1000]", text)
        self.assertIn("[more: read_file long.txt offset=400]", text)
        self.assertIn("[@blob.bin not attached: binary]", text)
        self.assertNotIn("@host", text.split("\n\n", 1)[1])
        self.assertNotIn("@nope", text.split("\n\n", 1)[1])

    def test_unreadable_file_is_noted_not_a_crash(self):
        (self.ws / "secret.txt").write_text("x")
        with mock.patch("pathlib.Path.read_bytes", side_effect=PermissionError(13, "Permission denied")):
            text = repl.user_message("see @secret.txt", [], self.ctx)["content"]
        self.assertIn("[@secret.txt not attached: Permission denied]", text)

    def test_image_needs_vision(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
        (self.ws / "shot.png").write_bytes(png)
        self.assertIn("no vision", repl.user_message("look @shot.png", [], self.ctx)["content"])
        self.ctx.vision = True
        self.assertEqual(repl.attach("look @shot.png", self.ctx), ("look @shot.png", ["shot.png"]))


class Init(unittest.TestCase):
    def test_runs_the_canned_prompt_as_a_task(self):
        r = repl.Repl.__new__(repl.Repl)
        tasks = []
        r.run_task = tasks.append
        with mock.patch.object(ui, "out"):
            r.cmd_init("")
            r.cmd_init("we use uv")
        self.assertEqual(tasks[0], repl.INIT)
        self.assertTrue(tasks[1].startswith(repl.INIT) and tasks[1].endswith("Also: we use uv"))
        self.assertIn("/init", repl.COMMANDS)


class Btw(unittest.TestCase):
    def test_side_question_is_not_kept(self):
        sent = []
        model = SimpleNamespace(extra={}, chat=lambda msgs, schemas: (sent.append((list(msgs), schemas, dict(model.extra)))
                                                                     or SimpleNamespace(text="42")))
        r = repl.Repl.__new__(repl.Repl)
        r.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}, {"role": "assistant", "content": "a"}]
        r.model, r.watching, r.args = model, contextlib.nullcontext, SimpleNamespace(base_url="x")
        r.ctx = SimpleNamespace(vision=False, incognito=False)
        before = list(r.messages)
        r.cmd_btw("what port?")
        msgs, schemas, extra = sent[0]
        self.assertEqual(r.messages, before)              # nothing added to the conversation
        self.assertIn("what port?", msgs[-1]["content"])   # but the history went along
        self.assertIn("run_shell", [s["function"]["name"] for s in schemas])  # the tool list goes along: the prompt's start must not change
        self.assertEqual(extra, {"strata_prefix": {"messages": 3}})
        self.assertEqual(model.extra, {})


class CrackedMd(unittest.TestCase):
    def test_how_to_write_it_is_added_only_to_a_task_that_names_it(self):
        import tempfile
        from pathlib import Path
        from cracked_code import agent, repl, safety, tools
        self.assertNotIn(agent.CRACKED_MD, agent.SYSTEM)  # SYSTEM names the file only for its ## Layout line
        with tempfile.TemporaryDirectory() as d:
            ctx = tools.Ctx(workspace=Path(d), guard=safety.Guard(Path(d), assume_yes=True))
            named = repl.user_message("please create a cracked.md for this folder", [], ctx)["content"]
            self.assertIn(agent.CRACKED_MD, named)
            self.assertEqual(repl.user_message(repl.INIT, [], ctx)["content"].count(agent.CRACKED_MD), 1)
            self.assertEqual(repl.user_message("fix the bug", [], ctx)["content"], "fix the bug")
