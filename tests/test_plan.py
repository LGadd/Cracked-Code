import tempfile
import unittest
from pathlib import Path

from cracked_code import plan


class Format(unittest.TestCase):
    TEXT = """Some research notes first.
# Plan: add save/load to inventory
- [ ] 1. Add save(path) | files: inventory/core.py | check: python3 -m unittest -q | tail -3
- [x] 2. Add load(path) | files: inventory/core.py, tests/t.py
  → load() written, 22 pass
3) Update the README
Unsure: step 3
"""

    def test_parse_reads_steps_files_checks_and_results(self):
        p = plan.parse(self.TEXT)
        self.assertEqual(p.goal, "add save/load to inventory")
        self.assertEqual([s.text for s in p.steps], ["Add save(path)", "Add load(path)", "Update the README"])
        self.assertEqual(p.steps[0].check, "python3 -m unittest -q | tail -3")  # a check keeps its own pipe
        self.assertEqual(p.steps[1].files, ["inventory/core.py", "tests/t.py"])
        self.assertEqual((p.steps[1].mark, p.steps[1].result), ("x", "load() written, 22 pass"))
        self.assertEqual(p.current(), 0)

    def test_render_round_trips(self):
        p = plan.parse(self.TEXT)
        p.source, p.state = "model", "running"
        again = plan.parse(plan.render(p))
        self.assertEqual(plan.render(again), plan.render(p))
        self.assertEqual((again.source, again.state), ("model", "running"))

    def test_problems_are_named(self):
        with self.assertRaisesRegex(plan.PlanError, "no steps"):
            plan.parse("I would start by reading the code.")
        with self.assertRaisesRegex(plan.PlanError, "step 1 names 3 files"):
            plan.parse("1. do it | files: a, b, c")
        with self.assertRaisesRegex(plan.PlanError, "31 steps"):
            plan.parse("\n".join(f"{i}. step" for i in range(1, 32)))

    def test_checks_must_be_commands(self):
        for bad in ("the audit lists exactly findstr.exe as missing and exits 0",
                    "`./audit-pe-tools.sh` exits 0 and prints `AUDIT_OK`", "grep 'unclosed"):
            with self.assertRaisesRegex(plan.PlanError, "step 1's check"):
                plan.parse(f"1. do it | check: {bad}")
        for good in ("./audit-pe-tools.sh", "`python3 -m unittest -q`", "! grep -q x a.cmd",
                     "for i in *.inf; do grep -q X $i || exit 1; done", "test -f a", "FOO=1 make"):
            self.assertTrue(plan.parse(f"1. do it | check: {good}").steps[0].check, good)
        plan.parse("- [x] 1. done long ago | check: the old sentence")  # done steps are left alone

    def test_mode_round_trips_and_defaults_to_auto(self):
        p = plan.parse(self.TEXT)
        self.assertEqual(p.mode, "auto")
        p.mode = "manual"
        self.assertEqual(plan.parse(plan.render(p)).mode, "manual")
        self.assertEqual(plan.parse("<!-- cracked plan: source=user state=draft -->\n1. a").mode, "auto")  # older files

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self.assertIsNone(plan.load(ws))
            plan.save(ws, plan.parse(self.TEXT))
            self.assertEqual(len(plan.load(ws).steps), 3)
            self.assertEqual((ws / ".cracked" / ".gitignore").read_text(), "*\n")

    def test_step_message_shows_done_now_and_stop(self):
        p = plan.parse(self.TEXT)
        p.steps[0].mark, p.steps[0].result = "x", "save() written"
        msg = plan.step_message(p, 2)
        self.assertIn("[PLAN step 3 of 3]", msg)
        self.assertIn("1. save() written", msg)
        self.assertIn("NOW: 3. Update the README", msg)
        self.assertIn("This is the last step", msg)
        self.assertIn("Do not start step 3", plan.step_message(p, 1).replace("step 3 (", "step 3 ("))  # step 2's message names step 3

    def test_label(self):
        p = plan.parse(self.TEXT)
        self.assertEqual(p.label(), "")  # a draft shows nothing
        p.state = "running"
        self.assertTrue(p.label().startswith("plan 1/3"))
        p.source = "model"
        self.assertTrue(p.label().startswith("☐ 1/3"))


