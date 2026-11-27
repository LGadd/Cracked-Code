# Plan mode: design (confirmed by the user 2026-11-11; built the same day, not yet run against a model)

**Built 2026-11-11** in `cracked_code/plan.py` (the file format, parser, step messages and read-only check) and
`agent.drive` / `agent.run_plan` (the step runner), plus `/plan` in `repl.py`, `todo_write` in `tools.py`, config
`auto_checklist`, and the dashboard's `checklist` switch and plan list. Where the build differs from the design below:
- **Folding:** finished steps fold through the existing task fold, so only once the prompt passes `FOLD_ABOVE` (24,000
  tokens). Below that line the history is kept whole, which keeps Strata's cache. The design said every step.
- **Pausing:** there is no `/plan stop`. Esc pauses mid-step, and `/plan` carries on. Typing a new task while the model's
  own checklist runs also pauses it.
- **The plan menu replaced `/plan go` (2026-11-11, asked by the user after the first real run).** When a plan is written,
  and at every pause, an arrow-key menu asks: **y** auto (every step in a row; stops only when the plan has to be
  rewritten), **m** manual (one step, then the menu again), **e** edit (the user says what to change, the model rewrites
  the plan with no tools, and the exchange stays in the history), **n** not now (`/plan` brings the menu back). Outside a
  colour terminal you type the letter. The mode is saved in the plan file (`mode=auto|manual`).
- **Why it changed: the step-1 loop (2026-11-11, `~/win-usb`).** The model wrote checks as sentences
  (`` `./audit-pe-tools.sh` exits 0 and prints AUDIT_OK… ``). They ran as shell, exited 127, and failed a finished step 3 times;
  the replan rewrote from step 1 and paused with "/plan go carries on", which started step 1 again. Typing "start step 2"
  ran outside the plan, so it was never ticked. The model also put 14 dummy commands (`the`, `exits`, `AUDIT_OK`…) in
  `~/.local/bin` to make the sentence "pass" (deleted 2026-11-11). Fixes: `parse` refuses a check that is not a command
  (backticks, or a first word that is not on PATH, a path, or a shell word); a check that exits 127 at run time is
  dropped, not retried; a replan always shows the menu outside Fuck It; the model can stop a run with `PLAN WRONG: <why>`;
  shell commands that write outside the workspace or use sudo ask even in Fuck It (`Guard.writes_outside`).
- **Typing while a plan waits** is an ordinary message; afterwards the harness says which step the plan waits at.
- **Shift+Tab:** no PLAN stop. Shift+Tab is on hold with the LOCKED IN combo (`ideas.md`).
- **Replanned steps** are remembered only while Cracked runs: after a restart, a step can be replanned once more.
- **Per-step turn limit, check tries and timeout** are constants (`agent.STEP_TURNS = 25`, `CHECK_TRIES = 3`,
  `CHECK_TIMEOUT = 600`), not config keys.
- **The final check** is a normal step: the model can fix small gaps itself instead of adding new steps.

Written 2026-11-11 at the user's request. Nothing here is implemented yet. Build order and estimates are at the end.

## The idea in one paragraph

The model researches and writes a plan as a checklist of small steps. **The harness owns the plan from then on:** it stores
it in a file, gives the model **one step at a time**, decides when a step is done, ticks it, and moves on. The model never
has to remember the plan, call a todo tool or keep track of where it is. It only ever sees: the goal, what is done (one line
each), the step to do now, and the next step's title. A small step with a clear finish line is the kind of task a small
local model already does well.

## Why the harness drives, not the model

- `todo` / `todo_write` (built 2026-11-06, parked): Qwen3.8 on Strata **called it 0 times in 6 runs** (`parked.md`).
- The stack technique's `focus` / `done` tools: "the model never calls them unprompted" (`roadmap.md`).
- What the model *does* do reliably is **end its turn when it thinks it has finished**. The executor uses exactly that
  signal: the end of a turn means "this step is done". No new tool, no schema tokens.

## Three phases

### 1. Research and write (`/plan <goal>`, or Shift+Tab to a PLAN stop)

