#!/bin/bash
set -e

echo "[start.sh] Starting Telegram bot..."
cd /app
exec python bot.py
