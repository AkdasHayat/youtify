#!/bin/bash
# Double-click to install youtify.
cd "$(dirname "$0")"
bash scripts/setup_mac.sh
echo
read -n 1 -s -r -p "Press any key to close this window."
