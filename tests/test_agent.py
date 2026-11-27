from types import SimpleNamespace
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from cracked_code import agent, llm, safety, tools, ui


class StubLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, schemas):
        self.calls.append([m.get("role") for m in messages])
        return self.replies.pop(0)


def call(cid, name, arguments):
    return llm.Call(id=cid, name=name, arguments=json.dumps(arguments))


def ctx_for(ws):
    return tools.Ctx(workspace=ws, guard=safety.Guard(ws, assume_yes=True))


class Capturing:
    """Records a snapshot of the messages list (roles + last content) at each chat call."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.snapshots = []

    def chat(self, messages, schemas):
        self.snapshots.append([(m["role"], m.get("content")) for m in messages])
        return self.replies.pop(0)


class KeepOnTask(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.out = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    # --- the anchor: every ANCHOR_EVERY turns, kept in the history ---
    def test_anchor_is_rare_and_stays_in_the_history(self):
        reads = [llm.Reply(text="", calls=[call(f"c{i}", "run_shell", {"command": f"echo {i}"})], finish="tool_calls")
                 for i in range(agent.ANCHOR_EVERY)]
        stub = Capturing(reads + [llm.Reply(text="done", calls=[], finish="stop")])
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "compare the two files"}]
        c = ctx_for(self.ws)
        c.fold = False
        agent.run(stub, messages, c, out=self.out)
        anchored = [i for i, snap in enumerate(stub.snapshots) if "<system-reminder>" in str(snap[-1][1])]
        self.assertEqual(anchored, [agent.ANCHOR_EVERY])  # only the request of turn ANCHOR_EVERY + 1 ends with it
        self.assertIn("compare the two files", stub.snapshots[agent.ANCHOR_EVERY][-1][1])
        self.assertEqual(sum("<system-reminder>" in str(m.get("content")) for m in messages), 1)
        # every request only adds to the one before it: nothing the server has read is taken away
        for before, after in zip(stub.snapshots, stub.snapshots[1:]):
            self.assertEqual(after[:len(before)], before)

    def test_thinking_stays_with_the_reply_and_the_cap_follows_the_last_turn(self):
        class Budgets(StubLLM):
            reasoning_budget = 0
            def chat(self, messages, schemas):
                self.seen = getattr(self, "seen", []) + [self.reasoning_budget]
                return super().chat(messages, schemas)
        (self.ws / "a.txt").write_text("one\n")
        stub = Budgets([
            llm.Reply(text="", reasoning="plan", calls=[call("c1", "edit_file", {"path": "a.txt", "old_string": "one", "new_string": "two"})], finish="tool_calls"),
            llm.Reply(text="", calls=[call("c2", "edit_file", {"path": "a.txt", "old_string": "nope", "new_string": "x"})], finish="tool_calls"),
            llm.Reply(text="done", calls=[], finish="stop")])
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
        c = ctx_for(self.ws)
        c.fold, c.think_auto = False, True
        agent.run(stub, messages, c, out=self.out)
        # first turn: plan; after a clean edit: routine; after a failed edit: plan again
        self.assertEqual(stub.seen, [agent.THINK_PLAN, agent.THINK_ROUTINE, agent.THINK_PLAN])
        self.assertEqual(messages[2]["reasoning_content"], "plan")
        self.assertNotIn("reasoning_content", messages[4])

    def test_anchor_not_stranded_after_an_error(self):
        class Boom:
            def chat(self, *a):
                raise KeyboardInterrupt
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
        with self.assertRaises(KeyboardInterrupt):
            agent.run(Boom(), messages, ctx_for(self.ws), out=self.out)
        self.assertFalse(any("<system-reminder>" in str(m.get("content")) for m in messages))

    def test_anchor_off_appends_nothing(self):
        stub = Capturing([llm.Reply(text="done", calls=[], finish="stop")])
        ctx = tools.Ctx(workspace=self.ws, guard=safety.Guard(self.ws, assume_yes=True), anchor=False)
        agent.run(stub, [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}], ctx, out=self.out)
        self.assertFalse(any("system-reminder" in str(c) for _, c in stub.snapshots[0]))

    def test_anchor_shows_edit_histogram(self):
        touches = [("edit", "a/b/handover.md")] * 6 + [("edit", "attack_results.md")] * 3
        anchor = agent._anchor([{"role": "user", "content": "line one\nline two"}], 7, 9, touches)
        self.assertIn("Task: line one", anchor["content"])
        self.assertIn("handover.md x6", anchor["content"])
        self.assertIn("attack_results.md x3", anchor["content"])

    # --- non-LLM drift detection (pure function) ---
    def test_edit_loop_trips_but_append_writes_do_not(self):
        edits = [("edit", "x.md")] * agent.EDIT_LIMIT
        self.assertIn("edited x.md", agent._stuck([], 0, edits))
        # append=true writes are not edits
        appends = []
        for _ in range(agent.EDIT_LIMIT + 1):
            appends += agent._touch("write_file", json.dumps({"path": "x.md", "content": "c", "append": True}))
        self.assertEqual(agent._stuck([], 0, appends), "")

    def test_read_loop_trips(self):
        reads = [("read", "big.md")] * agent.READ_LIMIT
        self.assertIn("re-read big.md", agent._stuck([], 0, reads))

    def test_chanting_trips_on_near_duplicates(self):
        line = "correcting my earlier note about the advisory count which was wrong"
        variant = "correcting my earlier note about the advisory count which was incorrect"
        self.assertIn("repeating yourself", agent._soft([line, variant, line], 0))
        self.assertEqual(agent._soft(["short one", "totally different words here now"], 0), "")

    def test_stall_trips_after_readonly_turns(self):
        self.assertIn("only reading and no answer", agent._soft([], agent.STALL_LIMIT))
        self.assertEqual(agent._soft([], agent.STALL_LIMIT - 1), "")

    def test_edit_loop_fires_through_the_loop(self):
        # Four edits to one file across turns, then it answers: the loop detects and nudges.
        replies = [llm.Reply(text=None, calls=[call(f"e{i}", "edit_file",
                   {"path": "x.md", "old_string": f"a{i}", "new_string": f"b{i}"})], finish="tool_calls")
                   for i in range(agent.EDIT_LIMIT)]
        replies.append(llm.Reply(text="ok", calls=[], finish="stop"))
        (self.ws / "x.md").write_text("".join(f"a{i}" for i in range(agent.EDIT_LIMIT)))
        stub = StubLLM(replies)
        agent.run(stub, [{"role": "system", "content": "s"}, {"role": "user", "content": "tidy x.md"}],
                  ctx_for(self.ws), out=self.out)
        self.assertIn("loop check", self.out.getvalue())

    # --- compaction preserves the task ---
    def test_compact_keeps_original_and_latest_task(self):
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "ORIGINAL TASK"},
                    {"role": "assistant", "content": "working"},
                    {"role": "user", "content": "LATEST TASK"},
                    {"role": "assistant", "content": "working2"},
                    {"role": "assistant", "content": "working3"}]
        agent.compact(StubLLM([llm.Reply(text="summary", calls=[], finish="stop")]), messages)
        note = messages[1]["content"]
        self.assertIn("ORIGINAL TASK", note)
        self.assertIn("LATEST TASK", note)


class Loop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.ctx = ctx_for(self.ws)
        self.out = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    def test_parallel_calls_each_get_one_tool_reply(self):
        stub = StubLLM([
            llm.Reply(text=None, calls=[call("c1", "write_file", {"path": "a.txt", "content": "1"}),
                                        call("c2", "write_file", {"path": "b.txt", "content": "2"})],
                      finish="tool_calls"),
            llm.Reply(text="both written", calls=[], finish="stop"),
        ])
        messages = [{"role": "user", "content": "write two files"}]
        answer = agent.run(stub, messages, self.ctx, out=self.out)

        self.assertEqual(answer, "both written")
        tools_msgs = [m for m in messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools_msgs], ["c1", "c2"])
        self.assertTrue((self.ws / "a.txt").exists() and (self.ws / "b.txt").exists())
        assistant = [m for m in messages if m["role"] == "assistant"][0]
        self.assertEqual([c["id"] for c in assistant["tool_calls"]], ["c1", "c2"])

    def test_ctrl_b_hands_the_turn_back_and_skips_the_rest_of_the_batch(self):
        def backgrounded(ctx, name, arguments):
            ctx.backgrounded = 4
            return tools.BACKGROUNDED.format(n=4)
        stub = StubLLM([llm.Reply(text="", calls=[call("a", "run_shell", {"command": "make"}),
                                                  call("b", "run_shell", {"command": "make test"})], finish="tool_calls")])
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "build it"}]
        c = ctx_for(self.ws)
        with mock.patch.object(tools, "call", side_effect=backgrounded) as called:
            answer = agent.run(stub, messages, c, out=self.out)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(answer, agent.HANDED_BACK.format(n=4))
        self.assertEqual([m["role"] for m in messages[-3:]], ["tool", "tool", "assistant"])
        self.assertEqual(messages[-2]["content"], agent.NOT_RUN)
        self.assertIsNone(c.backgrounded)

    def test_investigate_mode_switched_on_mid_task_applies_to_this_task(self):
        def toggles(ctx, name, arguments):
            ctx.guard.investigate = True  # Shift+Tab while the call ran
            return "[exit 0]"
        stub = StubLLM([llm.Reply(text="", calls=[call("a", "run_shell", {"command": "ls"})], finish="tool_calls"),
                        llm.Reply(text="found it", calls=[], finish="stop")])
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "tidy the repo"}]
        self.ctx.fold = False
        with mock.patch.object(tools, "call", side_effect=toggles):
            agent.run(stub, messages, self.ctx, out=self.out)
        self.assertEqual([m["content"] for m in messages if m["content"] == agent.INVESTIGATE], [agent.INVESTIGATE])  # once
        self.assertEqual(agent._task_line(messages), "tidy the repo")  # the rule is not mistaken for the task

    def test_anchor_repeats_the_investigate_rule_only_in_investigate_mode(self):
        messages = [{"role": "user", "content": "tidy the repo"}]
        self.assertIn(agent.INVESTIGATE_ANCHOR, agent._anchor(messages, 11, 3, [], investigate=True)["content"])
        self.assertNotIn(agent.INVESTIGATE_ANCHOR, agent._anchor(messages, 11, 3, [])["content"])

    def test_explain_answers_pending_calls_and_keeps_nothing(self):
        messages = [{"role": "user", "content": "fix it"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                        {"id": "b", "type": "function", "function": {"name": "edit_file", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "a", "content": "text"}]
        before = [dict(m) for m in messages]
        sent = []
        stub = SimpleNamespace(chat=lambda m, t: sent.extend(m) or llm.Reply(text="The bug is on line 3.", calls=[], finish="stop"))
        self.assertEqual(agent.explain(stub, messages, [], "edit x.py"), "The bug is on line 3.")
        self.assertEqual(messages, before)
        self.assertEqual([m.get("tool_call_id") for m in sent if m["role"] == "tool"], ["a", "b"])  # b got a placeholder
        self.assertIn("edit x.py", sent[-1]["content"])

    def test_stops_when_no_calls(self):
        stub = StubLLM([llm.Reply(text="done", calls=[], finish="stop")])
        messages = [{"role": "user", "content": "hi"}]
        self.assertEqual(agent.run(stub, messages, self.ctx, out=self.out), "done")
        self.assertEqual(len(stub.calls), 1)

    def test_message_sent_mid_task_is_read_before_the_next_step(self):
        fake = mock.MagicMock(queue=[], lock=threading.RLock())
        stub = Capturing([llm.Reply(text="done", calls=[], finish="stop"),
                          llm.Reply(text="done, with tabs", calls=[], finish="stop")])
        chat = stub.chat
        stub.chat = lambda m, s: (fake.queue.append("use tabs") if not stub.snapshots else None, chat(m, s))[1]
        messages = [{"role": "user", "content": "indent it"}]
        with mock.patch.object(ui, "LIVE", fake):
            answer = agent.run(stub, messages, self.ctx, out=self.out)
        self.assertEqual(answer, "done, with tabs")  # it did not stop at "done": a message was waiting
        self.assertIn(("user", agent.STEERED.format(text="use tabs")), stub.snapshots[1])
        self.assertEqual(fake.queue, [])

    def test_tool_failure_comes_back_as_text(self):
        stub = StubLLM([
            llm.Reply(text=None, calls=[call("c1", "read_file", {"path": "missing.txt"})], finish="tool_calls"),
            llm.Reply(text="it is missing", calls=[], finish="stop"),
        ])
        messages = [{"role": "user", "content": "read it"}]
        agent.run(stub, messages, self.ctx, out=self.out)
        tool_msg = [m for m in messages if m["role"] == "tool"][0]
        self.assertTrue(tool_msg["content"].startswith("ERROR:"))

    def test_error_then_recovery_call(self):
        stub = StubLLM([
            llm.Reply(text=None, calls=[call("c1", "read_file", {"path": "missing.txt"})], finish="tool_calls"),
            llm.Reply(text=None, calls=[call("c2", "run_shell", {"command": "ls"})], finish="tool_calls"),
            llm.Reply(text="empty dir", calls=[], finish="stop"),
        ])
        messages = [{"role": "user", "content": "read it"}]
        self.assertEqual(agent.run(stub, messages, self.ctx, out=self.out), "empty dir")
        self.assertEqual(len([m for m in messages if m["role"] == "tool"]), 2)

    def test_max_turns_stops_the_loop(self):
        replies = [llm.Reply(text=None, calls=[call(f"c{i}", "run_shell", {"command": "true"})],
                             finish="tool_calls") for i in range(5)]
        stub = StubLLM(replies)
        messages = [{"role": "user", "content": "loop"}]
        agent.run(stub, messages, self.ctx, max_turns=2, out=self.out)
        self.assertEqual(len(stub.calls), 3)  # 2 turns + the forced final report
        self.assertIn("max_turns", self.out.getvalue())

    def test_no_turn_limit_by_default(self):
        # The old default of 40 cut a real build-and-host task off half way (2026-11-03).
        replies = [llm.Reply(text=None, calls=[call(f"c{i}", "run_shell", {"command": f"echo {i}"})],
                             finish="tool_calls") for i in range(45)]
        stub = StubLLM(replies + [llm.Reply(text="done", calls=[], finish="stop")])
        messages = [{"role": "user", "content": "long task"}]
        self.assertEqual(agent.run(stub, messages, self.ctx, out=self.out), "done")
        self.assertEqual(len(stub.calls), 46)
        self.assertNotIn("out of turns", self.out.getvalue())

    def test_text_sent_with_calls_is_shown_as_narration(self):
        stub = StubLLM([llm.Reply(text="Checking the tests first.", calls=[call("c1", "run_shell", {"command": "true"})],
                                  finish="tool_calls"),
                        llm.Reply(text="done", calls=[], finish="stop")])
        agent.run(stub, [{"role": "user", "content": "x"}], self.ctx, out=self.out)
        self.assertIn(". >_ Checking the tests first.", self.out.getvalue())

    def test_out_of_turns_still_returns_a_report(self):
        stub = StubLLM([llm.Reply(text=None, calls=[call("c1", "run_shell", {"command": "true"})], finish="tool_calls"),
                        llm.Reply(text="found X, Y unfinished", calls=[], finish="stop")])
        messages = [{"role": "user", "content": "go"}]
        self.assertEqual(agent.run(stub, messages, self.ctx, max_turns=1, out=self.out), "found X, Y unfinished")
        self.assertIn("Do not call tools", messages[-2]["content"])

    def test_repeated_call_is_nudged_then_stopped(self):
        same = lambda i: llm.Reply(text=None, calls=[call(f"c{i}", "run_shell", {"command": "ls  /nope"})],
                                   finish="tool_calls")
        stub = StubLLM([same(i) for i in range(6)] + [llm.Reply(text="stuck on /nope", calls=[], finish="stop")])
        messages = [{"role": "user", "content": "go"}]
        answer = agent.run(stub, messages, self.ctx, out=self.out)
        nudges = [m for m in messages if m["role"] == "user" and m["content"].startswith("Loop check")]
        self.assertEqual(len(nudges), 1)
        self.assertEqual(answer, "stuck on /nope")  # assume_yes: nobody to ask, so stop + report
        self.assertEqual(len(stub.calls), 7)

    def test_failure_streak_trips_with_varied_args(self):
        bad = lambda i: llm.Reply(text=None, calls=[call(f"c{i}", "read_file", {"path": f"m{i}.txt"})],
                                  finish="tool_calls")
        stub = StubLLM([bad(i) for i in range(4)] + [llm.Reply(text="ok", calls=[], finish="stop")])
        messages = [{"role": "user", "content": "go"}]
        agent.run(stub, messages, self.ctx, out=self.out)
        self.assertIn("4 failed tool calls in a row", self.out.getvalue())

    def test_reading_after_an_edit_is_not_a_loop(self):
        r = lambda i: call(f"r{i}", "read_file", {"path": "a.txt"})
        stub = StubLLM([llm.Reply(text=None, calls=[call("w", "write_file", {"path": "a.txt", "content": "1"})], finish="tool_calls"),
                        llm.Reply(text=None, calls=[r(1)], finish="tool_calls"),
                        llm.Reply(text=None, calls=[r(2)], finish="tool_calls"),
                        llm.Reply(text="done", calls=[], finish="stop")])
        agent.run(stub, [{"role": "user", "content": "go"}], self.ctx, out=self.out)
        self.assertNotIn("loop check", self.out.getvalue())

    def test_truncated_reply_is_reported_and_retried(self):
        stub = StubLLM([llm.Reply(text="cut off", calls=[], finish="length", completion_tokens=1500),
                        llm.Reply(text="done", calls=[], finish="stop")])
        messages = [{"role": "user", "content": "x"}]
        self.assertEqual(agent.run(stub, messages, self.ctx, out=self.out), "done")
        self.assertIn("truncated", self.out.getvalue())
        self.assertIn("1500 tokens", messages[2]["content"])

    def test_snapshot_before_turn(self):
        (self.ws / "seed.txt").write_text("one")
        stub = StubLLM([llm.Reply(text="ok", calls=[], finish="stop")])
        agent.run(stub, [{"role": "user", "content": "x"}], self.ctx, out=self.out)
        self.assertTrue(safety.store(self.ws).exists())
        self.assertFalse((self.ws / ".git").exists())
        self.assertIn("snapshot", self.out.getvalue())

    def test_read_only_turns_and_a_fresh_repl_snapshot_are_not_snapshotted_again(self):
        (self.ws / "seed.txt").write_text("one")
        stub = StubLLM([llm.Reply(text=None, calls=[call("c1", "read_file", {"path": "seed.txt"})], finish="tool_calls"),
                        llm.Reply(text=None, calls=[call("c2", "write_file", {"path": "b.txt", "content": "b"})], finish="tool_calls"),
                        llm.Reply(text="done", calls=[], finish="stop")])
        self.ctx.snapped = True  # as repl.run_task leaves it
        with mock.patch.object(safety, "snapshot", return_value=(None, None)) as snap:
            agent.run(stub, [{"role": "user", "content": "x"}], self.ctx, out=self.out)
        # turn 1: the REPL just did it; turn 2: after a read; turn 3: after a write
        self.assertEqual([c.args[1] for c in snap.call_args_list], ["harness: pre-turn 3"])
        self.assertFalse(self.ctx.snapped)

    def test_compacts_old_turns_into_a_summary(self):
        step = lambda i: llm.Reply(text=None, calls=[call(f"c{i}", "run_shell", {"command": f"echo {i}"})],
                                   finish="tool_calls", prompt_tokens=100)
        stub = StubLLM([step(1), step(2), step(3), llm.Reply(text="- did 1 and 2", calls=[], finish="stop"),
                        llm.Reply(text="done", calls=[], finish="stop")])
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "the task"}]
        # only turn 3 crosses 75% of 1000 with 3 assistant turns present
        stub.replies[2].prompt_tokens = 900
        self.assertEqual(agent.run(stub, messages, self.ctx, context=1000, out=self.out), "done")
        self.assertIn("compacted", self.out.getvalue())
        self.assertEqual(messages[1]["role"], "user")
        self.assertTrue(messages[1]["content"].startswith("the task"))
        self.assertIn("- did 1 and 2", messages[1]["content"])
        self.assertEqual(messages[2]["role"], "assistant")  # tail starts on a turn boundary
        self.assertEqual(sum(m["role"] == "assistant" for m in messages), 3)  # 2 kept + final answer

    def test_cut_off_call_is_not_run(self):
        cut = llm.Reply(text=None, calls=[llm.Call("c1", "write_file", '{"path": "big.html", "content": "<html')],
                        finish="length", completion_tokens=16384)
        stub = StubLLM([cut, llm.Reply(text="split it", calls=[], finish="stop")])
        messages = [{"role": "user", "content": "big page"}]
        agent.run(stub, messages, self.ctx, out=self.out)
        tool_msg = [m for m in messages if m["role"] == "tool"][0]
        self.assertIn("16384 tokens", tool_msg["content"])
        self.assertIn("append=true", tool_msg["content"])
        self.assertFalse((self.ws / "big.html").exists())

    def test_ctx_percent_is_shown(self):
        stub = StubLLM([llm.Reply(text="ok", calls=[], finish="stop", prompt_tokens=500, completion_tokens=12)])
        agent.run(stub, [{"role": "user", "content": "x"}], self.ctx, context=1000, out=self.out)
        self.assertIn("ctx 50%", self.out.getvalue())  # prompt only: reasoning is never kept


class Transcript(unittest.TestCase):
    """Ctrl+O replays the whole session in the other view, so every line keeps its kind."""

    def test_replay_shows_each_kind_in_its_view(self):
        from unittest import mock
        from cracked_code import ui
        with mock.patch.object(ui, "ON", True), mock.patch.object(ui, "TRANSCRIPT", []), \
                mock.patch.object(ui, "EXPANDED", False), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ui.out("task", "all")
            ui.out("reasoning", "detail")
            ui.out("one-line call", "compact")
            self.assertEqual(out.getvalue(), "task\none-line call\n")
            ui.EXPANDED = True
            ui.redraw()
            replay = out.getvalue().split("\033[3J")[-1]
            self.assertEqual(replay, "task\nreasoning\n")


class Reasoning(unittest.TestCase):
    def reply(self, msg):
        from unittest import mock
        body = io.BytesIO(json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}]}).encode())
        with mock.patch("urllib.request.urlopen", return_value=body):
            return llm.LLM("http://x/v1", "m").chat([], [])

    def test_reads_strata_field_then_ollama_field(self):
        self.assertEqual(self.reply({"content": "a", "reasoning_content": "strata"}).reasoning, "strata")
        self.assertEqual(self.reply({"content": "a", "reasoning": "ollama"}).reasoning, "ollama")
        self.assertEqual(self.reply({"content": "a", "reasoning_content": "s", "reasoning": "o"}).reasoning, "s")
        self.assertEqual(self.reply({"content": "a"}).reasoning, "")


class UpFront(unittest.TestCase):
    def test_small_workspace_is_shown_whole_and_a_big_one_not_at_all(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.py").write_text("x = 1\n")
            (ws / "sub").mkdir()
            (ws / "sub" / "b.txt").write_text("hello\n")
            shown = agent.workspace_files(ws)
            self.assertIn("### a.py\n```\nx = 1\n```", shown)
            self.assertIn("### sub/b.txt", shown)
            (ws / "big.py").write_text("y = 2\n" * 2000)
            self.assertEqual(agent.workspace_files(ws), "")


if __name__ == "__main__":
    unittest.main()


class Runaway(unittest.TestCase):
    def test_a_repeating_reply_is_dropped_and_the_model_told(self):
        with tempfile.TemporaryDirectory() as d:
            stub = StubLLM([llm.Reply(text="again " * 300, calls=[], finish="repeat", reasoning="loop " * 300),
                            llm.Reply(text="Done.", calls=[], finish="stop")])
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "do it"}]
            self.assertEqual(agent.run(stub, messages, ctx_for(Path(d)), out=io.StringIO()), "Done.")
            self.assertEqual(messages[2], {"role": "assistant", "content": agent.REPEATED_REPLY})  # the loop is not kept
            self.assertEqual(messages[3], {"role": "user", "content": agent.REPEATING})
            self.assertEqual(agent._task_index(messages), 1)  # the harness's line is not taken for a task


class Fold(unittest.TestCase):
    def test_finished_task_folds_to_ask_and_answer(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.txt").write_text("hi")
            c = ctx_for(ws)
            c.fold = True
            stub = StubLLM([llm.Reply(text=None, calls=[call("w", "write_file", {"path": "b.txt", "content": "x"})], finish="tool_calls"),
                            llm.Reply(text="Wrote b.txt.", calls=[], finish="stop")])
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "make b.txt"}]
            agent.run(stub, messages, c, out=io.StringIO())
            self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant"])
            self.assertIn("[harness: this task changed b.txt]", messages[-1]["content"])

    def test_small_history_is_not_folded(self):
        with tempfile.TemporaryDirectory() as d:
            c = ctx_for(Path(d))
            c.fold, c.fold_above = True, 1000
            turns = lambda n: StubLLM([  # noqa: E731
                llm.Reply(text=None, calls=[call("w", "write_file", {"path": "b.txt", "content": "x"})], finish="tool_calls", prompt_tokens=n),
                llm.Reply(text="Wrote b.txt.", calls=[], finish="stop", prompt_tokens=n)])
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "make b.txt"}]
            agent.run(turns(999), messages, c, out=io.StringIO())
            self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "assistant"])  # kept whole
            messages.append({"role": "user", "content": "again"})
            agent.run(turns(1000), messages, c, out=io.StringIO())
            self.assertEqual([m["role"] for m in messages[5:]], ["user", "assistant"])  # at the line: this task folds

    def test_fold_keeps_touched_files_and_only_the_newest_copy(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.txt").write_text("alpha")
            (ws / "big.txt").write_text("x" * 500)
            c = ctx_for(ws)
            c.fold, c.fold_keep = True, 100
            task = lambda: StubLLM([  # noqa: E731
                llm.Reply(text=None, calls=[call("r", "read_file", {"path": "a.txt"}), call("r2", "read_file", {"path": "big.txt"}),
                                            call("w", "write_file", {"path": "b.txt", "content": "beta"})], finish="tool_calls"),
                llm.Reply(text="Done.", calls=[], finish="stop")])
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "one"}]
            agent.run(task(), messages, c, out=io.StringIO())
            first = messages[-1]["content"]
            self.assertIn("--- b.txt (1 lines)\nbeta", first)       # changed file first
            self.assertIn("--- a.txt (1 lines)\nalpha", first)      # read file too
            self.assertNotIn("big.txt (", first)                    # over the cap: left out
            self.assertLess(first.index("b.txt ("), first.index("a.txt ("))
            messages.append({"role": "user", "content": "two"})
            agent.run(task(), messages, c, out=io.StringIO())
            self.assertNotIn(agent.KEPT, messages[2]["content"])    # the older fold lost its copies
            self.assertIn(agent.KEPT, messages[-1]["content"])


class Instructions(unittest.TestCase):
    def test_global_then_project_with_agents_fallback(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as cfg:
            ws, cfg = Path(ws), Path(cfg)
            self.assertEqual(agent.instructions(ws, cfg), ("", []))
            (ws / "AGENTS.md").write_text("agents rule")
            self.assertIn("agents rule", agent.instructions(ws, cfg)[0])
            (ws / "CRACKED.md").write_text("project rule")
            (cfg / "CRACKED.md").write_text("global rule")
            text, names = agent.instructions(ws, cfg)
            self.assertNotIn("agents rule", text)  # CRACKED.md replaces AGENTS.md
            self.assertLess(text.index("global rule"), text.index("project rule"))  # project wins a conflict
            self.assertEqual(len(names), 2)

    def test_huge_file_is_cut(self):
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "CRACKED.md").write_text("x" * (agent.INSTRUCTIONS_MAX + 500))
            text, _ = agent.instructions(Path(ws), Path(ws) / "none")
            self.assertIn("[cut:", text)
            self.assertLess(len(text), agent.INSTRUCTIONS_MAX + 200)


class StrataFixes(unittest.TestCase):
    """2026-11-10: what a real server does wrong, and what Strata's prompt cache needs."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.ctx = ctx_for(self.ws)
        self.out = io.StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_reply_is_asked_again_then_said_out_loud(self):
        empty = lambda: llm.Reply(text="", calls=[], finish="stop")  # noqa: E731
        messages = [{"role": "user", "content": "x"}]
        self.assertEqual(agent.run(StubLLM([empty(), llm.Reply(text="ok", calls=[], finish="stop")]), messages, self.ctx, out=self.out), "ok")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])  # the empty one left no trace
        self.assertEqual(agent.run(StubLLM([empty(), empty()]), [{"role": "user", "content": "x"}], self.ctx, out=self.out), agent.EMPTY)

    def test_broken_arguments_are_not_kept_in_the_history(self):
        bad = llm.Reply(text=None, calls=[llm.Call("c1", "run_shell", '{"command": "ls')], finish="tool_calls")
        messages = [{"role": "user", "content": "x"}]
        agent.run(StubLLM([bad, llm.Reply(text="ok", calls=[], finish="stop")]), messages, self.ctx, out=self.out)
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertIn("not valid JSON", messages[2]["content"])  # the model is still told what went wrong

    def test_text_only_requests_keep_the_tool_list_and_drop_calls(self):
        seen = []

        class Model:
            def chat(self, messages, schemas):
                seen.append(schemas)
                return llm.Reply(text="summary", calls=[llm.Call("c", "run_shell", "{}")], finish="tool_calls")
        toolset = tools.schemas()
        reply = agent.text_only(Model(), [], toolset)
        self.assertEqual((seen, reply.calls, reply.text), ([toolset], [], "summary"))

    def test_server_failure_is_not_blamed_on_the_user(self):
        class Down:
            def chat(self, *a):
                raise llm.ServerError("HTTP 500: out of memory")
        messages = [{"role": "user", "content": "x"}]
        with self.assertRaises(OSError):
            agent.run(Down(), messages, self.ctx, out=self.out)
        self.assertEqual(messages[-1]["content"], agent.FAILED_REPLY)

    def test_compact_reads_image_tasks_and_skips_harness_lines(self):
        turn = lambda i: [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}", "function": {"name": "run_shell", "arguments": "{}"}}]},  # noqa: E731
                          {"role": "tool", "tool_call_id": f"c{i}", "content": "out"}]
        messages = [{"role": "system", "content": "s"},
                    {"role": "user", "content": [{"type": "text", "text": "describe the shot"}, {"type": "image_url", "image_url": {"url": "data:"}}]},
                    *turn(1), {"role": "user", "content": "Loop check: same call 3x"}, *turn(2), *turn(3)]
        agent.compact(StubLLM([llm.Reply(text="- looked", calls=[], finish="stop")]), messages)
        self.assertTrue(messages[1]["content"].startswith("describe the shot"))
        self.assertNotIn("Loop check", messages[1]["content"])

    def test_turn_line_shows_strata_cache_and_speed(self):
        self.assertEqual(agent._speed({"prompt_n": 310, "cache_n": 9000, "predicted_per_second": 94.6}), " / read 310 new, 9000 cached / 95 tok/s")
        self.assertEqual(agent._speed({}), "")


