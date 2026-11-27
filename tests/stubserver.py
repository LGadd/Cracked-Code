"""A fake OpenAI-compatible server: records what the harness sends, replays scripted replies.

Used by tests/test_images.py and by hand:

    python3 -m tests.stubserver 2>/tmp/stub.json --port 8181
    python3 -m cracked_code -p "look at it" --image x.png --base-url http://127.0.0.1:8181/v1

The point is the *request*: it asserts what a real server would have to accept.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Stub:
    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def take(self) -> dict:
        with self._lock:
            r = self.replies.pop(0) if self.replies else {"text": "done", "calls": []}
        return r


def reply(text=None, calls=(), finish="stop", in_tokens=10, out_tokens=5):
    return {"text": text, "calls": list(calls), "finish": finish,
            "in": in_tokens, "out": out_tokens}


def to_openai(r: dict) -> dict:
    msg: dict = {"role": "assistant", "content": r.get("text")}
    if r.get("calls"):
        msg["tool_calls"] = [
            {"id": c[0], "type": "function",
             "function": {"name": c[1], "arguments": json.dumps(c[2])}}
            for c in r["calls"]
        ]
        msg["content"] = r.get("text") or ""
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": msg, "finish_reason": r.get("finish", "stop")}],
        "usage": {"prompt_tokens": r.get("in", 10), "completion_tokens": r.get("out", 5),
                  "total_tokens": r.get("in", 10) + r.get("out", 5)},
    }


def make_handler(stub: Stub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # keep test output clean
            pass

        def do_GET(self):  # /v1/models; 404 unless the test set stub.models
            models = getattr(stub, "models", None)
            payload = json.dumps({"object": "list", "data": models}).encode() if models is not None else b"{}"
            self.send_response(200 if models is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path.endswith("/api/show"):     # Ollama's capability endpoint
                payload = json.dumps(getattr(stub, "show", {})).encode()
            else:
                stub.requests.append(body)
                if body.get("stream") and getattr(stub, "stream", False):
                    return self.sse(stub.take())
                payload = json.dumps(to_openai(stub.take())).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def sse(self, r: dict):
            """The reply as server-sent chunks, the way Strata sends them (reasoning, text, calls, usage last)."""
            deltas = [{"reasoning_content": w} for w in (r.get("reasoning") or "").split(" ") if w]
            deltas += [{"content": w} for w in (r.get("text") or "").split(" ") if w]
            for i, c in enumerate(r.get("calls") or []):
                args = json.dumps(c[2])
                deltas.append({"tool_calls": [{"index": i, "id": c[0], "function": {"name": c[1], "arguments": ""}}]})
                deltas += [{"tool_calls": [{"index": i, "function": {"arguments": args[j:j + 5]}}]} for j in range(0, len(args), 5)]
            chunks = [{"choices": [{"index": 0, "delta": d, "finish_reason": None}]} for d in deltas]
            chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": r.get("finish", "stop")}],
                           "usage": {"prompt_tokens": r.get("in", 10), "completion_tokens": r.get("out", 5)}})
            body = ": keep-alive\n\n" + "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())

    return Handler


def serve(stub: Stub, port: int = 0) -> tuple[HTTPServer, int, threading.Thread]:
    srv = HTTPServer(("127.0.0.1", port), make_handler(stub))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1], t


if __name__ == "__main__":
    import sys
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8181
    srv, p, _ = serve(Stub([]), port)
    print(f"stub on http://127.0.0.1:{p}/v1", flush=True)
    threading.Event().wait()
