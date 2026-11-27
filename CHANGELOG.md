# Changelog

## 0.1.0 (2026-11-27)

The first public release: the terminal harness. The web dashboard follows in a later release.

- **Agent loop** with native tool calls: `read_file`, `write_file`, `edit_file` (one or several edits, all or none),
  `replace_def` (a Python function or class by name), `delete_file`, `run_shell`, `web_search`, `web_fetch`, plus
  `see_image` when the server lists image input for the model. A syntax check runs on `.py`, `.json` and `.js`
  writes.
- **Built for Qwen3.8 Flash-Next on Strata:** no config needed on Strata's default port (the model, window and image
  support are read from the server); an append-only history that keeps Strata's prompt cache; thinking capped per
  turn (`auto_think`); a reply that repeats itself is cut early. Other OpenAI-compatible servers work, best effort.
- **Safety:** approvals with the diff shown first, a deny-list for the unrecoverable, and git snapshots before every
  turn in a separate store (`/undo`, `/diff`). Three modes on Shift+Tab: manual, Fuck It, INVESTIGATE (read only).
- **Context:** large outputs spill to files, auto-compaction, finished tasks fold away once the history is large,
  `LOCKED IN` mode for long tasks.
- **Plans:** `/plan <goal>` researches and writes a plan, then runs it one step at a time; the model's own checklists
  (`todo_write`) and `auto_checklist` use the same runner.
- **Terminal:** a live status line, mid-task steering, a coloured answer with tables and syntax-coloured code,
  `/` and `@` menus, sessions (`cracked -c`, `/resume`), `/btw`, `/init`, `/jobs` with Ctrl+B to background a
  command, `theme = "light"`, and a 256-colour fallback.
- **Project rules** in `CRACKED.md` (or `AGENTS.md`), global rules in `~/.config/cracked/CRACKED.md`.
- **Telegram pings** (optional) when a task ends or a question waits.
- **Windows** support through Git Bash.
