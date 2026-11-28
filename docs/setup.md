# Setup

## Requirements

- Python 3.11+. One third-party package: `prompt_toolkit` (pure Python, works on Windows), for the
  `>_` prompt's `/` and `@` menus and colours (2026-11-07). Everything else is the standard library.
  Without a keyboard (pipes, the dashboard's pty) it is never imported; if it is missing in a
  terminal, the REPL says so and falls back to plain input.
- [Strata](https://github.com/Niko1221/Strata) serving Qwen3.8 Flash-Next (the tested setup), or another
  OpenAI-compatible server that honours `tools` / `tool_calls` (best effort).
- Optional: `git` (the snapshot safety needs it), `bwrap` (only for the test jail).

## Run

```bash
python3 -m cracked_code                                  # REPL, workspace = cwd
python3 -m cracked_code -p "task"                        # one shot
pipx install . && cracked -p "task"                 # as a command
```

The installed command is `cracked` (`[project.scripts]` in `pyproject.toml`); the Python
package is `cracked_code` (`harness` until 2026-11-27), so `python3 -m cracked_code` works from any checkout. A checkout
can also be run without installing: a two-line wrapper on `PATH` with
`PYTHONPATH=<repo> python3 -m cracked_code "$@"`.

## Configuration

Cracked Code is built and tested for **Qwen3.8 Flash-Next on Strata**. With Strata running on its
default port it needs **no config file**: the model id, the context window and whether the model
takes images are read from Strata's `/v1/models` at startup.

Settings live in **`~/.config/cracked/config.toml`** (`$XDG_CONFIG_HOME` is respected;
`HARNESS_CONFIG=<path>` points elsewhere). Precedence, highest first:
**flag > `HARNESS_<KEY>` env var > config file > built-in default.**
`cracked --show-config` prints every value and where it came from (`server` = read from the server).
Unknown keys print a warning; a bad value or broken TOML stops startup with the reason.

```toml
# Only what differs from the defaults. For Strata on 127.0.0.1:8080 this file can be empty.
max_turns = 40
```

Another OpenAI-compatible server (Ollama, llama.cpp's `llama-server`) works on a best-effort basis:
set `base_url`, and `model` if it serves more than one. It is not tuned or tested, and a server
that does not report its window gets a 262144-token budget unless `context` is set.

| Key | Flag | Default | Meaning |
|---|---|---|---|
| `base_url` | `--base-url` | `http://127.0.0.1:8080/v1` | server root (Strata's default port) |
| `api_key_env` | | `""` | name of the env var holding the API key (e.g. `NVIDIA_API_KEY`), sent as `Authorization: Bearer`. The key itself never goes in the config. `""` = no key (local servers). Set but empty: Cracked stops and says so |
| `model` | `--model` | `""` | model id. Empty = the model the server is serving, when it serves exactly one |
| `context` | `--context` | 0 | window the harness budgets for. 0 = what the server reports, else 262144. A value above the server's window is lowered to it |
| `max_turns` | `--max-turns` | -1 | model round trips per task; -1 = no limit. `/turns N` in the REPL |
| `max_tokens` | `--max-tokens` | 16384 | per-reply cap; **reasoning counts against it** (4000 cut replies off mid-thought) |
| `compact_at` | `--compact-at` | 0.4 | auto-compact when a turn uses this share of `context` |
| `temperature` | | 0.2 | sampling temperature; 0 = greedy |
| `reasoning_effort` | | `""` | `none`/`low`/`medium`/`high`, sent only when set |
| `reasoning_budget` | | 0 | most thinking tokens per reply, sent as `reasoning_budget_tokens` (Strata closes the thinking there); 0 = not sent |
| `auto_think` | | true | auto thinking: effort `medium` (when `reasoning_effort` is `""`), capped at 2048 tokens a turn, 384 after a clean step (when `reasoning_budget` is 0). false = nothing sent, the model's own default thinking |
| `one_shot` | | false | ONE SHOT: the system prompt tells the model to make the change and stop, with no tests, builds or checks and no re-reads to confirm; you test it. The harness's own syntax check on `.py`/`.json`/`.js` writes still runs. New sessions read it at start; `/oneshot on|off` switches a running session (the system prompt changes, so the next request re-reads the whole prompt once) and saves the key. The dashboard header's `one shot` switch |
| `auto_checklist` | | false | a long task with no checklist (8 turns, or right after a compaction) gets one: the model is asked for the remaining steps once, and the harness walks it through them (`docs/plan-mode.md`). Turning LOCKED IN on from the dashboard also turns this on. `/autochecklist on|off` switches it from the terminal and saves the key |
| `locked_in` | | false | LOCKED IN mode for long tasks: a ledger of every ask (queued messages too), failed call and changed file is restated every 10 turns and after each compaction and kept through it; compacts at max(`compact_at`, 0.7) with a 25-bullet summary; no folding; no 384-token cap. Costs some speed: the history stays bigger. The dashboard header has a switch for it; a running session re-reads it when the config file changes, at its next turn. `/lockedin on|off` switches it from the terminal and saves the key (turning it on also turns `auto_checklist` on, as the dashboard does) |
| `narrate` | | false | ask the model for one short "what I'm doing and why" line per turn, shown as `● …` (~15–25 output tokens a turn, kept in the history). false = tool lines only |
| `map` | | true | list the workspace's files (up to 150, skipping `.git`, `node_modules`, `__pycache__`, `.venv`, build dirs) in the system prompt at startup, so the first turn reads instead of exploring |
| `warn_mb` | `--warn-mb` | 1024 | ask `[y/N/s]` before starting in a bigger workspace (`s` = start with no snapshots this session); 0 = never |
| `shell_timeout_max` | | 3600 | longest `run_shell` timeout the model may ask for, in seconds (was a fixed 600); 0 = no limit. Ctrl+B or `/jobs kill` covers a command that should not wait that long |
| `theme` | | dark | terminal colours: `light` for a light background (the neon green is unreadable on white). Without `COLORTERM=truecolor` (tmux, Terminal.app) colours drop to the nearest of 256 by themselves |
| `bell` | | true | ring the terminal bell when a task ends or an approval waits |
| `telegram` | | true | Telegram message when a task ends or fails, or a question waits 15 s; reads `TG_TOKEN`/`TG_CHAT` from `~/.config/cracked-code/telegram.env` (no file = off) |
| `snapshot` | | true | git snapshot before each task and turn, kept in `~/.local/share/cracked/snapshots/<hash>/` (the workspace's own `.git` is never touched). false = no `/undo` or `/diff` |
| `snapshot_max_mb` | | 20 | files bigger than this (models, renders) are never snapshotted, so never undone; checked by size only. 0 = no limit |
| `snapshot_skip` | | images, videos, audio, `node_modules/`, `.venv/`, `__pycache__/` | gitignore patterns never snapshotted; env `HARNESS_SNAPSHOT_SKIP` is comma-separated. Setting it replaces the default list |

Always on, no switch: the task anchor (a reminder added to the history every few turns; the history is
append-only, so each turn reads only new tokens), task folding (a finished task collapses to ask + answer + up to 16,000
chars of the files it touched), streaming, and image input when the server lists it for the model.

Removed 2026-11-10 (tag `pre-trim` has them): `exact_prefix`, `technique`, `evict_stale`, `backtrack`,
`contract`, `stream`, `vision`, `anchor`, `fold_tasks`, `fold_keep`, `[dashboard]`. Parked for a
later test, each one `git revert` away: `batch_check`, `grep`, `tool_search` + `defer`, `todo`
(`docs/parked.md`).

Flags with no config key: `-p/--prompt` (one task and exit; repeat it for several tasks in one session), `-c/--continue` (carry on with
this workspace's latest saved session), `--workspace` / `HARNESS_WORKSPACE`
(default cwd; the only folder file tools may touch without asking), `--image PATH` (attach to
the task, repeatable; needs a model the server lists image input for), `--yes` (start in **Fuck It Mode**: every approval is yes; Shift+Tab
toggles it in the REPL), `--show-config`.

Images need `ffmpeg` only for files over 1024 px or ~900 KB; smaller ones are sent untouched.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

No model and no network: they stub the LLM (`tests/test_agent.py`, `tests/stubserver.py`)
and exercise the guard, the deny-list, the snapshot, every tool, image input and the config file.

`scripts/toolcheck.py` is the live half: it runs five real prompts and passes only if
the model actually called the expected tools. Needs the server up. Takes 10–20 minutes,
so run it in the background and **with `python3 -u`** — redirected stdout is block
buffered, so a `timeout` kill loses every verdict printed so far (observed: a 900 s run
that looked like it printed nothing and exited 124).

```bash
timeout 1700 scripts/jail.sh python3 -u /mnt/scripts/toolcheck.py --max-turns 8
```

## Safe live testing

`live-tests/` holds the planned live suite: seven numbered scripts, each writing its full
transcript to `live-tests/results/<name>.log` so a run can be reviewed afterwards without
being watched live. `live-tests/README.md` says what each one proves and what a pass looks
like. `run-all.sh --dry-run` checks the jail plumbing without touching the model.

```bash
live-tests/run-all.sh              # all seven, sequentially
live-tests/03-web.sh               # one of them
```

Never point a live model at your real project by accident:

```bash
scripts/jail.sh python3 -m cracked_code --workspace /tmp -p "make notes.md, read it, delete it"
scripts/jail.sh python3 /mnt/scripts/toolcheck.py
```

Inside the jail the OS is read-only, `$HOME` does not exist, and the only writable
path is `/tmp` (host: `/tmp/harness-jail/ws`, throwaway). Network stays up so the
model server is reachable. The jail hides the config file with `$HOME`, so
`live-tests/_common.sh` passes the host's `base_url` and `model` in as env vars. Verified on 2026-11-02: `touch /etc/x` fails, `ls /home`
is empty, `git`/`nmap` work, `python3 -c "import cracked_code"` prints the version.

bwrap gotchas hit while writing this, both real:
- bind targets must already exist — `/` is read-only, so `--ro-bind x /newdir` fails.
  Mount on `/mnt` and `/tmp` instead.
- binds are applied in argument order, so `--ro-bind $PROJ /mnt` (whose source lives
  under `/home`) must come **before** `--tmpfs /home`, or the source resolves to the empty tmpfs.

## Layout

```
harness/
  __main__.py   argparse, REPL, one-shot
  agent.py      the loop + the system prompt
  llm.py        OpenAI-compatible chat with tools
  config.py     the config file: load (flag > env > file > default) and save
  images.py     image input for see_image / --image (ffmpeg for oversize files)
  tools.py      the tools: schema + handler in one registry
  safety.py     boundary, deny-list, approvals, git snapshot
  web.py        keyless DuckDuckGo search + text extraction
scripts/
  jail.sh       bwrap jail for live tests
  toolcheck.py  live tool-calling checks
live-tests/     the live suite (results/ holds its transcripts)
tests/          offline suite
docs/           plan, implemented, gotchas, setup, roadmap
```
