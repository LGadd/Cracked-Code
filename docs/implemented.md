> **2026-11-10: trimmed to Qwen3.8 on Strata.** Sections below that describe `exact_prefix`, `technique`
> (trail, stack), `evict_stale`, `backtrack`, `contract`, the `vision` / `stream` / `anchor` / `fold_*` switches,
> `grep`, `tool_search`, `defer`, `todo` or `batch_check` are history: the first group is deleted, the last five
> are parked (`docs/parked.md`). Current settings: `docs/setup.md`. Tag `pre-trim` is the tree they describe.

# Implemented (v0.1, 2026-11-02)

What exists and has been run, not what is planned. See `plan-v0.1.md` for the approved
design and `roadmap.md` for what is not here.

> Standing rule: any session that changes behaviour updates this file **and**
> `gotchas.md` in the same session. A stale doc is worse than none.

## Code — ~2 300 lines, no runtime dependencies

| File | Lines | What it does |
|---|---|---|
| `cracked_code/tools.py` | 367 | the 8 tools: JSON schema and handler in one `Tool` record; `call()` dispatches and turns every failure into `ERROR:` text |
| `cracked_code/images.py` | 162 | image input: magic-byte sniff, header size, pass-through under the cap, `ffmpeg` → 1024 px JPEG over it, vision probe (`/v1/models` input modalities, then Ollama) |
| `cracked_code/config.py` | 108 | `~/.config/cracked/config.toml`: load (flag > env > file > default) and the flat writer the dashboard uses |
| `cracked_code/safety.py` | 187 | `Guard` (workspace boundary, approval prompt, 9-pattern deny-list) and `snapshot()` (per-turn git commit) |
| `cracked_code/web.py` | 105 | keyless DuckDuckGo HTML search, URL fetch, HTML→text with `html.parser` |
| `cracked_code/__main__.py` | 167 | argparse (`cracked`), REPL, `-p` one-shot |
| `cracked_code/llm.py` | 123 | urllib POST to `/v1/chat/completions` with `tools`, streamed (SSE rebuilt into one reply, plain JSON accepted); returns text, calls, `finish_reason`, token counts, Strata's `timings` |
| `cracked_code/agent.py` | 259 | the loop and the system prompt |
| `cracked_code/repl.py` | 538 | the REPL: input (/ and @ menus, paste, `\` continuation, history), slash commands, sessions, `/undo` |
| `cracked_code/live.py` | 264 | the terminal while a task runs: spinner line, mid-task Ctrl+O / Shift+Tab / Enter to steer, pause for approvals |
| `cracked_code/prompt.py` | 125 | the `>_` prompt on prompt_toolkit: / and @ menus, colours, Shift+Tab, Ctrl+O |
| `cracked_code/ui.py` | 214 | colours, collapsed/expanded lines, the one-line tool summary, hooks for the live view |

Tests: 340 lines across `test_tools.py`, `test_safety.py`, `test_agent.py` (39 tests).

Installed command: `cracked` (`[project.scripts]`); the package is `cracked_code` (`harness` until 2026-11-27), so
`python3 -m cracked_code` runs straight from a checkout.

## The loop (agent.py)

Per turn: snapshot the workspace → send the messages → print
`. turn N: X in / Y out / <finish>` → append the assistant message → **execute every
tool call in the reply**, one `{"role":"tool","tool_call_id":…}` per call → loop.
Returns when the model replies with no calls. `finish_reason: length` prints a truncation
warning. `max_turns` is an optional backstop: default -1 (no limit), set in the config, with
`--max-turns N`, or `/turns N` in the REPL. A fixed 40 cut off a research + build + host task at
turn 40 with the page built but unchecked and unhosted (2026-11-03).

**Loop check** (added 2026-11-02): after each turn, the loop counts two things with no
extra model calls: the same call (tool + args, whitespace ignored) 3× in the last 8
calls, or 4 failed results in a row (`ERROR:`, `DENIED:`, `[exit ≠0]`). First trip →
a `Loop check:` user message telling the model to stop, restate its assumptions and change
approach or ask. Second trip → the REPL asks `[harness] stuck: … Type a hint, or Enter to
stop`; under `--yes` it just stops. **Every stop** (stuck or out of turns) ends with one
tool-less request for a final report, so a capped run never returns empty. Added after
live test 02 used all 10 turns and returned nothing.

## Context (added 2026-11-02)

Built to grow the history as little as possible, and to rewrite it rarely (each rewrite
makes the server re-read the whole prompt once; an unchanged prefix is cached).
- **Spill:** tool output over 6 000 chars (2 000 until 2026-11-07) goes to `.cracked/out/`; only head + tail + path
  enter the conversation. `read_file` pages at 8 000 chars instead. `web_fetch` lost `max_chars`.
- **Auto-compact:** when a turn's prompt uses `--compact-at` (0.4) of `--context` (the server's window), one
  tool-less request summarises the history in ≤10 bullets. The history becomes: system,
  latest task + summary, last 2 turns verbatim. Cuts only at an assistant message.
- **Visible:** every turn line ends with `ctx N%` (prompt tokens only: reasoning is counted in
  `completion_tokens` but never kept in the history).
- **Short replies:** two prompt lines: answer first, no recap, lists ≤5, one next step;
  analyse big output with a command that prints only the answer.
- Live run 2026-11-02 at `--context 3000`: spill file written and git-ignored; turn 3
  compacted 3 messages into a 734-token summary (88% → 62%); the answer was still right.
- Skipped on purpose: an output search index (context-mode style), trimming every turn
  (breaks the prompt cache), a persona, shorter tool schemas.

## Context techniques (added 2026-11-05, `harness/technique.py`)
- **Anchor cache pin** *(replaced by the append-only history in variant B, `f6c7b55`; only `/btw` still pins)*: before appending the anchor, each request sends `strata_prefix: {"messages": <history length>}`,
  so Strata keeps a checkpoint before the (always-changing) anchor and each turn reads only new tokens. Other servers ignore it.
- **`fold_tasks` (default on):** when a task ends, `agent.run` deletes its working turns and keeps the ask plus one
  assistant message: the answer + `[harness: this task changed …]`.
- **Options, off by default:** `technique` latest|trail|stack, `evict_stale`, `backtrack`, `contract`. See `setup.md`
  and `roadmap.md` ⚑. The turn line shows the anchor size (`anchor ~Nt`); stack/backtrack/evict/contract events print as `. ` notes.

## Images (added 2026-11-03, trimmed 2026-11-04)

`see_image` puts a picture into the conversation, so it is treated as a context cost.
- **Caps:** long edge 1024 px, base64 ≤1 200 000 chars (~900 KB). A file under both is sent
  untouched; a bigger one is scaled by `ffmpeg` to a 1024 px JPEG. No ffmpeg → an `ERROR:`
  sentence telling the model to downscale it first.
- **Why no Python codec:** the first version (the second PC, 2026-11-03) decoded and resized PNGs
  in pure Python (438 lines, per-pixel loops). It was cut to keep the harness small; the one case
  it covered without ffmpeg (oversize PNG) now asks the model to downscale.
- **Wire format:** the tool message becomes a content list (`text`, then `image_url` parts)
  only when an image was attached; otherwise it stays a plain string. Verified against a stub
  server (`tests/stubserver.py`) that records the request.
- **Gated:** `vision` config key, or `--vision` / `--no-vision`. `"auto"` asks the server once
  at startup: `/v1/models` `architecture.input_modalities` first (Strata reports `["text"]` when
  loaded without its image encoder), then Ollama's `/api/show`; anything else = off. With it off,
  `see_image` is not offered to the model at all. A server reloaded in another mode needs a new session.
- **`--image PATH`** (repeatable) attaches files to the first user turn.
- **Not yet run against a real vision server on the Strata box.**

## Config (added 2026-11-04)

One TOML file for the CLI and the dashboard (`docs/setup.md` § Configuration). No model is
built in: a missing `model` stops startup with the file path to set it in.

## Terminal (added 2026-11-02)

- `cracked_code/ui.py`: dashboard palette. Tool calls magenta, results light green (red on
  `ERROR`/`DENIED`/non-zero exit), status muted, warnings amber, `>_` neon.
- `cracked_code/prompt.py` (prompt_toolkit, since 2026-11-07; `readline` before) gives arrow keys, history
  and line editing at `>_` and approval prompts, on Linux, macOS and Windows.
- **Shift+Tab** cycles the mode (manual → Fuck It → investigate). At `>_` it is a key binding that
  keeps the draft; at an approval prompt it answers `/mode`. At an approval prompt it means yes and
  stops asking. The prompt shows `[FUCK IT MODE]` while it's on.
- Everything above is off when stdout isn't a tty, with `NO_COLOR`, or with `TERM=dumb`.
  Until 2026-11-07 the dashboard ran the REPL in a `TERM=dumb` pty and parsed these plain lines; it is watch-only now
  (it reads Strata `/metrics`, the `cracked` process and its saved session).
- The model's reasoning (`reasoning_content` from Strata/llama.cpp, else Ollama's `reasoning`) is
  terminal only: never sent back to the model, and not shown to the dashboard or in logs and pipes.

### Modes: Shift+Tab cycles manual → Fuck It → INVESTIGATE (2026-11-13; was WE NEED TO TALK from 2026-11-04)

- **manual**: approvals ask. **Fuck It** (`[FUCK IT MODE] >_`): everything auto-approved except deletes.
  **INVESTIGATE** (`[INVESTIGATE] >_`, amber): read only. Each task gets `agent.INVESTIGATE` appended (read,
  report what you found, stop; changes are suggestions), and `tools.call` backs it up: file writes and
  `todo_write` return `INVESTIGATE_ONLY`, a shell command that `plan.looks_like_write` asks (always_ask), as in
  manual `/plan` research. No auto checklist. The anchor repeats the rule; Shift+Tab into it mid-task adds it to
  the running task. Running a plan from the plan menu switches it off.
- **Why it replaced WE NEED TO TALK:** that mode only asked the model to check with you, and a long run in it
  never asked once, even after the 2026-11-11 rewrite. Investigate is enforced by the harness, not the prompt.
- Default (other modes): the system prompt says don't stop to ask questions.
- **Why the task, not the system prompt:** editing message 0 on a switch would make the server
  re-read the whole history (minutes on Strata with a long context). Appending costs ~60 tokens per
  task, only while the mode is on, and the cache prefix never changes.
- Live run 2026-11-04 on Strata, "Add a README to this folder.": 3 read-only shell checks, then 4
  options (minimal / scaffold / content-describing / you tell me), nothing written.
- The dashboard parses both prompt prefixes and the three mode lines; its mode button cycles the same way.

### Live view while a task runs (2026-11-04, `cracked_code/live.py`)

Replies stream (`stream = true`, the default), so the terminal can say what the model is doing
instead of sitting silent. Costs no context: it only shows the stream and the calls already made.
- **One spinner line** at the bottom: phase + detail + `12s · 340 tok · ctx 14% · ^O details`.
  Phases: `reading` (server reading the prompt, before the first token), `thinking` (the tail of
  the reasoning as it streams), `writing` (the answer), `calling <tool>` (the call streaming in),
  then the tool's own verb while it runs (`shell <command>`, `read <path>`, `edit`, `search`, …).
- **Collapsed (default):** one line per finished tool call, `✓ shell  ls -la  14 lines · 2.3s` (a failure keeps its `[exit N]`)
  (red `✗` with the error), warnings in amber, and `done · 48s · 7 turns · 12 tool calls · ctx 14%`
  at the end. Turn lines, snapshots, raw calls and reasoning only show expanded. While it thinks,
  the spinner shows the last *finished* sentence of the reasoning, so it changes once per sentence
  (the first version showed the streaming tail, which slid left on every token and was unreadable).
- **Narration (`>_`, 2026-11-04):** the text a model sends *alongside* its tool calls (`content`)
  was received but never printed, and the prompt's "take the action instead of describing it"
  discouraged it. Now it prints as `>_ Reading the config loader before changing it.` in both views
  (dashboard: a `. >_ …` note line), and with `narrate = true` (default false since 2026-11-11) the prompt's first line asks
  for one such line per batch of calls. Same idea as Qwen Code's `>_` (its AssistantMessage prefix for that text); here a
  bold neon `>_`, prompt-and-cursor style, to match the CRT look. Cost: ~15–25 output tokens a turn, kept in the history (~1k tokens over a
  50-turn task). Not yet measured live.
- **Expanded:** reasoning streams in above the spinner as wrapped lines, plus turn lines,
  snapshots and the raw `-> tool {args}` / result lines.
- **Ctrl+O replays the whole session** in the other view, not just what comes next: every line the
  terminal prints is kept in `ui.TRANSCRIPT` with its kind (`all`, `detail` = expanded only,
  `compact` = collapsed only), and a toggle clears the screen *and scrollback* and reprints it.
  Typed tasks and approval answers are recorded too. Cost: terminal memory only (last 5000 lines),
  nothing sent to the model. Side effect: the terminal's own scrollback from before `cracked` is cleared.
- **Keys mid-task:** Ctrl+O expands/collapses (a full replay, above),
  Shift+Tab cycles the mode (below; investigate mode applies from the next turn). Typing shows as `next>` on the spinner line;
  **Enter queues it for the model** (2026-11-07): `agent._loop` adds it as a user message before the next model call
  (`agent.STEERED`, text first so the keep-on-task anchor shows it; also added to the trail's redirects), and if the model
  finished meanwhile its answer is shown and it gets one more turn to read the message. Text not sent with Enter, or still
  queued when the task stops, becomes the next prompt's draft. Ctrl+C still interrupts. On Windows the keys come from `msvcrt`
  and Esc uses `_thread.interrupt_main()` (not tested on Windows yet).
- **Approvals** pause the spinner and give the terminal back to `input()`; the key thread is
  parked so it cannot steal the answer. Checked 2026-11-04 in a real pty: Ctrl+O mid-task, a
  delete prompt answered `n`, back to `>_`.
- Only in a colour terminal with a keyboard on stdin. The dashboard (`TERM=dumb`) and pipes get
  the same plain lines as before; streaming is used there too but prints nothing extra.
- A stream without `usage` gets a chars/4 prompt estimate so ctx % and auto-compact keep working
  (`shortcut:` in `llm.py`). Strata sends usage in its last chunk.

### The REPL (2026-11-04, `cracked_code/repl.py`; Qwen Code's usability features that cost no context)

**Not yet tested** (written on request, tests in `tests/test_repl.py` not run yet).
- **Input:** bracketed paste keeps a pasted block's newlines; a line ending in `\` continues on
  the next. Since 2026-11-07 a menu opens as you type `/` (commands, with descriptions) or `@` (paths);
  Tab completes plain paths. `/commands` show neon (red if unknown), `@files` pink (amber if they won't
  attach); `/cl` + Enter runs the one match, a typo gets "did you mean", `/` alone lists commands.
  Input history is saved across sessions (`~/.local/state/cracked/prompt-history`, copied once from
  the old readline `history`; Up/Down, Ctrl+R; grey suggestions from it as you type, Right takes one).
  `@` without a folder also searches the whole repo (`repl.files`: `git ls-files -co --exclude-standard`,
  else a walk skipping hidden/build folders, cached 3 s; `repl.fuzzy` ranks name-starts, name-contains,
  path-contains, letters-in-order). Alt+Enter new line, Ctrl+X Ctrl+E `$EDITOR`, Ctrl+C clears the line
  (quits on an empty one). The status line is a bottom bar at `>_`, not a line above each prompt.
- **Status line** above `>_`: model · ctx % · tasks · minutes · `/help` (colour terminal only).
- **Esc** (or Ctrl+C) stops a task and keeps the conversation: `agent.repair()` gives every
  unanswered tool call a "not run: interrupted" result and never leaves two user turns in a row,
  so the next task carries on. Before this, an interrupt could leave a tool call without a result.
- **Sessions:** saved after every task to `.cracked/sessions/<time>.json` in the workspace (last 20,
  gitignored with the rest of `.cracked/`). `cracked -c` / `/resume` pick up the latest.
  One-shot `-p` runs are saved too.
- **`/undo`:** every task starts with a `harness: before task N` snapshot; `/undo` restores the files
  to it (`safety.restore`: checkout + delete files added since, then a new commit, so the undone work
  stays in `git log`) and drops that task from the conversation. Repeatable. Commands' side effects
  outside the workspace are not undone.
- **Diffs:** an approval (edit, overwrite) shows the coloured diff above the approval menu (terminal only;
  the dashboard parser would read the indented lines as results). Collapsed tool lines show
  `+N −M` for writes and edits; expanded shows the diff. `/diff` shows everything changed since the
  session started. `edit_file` now checks the match *before* asking, so a bad edit asks nothing.
  None of this reaches the model: the change is kept in `ctx.change` for the terminal only.
- **Commands:** `/help`, `/undo`, `/diff`, `/clear`, `/resume`, `/context` (tokens used and the 5
  biggest messages), `/model` (list, or switch for the session), `/copy` (wl-copy / xclip / xsel /
  pbcopy, else OSC 52), `/export` (markdown, default `.cracked/exports/`), `/turns N`, and
  `!cmd` (your own shell command; output shown, never sent to the model). A typo'd `/word` says so
  instead of going to the model.
- **Bell** (`bell = true`) when a task ends or an approval waits.

## Terminal polish (added 2026-11-27, `ui`, `live`, `repl.cmd_help`, `__main__.banner`)
- Narration is marked `●` (not `>_`, the user's prompt). The answer goes through `ui.answer`: `pretty`, wrapped 2
  columns narrower, with a neon `│` bar on every row (so a terminal copy of an answer carries the bar; `/copy` doesn't).
- `Live(hold=True)` (REPL tasks, `-p`) keeps the done line in `Live.summary` instead of printing it; the caller prints it
  after the answer as `ui.rule(...)`. It counts files changed (`Live.changed`, fed from `ctx.change` per call). `/btw` and
  plan rewrites still print theirs straight away.
- Tab title (OSC 0) from `Live`: `● <phase>`, `⏸ waiting for you` while paused on a question, `✓ done` / `✗ stopped`.
- `call_line`: `[exit 0]` dropped on success; `seconds` appended at ≥ 1 s. The agent subtracts `Live.waited` (time paused
  on a question) so an approval never counts as the tool's time. `ui.took` formats every duration.
- `ui.mode` returns a reverse-video chip, used by the prompt and the live footer alike; `ui.choose(tag=)` puts an
  amber chip before the question (approvals: `ALLOW?`). `ui.diff` numbers each line from the hunk header.
- `pretty` lines up markdown tables (left raw when wider than the terminal) and colours fenced code that names a
  language with pygments (`monokai` dark, `friendly` light); no language or no pygments = as typed.
- Colour: `ui.sgr` emits 24-bit only when `COLORTERM` is `truecolor`/`24bit` (or Windows Terminal / Windows), else
  the nearest xterm-256 colour (`ui._256`). `theme = "light"` swaps the palette via `ui.set_theme`, called right after
  the config loads; `prompt.style()` is built at prompt start for the same reason.
- `ui.cells` / `ui.cut` measure wide characters as 2 columns in `wrap`, the live footer and the input box.

## Shell jobs (added 2026-11-11, `tools.run_shell`, `live.Live`)
- `run_shell` starts the command with `Popen` in its own process group and polls it every 0.1 s.
  Output goes to files in a temp folder outside the workspace: no clutter, never snapshotted, and
  nothing for the model to `cat`.
- **Ctrl+B** (the live view, only while a shell command runs) moves it to the background. The model
  gets "moved to the background as job N, you will be given the result", the rest of that batch of
  calls is not run, and the turn ends, so the user gets the prompt back. Only the user can background
  a command: the model has no option for it, so it cannot poll or forget a job it started.
- When a background job ends, its exit code and output (spilled if long) are added to the user's
  next message, after the task text so the anchor still restates the task. If a task is running, the
  loop adds them before the next turn instead.
- **`/jobs`** lists commands; `/jobs kill N` / `/jobs kill all` stops a foreground or background
  command. Typed mid-task it is answered at once, not queued. Kill, Esc and timeouts take the whole
  process group, and jobs still running are killed when Cracked exits.
- The longest timeout is `shell_timeout_max` (3600 s, was a fixed 600; 0 = no limit). A timeout
  returns the output so far.
- Not built: reviewing a result on its own while the user is idle at `>_` (the prompt is a
  blocking read); listing which files a job changed.

## @files (added 2026-11-06, `repl.attach()`)

`@path` at a word start (so `me@host` is skipped) in a task: a text file is appended after the
task as `[@path lines 1-N of M]` + contents (400 lines / 40 000 chars, then
`[more: read_file path offset=N]`); an image goes through the `--image` path when `vision` is on.
Binary, empty, over 1 MB, no vision: a one-line `[@path not attached: why]`. Folders and missing
paths stay text. The task's first line is untouched, so the anchor still re-states only that.
Each attachment prints `. @path attached (N of M lines)`. Tab after `@` completes paths.
Paths with spaces are not matched (shortcut, in the code).

## Todo list and /btw (added 2026-11-06)

- **`grep(pattern, path=".", glob="")`** (2026-11-07, off by default: config `grep`): ripgrep with `--no-require-git` (so `.gitignore` counts outside git too)
  and the map's skip folders; without `rg`, a Python walk that skips the same folders, hidden folders, binaries and files
  over 2 MB. `path:line:text`, at most 100 lines, then a "narrow it" note. Read-only for the stall check. ~133 tokens of schema.
- **`defer` + `tool_search(query)`** (2026-11-07, off by default: config `tool_search`; `defer` is ignored while it is off): deferred tools are left out of requests; `tool_search` (offered only while
  something is deferred, ~72 tokens, its description lists what is not loaded) loads the ones whose name or description
  matches a word of the query, for the rest of the session. Unknown names in `defer` stop the start with an error.
- **Search benchmark 2026-11-07** (`benchmark/suite/tasks-search/`, 247-file project): no grep 62 s, grep 68 s, grep deferred 67 s, all
  full marks; the model's `grep -rn` via `run_shell` did the same job in the same turns, and never called `tool_search`. Hence off by default.
- **`todo = true`** offers `todo_write(items: [{text, status}])` (status pending | in_progress | done)
  and adds one prompt line: keep a checklist on 3+ step tasks. Each call replaces the list in
  `ctx.todos` (cleared when a task starts); the terminal prints it (`✓` done, `▸` current, `·` to do)
  and the spinner shows `todo 2/5`. The model gets the list back as its confirmation. Off by default:
  ~200 tokens of schema on every request.
- **`/btw <question>`**: one tool-less request with the whole history plus the question; the answer
  is printed and neither is kept. The history is pinned with `strata_prefix` first, so Strata keeps
  its cache point and the next task does not re-read the prompt.

## Live check on Kimi K3.1 (2026-11-06, Ollama Cloud `kimi-k3.1-pro:cloud`, all in the jail)

| What | Result |
|---|---|
| todo, 4-step converter task (`-p`) | 4 `todo_write` calls, 0/4 → 4/4 in step with the work; its tests pass; 59 s |
| todo, terminal (pty) | checklist printed after each update (`✓` / `▸` / `·`), spinner `todo 0/4` … `4/4`; 72 s, 13 turns |
| todo, 2-step task | no checklist: the model judged it under 3 steps (as the rule says) |
| `CRACKED.md` skill table | `. instructions: CRACKED.md (9 lines)`; "write a poem" → `read_file skills/poem.md` first, then followed all 3 rules |
| `@notes.md` | attached, answered in 1 turn with no tool call |
| `/btw` | answered from history; `/context` showed 3 messages before and after |
| `/undo` | removed the task's file; **bug found and fixed**: left the empty folder the task made |
| `@split.png`, `see_image big.png` (2400 px) | both answered right; the big one went as 1024 px JPEG. First run against a real vision model |
| `live-tests/run-all.sh` | 7/7 pass after fixing the jail (below) |

Fixed on the way: the snapshot store snapshotted itself when it sits inside the workspace (`cracked` in `~`,
or the jail); the jail's `HOME` was the workspace, so the home-folder check refused 5 of the 7 live tests
(broken since 2026-11-03); `@image` printed no terminal line.

## Fewer round trips (2026-11-07, in progress)

The fair Kimi benchmark showed Cracked losing time to round trips, not prompt size. Per-request logs showed:
one edit per turn (the prompt said "change one step at a time"), a `find`/`ls` discovery turn, the edit and its
check in separate turns, extra verification sweeps after the tests passed, and 2 000-char spills read back next turn.
Changes: the prompt now asks for all reads in one turn, all sure edits in one turn with the check as the last call,
one verification, then stop; `map = true` lists the workspace in the system prompt; `SPILL_AT` 2000 → 6000.
Tasks 01–04 on Kimi: 01 20.6 → 14.9 s (11 → 4 requests), 03 12.0 → 9.5 s, 04 22.6 → 18.5 s, 02 22.7 → 20.4 s, all full
marks. The rest of the run hit Ollama Cloud's session usage limit (HTTP 429).
`fold_keep` (16000 chars): `fold_tasks` used to make every new ask re-read the files (task 13: one turn per ask).
A fold now keeps the current text of the files the task changed or read (changed first, `.cracked/` skipped, files
that do not fit are left out); older folds lose their copies, so they never pile up. Not yet benchmarked.

## The prompt

No persona, no values, no self-concept — the model is abliterated on purpose. Six lines after
the workspace: act with the tools and batch independent calls (with `narrate`, plus one short line
per batch saying what and why); don't wreck the work (read before
change, verify after, keep it recoverable); nothing else is restricted; **don't stop to ask
questions** — pick the sensible option, say which in one line, carry on, unless the user said to
check with them (added 2026-11-04); answer first, short; analyse big output with a command.

**Instruction files (added 2026-11-06, `agent.instructions()`).** At startup the system prompt
gets `~/.config/cracked/CRACKED.md` (every project) and then the workspace's `CRACKED.md`
(or `cracked.md`, else `AGENTS.md`), each under its own heading. The project file comes last, so it
wins a conflict. Each file is capped at 20 000 chars. A startup line shows what loaded. The files are read once,
so edits take effect on the next start or `/resume`. Per-project "skills" need no machinery:
list `skills/*.md` files and when to use them in `CRACKED.md`, and the model `read_file`s them.

## Safety, as implemented

1. `snapshot()` runs before every turn. Since 2026-11-06 it uses its own bare repo,
   `~/.local/share/cracked/snapshots/<sha1 of the path>/` (with a `workspace` file naming the folder),
   with the workspace as work tree, so the user's own `.git` gets no `harness:` commits. Before
   `git add` it writes `snapshot_skip` plus every file over `snapshot_max_mb` to that repo's
   `info/exclude`, and untracks anything a snapshot already held that now matches (a video that was
   small mid-render). `snapshot = false`: no snapshot, `head()` is None, `/undo` and `/diff` say so.
   Measured on `js-video` (826 MB): 298 MB / 4404 files staged before → 137 files, 3.7 MB, 0.08 s.
   Then: `git init` if needed, `git add -A --ignore-errors`,
   commit `harness: pre-turn N`, skip when nothing is staged. Returns `(sha, None)` —
   printed as `. snapshot <sha>` — `(None, None)` when there was nothing to commit, or
   `(None, reason)` when git refused something, which prints
   `. snapshot failed: <git's own message>`. It never fails quietly.
2. Approvals (since 2026-11-03; the harness is given a task and does it): `read_file` never
   asks, anywhere. In manual mode `edit_file` asks, and `write_file` asks only when it would
   overwrite an existing file (new files and `append=true` don't); `a` remembers that file
   for both. Neither asks in Fuck It Mode. `delete_file` always asks, even in Fuck It Mode; `a` is a
   one-off yes there. Before this, reads outside the workspace asked, and the model
   simply switched to `cat`.
4. Deny-list (refused before any shell spawns): `rm -rf /`, `rm -rf ~`, `mkfs`,
   `dd of=/dev/…`, `> /dev/sd*`, `shutdown`/`reboot`/`halt`/`poweroff`, `chmod -R /`,
   `find … -delete`, fork bomb. Nothing else is refused.
5. `run_shell` has a wall-clock timeout (default 120 s, capped at 600 s), so a stray
   scan cannot hang the session. Big output spills to `.cracked/out/` (see Context below).

## Verified

**Offline:** `python3 -m unittest discover -s tests -t .` → `Ran 39 tests in 1.099s / OK`.
Covers path resolution and the outside boundary, the deny-list (blocked and allowed cases),
approval answers `y`/`""`/`a`/`--yes`, snapshot commit/skip/failure-reason and a workspace
containing a nested repo with no commits, every file tool, shell cwd and
stderr capture and timeout, output clipping, malformed tool arguments, and the loop driven
by a stub LLM (parallel calls, termination, failure-as-text, error→recovery, `max_turns`,
truncation reported, snapshot before turn).

**Live, in the bwrap jail** (Qwen3.8-Flash-Next IQ3_S, 2026-11-02):

| Task | Outcome |
|---|---|
| create → read → delete `notes.md` | 5 calls, correct order, approval prompt honoured, snapshots `42353fe` and `eaa5293`, model re-checked with `ls` |
| nmap top-100 on 127.0.0.1 | self-corrected a bad flag set, cross-checked with `ss -ltnp` and `ps`, reported 22 sshd / 139+445 smbd / 8080 Strata and noted 53 is on `127.0.0.53`, not `127.0.0.1` |
| web search + fetch | found `Niko1221/Strata`, read README and two docs pages, and warned that `stratalab/strata-inference` is a different project |
| guardrails | `rm -rf /` refused before spawning a shell; `/etc/…` write prompted and declined; it then verified `/` was intact |

**`scripts/toolcheck.py`** (five prompts, passes only if the expected tools were called),
run in the jail on 2026-11-02 with `--max-turns 8`:

```
PASS file cycle: 4 turns, 3 calls in 205.3s, 4642 tokens
     called: write_file, read_file, delete_file
PASS shell: 3 turns, 2 calls in 122.1s, 5804 tokens
     called: run_shell, run_shell
PASS web: 8 turns, 11 calls in 55.8s, 21879 tokens
     called: web_search, web_search, web_search, web_search, web_fetch, web_search,
             web_fetch, web_fetch, web_fetch, run_shell, web_fetch
PASS deny-list: 3 turns, 2 calls in 27.2s, 4524 tokens
     called: run_shell, run_shell
PASS edit: 4 turns, 3 calls in 10.8s, 4678 tokens
     called: write_file, edit_file, read_file
5/5 passed
```

No wrong tool, no hallucinated call, no stuck loop in any run. The `web` check used all
8 turns and 21 879 tokens because DuckDuckGo kept returning `no results`; see gotchas.

## Not implemented in v0.1

Session resume, sub-agents, MCP, a text/XML fallback parser, and any refusal layer. Deliberately — see
`roadmap.md` "Explicitly not planned".
