#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Startup script: launches PO Token server + Telegram bot
#  Used as the Docker container entrypoint on Render
# ─────────────────────────────────────────────────────────────

set -e

# Limit Deno memory to prevent OOM kills on free-tier hosts
export DENO_V8_FLAGS="--max-old-space-size=128"
# Limit Node.js memory for the POT server
export NODE_OPTIONS="--max-old-space-size=128"

# ─── Start PO Token Provider HTTP Server ──────────────────────
# The bgutil PO Token server generates Proof-of-Origin tokens
# that YouTube now requires. It listens on 127.0.0.1:4416 and
# the yt-dlp plugin automatically connects to it.

if [ -f /opt/pot-provider/server/build/main.js ]; then
    echo "[start.sh] Starting PO Token provider server on 127.0.0.1:4416..."
    cd /opt/pot-provider/server
    node build/main.js &
    POT_PID=$!

    # Wait for server to become available (max 15 seconds)
    for i in $(seq 1 15); do
        if curl -sf http://127.0.0.1:4416/ping > /dev/null 2>&1; then
            echo "[start.sh] ✅ PO Token server is ready (PID: $POT_PID)"
            break
        fi
        if [ "$i" -eq 15 ]; then
            echo "[start.sh] ⚠️  PO Token server did not start within 15s. Continuing without it."
        fi
        sleep 1
    done
else
    echo "[start.sh] ⚠️  PO Token server not found at /opt/pot-provider/server/build/main.js"
    echo "[start.sh]    YouTube downloads may fail without PO Token generation."
fi

# ─── Start the Telegram Bot ──────────────────────────────────
echo "[start.sh] Starting Telegram bot..."
cd /app
exec python bot.py
