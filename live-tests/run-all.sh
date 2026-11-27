#!/bin/bash
# Runs every live test in order, one at a time, against base_url + model from the config
# file (or HARNESS_BASE_URL / HARNESS_MODEL). Do not overlap them: a local model owns the
# GPU, and the logs and jail workspace are shared.
#   live-tests/run-all.sh              all tests
#   live-tests/run-all.sh --dry-run    no model calls: lists the tests and checks the jail plumbing
set -uo pipefail
source "$(dirname "$0")/_common.sh"  # sets HERE, HARNESS_BASE_URL, HARNESS_MODEL
BASE_URL=$HARNESS_BASE_URL

if [ "${1:-}" = "--dry-run" ]; then
  echo "tests:"
  ls "$HERE"/[0-9][0-9]-*.sh
  echo "jail check (no model needed):"
  "$HERE/../scripts/jail.sh" sh -c 'python3 -c "import cracked_code; print(\"cracked_code\", cracked_code.__version__)"; echo -n "home inside jail: "; ls /home; touch /etc/x 2>&1 | head -1'
  exit $?
fi

models=$(curl -s -m 10 "$BASE_URL/models")
if [ -z "$models" ]; then
  echo "no answer from $BASE_URL - start the model server first"
  exit 1
fi
[ "$HARNESS_MODEL" = "-" ] && { echo "no model set: add model = \"<id>\" to the config file (cracked --show-config)"; exit 1; }
echo "$models" | grep -qF "\"$HARNESS_MODEL\"" || echo "warning: $BASE_URL does not list $HARNESS_MODEL"

summary="$HERE/results/summary.txt"
mkdir -p "$HERE/results"
: > "$summary"
for t in "$HERE"/[0-9][0-9]-*.sh; do
  echo "=== $(basename "$t") $(date '+%H:%M:%S')" | tee -a "$summary"
  "$t" 2>&1 | tee -a "$summary"
done
echo "all results in $HERE/results/"
