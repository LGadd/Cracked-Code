#!/bin/bash
# Proves: the REPL keeps history across turns (the second answer needs the first turn's work).
# Pass: turn two names 42 without re-reading the file being the only step.
# Note: this is the REPL path, which the one-shot tests never exercise.
source "$(dirname "$0")/_common.sh"
TIMEOUT=600 run_jail 06-repl $'Put the number 42 into a file called num.txt.\nWhat number is in num.txt?\n/q\n' \
  python3 -u -m cracked_code --workspace /tmp --max-turns 6
