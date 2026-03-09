#!/bin/bash
# YouTube -> MP3 중계 서버 중지
PID=$(lsof -i :8080 -sTCP:LISTEN -t 2>/dev/null)

if [ -z "$PID" ]; then
    echo "[*] Relay server is not running"
else
    kill "$PID"
    echo "[OK] Relay server stopped (PID: $PID)"
fi
