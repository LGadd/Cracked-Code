# Gotchas

Measured traps, not speculation. Each was observed in a real run (2026-11-02,
Qwen3.8-Flash-Next IQ3_S on Strata, RTX 5070 Ti).

## Context

- **The model inflates its own context.** `web_fetch`'s `max_chars` is a model-supplied
  argument, so the model can ask for 20 000 chars twice. One run went 1 515 → 5 620
  prompt tokens in a single turn and peaked at 14 609. At the measured ~2 760 tok/s
  prefill, 14 600 tokens is ~5 s of prefill *per turn*, on top of generation.
  Fixed 2026-11-02: `max_chars` is gone and long pages spill to a file (see Output size).
- **Seven tools cost ~1 031 tokens** before any history (system + user + schemas).
  Roughly 150 tokens per tool. Adding tools is not free.
- **Reasoning eats `max_tokens`.** Strata returns `reasoning_content` separately but counts it
  in `completion_tokens`. At the old `--max-tokens 4000` a long think hit the cap at only 39%
  context (2026-11-02): `truncated by max_tokens`. Default is now 16384. Since variant B
  (`f6c7b55`) the harness keeps the reasoning in the history (`agent.assistant_message`), so the next
  prompt extends the server's cache; folding a finished task drops it.
- **Context fills fastest from reading whole files.** In the 2026-11-02 playground session
  the model `cat`-ed 9 harness files from outside the workspace, about 55 000 chars,
  because `read_file` asked outside the workspace and `cat` doesn't (confirmed in a re-run:
  refused, then `cat`). Reads never ask now (2026-11-03). Spilling only helps
  when the model doesn't need the content. To read everything, it has to load everything.
- **Prefill dominates long sessions.** A 20 000-token history is ~7 s per turn on this GPU.

## Tool calling

- **`finish_reason: "length"` cuts big writes off.** The model puts a whole file in one
  `write_file` (30 295 chars = 10 459 tokens in one run; over 16 384 in another), so whether
  it fits is luck. Since 2026-11-03 a cut-off reply is never run: the model is told it hit
  max_tokens and to write in parts with `write_file append=true`. The `write_file`
  description says the same up front. Same nudge when the cut lands in the reasoning,
  before any call (that case used to end the run with an empty answer). Live check at
  `--max-tokens 1500`: cut off once, then 4 appended parts, all 150 items present.
- **Parallel calls run sequentially, in order.** A 300 s `run_shell` blocks the rest of
  the batch. The model batches freely; it does not know they are serial.
- **Tool failures are returned as text, not exceptions.** The model recovers well from
  `ERROR:` / `DENIED:` strings — observed recovering from a bad `nmap` flag set on the
  next turn without prompting. Keep failures as readable text.
- **`edit_file` needs a unique `old_string`.** It errors with the hit count when the
  string appears more than once. When it is not found, the error shows the closest lines of the
  file, so the model can retry without a `read_file` turn.
- **A write or edit that breaks the syntax is flagged in its own result** (`.py`, `.json`, and `.js` when
  `node` is installed). `write_file append=true` is not checked: a part is unfinished by design.

## Safety limits (know these, they are real)

- **The deny-list is substring matching.** A compound command is blocked *wholesale* if a
  blocked pattern appears anywhere in it — observed: a long `echo …; rm -rf /; echo …`
  probe was refused entirely. Also, short keywords can false-positive on filenames
  (`halt`, `shutdown` in a path). The list is 9 patterns and deliberately narrow.
- **Symlinks are not resolved** in `Guard.resolve()` (marked `shortcut:` in the code).
  A symlink inside the workspace pointing outside escapes the boundary check.
- **`run_shell` is not approval-gated**, only deny-listed. So "delete always asks" covers
  `delete_file` only: `rm` through the shell never asks. `rm -rf ./build` inside the
  workspace runs without asking. The git snapshot, not a prompt, is the safety net there.
- **Starting in `~` or `/` asks first.** The snapshot `git add -A`s the whole workspace, and
  an SSH login starts in `~`: a bare `cracked` there sat silent for minutes staging 38 GB into
  `~/.git` before the model was ever called (2026-11-03). Now it measures the folder (~4 s for
  a 25 GB home) and asks `[y/N]`; with no terminal to ask on it exits 2 unless `--yes`. Any other
  workspace over `warn_mb` (default 1024) asks too; `--yes` or no terminal only warns, and
  `warn_mb = 0` turns it off. Answering `s` starts without snapshots for that session (no `/undo`, `/diff`).
  The size leaves out `snapshot_skip` and over-`snapshot_max_mb` files but ignores `.gitignore`, so it can overstate what is staged.
  The first snapshot runs at startup, after a `. snapshotting N files…` line when it is big.