- **What research may do depends on the mode (changed by the user 2026-11-12; before, every mode was read-only with a
  short safe list and everything else asked, even in Fuck It):**
  - **Manual and WE NEED TO TALK: read anything.** `write_file`, `edit_file`, `replace_def` and `delete_file` are refused
    ("plan mode: research only"). Shell runs unasked unless `plan.looks_like_write` says it changes something (a redirect
    into a file, `rm`/`mv`/`cp`/`mkdir`/`tee`…, `sed -i`, `find -delete`, git other than log/status/diff/show…, a package
    install, `sudo`); those ask. Option A (user's pick): scripts and programs (`python3 x.py`, `make`) run unasked, so
    one that writes on its own is not caught.
  - **Fuck It: read and write anything.** Writes and all shell run; the research prompt says to write only to test ideas
    and not to carry out the plan. `delete_file` and `Guard.writes_outside` (outside the workspace, sudo) still ask.
  - **Every mode ends at the plan menu**; nothing runs the plan before the user picks.
  - The rule is checked at each tool call, so Shift+Tab mid-research applies at once; the prompt text is set at the start.
- The research prompt asks for a plan in one strict format (below): 5 to 25 steps, each one action, at most 2 files, small
  enough for about 10 turns, and **a check** where one exists (a command that passes when the step is right).
- The harness **parses** the reply. If it doesn't parse, or has more than 30 steps, or a step names more than 2 files, the
  model gets one text-only retry with the exact problem. If that also fails, the user gets the raw text to fix by hand.
- The plan is saved to `.cracked/plan.md` in the workspace.

Plan format (the parser's contract, and what the user edits):

```markdown
# Plan: <goal, one line>
- [ ] 1. Add Inventory.save(path) writing JSON | files: inventory/core.py | check: python3 -m unittest -q
- [ ] 2. Add load(path) with validation | files: inventory/core.py | check: python3 -m unittest -q
- [ ] 3. Tests for save/load round trip and bad JSON | files: tests/test_inventory.py | check: python3 -m unittest -q
```

`[ ]` is to do, `[x]` done, `[~]` in progress, `[!]` blocked, `[-]` skipped. Each step has a one-line result added when
it is done: `  → wrote save(); 18 tests pass`.

### 2. Review (`/plan` shows it and the plan menu, `/plan edit` opens `$EDITOR`)

- The plan is printed as a checklist, then the plan menu (y auto · m manual · e edit · n not now). The user can also
  edit the file (add, delete, reorder, reword) with `/plan edit`; the harness re-parses it.
- With **WE NEED TO TALK** on, the model is also asked which steps it's unsure about, and lists them before the menu.
- **Always wait for the menu, in every mode including Fuck It** (decided by the user 2026-11-11). No autostart key.

### 3. Execute: the harness walks the checklist

For each unticked step, the harness builds a **step message** (a user message, so the prompt cache only grows at its end):

```
[PLAN step 3 of 9] Goal: add save/load to the inventory package
Done: 1. save() written, 18 tests pass · 2. load() with validation, 22 pass
NOW: 3. Tests for save/load round trip and bad JSON (files: tests/test_inventory.py)
Finish line: `python3 -m unittest -q` passes. Then reply in one line with what you did. Do not start step 4 (README section).
```

Then the normal agent loop runs, with all its loop checks, snapshots and approvals. When the model ends its turn:

1. **Check.** If the step has a `check:`, the harness runs it. Exit 0 means done. A failure goes back to the model as
   "the check failed: <first 40 lines>; the step is not done", and the loop continues. This uses the same "check before
   done" idea as in `ideas.md`, made concrete by the plan.
2. **Tick.** Set `[x]`, record the model's one-line reply as the step's result, and save the file.
3. **Shrink.** With LOCKED IN off, fold the step's working turns down to `step N → result` (the existing fold, applied per
   step). Context stays at roughly "system prompt + plan + current step", however long the plan. With LOCKED IN on, nothing
   is folded (see below).
4. **Next.** Send the next step message. The model never chooses what comes next.

**When a step doesn't finish:**
- Each step has its own turn limit (`plan_step_turns`, default 25), separate from `max_turns`.
- The step fails its check 3 times, or hits the turn limit, or a hard loop check fires: the step is marked `[!]` and the
  harness makes **one replan request** (text-only): "Step N is blocked because <reason>. Rewrite the remaining steps."
  The answer is parsed in the same format.
  - Outside Fuck It: the user sees the new remaining steps and the plan menu.
  - Fuck It: the new steps are applied, and the terminal and dashboard say so.
  - A second block on the same step stops the run and asks the user. It never loops for ever.

**Messages queued while a plan runs** (Enter mid-task) are handled as today: the model reads them before its next turn.
They also go into the ledger. If one starts with `+` (e.g. `+ also add a --json flag`), the harness adds it as a new step
at the end of the plan, marked `(you)`, instead of letting it hijack the current step.

**The end.** When every step is `[x]` or `[-]`, the harness runs a final turn: all distinct checks once more, then "compare
the result against the goal and say what, if anything, is missing". Anything missing becomes new steps (with approval
outside Fuck It).

