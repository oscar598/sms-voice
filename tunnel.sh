#!/bin/bash
# Public tunnel to the local server on a FIXED ngrok domain, restarted if it ever exits.
# The dashboard (Lovable) is configured once with https://$NGROK_DOMAIN and never changes.
#   ./tunnel.sh            # uses NGROK_DOMAIN below and PORT (default 5055)
set -u
cd "$(dirname "$0")"

NGROK_DOMAIN="${NGROK_DOMAIN:-plethora-oversold-spelling.ngrok-free.dev}"
PORT="${PORT:-5055}"

if [ ! -x ./ngrok ]; then
  echo "ngrok is not in $(pwd). Download the macOS Intel (amd64) build from ngrok.com/download and unzip it here."
  exit 1
fi
if ! ./ngrok config check >/dev/null 2>&1; then
  echo "ngrok has no valid config yet. Run: ./ngrok config add-authtoken <your token from the ngrok dashboard>"
  exit 1
fi

# Newer ngrok v3 takes --url; older v3 releases take --domain.
if ./ngrok http --help 2>&1 | grep -q -- "--url"; then
  FLAG="--url=https://$NGROK_DOMAIN"
else
  FLAG="--domain=$NGROK_DOMAIN"
fi

echo "Tunnel: https://$NGROK_DOMAIN -> http://localhost:$PORT  (Ctrl-C twice to stop)"
while true; do
  ./ngrok http "$FLAG" "$PORT" --log=stdout --log-level=warn
  echo "$(date '+%H:%M:%S') ngrok exited (code $?); restarting in 3 s. Same address."
  sleep 3
done