class LooksLikeWrite(unittest.TestCase):
    def test_reads_run_unasked(self):
        for cmd in ("ls -la", "cat a.py | grep def", "git log --oneline -5", "git status", "find . -name '*.py'",
                    "rg -n foo src", "wimlib-imagex dir boot.wim 1 | grep -i findstr", "iconv -f UTF-16LE -t UTF-8 a.inf",
                    "python3 /tmp/audit.py", "make -n", "ls 2>/dev/null", "cmd 2>&1 | head", "grep 'a->b' x",
                    "pip list", "lsblk -o NAME,TRAN", "echo \"x > y\""):
            self.assertFalse(plan.looks_like_write(cmd), cmd)

    def test_changes_ask(self):
        for cmd in ("rm x", "cat a > b", "echo x >> log", "ls; rm x", "find . -delete", "find . -exec rm {} +",
                    "git checkout main", "git commit -am x", "sort -o out in", "echo $(rm x)", "cat `rm x`",
                    "sed -i s/a/b/ f", "sed -i.bak s/a/b/ f", "pip install x", "sudo ls", "ls | xargs rm",
                    "timeout 5 touch x", "FOO=1 mkdir d", "pacman -Syu", "uv add rich", "cmd 2> err.txt",
                    "/usr/bin/cp a b", "ls && mv a b"):
            self.assertTrue(plan.looks_like_write(cmd), cmd)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- the executor
import io
import json
from unittest import mock

from cracked_code import agent, llm, safety, tools


def _call(cid, name, arguments):
    return llm.Call(id=cid, name=name, arguments=json.dumps(arguments))


class Stub:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def chat(self, messages, schemas):
        self.sent.append(messages[-1].get("content"))
        return self.replies.pop(0)


def say(text):
    return llm.Reply(text=text, calls=[], finish="stop")