## With LOCKED IN on or off

The plan file **is** the source of truth in both modes, so neither mode can lose it.

| | LOCKED IN off (default) | LOCKED IN on |
|---|---|---|
| Finished steps | folded to one line each: low context, fast | kept in the history: the model can look back at the details |
| Step message | goal + done list + now + next | the same, plus the ledger (every ask, failed call, changed file) |
| Compaction | rarely reached, because of the per-step fold | at 70%. The note is the plan checklist + ledger + summary, so the position in the plan survives |
| Best for | long plans of independent steps | plans where later steps depend on the details of earlier ones |

Turning LOCKED IN on or off mid-plan (header switch) takes effect at the next step, the same way as today.

## Survives crashes and restarts

- `.cracked/plan.md` is written after every tick. `cracked -c` (or `/resume`) with an unfinished plan asks: "continue the
  plan at step N?"
- `/undo` rolls back a step's files (git snapshots are already taken each turn). `/plan back` also un-ticks the last step.

## Commands, keys, dashboard

- `/plan <goal>` research, `/plan` show + menu, `/plan edit`, `/plan skip N`, `/plan back`, `/plan clear`.
- Shift+Tab: a PLAN stop in the mode cycle. Its position is still to be decided, together with the LOCKED IN combo stop
  in `ideas.md`.
