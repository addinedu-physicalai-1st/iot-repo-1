#!/bin/bash
# YouTube -> MP3 중계 서버 시작
set -e
cd "$(dirname "$0")"

if lsof -i :8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "[!] Port 8080 already in use"
    exit 1
fi

echo "[*] Starting relay server on port 8080..."
nohup python3 relay_server.py > /tmp/relay_server.log 2>&1 &

sleep 2
if lsof -i :8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "[OK] Relay server started (PID: $(lsof -i :8080 -sTCP:LISTEN -t))"
    echo "[OK] Log: /tmp/relay_server.log"
else
    echo "[FAIL] Server failed to start. Check /tmp/relay_server.log"
    exit 1
fi
