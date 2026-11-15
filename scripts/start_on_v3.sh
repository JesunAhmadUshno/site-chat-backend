#!/bin/bash
# site-chat stack on BlackBoxV3: llama.cpp model server, FastAPI
# gateway (which spawns the MCP grounding server itself), and a
# Cloudflare quick tunnel for the gateway. User-space only, no sudo,
# no Docker, mirroring the fde-backend-service pattern on this host.
# Nothing here auto-starts after a reboot; run this script by hand.
#
#   bash ~/site-chat-start.sh
#
# Logs land in ~/site-chat-logs/. The tunnel URL is printed at the end
# and also written to ~/site-chat-logs/tunnel-url.txt. Quick tunnel
# URLs are temporary: they change on every tunnel restart.

set -u
REPO="$HOME/site-chat-backend"
LOGS="$HOME/site-chat-logs"
MODEL="$HOME/llm-obs-live/model/qwen2.5-0.5b-instruct-q4_k_m.gguf"
LLAMA_SERVER="$HOME/llm-obs-live/llama/llama-b12418/llama-server"
mkdir -p "$LOGS"

port_up() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&- 3<&-; }

if ! port_up 8300; then
  echo "starting llama-server on 127.0.0.1:8300"
  nohup "$LLAMA_SERVER" -m "$MODEL" --host 127.0.0.1 --port 8300 -c 4096 \
    > "$LOGS/llama.log" 2>&1 &
fi

if ! port_up 8200; then
  echo "starting gateway on 127.0.0.1:8200"
  cd "$REPO" || exit 1
  LLAMA_URL=http://127.0.0.1:8300 \
    nohup "$REPO/.venv/bin/uvicorn" app.gateway:app --host 127.0.0.1 --port 8200 \
    > "$LOGS/gateway.log" 2>&1 &
fi

for i in $(seq 1 30); do
  curl -s http://127.0.0.1:8200/health > /dev/null 2>&1 && break
  sleep 1
done
curl -s http://127.0.0.1:8200/health || true
echo

if ! pgrep -f "cloudflared tunnel --url http://127.0.0.1:8200" > /dev/null; then
  echo "starting quick tunnel for 8200"
  nohup "$HOME/bin/cloudflared" tunnel --url http://127.0.0.1:8200 --no-autoupdate \
    > "$LOGS/tunnel.log" 2>&1 &
fi

for i in $(seq 1 30); do
  URL=$(grep -o "https://[a-z0-9-]*\.trycloudflare\.com" "$LOGS/tunnel.log" 2>/dev/null | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
echo "tunnel: $URL"
[ -n "$URL" ] && echo "$URL" > "$LOGS/tunnel-url.txt"
