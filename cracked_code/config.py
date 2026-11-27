"""One config file: ~/.config/cracked/config.toml.

Precedence: command-line flag > HARNESS_<KEY> env var > config file > DEFAULTS.
People and AI agents edit the file by hand; tools write it with save().
`cracked --show-config` prints every value and where it came from.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

DEFAULTS = {
    "base_url": "http://127.0.0.1:8080/v1",  # Strata's default port; any OpenAI-compatible /v1 works
    "model": "",           # "" = the model the server is serving, when it serves exactly one (Strata does)
    "api_key_env": "",     # name of the env var that holds the API key (e.g. NVIDIA_API_KEY), sent as a Bearer token; "" = no key (local servers)
    "max_turns": -1,       # model round trips per task; -1 = no limit
    "max_tokens": 16384,   # per reply; reasoning counts against it (4000 cut replies off, 2026-11-02)
    "context": 0,          # window the harness budgets for; 0 = what the server reports (Strata does), else 262144.
                           # A value above the server's own window is lowered to it
    "temperature": 0.2,    # sampling temperature; 0 = greedy
    "reasoning_effort": "",  # sent to the server when set: none | low | medium | high; "" = not sent (the server's default)
    "reasoning_budget": 0,   # most thinking tokens per reply, sent as reasoning_budget_tokens (Strata; llama.cpp); 0 = not sent
    "auto_think": True,    # auto thinking: effort medium, capped at 2048 tokens a turn (384 after a clean step); false = the model's own default thinking
    "locked_in": False,    # LOCKED IN mode for long tasks: every ask, error and changed file survives compaction, no folding, no 384 cap; a little slower
    "one_shot": False,     # ONE SHOT: the model makes the change and stops without running tests or checks, you test it; new sessions only
    "auto_checklist": False,  # a long task with no checklist (8 turns, or after a compaction) gets one, walked step by step; LOCKED IN's switch turns it on
    "compact_at": 0.4,     # auto-compact at this share of context
    "warn_mb": 1024,       # ask before starting in a bigger workspace; 0 = never ask
    "narrate": False,      # ask for one "what I'm doing" line per turn (~15-25 tokens each); false = tool lines only
    "map": True,           # list the workspace's files (up to 150) in the system prompt, so the first turn need not explore
    "theme": "dark",       # terminal colours: dark, or light for a light background (the neon is unreadable on white)
    "bell": True,          # ring the terminal bell when a task ends or an approval waits
    "telegram": True,      # message Telegram when a task ends, or a question waits 15 s; needs ~/.config/cracked-code/telegram.env
    "snapshot": True,      # git snapshot before each task and turn, for /undo and /diff; false = off (no undo)
    "snapshot_max_mb": 20, # files bigger than this (videos, models) are never snapshotted, so never undone; 0 = no limit
    "shell_timeout_max": 3600,  # longest run_shell timeout the model may ask for, in seconds; 0 = no limit
    "snapshot_skip": ["*.mp4", "*.mov", "*.mkv", "*.webm", "*.avi", "*.m4v", "*.wmv", "*.flv", "*.mpg", "*.mpeg",  # gitignore patterns never snapshotted (media, deps)
                      "*.wav", "*.mp3", "*.flac", "*.ogg", "*.m4a",
                      "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.webp", "*.bmp", "*.ico", "*.tif", "*.tiff", "*.avif", "*.heic",
                      "node_modules/", ".venv/", "__pycache__/"],
}
HEADER = ("# Cracked Code config. Keys and meanings: docs/setup.md.\n"
          "# Written by a tool: comments added here are not kept when it saves.\n")


def path() -> Path:
    if p := os.environ.get("HARNESS_CONFIG"):
        return Path(p).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cracked" / "config.toml"


def _cast(value, default):
    """Env strings (and loose file values) into the default's type."""
    if isinstance(default, bool) and isinstance(value, str):  # bool("false") is True
        v = value.strip().lower()
        if v not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
            raise ValueError(value)
        return v in ("1", "true", "yes", "on")
    if isinstance(default, list) and isinstance(value, str):  # env: comma-separated
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, str) and not isinstance(default, str):
        return type(default)(value)
    return value


def load(file: Path | None = None) -> tuple[dict, dict]:
    """(values, sources). sources maps each key to 'env', 'file' or 'default'.

    Raises ValueError with a readable message for a broken file or a bad value.
    """
    file = file or path()
    data: dict = {}
    if file.exists():
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as e:
            raise ValueError(f"cannot read {file}: {e}") from None
    for k in data.keys() - DEFAULTS.keys():
        print(f"warning: unknown key {k!r} in {file} (known: {', '.join(DEFAULTS)})", file=sys.stderr)
    values, sources = {}, {}
    for k, default in DEFAULTS.items():
        env = os.environ.get(f"HARNESS_{k.upper()}")
        raw, src = (env, "env") if env not in (None, "") else (data[k], "file") if k in data else (default, "default")
        try:
            values[k], sources[k] = _cast(raw, default), src
        except ValueError:
            raise ValueError(f"{k} = {raw!r} ({src}) is not a {type(default).__name__}") from None
    return values, sources


def save(values: dict, file: Path | None = None) -> Path:
    """Write the known keys as flat TOML. Values equal to the default are still written,
    so the file shows every setting."""
    file = file or path()

    def toml(v) -> str:
        return str(v).lower() if isinstance(v, bool) else json.dumps(v) if isinstance(v, (str, list)) else str(v)

    lines = [HEADER] + [f"{k} = {toml(values.get(k, d))}" for k, d in DEFAULTS.items()]
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(file)  # atomic: a crash mid-save never leaves half a config
    return file
