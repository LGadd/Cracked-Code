# Cracked Code on Windows (beta)

Windows 10 and 11 are supported **in beta**: the offline tests run on Windows in CI for every change, and the setup
below was installed from GitHub and tested against a real model. Linux gets more use and more testing.

## Tested setup (2026-11-27)

| Piece | Version |
|---|---|
| Windows | 11 Pro |
| Python | 3.14.3 (CI also runs 3.11 and 3.13) |
| Git for Windows | 2.53 (provides Git Bash, which runs the model's shell commands) |
| uv | installs the `cracked` command |
| Model server | Ollama 0.40.14 with `glm-5.3-flash:cloud` (Ollama Cloud) |
| Cracked Code | 0.1.0, from `git+https://github.com/LGadd/Cracked-Code.git@v0.1.0` |

What passed: a task that wrote `hello.py`, ran it in Git Bash and reported the output; `cracked -c` resuming that
session; a second task that edited the file and ran it again; `/undo` putting the file back.

Not tested yet on Windows by hand: the interactive keys while a task runs (Shift+Tab, Ctrl+O, Esc, typing to steer).
They worked in the 2026-11-13 port test and their code is covered by the offline tests.

## Install

1. Install **Python 3.11+** (python.org or the Microsoft Store), **Git for Windows** and **uv**
   (`winget install astral-sh.uv`).
2. Install Cracked Code:

   ```powershell
   uv tool install git+https://github.com/LGadd/Cracked-Code.git@v0.1.0
   ```

   This puts a real `cracked.exe` in `%USERPROFILE%\.local\bin` (run `uv tool update-shell` once if that folder is
   not on your PATH). Avoid a `.cmd` wrapper: cmd asks "Terminate batch job (Y/N)?" after every Ctrl+C, and Ctrl+C
   is the harness's own interrupt key.
3. Point it at your model server in `%USERPROFILE%\.config\cracked\config.toml`. For Ollama:

   ```toml
   base_url = "http://127.0.0.1:11434/v1"
   model = "glm-5.3-flash:cloud"
   ```

4. Check it, then run it in a project folder:

   ```powershell
   cracked --show-config
   cd C:\path\to\project
   cracked
   ```

### Ollama notes

- Cloud models (`…:cloud`) need `ollama signin` once. Pull the model first: `ollama pull glm-5.3-flash:cloud`.
- Ollama must be running: start it from the Start menu, or have it start at sign-in. If its tray app won't start,
  `ollama serve` in a terminal works.

### If `uv tool install` fails with "untrusted mount point (os error 448)"

Seen over SSH (OpenSSH server on Windows): Windows refused to follow the links to uv's own Python copies. Give uv
the real Python executable instead:

```powershell
uv tool install --python "$env:LOCALAPPDATA\Python\bin\python.exe" git+https://github.com/LGadd/Cracked-Code.git@v0.1.0
```

(`where python` lists the candidates; skip the `WindowsApps` one, which is the Store stub.)

## What is different from Linux

- **Shell commands run in Git Bash**, not cmd.exe, so the model's POSIX commands (pipes, `&&`, `grep`, `cmd &`)
  work. Cracked finds `bash.exe` from `git`'s location or the usual install folders. It never uses
  `C:\Windows\System32\bash.exe`, which is the WSL launcher. `HARNESS_SHELL=<path>` picks another shell, and
  `HARNESS_SHELL=cmd` goes back to cmd.exe.
- **The model is told it is on Windows:** use `python` rather than `python3` (on Windows `python3` is usually the
  Microsoft Store stub), and start servers in the background with `> server.log 2>&1 &`.
- **Paths use `/`** everywhere the model sees them (tool results, the `@` menu, spill files), because its shell is
  bash. Git Bash paths such as `/etc/x` count as outside the workspace, so writes there ask.
- **Output** is UTF-8, also when it goes to a pipe or a file, so `✓` and the spinner cannot crash a cp1252 console.
  Windows Terminal handles the colours already; in the classic console Cracked turns on VT mode.
- **Job kills** take the whole process tree (`taskkill /T`).
- **Not ported:** `live-tests/*.sh` and `scripts/jail.sh` (the bubblewrap jail) are Linux-only.

## Git checkout

If you copy a checkout from Linux, every `.sh` file may show as modified because the exec bit is lost:
`git config core.fileMode false` fixes that.
