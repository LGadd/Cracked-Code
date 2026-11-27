import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from cracked_code import safety


class Boundary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.guard = safety.Guard(self.ws)

    def tearDown(self):
        self._tmp.cleanup()

    def test_relative_resolves_inside(self):
        self.assertEqual(self.guard.resolve("a/b.txt"), self.ws / "a/b.txt")

    def test_dotdot_stays_inside_by_path(self):
        self.assertEqual(self.guard.resolve("a/../b.txt"), self.ws / "b.txt")

    def test_home_is_outside(self):
        self.assertTrue(self.guard.outside(self.guard.resolve("~/secrets.txt")))

    def test_root_file_is_outside(self):
        self.assertTrue(self.guard.outside(Path("/etc/passwd")))

    def test_workspace_itself_is_inside(self):
        self.assertFalse(self.guard.outside(self.ws))

    def test_sibling_folder_with_prefix_is_outside(self):
        self.assertTrue(self.guard.outside(Path(str(self.ws) + "-other/x")))


class UnsafeWorkspace(unittest.TestCase):
    def test_home_and_root_warned(self):
        self.assertIsNotNone(safety.home_warning(Path.home()))
        self.assertIsNotNone(safety.home_warning(Path("/")))

    def test_project_folder_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(safety.home_warning(Path(d)))