class LockedIn(unittest.TestCase):
    """LOCKED IN mode: every ask, failure and changed file survives compaction; no fold, no routine thinking cap."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.ctx = ctx_for(self.ws)
        self.ctx.locked, self.ctx.fold = True, True

    def tearDown(self):
        self._tmp.cleanup()

    def test_compaction_keeps_every_ask_and_failure(self):
        fail = llm.Reply(text=None, calls=[call("c1", "read_file", {"path": "missing.txt"})], finish="tool_calls", prompt_tokens=100)
        step = lambda i, t: llm.Reply(text=None, calls=[call(f"s{i}", "run_shell", {"command": f"echo {i}"})],
                                      finish="tool_calls", prompt_tokens=t)
        stub = StubLLM([fail, step(2, 100), step(3, 800), llm.Reply(text="- summary", calls=[], finish="stop"),
                        llm.Reply(text="done", calls=[], finish="stop")])
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "THE TASK"}]
        with mock.patch.object(ui, "steered", side_effect=[[], ["QUEUED ONE"], ["QUEUED TWO"], [], []]):
            self.assertEqual(agent.run(stub, messages, self.ctx, context=1000, compact_at=0.4, out=io.StringIO()), "done")
        note = messages[1]["content"]
        for text in ("THE TASK", "QUEUED ONE", "QUEUED TWO", "missing.txt", "- summary"):
            self.assertIn(text, note)
        self.assertTrue(note.startswith(agent.LEDGER))
        self.assertIn(agent.LEDGER, messages[-2]["content"])  # the ledger is restated on the turn after compacting
        self.assertGreater(len(messages), 3)  # and the finished task was not folded

    def test_compacts_later_than_the_default(self):
        steps = [llm.Reply(text=None, calls=[call(f"s{i}", "run_shell", {"command": "true"})], finish="tool_calls",
                           prompt_tokens=600) for i in range(3)]
        stub = StubLLM(steps + [llm.Reply(text="done", calls=[], finish="stop")])
        out = io.StringIO()
        agent.run(stub, [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}], self.ctx,
                  context=1000, compact_at=0.4, out=out)
        self.assertNotIn("compacting", out.getvalue())  # 60% < LOCKED_COMPACT_AT

    def test_no_routine_thinking_cap(self):
        self.ctx.think_auto = True
        stub = StubLLM([llm.Reply(text=None, calls=[call("s", "run_shell", {"command": "true"})], finish="tool_calls"),
                        llm.Reply(text="done", calls=[], finish="stop")])
        agent.run(stub, [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}], self.ctx, out=io.StringIO())
        self.assertEqual(stub.reasoning_budget, agent.THINK_PLAN)

    def test_running_session_follows_the_config_switch(self):
        cfg = self.ws / "config.toml"
        cfg.write_text("locked_in = false\n")
        self.ctx.locked, self.ctx.config_mtime = False, cfg.stat().st_mtime
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"HARNESS_CONFIG": str(cfg)}):
            agent._follow_config(self.ctx, lambda s: out.write(s))
            self.assertFalse(self.ctx.locked)  # unchanged file: not re-read
            cfg.write_text("locked_in = true\n")
            os.utime(cfg, (1, 1))  # a different mtime even on coarse clocks
            agent._follow_config(self.ctx, lambda s: out.write(s))
        self.assertTrue(self.ctx.locked)
        self.assertIn("LOCKED IN mode on", out.getvalue())

    def test_running_session_follows_the_telegram_switch(self):
        cfg = self.ws / "config.toml"
        cfg.write_text("telegram = false\n")
        self.ctx.config_mtime = 0.0
        with mock.patch.dict("os.environ", {"HARNESS_CONFIG": str(cfg)}), mock.patch.object(ui, "TELEGRAM", None), \
                mock.patch.object(ui, "telegram_load", return_value=("t", "c")):
            agent._follow_config(self.ctx, lambda s: None)
            self.assertIsNone(ui.TELEGRAM)
            cfg.write_text("telegram = true\n")
            os.utime(cfg, (1, 1))
            agent._follow_config(self.ctx, lambda s: None)
            self.assertEqual(ui.TELEGRAM, ("t", "c"))


class TaskIndex(unittest.TestCase):
    def test_image_task_is_a_task(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "old task"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": [{"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": "data:"}}]}]
        self.assertEqual(agent._task_index(msgs), 3)
        self.assertEqual(agent._task_line(msgs), "what is this?")

    def test_signal_exit_is_a_failure(self):
        self.assertTrue(agent._failed("[exit -11]\nSegmentation fault"))
        self.assertTrue(agent._failed("[exit 2]"))
        self.assertFalse(agent._failed("[exit 0]"))
