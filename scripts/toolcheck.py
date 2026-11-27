#!/usr/bin/env python3
"""Live tool-calling checks against a running OpenAI-compatible server.

The offline suite (tests/) proves the loop; this proves the *model* drives it.
Needs the server up. Each check runs in its own throwaway workspace and passes
only if the expected tools were actually called.

    python3 scripts/toolcheck.py                     # against base_url + model from the config file
    python3 scripts/toolcheck.py --base-url http://host:8080/v1 --model m
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cracked_code import agent, config, llm, safety, tools  # noqa: E402

# (name, prompt, tools that must appear in the call sequence)
CHECKS = [
    (
        "file cycle",
        "Create notes.md with three bullets, read it back, then delete it.",
        ["write_file", "read_file", "delete_file"],
    ),
    (
        "shell",
        "Scan the top 20 ports of 127.0.0.1 and tell me which are open.",
        ["run_shell"],
    ),
    (
        "web",
        "Search the web for the Strata ggml inference server and fetch one result.",
        ["web_search", "web_fetch"],
    ),
    (
        "deny-list",
        'Run the shell command "rm -rf /" and report exactly what happens.',
        ["run_shell"],
    ),
    (
        "edit",
        "Write a.txt containing exactly `line one`, then change it to `line two` "
        "with the edit tool, then read it back.",
        ["write_file", "edit_file", "read_file"],
    ),
]


class Recorder:
    """Wraps the LLM so we can see which tools the model actually asked for."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[str] = []
        self.turns: list[tuple[int, int, dict]] = []

    def chat(self, messages, schemas):
        reply = self.inner.chat(messages, schemas)
        self.calls += [c.name for c in reply.calls]
        self.turns.append((reply.prompt_tokens, reply.completion_tokens, reply.timings))
        return reply


def main() -> int:
    ap = argparse.ArgumentParser()
    cfg = config.load()[0]
    ap.add_argument("--base-url", default=cfg["base_url"])
    ap.add_argument("--model", default=cfg["model"])
    ap.add_argument("--max-turns", type=int, default=12)
    args = ap.parse_args()

    dev_null = open("/dev/null", "w")
    failed = 0
    for name, prompt, expected in CHECKS:
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            ctx = tools.Ctx(workspace=ws, guard=safety.Guard(ws, assume_yes=True, out=dev_null))
            model = Recorder(llm.LLM(args.base_url, args.model))
            started = time.time()
            try:
                agent.run(model, [{"role": "user", "content": prompt}], ctx,
                          max_turns=args.max_turns, out=dev_null)
            except OSError as e:
                print(f"FAIL {name}: cannot reach {args.base_url}: {e}")
                failed += 1
                continue
            elapsed = time.time() - started
            missing = [t for t in expected if t not in model.calls]
            tokens = sum(t[0] + t[1] for t in model.turns)
            verdict = "PASS" if not missing else "FAIL"
            failed += bool(missing)
            print(f"{verdict} {name}: {len(model.turns)} turns, {len(model.calls)} calls "
                  f"in {elapsed:.1f}s, {tokens} tokens")
            print(f"     called: {', '.join(model.calls) or '(nothing)'}")
            if missing:
                print(f"     missing: {', '.join(missing)}")
    dev_null.close()
    print(f"{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
