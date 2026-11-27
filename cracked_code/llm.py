"""OpenAI-compatible chat client, built for Strata; other servers work on the common subset. Stdlib only."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Call:
    id: str
    name: str
    arguments: str


@dataclass
class Reply:
    text: str | None
    calls: list[Call]
    finish: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning: str = ""
    timings: dict = field(default_factory=dict)


class ServerError(OSError):
    """The server answered, but with an error: carries its own message (not "cannot reach")."""


RETRY_WAIT = 2  # seconds before the one retry of a dropped connection or a 503 (server busy or restarting)


def repeating(tail: str) -> bool:
    """True when the text ends in the same block of 40-400 chars written 6 times in a row: a runaway reply.
    A block of 8 or fewer different characters (a ruler line, padding) does not count."""
    for n in range(40, min(400, len(tail) // 6) + 1):
        unit = tail[-n:]
        if len(set(unit)) > 8 and tail.endswith(unit * 6):
            return True
    return False


class LLM:
    def __init__(self, base_url: str, model: str, max_tokens: int = 16384, timeout: int = 900,
                 on_delta: Callable[[str, str], None] | None = None,
                 temperature: float = 0.2, reasoning_effort: str = "", reasoning_budget: int = 0, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort  # "" = the field is not sent
        self.reasoning_budget = reasoning_budget  # 0 = the field is not sent
        self.budget_ok = True     # False after a server rejected reasoning_budget_tokens (NVIDIA's API does)
        self.api_key = api_key    # sent as a Bearer token when set (hosted APIs); local servers need none
        self.timeout = timeout
        self.on_delta = on_delta  # ("reasoning" | "content" | "tool", text) as it streams: the terminal's live line
        self.extra: dict = {}     # extra request fields, e.g. Strata's strata_prefix pin

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            # Default 0.2, measured on Strata IQ3_S: valid parallel tool calls; greedy
            # decoding was not tested there and can repeat. See docs/gotchas.md.
            "temperature": self.temperature,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if self.reasoning_budget > 0 and self.budget_ok:  # Strata closes the thinking there (a template without effort levels ignores the effort)
            body["reasoning_budget_tokens"] = self.reasoning_budget
        body.update(self.extra)
        if tools:  # empty = text-only turn, e.g. the forced final report
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body["stream"] = True  # the terminal shows the reply as it is generated
        body["stream_options"] = {"include_usage": True}  # usage drives ctx % and auto-compact
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            json.dumps(body).encode(),
            {"Content-Type": "application/json", "Accept": "application/json",
             **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    # A server that ignores "stream" answers with plain JSON: read whichever came back.
                    streamed = "text/event-stream" in (getattr(resp, "headers", None) or {}).get("Content-Type", "")
                    data = self._read_stream(resp) if streamed else json.load(resp)
                break
            except urllib.error.HTTPError as e:
                detail = _error_text(e.read().decode("utf-8", "replace"))
                e.close()
                if e.code == 503 and attempt == 1:
                    time.sleep(RETRY_WAIT)
                    continue
                if e.code == 400 and self.reasoning_effort and "reasoning effort" in detail.lower():
                    # This model has other thinking levels than Qwen3.8 (DeepSeek on Strata: low, high, max).
                    self.reasoning_effort = ""  # for the rest of the session: the server's own default
                    return self.chat(messages, tools)
                if e.code == 400 and "reasoning_budget_tokens" in body and "reasoning_budget_tokens" in detail:
                    self.budget_ok = False  # for the rest of the session; the auto-think cap then rests on the effort alone
                    return self.chat(messages, tools)
                raise ServerError(f"HTTP {e.code} from {self.base_url}: {detail or e.reason}") from None
            except (ConnectionResetError, http.client.RemoteDisconnected, http.client.IncompleteRead) as e:
                if attempt == 1:
                    time.sleep(RETRY_WAIT)
                    continue
                raise ServerError(f"{self.base_url} dropped the connection twice: {e}") from None
            except ValueError as e:  # not JSON where JSON was due
                raise ServerError(f"{self.base_url} sent data that is not JSON: {e}") from None
        if not data.get("choices"):
            raise ServerError(f"{self.base_url} sent no reply: {_error_text(json.dumps(data)) or 'empty response'}")

        choice = data["choices"][0]
        msg = choice.get("message") or {}
        calls = [
            Call(
                c.get("id") or f"call{i}",
                c["function"]["name"],
                c["function"].get("arguments") or "{}",
            )
            for i, c in enumerate(msg.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        return Reply(
            text=msg.get("content"),
            calls=calls,
            finish=choice.get("finish_reason") or "",
            # shortcut: a stream without usage gets a chars/4 estimate so auto-compact still fires;
            # replace with a tokenizer count if a server is found that streams without usage.
            prompt_tokens=usage.get("prompt_tokens") or (len(json.dumps(messages)) // 4 if streamed else 0),
            completion_tokens=usage.get("completion_tokens", 0),
            reasoning=msg.get("reasoning_content") or msg.get("reasoning") or "",  # Strata/llama.cpp, then Ollama
            timings=data.get("timings") or {},
        )

    def _tell(self, kind: str, text: str) -> None:
        if self.on_delta:
            self.on_delta(kind, text)

    def _read_stream(self, resp) -> dict:
        """Server-sent chunks back into the shape of a non-streamed reply."""
        text, reasoning, calls = [], [], {}
        finish, usage, timings = "", {}, {}
        tail, unchecked = "", 0  # the last 2,400 chars of thinking and text, for the runaway check
        for raw in resp:
            if unchecked >= 200:  # not on every chunk: the check walks 360 block sizes
                unchecked = 0
                if repeating(tail):  # stop reading; closing the connection stops the server (else it writes on to max_tokens)
                    finish = "repeat"
                    break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):  # blank lines and ": keep-alive" comments
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("error") and not chunk.get("choices"):  # the server gave up mid-stream
                raise ServerError(f"{self.base_url} failed mid-reply: {_error_text(payload)}")
            usage, timings = chunk.get("usage") or usage, chunk.get("timings") or timings
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                finish = ch.get("finish_reason") or finish
                if r := d.get("reasoning_content") or d.get("reasoning"):  # Strata/llama.cpp, then Ollama
                    reasoning.append(r)
                    tail, unchecked = (tail + r)[-2400:], unchecked + len(r)
                    self._tell("reasoning", r)
                if c := d.get("content"):
                    text.append(c)
                    tail, unchecked = (tail + c)[-2400:], unchecked + len(c)
                    self._tell("content", c)
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    f = tc.get("function") or {}
                    if f.get("name"):
                        slot["function"]["name"] += f["name"]
                        self._tell("tool", slot["function"]["name"])
                    slot["function"]["arguments"] += f.get("arguments") or ""
        msg = {"content": "".join(text) or None, "reasoning_content": "".join(reasoning),
               "tool_calls": [calls[i] for i in sorted(calls)]}
        return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage, "timings": timings}


def failure(base_url: str, e: OSError) -> str:
    """One line for the user: the server's own error, or that it could not be reached."""
    return str(e) if isinstance(e, ServerError) else f"cannot reach {base_url}: {e}"