class Executor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.ctx = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws, assume_yes=True))
        self.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "the goal"}, {"role": "assistant", "content": "ok"}]

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, text):
        p = plan.parse(text)
        self.ctx.plan = p
        return p

    def test_walks_each_step_ticks_and_finishes(self):
        p = self.plan("# Plan: g\n1. make a | check: test -f a\n2. make b")
        stub = Stub([llm.Reply(text=None, calls=[_call("w", "write_file", {"path": "a", "content": "x"})], finish="tool_calls"),
                     say("wrote a"), say("made b"), say("all good")])
        answer = agent.run_plan(stub, self.messages, self.ctx, out=io.StringIO())
        self.assertEqual(answer, "all good")
        self.assertEqual([s.mark for s in p.steps], ["x", "x"])
        self.assertEqual(p.steps[0].result, "wrote a")
        self.assertEqual(p.state, "done")
        self.assertIn("[PLAN step 1 of 2]", stub.sent[0])
        self.assertIn("Do not start step 2", stub.sent[0])
        self.assertIn("[PLAN complete]", stub.sent[-1])
        self.assertIn("`test -f a` passed", stub.sent[-1])
        self.assertEqual(plan.load(self.ws).state, "done")

    def test_failed_check_goes_back_then_blocks_and_pauses(self):
        p = self.plan("# Plan: g\n1. make a | check: test -f a\n2. other")
        stub = Stub([say("done"), say("done again"), say("really done"), say("not a plan at all")])
        out = io.StringIO()
        self.ctx.guard.assume_yes = False
        answer = agent.run_plan(stub, self.messages, self.ctx, out=out)
        self.assertIn("[PLAN check failed]", stub.sent[1])
        self.assertEqual(p.steps[0].mark, "!")
        self.assertIn("Plan paused: step 1 is blocked", answer)  # the rewrite did not parse
        self.assertEqual(p.state, "draft")

    def test_blocked_step_is_replanned_in_fuck_it_mode(self):
        p = self.plan("# Plan: g\n1. make a | check: test -f a\n2. other")
        stub = Stub([say("done"), say("done"), say("done"),
                     say("- [ ] 1. write a differently\n- [ ] 2. other"), say("wrote it"), say("other done"), say("final")])
        answer = agent.run_plan(stub, self.messages, self.ctx, out=io.StringIO())
        self.assertEqual(answer, "final")
        self.assertEqual([s.text for s in p.steps], ["write a differently", "other"])
        self.assertEqual(p.replanned, [1])

    def test_check_that_cannot_run_is_dropped_not_retried(self):
        p = self.plan("# Plan: g\n1. a\n2. b")
        p.steps[0].check = "no-such-command-xyz"  # an older plan file, from before parse refused these
        stub = Stub([say("did a"), say("did b"), say("end")])
        out = io.StringIO()
        self.assertEqual(agent.run_plan(stub, self.messages, self.ctx, out=out), "end")
        self.assertIn("could not run (command not found), dropped", out.getvalue())
        self.assertEqual([s.mark for s in p.steps], ["x", "x"])
        self.assertEqual(len(p.replanned), 0)

    def test_manual_pauses_after_each_step_for_the_menu(self):
        p = self.plan("# Plan: g\n1. a\n2. b")
        p.mode = "manual"
        stub = Stub([say("did a"), say("did b"), say("end")])
        answer = agent.run_plan(stub, self.messages, self.ctx, out=io.StringIO())
        self.assertTrue(answer.startswith("Step 1 done: did a"))
        self.assertTrue(self.ctx.plan_ask)
        self.assertEqual(([s.mark for s in p.steps], p.state), (["x", " "], "draft"))
        self.ctx.plan_ask = False
        self.assertEqual(agent.run_plan(stub, self.messages, self.ctx, out=io.StringIO()), "end")  # last step: no pause
        self.assertFalse(self.ctx.plan_ask)
        self.assertEqual(p.state, "done")

    def test_plan_wrong_rewrites_and_asks_outside_fuck_it(self):
        p = self.plan("# Plan: g\n1. a\n2. b\n3. c")
        p.steps[0].mark = "x"
        self.ctx.guard.assume_yes = False
        stub = Stub([say("PLAN WRONG: b is already there"), say("- [ ] 1. c only")])
        answer = agent.run_plan(stub, self.messages, self.ctx, out=io.StringIO())
        self.assertIn("rewrote the rest of the plan", answer)
        self.assertIn("PLAN WRONG", stub.sent[0])  # the step message tells the model how
        self.assertTrue(self.ctx.plan_ask)
        self.assertEqual([(s.text, s.mark) for s in p.steps], [("a", "x"), ("c only", " ")])  # step 1 stays done

    def test_todo_write_hands_the_task_to_the_executor(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "build the thing"}]
        items = [{"text": "one"}, {"text": "two"}, {"text": "three"}]
        stub = Stub([llm.Reply(text=None, calls=[_call("t", "todo_write", {"items": items})], finish="tool_calls"),
                     say("did one"), say("did two"), say("did three"), say("report")])
        out = io.StringIO()
        self.assertEqual(agent.drive(stub, msgs, self.ctx, out=out), "report")
        self.assertIn("checklist: 3 steps (the model's own)", out.getvalue())
        self.assertEqual((self.ctx.plan.source, self.ctx.plan.goal, self.ctx.plan.state), ("model", "build the thing", "done"))

    def test_todo_write_refused_while_a_user_plan_is_in_charge_and_needs_three_steps(self):
        self.plan("# Plan: g\n1. a\n2. b")
        self.assertIn("user's plan is in charge", tools.todo_write(self.ctx, [{"text": "x"}] * 3))
        self.ctx.plan = None
        self.assertIn("3 or more", tools.todo_write(self.ctx, [{"text": "x"}]))

    def test_todo_write_revision_keeps_ticks_and_shows_dropped(self):
        p = self.plan("1. a\n2. b\n3. c")
        p.source, p.steps[0].mark = "model", "x"
        tools.todo_write(self.ctx, [{"text": "a"}, {"text": "c"}, {"text": "d"}])
        self.assertEqual([(s.text, s.mark) for s in self.ctx.plan.steps],
                         [("a", "x"), ("c", " "), ("d", " "), ("b (dropped by the model)", "-")])

    def test_auto_checklist_after_the_set_turn(self):
        self.ctx.auto_checklist = True
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "long job"}]
        steps = [llm.Reply(text=None, calls=[_call(f"c{i}", "run_shell", {"command": f"echo {i}"})], finish="tool_calls")
                 for i in range(agent.AUTO_CHECKLIST_AT)]
        stub = Stub(steps + [say("- [ ] 1. x\n- [ ] 2. y\n- [ ] 3. z"), say("x"), say("y"), say("z"), say("end")])
        self.assertEqual(agent.drive(stub, msgs, self.ctx, out=io.StringIO()), "end")
        self.assertIn(plan.CHECKLIST, stub.sent[agent.AUTO_CHECKLIST_AT])
        self.assertEqual(self.ctx.plan.state, "done")

    def test_auto_checklist_none_carries_on_quietly(self):
        self.ctx.auto_checklist = True
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "short job"}]
        steps = [llm.Reply(text=None, calls=[_call(f"c{i}", "run_shell", {"command": f"echo {i}"})], finish="tool_calls")
                 for i in range(agent.AUTO_CHECKLIST_AT)]
        out = io.StringIO()
        self.assertEqual(agent.drive(Stub(steps + [say("NONE"), say("end")]), msgs, self.ctx, out=out), "end")
        self.assertIsNone(self.ctx.plan)
        self.assertIn("not needed", out.getvalue())
        self.assertNotIn("none made", out.getvalue())

    def test_plus_message_adds_a_step(self):
        p = self.plan("1. a\n2. b")
        p.state = "running"
        with mock.patch.object(agent.ui, "steered", side_effect=[["+ also c"], [], [], [], []]):
            agent.run_plan(Stub([say("a"), say("b"), say("c"), say("end")]), self.messages, self.ctx, out=io.StringIO())
        self.assertEqual([s.text for s in p.steps], ["a", "b", "also c (you)"])
        self.assertTrue(all(s.mark == "x" for s in p.steps))


