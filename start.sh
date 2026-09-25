#!/usr/bin/env bash
# Start MicMic and open it in Chrome.
set -e
cd "$(dirname "$0")"
PORT=8799
# Real sending is ON. Every message is still read back aloud with a 6 second
# cancel window, and 11 different ways of saying "stop" were verified to cancel it.
export MICMIC_ALLOW_SEND=1
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "already running on $PORT"
else
  python3 -m savta.server &
  sleep 3
fi
open -a "Google Chrome" "http://127.0.0.1:$PORT/"
echo "MicMic is open. Allow the microphone when Chrome asks."
