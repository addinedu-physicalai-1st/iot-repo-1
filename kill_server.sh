#!/usr/bin/env bash
# ================================================================
# Voice IoT Controller - 서버 종료 스크립트
# ================================================================
# 사용법:
#   ./kill_server.sh          # 서버 프로세스 종료
# ================================================================

set -e

echo "Voice IoT Controller 서버 종료 중..."

# uvicorn / FastAPI (HTTP:8000)
PIDS_8000=$(lsof -t -i:8000 2>/dev/null || true)
if [ -n "$PIDS_8000" ]; then
    kill $PIDS_8000 2>/dev/null || true
    echo "  HTTP/WS 서버 종료 (port 8000)"
else
    echo "  HTTP/WS 서버 — 실행 중이 아님"
fi

# TCP 서버 (TCP:9000)
PIDS_9000=$(lsof -t -i:9000 2>/dev/null || true)
if [ -n "$PIDS_9000" ]; then
    kill $PIDS_9000 2>/dev/null || true
    echo "  TCP 서버 종료 (port 9000)"
else
    echo "  TCP 서버 — 실행 중이 아님"
fi

echo "완료."
