#!/bin/bash
# Proves: edit_file's exact-and-unique contract works through the model.
# Pass: write_file -> edit_file (1 replacement) -> read_file showing the new line.
source "$(dirname "$0")/_common.sh"
TIMEOUT=600 run_jail 05-edit-cycle $'y\ny\n' \
  python3 -u -m cracked_code --workspace /tmp --max-turns 8 \
  -p 'Write a file a.txt containing exactly `line one`. Then change it to `line two` using the edit tool, then read it back.'
