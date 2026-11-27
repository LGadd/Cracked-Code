#!/bin/bash
# Proves: web_search and web_fetch work keylessly, and that the model keeps going when
# DuckDuckGo answers "no results" (that is throttling, expected, not a failure).
# Pass: at least one web_search and one web_fetch, then a summary built from fetched text.
source "$(dirname "$0")/_common.sh"
TIMEOUT=600 run_jail 03-web $'y\ny\n' \
  python3 -u -m cracked_code --workspace /tmp --max-turns 10 \
  -p "Search the web for the Strata ggml-based inference server and tell me what its repo says. Use your web tools, not shell."
