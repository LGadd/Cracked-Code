# Cracked Code on Windows

**Not supported:** Cracked Code is built for Linux. It does run on Windows 10 and 11 with Python 3.11+ and **Git for
Windows** installed, and the offline tests pass there, but nothing is tested against a real model on Windows before a
release.

## Install

```powershell
uv tool install cracked-code      # or: pipx install cracked-code
```

This gives a real `cracked.exe` on your PATH. Avoid a `.cmd` wrapper: cmd asks "Terminate batch job (Y/N)?" after
every Ctrl+C, and Ctrl+C is the harness's own interrupt key.

## What is different from Linux

- **Shell commands run in Git Bash**, not cmd.exe, so the model's POSIX commands (pipes, `&&`, `grep`, `cmd &`)
  work. Cracked finds `bash.exe` from `git`'s location or the usual install folders. It never uses
  `C:\Windows\System32\bash.exe`, which is the WSL launcher. `HARNESS_SHELL=<path>` picks another shell, and
  `HARNESS_SHELL=cmd` goes back to cmd.exe.
- **The model is told it is on Windows:** use `python` rather than `python3` (on Windows `python3` is usually the
  Microsoft Store stub), and start servers in the background with `> server.log 2>&1 &`.
- **Paths use `/`** everywhere the model sees them (tool results, the `@` menu, spill files), because its shell is
  bash. Git Bash paths such as `/etc/x` count as outside the workspace, so writes there ask.
- **Keys:** Shift+Tab, Ctrl+O, Esc and typing mid-task all work, in Windows Terminal and in the classic console.
  Esc stops a task without killing the process, and the session is kept.
- **Colours:** Windows Terminal handles them already. In the classic console Cracked turns on VT mode and switches
  the output to UTF-8, so the spinner cannot crash a cp1252 console.
- **Job kills** take the whole process tree (`taskkill /T`).

## Not ported

`live-tests/*.sh` and `scripts/jail.sh` (the bubblewrap jail) are Linux-only.

## Git checkout

If you copy a checkout from Linux, every `.sh` file may show as modified because the exec bit is lost:
`git config core.fileMode false` fixes that.
