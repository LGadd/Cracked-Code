#!/bin/bash
# Proves: `pip install .` works and the installed `cracked` command runs one turn.
# Pass: venv builds, pip succeeds, `cracked --help` prints usage, one-shot replies.
# Needs network once (setuptools from PyPI). Runs OUTSIDE the jail; writes only to
# /tmp/harness-venv and the throwaway jail workspace.
source "$(dirname "$0")/_common.sh"
out="$HERE/results/07-install.log"
VENV=/tmp/harness-venv
{
  rm -rf "$VENV"
  python3 -m venv "$VENV" && "$VENV/bin/pip" install --quiet "$HERE/.." && "$VENV/bin/cracked" --help
  HARNESS_WORKSPACE=/tmp/harness-jail/ws timeout 180 "$VENV/bin/cracked" -p "Reply with exactly: OK"
} > "$out" 2>&1
echo "exit=$?" >> "$out"
echo "result: $out"
tail -4 "$out"