- **Snapshots happen at the start of a turn**, so the damage from the *last* turn is not
  committed until the next one. `git status` in the workspace shows uncommitted state;
  The snapshot repo is not the workspace's: `git --git-dir ~/.local/share/cracked/snapshots/<hash>
  --work-tree . log --oneline` shows the points (`cat …/<hash>/workspace` says which folder a store is).
  Workspaces snapshotted before 2026-11-06 still have the old `harness:` commits in their own `.git`;
  sessions saved then cannot `/undo` after `/resume`.
- **`cracked` in `~` puts the snapshot store inside the workspace** (`~/.local/share/cracked`). It is
  excluded from its own snapshots and from the size check (fixed 2026-11-06: before, 55 of 56 tracked
  files were the store itself).
- **The jail's `HOME` must not be the workspace.** It was `/tmp` = the workspace, so the home-folder
  check refused to start every live test without `--yes` (5 of 7, from 2026-11-03 to 2026-11-06). Now `/home/jail`.
- **Skipped files cannot be undone.** Anything matching `snapshot_skip` or over `snapshot_max_mb`
  is invisible to `/undo` and `/diff`: a re-render or `rm` of a video through the shell is permanent.
- **A nested git repo inside the workspace is not protected.** A directory that has its
  own `.git` with no commit checked out (a scratch dir left behind by a killed run) makes
  `git add` refuse it: observed on 2026-11-02 — the old code ignored `git add`'s exit
  status and read only `stderr`, so the snapshot died silently and printed
  `. snapshot snapshot failed:` with nothing after it. Now `git add -A --ignore-errors`
  stages everything else and the refusal is reported once per turn
  (`. snapshot failed: error: 'x/' does not have a commit checked out …`). The nested
  repo itself is still untracked: protect it with its own commits, or delete the scratch dir.
- **`delete_file` deletes one file only.** Directories return an error telling the model
  to use `run_shell` with `rm -r`.

## Output size

- Since 2026-11-02, any tool output over 6 000 chars (2 000 until 2026-11-07; except `read_file`) is saved to
  `.cracked/out/<n>-<tool>.txt` in the workspace; the model sees 600 chars from each end
  plus the path. `.cracked/` holds a `*` `.gitignore`, so snapshots skip it. Files pile
  up across sessions: delete `.cracked/` when you like.
- `read_file` pages instead: at most 8 000 chars, cut at a line boundary, ending with
  `[more: read_file offset=N]`.
- The spill file starts with the `[exit N]` line, so its line numbers are off by one
  from the command's output. The model noticed this itself in the 2026-11-02 live run.

## Images

- **Grainy frames do not compress.** Rendered CRT video frames (grain + scanlines) were still
  over the cap as a 1024 px PNG; as a 1024 px JPEG the same frame was 89 KB (js-video run,
  2026-11-03, DeepSeek V4.1 Flash on Ollama). Oversize files therefore always go out as JPEG.
- **One image per turn.** The model calls `see_image` once per turn even with 8 frames to
  check, so a video job spent 35 of its 114 turns looking at images. Leave `max_turns` at -1
  for that kind of job.

## Web

- **DuckDuckGo returns `no results` on rapid repeat queries.** In one run 3 of 4 later
  searches came back empty while the first two worked; in `toolcheck.py` the web check
  made 5 searches, several empty, and burned all 8 turns and 21 879 tokens doing it.
  Treat `no results` as throttling, not as "does not exist"; vary the query or
  `web_fetch` a known URL.
- **DDG snippets are parsed from HTML** with `html.parser`. Nested tags inside a snippet
  can truncate it (marked `shortcut:` in `cracked_code/web.py`). No API key, so it can break
  without warning if DDG changes markup.

## Speed

Measured with `scripts/toolcheck.py --max-turns 8` on the 5070 Ti: a 4-turn file cycle
took 205 s, a 3-turn port scan 122 s, a 3-turn deny-list check 27 s, a 4-turn edit 11 s.
The spread is generation length and prefill, not the harness: the same prompt costs more
once the history is long. Plan real work in minutes per task, not seconds, and keep
`--max-turns` low when probing.

- **Anything else drawing on the GPU slows Strata, a browser tab included.** On 2026-11-09 the dashboard's
  CRT effects (grain, blend modes, 60 fps canvas, blur) took 46% of the GPU and 460 MiB of VRAM in Firefox:
  decode 100 → 56 tok/s, prefill 3,065 → 1,869, task 11 took up to 96 s instead of ~55 s. The dashboard now opens
  in **lite** mode (numbers only, no effects or graphs), which measures the same as an empty tab. Its **FX**
  button brings the graphs back: don't leave it on while a task runs. The Qwen Code dashboard (:8095) still
  costs ~38% decode when open. If tok/s drops, run `nvidia-smi pmon -c 5` and look for `firefox` in the `sm` column.
- **Model load:** ~28 s from the NVMe (`/data/Strata-data`, since 2026-11-09), was ~111 s from the SATA SSD.
  The disk doesn't affect tok/s: once loaded, inference reads ~2 MB/s from it.

## Environment

- **nmap inside a user namespace:** `-O` (OS fingerprinting) needs real root and fails.
  Connect/SYN scanning of localhost worked fine from inside bwrap.
- **Strata holds the whole GPU.** Nothing else can load a model while it runs, so the
  harness and the model cannot both be swapped mid-session.
- **`run_shell` output goes to temp files, not pipes.** A backgrounded server
  (`http-server . &`) inherited the pipe and held it open, so the call waited out its whole
  timeout and returned "timed out" while the server was in fact running (2026-11-03). A plain
  `&` job still dies with SIGHUP when an SSH session closes, so use `setsid nohup … > log 2>&1 &`
  for anything that must outlive the session (the tool description says so).

## Strata's prompt cache (measured 2026-11-10)

- **Never send a request without the tool list.** Qwen3.8's chat template writes the tools at the very top of the
  prompt, so `tools: []` changes the prompt from its first tokens and Strata re-reads the whole history. On a
  71,600-token history: 70,642 tokens re-read in 22.2 s without the list, 37 tokens in 0.5 s with it. The final
  report, auto-compact and `/btw` therefore send the list and drop any call in the reply (`agent.text_only`).
- The same applies to anything that changes the list mid-session: `/incognito` costs one full re-read each time
  it is toggled.
- Strata's own defaults for Qwen3.8 (`GET /props`): temperature 1.0, top_p 0.95, top_k 20, no penalties. Cracked
  sends only `temperature` (0.2), so top_p and top_k are Strata's.
