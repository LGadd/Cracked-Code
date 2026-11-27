#!/bin/bash
# Shared by the numbered live tests: fixed result path, jail runner, timeout.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
JAIL="$HERE/../scripts/jail.sh"
mkdir -p "$HERE/results"
# The jail hides $HOME and with it the config file: pass the host's base_url and model as env.
read -r HARNESS_BASE_URL HARNESS_MODEL < <(cd "$HERE/.." && python3 -c \
  'from cracked_code import config; v = config.load()[0]; print(v["base_url"], v["model"] or "-")')
export HARNESS_BASE_URL HARNESS_MODEL

# run_jail <result-name> <stdin-text> <command...>
# Writes the whole transcript to live-tests/results/<result-name>.log (overwritten),
# appends the exit code, prints the path, and shows the last lines.
run_jail() {
  local name=$1 stdin=$2
  shift 2
  local out="$HERE/results/$name.log"
  printf '%s' "$stdin" | timeout "${TIMEOUT:-600}" "$JAIL" "$@" > "$out" 2>&1
  echo "exit=$?" >> "$out"
  echo "result: $out"
  tail -4 "$out"
}
