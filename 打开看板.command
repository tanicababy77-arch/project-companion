#!/bin/bash
cd "$(dirname "$0")" || exit 1
if ! lsof -nP -iTCP:8900 -sTCP:LISTEN -t >/dev/null 2>&1; then
  nohup python3 project_panel.py --port 8900 >> /tmp/panel.log 2>&1 &
  sleep 2
fi
open "http://127.0.0.1:8900"