- Status bar: `plan 3/9 · Tests for save/load…`.
- Dashboard Live tab: the checklist with ticks and the current step highlighted, read from `.cracked/plan.md`
  (the dashboard already finds each session's workspace).

## The model's own checklist: `todo_write` comes back and plugs into the same executor

Asked for by the user 2026-11-11, for complex real-world tasks. The removed code is in `14ec136^` (removed by `14ec136`;
`parked.md` has the evidence).

**What changes from the parked version:** before, the list was only displayed, and the model had to keep it up to date
itself. Now the list goes into the **same plan file and executor** as plan mode, so the harness drives it.

1. **Creating a list.** The model calls `todo_write(items)` with 3 or more items, each one line, with an optional `check`.
   The harness writes them to `.cracked/plan.md` marked `source: model`, using the plan format.
2. **Following it.** From the next turn the executor takes over, as in phase 3. The model gets one step message per item,
   the end of its turn means the item is done (the check runs if it gave one), the harness ticks it, and the next item
   follows. With LOCKED IN off, finished items fold to one line, as plan steps do.
3. **Changing it.** The model can call `todo_write` again to add, split, reorder or drop items. Items already done keep
   their tick. A list that drops an undone item is accepted, and the item is shown as `[-] (dropped by the model)`.
4. **No plan menu for the model's own list.** It is the model working inside a task the user already started; stopping
   to ask would break the run. Exception: with WE NEED TO TALK on, the list is shown and the user approves it first.
5. **One list at a time.** If a user plan is running, `todo_write` is refused ("a plan is running; finish the step").
   The user's plan always wins.
6. **The list survives** compaction and restarts in the same way as a plan (the file is the source of truth).

**Subtle signs in Cracked that the model made itself a list:**
- One dim line when it is created: `. checklist: 7 steps (the model's own) · /plan to see it`.
- The status bar under the prompt gets a muted `☐ 3/7 Write the load() tests…`, where a user plan shows `plan 3/9`.
  Nothing flashes and there is no colour change.
- Ctrl+O (details) prints the full list with ticks. `/plan` shows it, and `/plan stop` or `/plan clear` work on it too.
- Dashboard Live tab: the same checklist as for plans, labelled "model's checklist".

**Costs and risks:**
- About 150 tokens of tool schema on every request (measured when it was first built). Nothing else unless it is used.
- **The main risk is the evidence:** Qwen3.8 on Strata called `todo_write` **0 times in 6 runs**, although Kimi K3.1 did
  call it. Those were short suite tasks, so long real tasks may differ, but don't count on the model starting it.
  Suggestion below.

**Suggestion: the harness offers the list when the model doesn't make one.** If a task reaches 8 turns with no list, or
right after a compaction, the harness sends one text-only request: "List the remaining steps of this task as a checklist."
The harness parses the reply into the same file, so the executor runs whether the model calls the tool or not.

`auto_checklist` is its own config key and switch (decided by the user 2026-11-11):
- It has its own switch in the dashboard header, next to `locked in`, and it works the same way: the switch saves to the
  config file, so a running session picks it up at its next turn.
- **Turning LOCKED IN on also turns `auto_checklist` on.** Whatever turns LOCKED IN on (the header switch now, Shift+Tab later)
  writes both keys in one save. If `auto_checklist` was off, its dashboard switch shows on straight away.
- Turning LOCKED IN off leaves `auto_checklist` as it is. The user switches it off separately if they want to.
- The harness reads only `auto_checklist`. The link to LOCKED IN lives in whatever writes the switch, so a config file edited
  by hand to `locked_in = true` with `auto_checklist = false` is taken as written.
- Default: off. It costs one request (about 1 to 3 s) per long task.

Build: about 2 h (restore the tool, connect it to the plan file and executor, the status bar and Ctrl+O signs, the
refusal while a plan runs) plus about 1 h for `auto_checklist`. It goes after stage 3 below, because it needs the executor.

## What it costs

- Research: one extra phase (a few turns of reading plus one plan reply, about 500 to 1,500 tokens).
- Each step: one step message (about 80 to 200 tokens) plus the check command's run time. A failing check costs turns, but
  it catches a false "done".
- With the per-step fold, a long plan uses **less** context than one long task today. The trade-off is that folds rewrite
  the history, so each step starts with one uncached prompt read (about 1 to 2 s on Strata, the cost measured for task folds
  on 2026-11-11).

## Risks and open questions

1. **Steps too big or too vague.** This is the main risk. Mitigations: the format limits (2 files, a check), the parser's
   retry, user review. Measure how often a step needs a replan.
2. **The model starts the next step anyway.** The step message says not to. If it happens, the check still decides, and
   the next step's message says "may already be done: check first".
3. **No check possible** (docs, design work). Then the end of the model's turn alone counts as done, which is the weakest case.
   Maybe require the one-line result to name the file and section changed.
4. ~~Shell in research~~ Decided 2026-11-11: a safe read-only list runs, the rest asks (phase 1). Changed 2026-11-12: see
   phase 1 (manual reads freely, Fuck It reads and writes).
5. ~~Who approves the plan~~ Decided 2026-11-11: always the plan menu, Fuck It included (phase 2).
6. **The write detector must stay tight.** `tests/test_plan.py LooksLikeWrite`: commands that change things, like
   `find -delete`, `git checkout`, `cat x > y` or `ls | xargs rm`, must still ask.

## Build order (estimates for building it; the user runs the live tests)

1. Plan file: format, parser, writer, round-trip tests: **about 1 h**
2. Research phase: read-only guard, plan prompt, parse and retry, `/plan` and `/plan edit`: **about 1.5 h**
3. Executor: step messages, end of turn means done, check runner, tick and result, per-step fold, step turn limit: **about 3 h**
4. Blocked steps and replan, `+` queued steps, final check: **about 1.5 h**
5. Resume, Shift+Tab stop, status bar, dashboard checklist: **about 2 h**
5b. `todo_write` back, connected to the executor, the subtle signs, and `auto_checklist`: **about 3 h**
6. Live test on Qwen3.8 IQ3 (user), then a benchmark: plan on against off on the long tasks 11 to 13 plus one 10+ step
   task, 3 runs each.

First step to build: 1 (plan file and parser). It is useful on its own and everything else depends on it.
