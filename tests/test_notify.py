import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from cracked_code import ui


class Notify(unittest.TestCase):
    def test_load(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.env"
            self.assertIsNone(ui.telegram_load(f))  # missing file
            f.write_text("TG_TOKEN=abc\n")
            self.assertIsNone(ui.telegram_load(f))  # no chat
            f.write_text("# c\nTG_TOKEN=abc:1=2\nTG_CHAT=42\n")
            self.assertEqual(ui.telegram_load(f), ("abc:1=2", "42"))

    def test_off_sends_nothing(self):
        with mock.patch.object(ui, "TELEGRAM", None), mock.patch.object(ui, "_telegram_send") as send:
            ui.notify("x")
            with ui.waiting("q"):
                pass
        send.assert_not_called()

    def test_waiting_pings_only_after_delay(self):
        sent = []
        with mock.patch.object(ui, "TELEGRAM", ("t", "c")), mock.patch.object(ui, "WAIT_PING", 0.2), \
                mock.patch.object(ui, "notify", sent.append):
            with ui.waiting("quick"):
                pass  # answered at once: no ping
            with ui.waiting("\033[1mslow\033[0m"):
                time.sleep(0.4)
        self.assertEqual(sent, ["⏸ <b>waiting for you</b>\nslow"])

    def test_waiting_message_has_capped_diff(self):
        m = ui.waiting_message("edit a.py - allow?", "\n".join(f"+line {i} <x>" for i in range(30)))
        self.assertIn("<pre>+line 0 &lt;x&gt;", m)
        self.assertIn("… 10 more lines</pre>", m)
        self.assertNotIn("line 20", m)
        self.assertEqual(ui.waiting_message("q"), "⏸ <b>waiting for you</b>\nq")

    def test_done_message(self):
        numstat = "52\t2\tharness/ui.py\n-\t-\tlogo.png\n" + "".join(f"1\t0\tf{i}.py\n" for i in range(5))
        m = ui.done_message("fix the bug\nmore detail", "Fixed <it>.\nTests pass.", 134, numstat)
        self.assertTrue(m.startswith("✅ <b>finished</b> · 2m 14s\n<b>asked:</b> fix the bug\n"))
        self.assertIn("ui.py +52 −2, logo.png (binary)", m)
        self.assertIn("(+2 more)", m)
        self.assertIn("Fixed &lt;it&gt;.\nTests pass.", m)
        self.assertNotIn("files:", ui.done_message("q", "a", 3))
        self.assertLessEqual(len(ui.done_message("q" * 9000, "a" * 9000, 3, numstat * 100)), 4000)

    def test_send_posts_to_telegram(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, self.rfile.read(int(self.headers["Content-Length"])).decode()))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        real = ui.urllib.request.urlopen
        with mock.patch.object(ui, "TELEGRAM", ("TOK", "42")), mock.patch.object(ui, "WHERE", "ws"), \
                mock.patch.object(ui.urllib.request, "urlopen",
                                  lambda url, data, timeout: real(url.replace("https://api.telegram.org", f"http://127.0.0.1:{srv.server_port}"), data, timeout)):
            ui._telegram_send("✅ done")  # already HTML: _telegram_send only adds the workspace
        srv.server_close()
        self.assertEqual(got[0][0], "/botTOK/sendMessage")
        self.assertIn("chat_id=42", got[0][1])
        self.assertIn("parse_mode=HTML", got[0][1])
        self.assertIn("text=%3Cb%3Ews%3C%2Fb%3E+%C2%B7+%E2%9C%85+done", got[0][1])


if __name__ == "__main__":
    unittest.main()
