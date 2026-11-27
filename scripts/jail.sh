#!/bin/sh
# Run the harness in a bwrap jail so a live test cannot touch the OS or this repo.
#   - / is read-only, /home and /root are hidden behind tmpfs; HOME is an empty /home/jail,
#     not the workspace, so the "starting in your home folder" check does not refuse to start
#   - the only writable place is /tmp, which is a throwaway dir on the host
#   - the repo is mounted read-only at /mnt and put on PYTHONPATH
#   - network stays shared so the jail can reach the model server
# Usage:
#   scripts/jail.sh python3 -m cracked_code --workspace /tmp -p "your task"
#   scripts/jail.sh python3 /mnt/scripts/toolcheck.py
set -eu
PROJ=$(cd "$(dirname "$0")/.." && pwd)
WS=${HARNESS_JAIL_WS:-/tmp/harness-jail/ws}
mkdir -p "$WS"
echo "jail workspace (host path): $WS" >&2
exec bwrap \
  --ro-bind / / \
  --ro-bind "$PROJ" /mnt \
  --bind "$WS" /tmp \
  --tmpfs /home \
  --tmpfs /root \
  --dir /home/jail \
  --dev /dev \
  --proc /proc \
  --unshare-user --unshare-uts --unshare-ipc --unshare-cgroup \
  --die-with-parent \
  --setenv HOME /home/jail \
  --setenv PYTHONPATH /mnt \
  --setenv GIT_CONFIG_COUNT 1 \
  --setenv GIT_CONFIG_KEY_0 safe.directory \
  --setenv GIT_CONFIG_VALUE_0 '*' \
  -- "$@"
