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

# 포트 종료 함수: SIGTERM → 대기 → SIGKILL → 포트 해제 확인
kill_port() {
    local port=$1
    local label=$2
    local pids
    pids=$(lsof -t -i:"$port" 2>/dev/null)
    if [ -z "$pids" ]; then
        echo "  $label — 실행 중이 아님"
        return
    fi
    # SIGTERM
    kill $pids 2>/dev/null
    # 최대 5초 대기
    for _ in $(seq 1 10); do
        if ! lsof -t -i:"$port" &>/dev/null; then
            echo "  $label 종료 완료 (port $port)"
            return
        fi
        sleep 0.5
    done
    # SIGKILL
    pids=$(lsof -t -i:"$port" 2>/dev/null)
    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null
        sleep 1
        echo "  $label 강제 종료 (port $port)"
    fi
}

# nginx (HTTPS 역방향 프록시)
if command -v nginx &>/dev/null && nginx -s stop 2>/dev/null; then
    echo "  nginx 종료"
elif [ -f "scripts/run_nginx.sh" ]; then
    ./scripts/run_nginx.sh stop 2>/dev/null && echo "  nginx 종료" || echo "  nginx — 실행 중이 아님"
else
    echo "  nginx — 실행 중이 아님"
fi

kill_port 8000 "HTTP/WS 서버"
kill_port 9000 "TCP 서버"

RELAY_PORT="${RELAY_PORT:-8080}"
kill_port "$RELAY_PORT" "Relay 서버"

echo "완료."