def _error_text(body: str) -> str:
    """The message out of an OpenAI-style error body, else the body's start."""
    try:
        err = json.loads(body).get("error")
        return str(err.get("message") if isinstance(err, dict) else err or "")[:500] or body[:500]
    except (ValueError, AttributeError):
        return " ".join(body.split())[:500]


def probe(base_url: str, model: str = "", timeout: float = 3) -> dict:
    """What the server says about itself, from one GET /models, so nobody types it into a config:
    the model ids it serves, and for `model` (or the only one served) its context window and
    whether it takes images. Strata reports all three; a server that says less leaves them
    empty (context 0, vision False) and the config decides."""
    out = {"ids": [], "model": model, "context": 0, "vision": False}
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=timeout) as resp:
            data = [m for m in json.load(resp).get("data") or [] if isinstance(m, dict) and m.get("id")]
    except Exception:  # unreachable, no /models, not JSON
        return out
    out["ids"] = [m["id"] for m in data]
    if not model and len(data) == 1:
        out["model"] = data[0]["id"]
    entry = next((m for m in data if m["id"] == out["model"]), {})
    n_ctx = (entry.get("meta") or {}).get("n_ctx")
    out["context"] = n_ctx if isinstance(n_ctx, int) and n_ctx > 0 else 0
    out["vision"] = "image" in ((entry.get("architecture") or {}).get("input_modalities") or [])
    return out
