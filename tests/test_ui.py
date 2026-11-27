"""Terminal formatting: word-wrap and the answer/plan formatter."""

import re
import unittest
from unittest import mock

from cracked_code import plan, ui

PLAIN = lambda s: ui.CODES.sub("", s)


class Wrap(unittest.TestCase):
    def test_wide_characters_take_two_columns(self):
        self.assertEqual(ui.cells("ab漢字✅"), 8)
        self.assertEqual(ui.cut("漢字漢字", 5), "漢字")
        rows = ui.wrap("漢字漢字 漢字漢字 漢字漢字", 10).split("\n")
        self.assertEqual(rows, ["漢字漢字", "漢字漢字", "漢字漢字"])

    def test_words_are_never_cut(self):
        text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        rows = ui.wrap(text, 20).split("\n")
        self.assertTrue(all(len(r) <= 20 for r in rows))
        self.assertEqual(" ".join(rows).split(), text.split())

    def test_colour_codes_take_no_room_and_list_items_hang(self):
        line = ui.paint("12.", ui.AMBER, bold=True) + " " + "word " * 10
        rows = ui.wrap(line, 24).split("\n")
        self.assertTrue(all(len(PLAIN(r)) <= 24 for r in rows))
        self.assertTrue(all(PLAIN(r).startswith("    word") for r in rows[1:]))

    def test_short_lines_untouched(self):
        self.assertEqual(ui.wrap("a b\nc", 80), "a b\nc")


@mock.patch.object(ui, "ON", True)
class Pretty(unittest.TestCase):
    def test_headings_and_steps_get_gaps_and_colour(self):
        out = ui.pretty("## Plan\nSome intro.\n1. First thing\n2. Second thing\n- a bullet\n**Next steps:**\n3. Third")
        rows = [PLAIN(r) for r in out.split("\n")]
        self.assertEqual(rows, ["Plan", "Some intro.", "", "1. First thing", "", "2. Second thing", "• a bullet",
                                "", "Next steps:", "", "3. Third"])
        self.assertIn(ui.AMBER, out)

    def test_plan_reply_puts_files_and_check_on_their_own_lines(self):
        rows = [PLAIN(r) for r in ui.pretty("# Plan: x\n- [ ] 1. Add it | files: a.py | check: pytest -q").split("\n")]
        self.assertEqual(rows, ["Plan: x", "", "· 1. Add it", "     files: a.py", "     check: pytest -q"])

    def test_checklist_shows_results_under_steps(self):
        p = plan.Plan(goal="g", steps=[plan.Step(text="one", mark="x", result="did it"), plan.Step(text="two")])
        rows = [PLAIN(r) for r in ui.pretty(plan.checklist(p)).split("\n")]
        self.assertEqual(rows, ["✓ 1. one", "     → did it", "", "· 2. two"])

    def test_code_blocks_are_left_alone(self):
        rows = [PLAIN(r) for r in ui.pretty("```\n1. not a step\n# not a heading\n```").split("\n")]
        self.assertEqual(rows, ["```", "1. not a step", "# not a heading", "```"])

    def test_off_on_a_pipe(self):
        with mock.patch.object(ui, "ON", False):
            self.assertEqual(ui.pretty("## x\n1. y"), "## x\n1. y")


class Look(unittest.TestCase):
    """2026-11-27 polish: 256-colour fallback, themes, chips, the answer bar, tables, code colours, diff numbers."""
    def test_256_colour_fallback(self):
        self.assertEqual(ui._256("255;0;0"), 196)
        self.assertEqual(ui._256("128;128;128"), 244)  # a grey lands on the grey ramp
        with mock.patch.object(ui, "TRUE", False):
            self.assertEqual(ui.sgr("0;0;0"), "38;5;16")
        with mock.patch.object(ui, "TRUE", True):
            self.assertEqual(ui.sgr("1;2;3"), "38;2;1;2;3")

    def test_light_theme_swaps_the_palette(self):
        try:
            ui.set_theme("light")
            self.assertEqual(ui.NEON, ui.THEMES["light"][0])
            ui.set_theme("nonsense")
            self.assertEqual((ui.THEME, ui.NEON), ("dark", ui.THEMES["dark"][0]))
        finally:
            ui.set_theme("dark")

    def test_took_and_chip(self):
        self.assertEqual((ui.took(42.4), ui.took(185)), ("42s", "3m 5s"))
        self.assertEqual(ui.chip("ALLOW?", ui.AMBER), "[ALLOW?]")  # no colour: brackets
        with mock.patch.object(ui, "ON", True):
            self.assertIn("\033[1;7;", ui.chip("ALLOW?", ui.AMBER))

    @mock.patch.object(ui, "ON", True)
    def test_answer_has_a_bar_down_its_left_edge(self):
        rows = [PLAIN(r) for r in ui.answer("Done.\n\n- one").split("\n")]
        self.assertEqual(rows, ["│ Done.", "│", "│ • one"])

    @mock.patch.object(ui, "ON", True)
    def test_tables_line_up(self):
        rows = [PLAIN(r) for r in ui.pretty("| a | bb |\n|---|---|\n| ccc | `d` |\nafter").split("\n")]
        self.assertEqual(rows, ["a   │ bb", "────┼───", "ccc │ d", "after"])

    @mock.patch.object(ui, "ON", True)
    def test_code_blocks_get_syntax_colours(self):
        out = ui.pretty("```python\ndef f():\n    return 1\n```")
        self.assertEqual([PLAIN(r) for r in out.split("\n")], ["```python", "def f():", "    return 1", "```"])
        self.assertGreater(out.count("\033["), 6)  # pygments coloured the code

    def test_diff_lines_carry_their_numbers(self):
        d = "--- a/x\n+++ b/x\n@@ -10,3 +10,3 @@\n keep\n-old\n+new\n keep"
        rows = [PLAIN(r).rstrip() for r in ui.diff(d).split("\n")]
        self.assertEqual(rows[3:], ["   10  keep", "   11 -old", "   11 +new", "   12  keep"])
