#!/usr/bin/env bash
# ================================================================
# Voice IoT Controller - 서버 종료 스크립트
# ================================================================
# 사용법:
#   ./kill_server.sh          # 서버 프로세스 종료
# ================================================================

# 프로젝트 루트로 이동 (run_server.sh와 동일 패턴)
cd "$(dirname "$0")"

echo "Voice IoT Controller 서버 종료 중..."

# .env 로드 (RELAY_PORT 확인용)
if [ -f ".env" ]; then
    set -a; source .env; set +a
fi

# nginx (HTTPS 역방향 프록시)
if command -v nginx &>/dev/null && nginx -s stop 2>/dev/null; then
    echo "  nginx 종료"
elif [ -f "scripts/run_nginx.sh" ]; then
    ./scripts/run_nginx.sh stop 2>/dev/null && echo "  nginx 종료" || echo "  nginx — 실행 중이 아님"
else
    echo "  nginx — 실행 중이 아님"
fi

# uvicorn / FastAPI (HTTP:8000)
PIDS_8000=$(lsof -t -i:8000 2>/dev/null)
if [ -n "$PIDS_8000" ]; then
    kill $PIDS_8000 2>/dev/null
    echo "  HTTP/WS 서버 종료 (port 8000)"
else
    echo "  HTTP/WS 서버 — 실행 중이 아님"
fi

# TCP 서버 (TCP:9000)
PIDS_9000=$(lsof -t -i:9000 2>/dev/null)
if [ -n "$PIDS_9000" ]; then
    kill $PIDS_9000 2>/dev/null
    echo "  TCP 서버 종료 (port 9000)"
else
    echo "  TCP 서버 — 실행 중이 아님"
fi

# YouTube → MP3 중계 서버 (Relay)
RELAY_PORT="${RELAY_PORT:-8080}"
PIDS_RELAY=$(lsof -t -i:"$RELAY_PORT" 2>/dev/null)
if [ -n "$PIDS_RELAY" ]; then
    kill $PIDS_RELAY 2>/dev/null
    echo "  Relay 서버 종료 (port $RELAY_PORT)"
else
    echo "  Relay 서버 — 실행 중이 아님"
fi

echo "완료."
