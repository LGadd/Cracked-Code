# Contributing

Thanks for helping. Cracked Code stays small on purpose, so read this before a big change.

## Run the tests

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e .
.venv/bin/python -m unittest discover -s tests -t .   # offline, about 10 s, no model or network
live-tests/run-all.sh --dry-run                        # Linux: checks the bubblewrap jail, no GPU
live-tests/run-all.sh                                  # 7 live tests against a real model (~15 min)
```

Every pull request runs the offline tests on Linux and Windows, Python 3.11 and 3.13.

## What gets accepted

- **Small and measured.** Every token in the prompt and every tool schema is paid on every request. A change that
  adds prompt text or a tool needs a before-and-after on the same model (the time, the requests and the result).
- **No new dependencies** unless they are pure Python and work on Windows. The prompt (`prompt_toolkit`) and the
  code colours (`pygments`) are the only two.
- **Each new behaviour gets a test** in `tests/`. Failures are worth recording too: add them to `docs/gotchas.md`.
- **Docs in the same change:** a config key goes in `docs/setup.md`, a behaviour in `docs/implemented.md`, a user-visible
  change in `CHANGELOG.md`.
- **Plain output stays stable.** When stdout is not a colour terminal (`TERM=dumb`, a pipe), Cracked prints plain
  lines (`-> tool`, `. turn N`, `. >_ …`) that other programs parse. Don't change those prefixes casually.

## Reporting a bug

Include the command you ran, the model and server, `cracked --show-config`, and the last lines of the output with
**Ctrl+O** (details) on.
