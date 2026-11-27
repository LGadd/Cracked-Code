#!/bin/bash
# Proves: network/pentest work through run_shell, and recovery from a bad tool call.
# Pass: nmap runs, open ports are reported, and it cross-checks with ss or ps.
#       Known-good on this PC: 22 sshd, 139+445 smbd, 8080 Strata.
#       nmap -O fails inside the jail (needs real root); recovering is part of the pass.
source "$(dirname "$0")/_common.sh"
TIMEOUT=600 run_jail 02-port-scan $'y\ny\n' \
  python3 -u -m cracked_code --workspace /tmp --max-turns 20 \
  -p "Scan the top 100 ports of 127.0.0.1 and tell me which are open and what is on them."