class DenyList(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.guard = safety.Guard(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def assertBlocked(self, cmd):
        self.assertIsNotNone(self.guard.shell_denied(cmd), f"expected blocked: {cmd}")

    def assertAllowed(self, cmd):
        self.assertIsNone(self.guard.shell_denied(cmd), f"expected allowed: {cmd}")

    def test_blocked(self):
        for cmd in ["rm -rf /", "rm -rf /*", "sudo rm -rf /", "rm -rf ~", "rm -rf ~/*",
                    "mkfs.ext4 /dev/sdb1", "dd if=/dev/zero of=/dev/sda bs=1M",
                    "echo x > /dev/sda", "shutdown now", "sudo reboot",
                    "chmod -R 777 /", "find / -name tmp -delete", ":(){ :|:& };:"]:
            self.assertBlocked(cmd)

    def test_allowed(self):
        for cmd in ["rm -rf ./build", "rm -rf /tmp/scratch/x", "rm file.txt",
                    "find . -name '*.pyc' -delete".replace(" -delete", " -name x"),
                    "nmap -sV 192.168.1.0/24", "nc -zv host 22", "curl -s https://x.dev",
                    "git status", "python3 -m unittest", "ls /etc", "cat ~/notes.md"]:
            self.assertAllowed(cmd)

    def test_find_delete_blocked(self):
        self.assertBlocked("find . -name '*.pyc' -delete")


class Approvals(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.guard = safety.Guard(self.ws)

    def tearDown(self):
        self._tmp.cleanup()

    def _answer(self, text):
        from unittest import mock
        patcher = mock.patch("builtins.input", lambda *a, **k: text)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_delete_asks_even_in_fuck_it_mode(self):
        guard = safety.Guard(self.ws, assume_yes=True)
        self._answer("")
        self.assertFalse(guard.allow("delete:/x", "delete /x", always_ask=True))
        self._answer("y")  # yes this once, but not remembered
        self.assertTrue(guard.allow("delete:/x", "delete /x", always_ask=True))
        self._answer("")
        self.assertFalse(guard.allow("delete:/x", "delete /x", always_ask=True))

    def test_yes(self):
        self._answer("y")
        self.assertTrue(self.guard.allow("delete:/x", "delete /x"))

    def test_default_no(self):
        self._answer("")
        self.assertFalse(self.guard.allow("delete:/x", "delete /x"))

    def test_always_remembered(self):
        self._answer("a")
        self.assertTrue(self.guard.allow("delete:/x", "delete /x"))
        self._answer("never asked again")
        self.assertTrue(self.guard.allow("delete:/x", "delete /x"))

    def test_menu_offers_no_always_for_deletes_and_starts_on_no(self):
        from unittest import mock
        with mock.patch.object(safety.ui, "choose", return_value="n") as menu:
            self.guard.allow("delete:/x", "delete /x", always_ask=True)
        options, kw = menu.call_args.args[1], menu.call_args.kwargs
        self.assertEqual([k for k, _ in options], ["y", "n"])  # no explain hook set: no e either
        self.assertEqual(kw["default"], "n")

    def test_explain_asks_the_model_then_the_menu_again(self):
        from unittest import mock
        self.guard.explain = mock.Mock(return_value="it fixes the bug")
        with mock.patch.object(safety.ui, "choose", side_effect=["e", "y"]) as menu, \
                mock.patch.object(safety.ui, "out") as shown:
            self.assertTrue(self.guard.allow("edit:/x", "edit /x"))
        self.guard.explain.assert_called_once_with("edit /x")
        self.assertIn("it fixes the bug", shown.call_args.args[0])
        self.assertEqual([k for k, _ in menu.call_args_list[0].args[1]], ["y", "a", "e", "n"])
        self.assertEqual([k for k, _ in menu.call_args_list[1].args[1]], ["y", "a", "n"])  # asked once

    def test_assume_yes(self):
        guard = safety.Guard(self.ws, assume_yes=True)
        self._answer("EOF")
        self.assertTrue(guard.allow("delete:/x", "delete /x"))


class Snapshot(unittest.TestCase):
    def test_creates_repo_and_commits(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.txt").write_text("one")
            sha, err = safety.snapshot(ws, "pre-turn 1")
            self.assertTrue(sha, f"snapshot failed: {err}")
            self.assertEqual(safety.snapshot(ws, "nothing changed"), (None, None))
            (ws / "a.txt").write_text("two")
            self.assertTrue(safety.snapshot(ws, "pre-turn 2")[0])
            log = safety.git(ws, "log", "--format=%s").stdout
            self.assertIn("pre-turn 1", log)
            self.assertIn("pre-turn 2", log)

    def test_workspace_size_skips_git(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.bin").write_bytes(b"x" * 1000)
            (ws / ".git").mkdir()
            (ws / ".git" / "big").write_bytes(b"x" * 5000)
            self.assertEqual(safety.workspace_size(ws), (1000, 1))

    def test_skipped_folders_are_not_walked(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "a.bin").write_bytes(b"x" * 1000)
            (ws / "web" / "node_modules").mkdir(parents=True)
            (ws / "web" / "node_modules" / "big").write_bytes(b"x" * 5000)
            self.assertEqual(safety.workspace_size(ws), (1000, 1))

    def test_nested_repo_without_commits_still_snapshots(self):
        # A scratch dir left behind mid-run is a git repo with nothing checked
        # out; git add refuses it and used to take the whole snapshot with it.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            subprocess.run(["git", "init", "-q", str(ws / "scratch")], capture_output=True)
            (ws / "a.txt").write_text("one")
            sha, err = safety.snapshot(ws, "pre-turn 1")
            self.assertTrue(sha, f"snapshot failed: {err}")
            self.assertIn("a.txt", safety.git(ws, "ls-files").stdout)

    def test_users_own_repo_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            subprocess.run(["git", "init", "-q", str(ws)], capture_output=True)
            (ws / "a.txt").write_text("one")
            self.assertTrue(safety.snapshot(ws, "pre-turn 1")[0])
            own = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True, text=True)
            self.assertNotEqual(own.returncode, 0)  # still no commits in the user's repo
            self.assertIn("a.txt", safety.git(ws, "ls-files").stdout)
            self.assertNotIn(".git/", safety.git(ws, "ls-files").stdout)

    def test_big_files_stay_out_even_after_growing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(safety, "MAX_MB", 1):
            ws = Path(d)
            (ws / "small.txt").write_text("x")
            (ws / "film [v2].bin").write_bytes(b"x" * 10)  # starts small, mid-render
            safety.snapshot(ws, "pre-turn 1")
            self.assertIn("film [v2].bin", safety.git(ws, "ls-files").stdout)
            (ws / "film [v2].bin").write_bytes(b"x" * (2 * 1024 * 1024))
            (ws / "model.gguf").write_bytes(b"x" * (2 * 1024 * 1024))
            safety.snapshot(ws, "pre-turn 2")
            tracked = safety.git(ws, "ls-files").stdout
            self.assertEqual(tracked.split(), ["small.txt"])
            self.assertEqual(safety.workspace_size(ws), (1, 1))

    def test_skip_patterns_stay_out(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(safety, "SKIP", ["*.mp4", "node_modules/"]):
            ws = Path(d)
            (ws / "render.mjs").write_text("x")
            (ws / "out").mkdir()
            (ws / "out" / "clip.mp4").write_bytes(b"x")
            (ws / "node_modules" / "pkg").mkdir(parents=True)
            (ws / "node_modules" / "pkg" / "i.js").write_text("x")
            self.assertTrue(safety.snapshot(ws, "pre-turn 1")[0])
            self.assertEqual(safety.git(ws, "ls-files").stdout.split(), ["render.mjs"])

    def test_workspace_size_skips_skip_patterns(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(safety, "SKIP", ["*.png", "node_modules/"]):
            ws = Path(d)
            (ws / "a.txt").write_text("x")
            (ws / "pics").mkdir()
            (ws / "pics" / "big.png").write_bytes(b"x" * 5000)
            self.assertEqual(safety.workspace_size(ws), (1, 1))

    def test_store_inside_the_workspace_is_not_snapshotted(self):
        # cracked in ~: the store (~/.local/share/cracked) is inside the workspace
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"XDG_DATA_HOME": f"{d}/.local/share"}):
            ws = Path(d)
            (ws / "a.txt").write_text("x")
            safety.snapshot(ws, "one")
            (ws / "a.txt").write_text("y")
            self.assertTrue(safety.snapshot(ws, "two")[0])
            self.assertEqual(safety.git(ws, "ls-files").stdout.split(), ["a.txt"])
            self.assertEqual(safety.workspace_size(ws), (1, 1))

    def test_off_switch(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(safety, "SNAPSHOT", False):
            ws = Path(d)
            (ws / "a.txt").write_text("one")
            self.assertEqual(safety.snapshot(ws, "pre-turn 1"), (None, None))
            self.assertIsNone(safety.head(ws))
            self.assertFalse(safety.store(ws).exists())

    def test_failure_reports_a_reason(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            subprocess.run(["git", "init", "-q", str(ws / "scratch")], capture_output=True)
            sha, err = safety.snapshot(ws, "pre-turn 1")
            self.assertIsNone(sha)
            self.assertTrue(err, "expected a reason, not an empty message")


class FuckItMode(unittest.TestCase):
    def test_shift_tab_at_an_approval_says_yes_and_stops_asking(self):
        import io
        from unittest import mock
        g = safety.Guard(Path("/tmp/ws"), out=io.StringIO())
        with mock.patch("builtins.input", return_value="/mode "):
            self.assertTrue(g.allow("delete:/tmp/ws/x", "delete x"))
        self.assertTrue(g.assume_yes)
        with mock.patch("builtins.input", side_effect=AssertionError("asked again")):
            self.assertTrue(g.allow("delete:/tmp/ws/y", "delete y"))


if __name__ == "__main__":
    unittest.main()


class Outside(unittest.TestCase):
    def test_writes_outside_the_workspace_are_named(self):
        g = safety.Guard(Path("/home/u/proj"))
        for cmd, hit in [("mkdir -p ~/.local/bin", ".local/bin"), ("printf x > /usr/local/bin/the", "/usr/local/bin/the"),
                         ("cp a.sh /etc/x", "/etc/x"), ("sudo -n touch /x", "sudo"), ("dd if=a of=/dev/sdc", "/dev/sdc"),
                         ("ls; touch $HOME/z", "z"), ("chmod +x /opt/t", "/opt/t")]:
            self.assertIn(hit, (g.writes_outside(cmd) or "").replace(os.sep, "/"), cmd)
        for cmd in ("echo a > out.txt", "cp /etc/hosts .", "ls 2>&1 >/dev/null", "cat x | tee /tmp/y",
                    "chmod +x audit.sh", "echo 'a->b'", "pseudo ls", "mkdir -p build/x"):
            self.assertIsNone(g.writes_outside(cmd), cmd)

    def test_asks_even_in_fuck_it_mode_and_always_sticks(self):
        import tempfile
        from cracked_code import tools
        with tempfile.TemporaryDirectory() as d:
            ctx = tools.Ctx(workspace=Path(d), guard=safety.Guard(Path(d), assume_yes=True))
            cmd = "touch /nonexistent-cracked-test/x"  # outside, and harmless if it ever ran
            with mock.patch.object(safety.ui, "choose", return_value="n") as asked:
                self.assertTrue(tools.run_shell(ctx, cmd).startswith("DENIED"))
            asked.assert_called_once()
            with mock.patch.object(safety.ui, "choose", return_value="a"):
                self.assertIn("No such file", tools.run_shell(ctx, cmd))
            with mock.patch.object(safety.ui, "choose") as again:
                tools.run_shell(ctx, cmd)
            again.assert_not_called()
            with mock.patch.object(safety.ui, "choose") as never:
                tools.run_shell(ctx, "touch inside.txt")
            never.assert_not_called()
            self.assertTrue((Path(d) / "inside.txt").exists())
