#!/bin/bash
# Proves: the file tool cycle, the delete approval prompt, the per-turn git snapshot.
# Pass: write_file -> read_file -> delete_file in that order; an "allow? [y/N/a]" line
#       for the delete; two ". snapshot <sha>" lines; the file gone at the end.
source "$(dirname "$0")/_common.sh"
TIMEOUT=600 run_jail 01-file-cycle $'y\ny\ny\n' \
  python3 -u -m cracked_code --workspace /tmp --max-turns 10 \
  -p "Create notes.md with three bullet points about this workspace, read it back to confirm, then delete it."