class Choose(unittest.TestCase):
    OPTIONS = [("y", "auto"), ("m", "manual"), ("e", "edit"), ("n", "no")]

    def pick(self, *typed):
        with mock.patch.object(agent.ui, "CHOOSE", None), mock.patch.object(agent.ui, "ASK", side_effect=list(typed)), \
                mock.patch("builtins.print"):
            return agent.ui.choose("Run it?", self.OPTIONS)

    def test_typed_fallback(self):
        self.assertEqual(self.pick(""), "y")  # Enter alone: the first
        self.assertEqual(self.pick("M"), "m")
        self.assertEqual(self.pick("zz", "edit"), "e")  # unknown asks again; a word counts by its first letter
        self.assertEqual(self.pick("/q"), "n")


class Research(unittest.TestCase):
    def ctx(self, ws, fuck_it):
        return tools.Ctx(workspace=ws, guard=safety.Guard(ws, assume_yes=fuck_it), researching=True)

    def test_manual_reads_freely_and_writes_ask_or_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            ctx = self.ctx(ws, False)
            self.assertEqual(tools.call(ctx, "write_file", json.dumps({"path": "a", "content": "x"})), tools.RESEARCH_ONLY)
            self.assertFalse((ws / "a").exists())
            with mock.patch.object(ctx.guard, "allow") as ask:
                self.assertIn("hello", tools.call(ctx, "run_shell", json.dumps({"command": "python3 -c 'print(\"hello\")'"})))
            ask.assert_not_called()  # a program that only looks runs unasked (option A)
            with mock.patch.object(ctx.guard, "allow", return_value=False) as ask:
                self.assertTrue(tools.call(ctx, "run_shell", json.dumps({"command": "touch b"})).startswith("DENIED"))
            self.assertTrue(ask.call_args.kwargs["always_ask"])
            self.assertFalse((ws / "b").exists())

    def test_fuck_it_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            ctx = self.ctx(ws, True)
            self.assertIn("wrote", tools.call(ctx, "write_file", json.dumps({"path": "a", "content": "x"})).lower())
            tools.call(ctx, "run_shell", json.dumps({"command": "touch b"}))
            self.assertTrue((ws / "a").exists() and (ws / "b").exists())

    def test_research_text_follows_the_mode(self):
        self.assertIn(plan.READ_ONLY_RULES, plan.research(False))
        self.assertIn(plan.WRITE_RULES, plan.research(True))

    def test_fuck_it_research_refuses_outside_writes_without_asking(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = self.ctx(Path(d), True)
            with mock.patch.object(ctx.guard, "allow") as ask:
                for cmd in ("touch /nonexistent-cracked-test/x", "sudo -n true"):
                    self.assertTrue(tools.call(ctx, "run_shell", json.dumps({"command": cmd})).startswith("DENIED"), cmd)
            ask.assert_not_called()
            with mock.patch.object(ctx.guard, "allow", return_value=False) as ask:  # deletes still ask
                (Path(d) / "f").write_text("x")
                tools.call(ctx, "delete_file", json.dumps({"path": "f"}))
            ask.assert_called_once()

    def test_investigate_mode_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            ctx = tools.Ctx(workspace=ws, guard=safety.Guard(ws))
            ctx.guard.investigate = True
            for name, args in [("write_file", {"path": "a", "content": "x"}), ("todo_write", {"items": ["a", "b", "c"]})]:
                self.assertEqual(tools.call(ctx, name, json.dumps(args)), tools.INVESTIGATE_ONLY, name)
            self.assertFalse((ws / "a").exists())
            with mock.patch.object(ctx.guard, "allow", return_value=False) as ask:
                self.assertIn("read only", tools.call(ctx, "run_shell", json.dumps({"command": "touch b"})))
            self.assertIn("investigate mode", ask.call_args.args[1])
            self.assertFalse((ws / "b").exists())
