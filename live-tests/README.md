# Live tests (run these yourself)

These need the GPU, so they are yours to run: when I drive the model and also test the
model, the box is doing the same job twice and everything is slow.

## How to run

```bash
cd cracked-code                    # the repo root
live-tests/run-all.sh              # everything, one at a time, ~15 min
live-tests/03-web.sh               # one test
live-tests/run-all.sh --dry-run    # no GPU: checks the jail plumbing and lists the tests
```

Start the model first: `systemctl --user start strata`. `run-all.sh` refuses to run if the
model is not loaded.

## Where results go

Every test writes its full transcript to a fixed path, and prints that path when it
finishes. Overwritten on each run.

```
live-tests/results/01-file-cycle.log
live-tests/results/02-port-scan.log
live-tests/results/03-web.log
live-tests/results/04-guardrails.log
live-tests/results/05-edit-cycle.log
live-tests/results/06-repl.log
live-tests/results/07-install.log
live-tests/results/summary.txt      # written by run-all.sh
```

To have me review them, just say so — I read those files directly.

## The plan: what each test proves

| # | Test | Pass looks like |
|---|---|---|
| 01 | file cycle | `write_file` then `read_file` then `delete_file`, in that order. The delete prints `[harness] delete /tmp/notes.md - allow? [y/N/a]` and the answer `y` is fed in. Two `. snapshot <sha>` lines appear. Ends with the file gone. |
| 02 | port scan | Uses `run_shell` with `nmap`, reports the open ports, and cross-checks with `ss` or `ps`. Known-good answer on this PC: 22 sshd, 139+445 smbd, 8080 Strata. `-O` fails (needs real root) — recovering from that is part of the pass. |
| 03 | web | Calls `web_search` then `web_fetch`. Content matters less than the tool use: DuckDuckGo often returns `no results` on repeat queries, and the model retrying is expected behaviour, not a failure. |
| 04 | guardrails | Runs with `--yes` (Fuck It Mode). `rm -rf /` returns `DENIED: ... is blocked: rm of the filesystem root` **before** any shell spawns. The write to `/etc/harness-test.txt` does **not** prompt and fails at the OS (`ERROR: cannot write`, read-only jail). `delete_file keep.txt` still prompts, `n` is fed in → `DENIED: user declined to delete`. |
| 05 | edit cycle | `write_file` → `edit_file` (one exact replacement) → `read_file` showing the changed line. Proves the edit tool's unique-match contract works through the model. |
| 06 | REPL + memory | Two prompts in one session: it writes a number to a file in turn one and names that number correctly in turn two. Proves history carries across REPL turns. Interactive by design. |
| 07 | install | `python3 -m venv` + `pip install .` into `/tmp/harness-venv`, then `cracked --help` and one one-shot turn. Needs network once (setuptools from PyPI). Runs outside the jail; writes only to `/tmp/harness-venv`. |

## Safety while testing

Tests 01–06 run inside `scripts/jail.sh`: `/` read-only, `$HOME` hidden, only `/tmp`
writable (host path `live-tests/../` is unaffected; the jail workspace is
`/tmp/harness-jail/ws`, throwaway). Network stays up so the jail reaches Strata.
Test 07 is the only one that touches the host, and only `/tmp/harness-venv`.

Nothing here can damage the project: the repo is mounted read-only inside the jail.

## Timing

Rough cost on the 5070 Ti when nothing else is using the model: 01 ~3.5 min, 02 ~2 min,
03 ~1 min, 04 ~30 s, 05 ~15 s, 06 ~2 min, 07 ~1 min plus pip. If a test exceeds its
`timeout`, its log still keeps everything printed up to that point (`python3 -u`).
