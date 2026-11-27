"""Image input: sniffing, the caps, and the wire format a real server sees."""

import base64
import io
import json
import shutil
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from cracked_code import agent, images, llm, safety, tools
from tests import stubserver as stub


def make_png(w: int, h: int, rgb=(200, 30, 90)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, body):
        return (len(body).to_bytes(4, "big") + tag + body +
                (zlib.crc32(tag + body) & 0xFFFFFFFF).to_bytes(4, "big"))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_jpeg_header(w: int, h: int) -> bytes:
    """Just enough JPEG for kind() and size(): SOI + an SOF0 segment."""
    return b"\xff\xd8\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x00" * 16


class Sniff(unittest.TestCase):
    def test_sniffed_not_named(self):
        self.assertEqual(images.kind(make_png(4, 4)), "png")  # whatever the file is called

    def test_not_an_image(self):
        with self.assertRaises(images.Unsupported):
            images.kind(b"hello world, definitely text\n")

    def test_sizes_from_headers(self):
        self.assertEqual(images.size(make_png(9, 5), "png"), (9, 5))
        self.assertEqual(images.size(make_jpeg_header(640, 480), "jpeg"), (640, 480))


class Prepare(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_small_png_untouched(self):
        p = self.dir / "small.png"
        p.write_bytes(make_png(20, 20))
        part, info = images.prepare(p)
        self.assertIsNone(info["sent"])
        self.assertEqual(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]), p.read_bytes())

    def test_oversize_goes_through_ffmpeg_as_jpeg(self):
        p = self.dir / "wide.png"
        p.write_bytes(make_png(2000, 10))
        with mock.patch.object(images, "_ffmpeg_jpeg", return_value=make_jpeg_header(1024, 5)):
            part, info = images.prepare(p)
        self.assertTrue(part["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertIn("sent as 1024x5 JPEG", images.describe(info))

    def test_oversize_without_ffmpeg_is_a_sentence(self):
        p = self.dir / "wide.png"
        p.write_bytes(make_png(2000, 10))
        with mock.patch.object(images, "_ffmpeg_jpeg", return_value=None):
            with self.assertRaises(images.Unsupported) as e:
                images.prepare(p)
        self.assertIn("Downscale it", str(e.exception))

    def test_real_ffmpeg_downscales(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("no ffmpeg")
        p = self.dir / "big.png"
        p.write_bytes(make_png(1500, 1100))
        _, info = images.prepare(p)
        (w, h), _n = info["sent"]
        self.assertEqual(max(w, h), images.MAX_EDGE)


class ToolSurface(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.guard = safety.Guard(self.ws, assume_yes=True)
        (self.ws / "shot.png").write_bytes(make_png(30, 30))

    def tearDown(self):
        self._tmp.cleanup()

    def test_text_only_model_gets_a_sentence(self):
        ctx = tools.Ctx(workspace=self.ws, guard=self.guard, vision=False)
        out = tools.see_image(ctx, "shot.png")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertEqual(ctx.pending_images, [])

    def test_tool_only_offered_with_vision(self):
        names = lambda v: [s["function"]["name"] for s in tools.schemas(v)]  # noqa: E731
        self.assertNotIn("see_image", names(False))
        self.assertIn("see_image", names(True))

    def test_vision_model_attaches_once(self):
        ctx = tools.Ctx(workspace=self.ws, guard=self.guard, vision=True)
        out = tools.call(ctx, "see_image", json.dumps({"path": "shot.png"}))
        self.assertIn("image attached", out)
        msg = tools.attach_pending(ctx, out, "call1")
        self.assertEqual(msg["tool_call_id"], "call1")
        self.assertEqual([p["type"] for p in msg["content"]], ["text", "image_url"])
        self.assertEqual(ctx.pending_images, [])   # consumed, not re-sent next turn

    def test_plain_result_stays_a_string(self):
        ctx = tools.Ctx(workspace=self.ws, guard=self.guard, vision=True)
        self.assertEqual(tools.attach_pending(ctx, "just text", "c2"),
                         {"role": "tool", "tool_call_id": "c2", "content": "just text"})

    def test_bad_image_is_an_error_not_a_crash(self):
        ctx = tools.Ctx(workspace=self.ws, guard=self.guard, vision=True)
        (self.ws / "notes.txt").write_text("text")
        self.assertIn("ERROR:", tools.see_image(ctx, "notes.txt"))


class Probe(unittest.TestCase):
    def test_unreachable_server_says_nothing(self):
        self.assertEqual(llm.probe("http://127.0.0.1:9/v1", "m", timeout=1), {"ids": [], "model": "m", "context": 0, "vision": False})

    def test_model_window_and_images_are_read_from_models(self):
        # Strata says all three in /v1/models; text-only Strata must turn see_image off.
        for mods, want in ((["text"], False), (["text", "image"], True)):
            st = stub.Stub([])
            st.models = [{"id": "other", "architecture": {"input_modalities": ["text", "image"]}},
                         {"id": "m", "meta": {"n_ctx": 262144}, "architecture": {"input_modalities": mods}}]
            srv, port, _ = stub.serve(st)
            try:
                got = llm.probe(f"http://127.0.0.1:{port}/v1", "m", timeout=2)
                none = llm.probe(f"http://127.0.0.1:{port}/v1", timeout=2)
            finally:
                srv.shutdown()
                srv.server_close()
            self.assertEqual((got["vision"], got["context"], got["ids"]), (want, 262144, ["other", "m"]))
            self.assertEqual(none["model"], "")  # two models served: the harness does not guess


class Wire(unittest.TestCase):
    """What a real server receives: the tool message must carry the image part."""

    def test_image_part_survives_into_the_request(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "shot.png").write_bytes(make_png(20, 20))
            st = stub.Stub([stub.reply(calls=[("c1", "see_image", {"path": "shot.png"})]),
                            stub.reply(text="a pink square")])
            srv, port, _ = stub.serve(st)
            try:
                ctx = tools.Ctx(workspace=ws, guard=safety.Guard(ws, assume_yes=True), vision=True)
                model = llm.LLM(f"http://127.0.0.1:{port}/v1", "stub", max_tokens=100)
                msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "look"}]
                out = agent.run(model, msgs, ctx, max_turns=4, out=io.StringIO())
            finally:
                srv.shutdown()
                srv.server_close()
        self.assertEqual(out, "a pink square")
        tool_msgs = [m for m in st.requests[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        content = tool_msgs[0]["content"]
        self.assertEqual([p["type"] for p in content], ["text", "image_url"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
