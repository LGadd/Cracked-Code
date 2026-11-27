"""Config file: precedence, the writer, and what the server fills in."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cracked_code import __main__ as cli, config
from tests import stubserver as stub


DEAD = "http://127.0.0.1:9/v1"  # nothing listens: the tests must never find a real model server


class Config(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file = Path(self._tmp.name) / "config.toml"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
        self._env = mock.patch.dict(os.environ, {**clean, "HARNESS_CONFIG": str(self.file), "HARNESS_BASE_URL": DEAD}, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_defaults_without_a_file(self):
        del os.environ["HARNESS_BASE_URL"]
        values, sources = config.load()
        self.assertEqual(values, config.DEFAULTS)
        self.assertEqual(set(sources.values()), {"default"})

    def test_env_beats_file_beats_default(self):
        self.file.write_text('model = "from-file"\nmax_turns = 7\n')
        os.environ["HARNESS_MODEL"] = "from-env"
        values, sources = config.load()
        self.assertEqual((values["model"], sources["model"]), ("from-env", "env"))
        self.assertEqual((values["max_turns"], sources["max_turns"]), (7, "file"))
        self.assertEqual(sources["context"], "default")

    def test_env_strings_are_typed(self):
        os.environ.update(HARNESS_MAX_TURNS="12", HARNESS_COMPACT_AT="0.6", HARNESS_BELL="0")
        values, _ = config.load()
        self.assertEqual((values["max_turns"], values["compact_at"], values["bell"]), (12, 0.6, False))

    def test_bad_value_and_bad_file_are_readable_errors(self):
        os.environ["HARNESS_MAX_TURNS"] = "lots"
        with self.assertRaisesRegex(ValueError, "max_turns"):
            config.load()
        del os.environ["HARNESS_MAX_TURNS"]
        self.file.write_text("model = \n")
        with self.assertRaisesRegex(ValueError, "cannot read"):
            config.load()

    def test_save_then_load_round_trip(self):
        values = {**config.DEFAULTS, "model": 'qwen "q" 3', "compact_at": 0.5, "base_url": DEAD}
        config.save(values)
        loaded, sources = config.load()
        self.assertEqual(loaded, values)
        self.assertEqual(sources["model"], "file")

    def test_server_fills_in_model_and_window(self):
        st = stub.Stub([])
        st.models = [{"id": "qwen", "meta": {"n_ctx": 131072}, "architecture": {"input_modalities": ["text"]}}]
        srv, port, _ = stub.serve(st)
        os.environ["HARNESS_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
        try:
            for extra, want in (([], 131072), (["--context", "500000"], 131072), (["--context", "4096"], 4096)):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(cli.main(["--show-config", *extra]), 0)
                self.assertIn("model = 'qwen'  (server)", out.getvalue())
                self.assertIn(f"context = {want}  ", out.getvalue())
                self.assertIn("# images: off", out.getvalue())
        finally:
            srv.shutdown()
            srv.server_close()

    def test_cli_without_model_exits_with_where_to_set_it(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cli.main(["-p", "hi", "--workspace", self._tmp.name]), 2)
        self.assertIn(str(self.file), err.getvalue())

    def test_show_config_names_the_source(self):
        self.file.write_text('model = "m"\n')
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--show-config", "--max-turns", "5"]), 0)
        self.assertIn("model = 'm'  (file)", out.getvalue())
        self.assertIn("max_turns = 5  (flag)", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class Lists(unittest.TestCase):
    def test_env_list_is_comma_separated(self):
        self.assertEqual(config._cast("*.mp4, node_modules/", ["x"]), ["*.mp4", "node_modules/"])
