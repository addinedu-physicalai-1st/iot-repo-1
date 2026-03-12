#!/usr/bin/env bash
# ================================================================
# Voice IoT Controller - 서버 시작 스크립트
# ================================================================
# 사용법:
#   ./run_server.sh                              # 전체 기능 실행
#   DISABLE_STT=1 DISABLE_TTS=1 ./run_server.sh  # STT/TTS 없이 실행
#   DISABLE_DB=1 ./run_server.sh                 # MySQL 로깅 없이 실행
#   DISABLE_SMARTGATE=1 ./run_server.sh          # SmartGate 2FA 없이 실행
#
# 환경 변수:
#   .env 파일에서 민감정보 자동 로드 (cp .env_example .env)
#   DISABLE_STT=1       : STT/웨이크워드 비활성화
#   DISABLE_TTS=1       : TTS 비활성화
#   DISABLE_DB=1        : MySQL 이벤트 로깅 비활성화
#   DISABLE_CAM=1       : ESP32-CAM 카메라 비활성화
#   DISABLE_SMARTGATE=1 : SmartGate 2FA 비활성화
#
# 포트:
#   8000 : FastAPI (HTTP + WebSocket)
#   9000 : ESP32 TCP 서버
# ================================================================

set -e

# 프로젝트 루트로 이동
cd "$(dirname "$0")"

# ── .env 로드 (없으면 .env_example 복사 후 JWT_SECRET 자동 생성) ──
if [ ! -f ".env" ]; then
    if [ -f ".env_example" ]; then
        cp .env_example .env
        echo ".env 생성 완료 (.env_example 복사)"
    else
        touch .env
        echo ".env 신규 생성"
    fi
fi

# JWT_SECRET 없거나 빈 값일 때만 최초 1회 생성 (재시작해도 유지)
if ! grep -q "^JWT_SECRET=.\+" .env 2>/dev/null; then
    JWT_VAL=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i '/^JWT_SECRET=/d' .env
    echo "JWT_SECRET=${JWT_VAL}" >> .env
    echo "[보안] JWT_SECRET 최초 생성 완료"
else
    echo "[보안] JWT_SECRET 기존 값 유지"
fi

# .env 로드 (단일 로드)
set -a
source .env
set +a
echo ".env 환경변수 로드 완료"

# 포트 종료 함수: SIGTERM → 대기 → SIGKILL (kill_server.sh 동일 로직)
kill_port() {
    local port=$1
    local label=$2
    local pids
    pids=$(lsof -t -i:"$port" 2>/dev/null || true)
    if [ -z "$pids" ]; then
        return
    fi
    kill $pids 2>/dev/null || true
    for _ in $(seq 1 10); do
        if ! lsof -t -i:"$port" &>/dev/null; then
            echo "  $label 종료 완료 (port $port)"
            return
        fi
        sleep 0.5
    done
    pids=$(lsof -t -i:"$port" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null || true
        sleep 1
        echo "  $label 강제 종료 (port $port)"
    fi
}

# 기존 프로세스 정리 (포트 충돌 방지)
echo "기존 프로세스 정리 중..."
./scripts/run_nginx.sh stop 2>/dev/null || true
RELAY_PORT="${RELAY_PORT:-8080}"
kill_port 9000 "TCP 서버"
kill_port 8000 "HTTP/WS 서버"
kill_port "$RELAY_PORT" "Relay 서버"

# nginx 종료 트랩 (스크립트 종료 시 자동 실행)
NGINX_STARTED=0
cleanup_nginx() {
  if [ "$NGINX_STARTED" = "1" ]; then
    echo "nginx 종료 중..."
    ./scripts/run_nginx.sh stop 2>/dev/null || true
  fi
}
trap cleanup_nginx EXIT

# 가상환경 자동 활성화 (.venv 존재 시)
if [ -d ".venv" ] && [ -f ".venv/bin/activate" ]; then
    echo "가상환경 활성화: .venv"
    source .venv/bin/activate
fi

# Ollama 실행 확인
OLLAMA_URL="${OLLAMA_HOST:-http://localhost:11434}"
if command -v ollama &>/dev/null; then
    if curl -s "${OLLAMA_URL}/api/tags" &>/dev/null; then
        echo "Ollama 연결 확인 완료 (${OLLAMA_URL})"
    else
        echo "경고: Ollama 서버 응답 없음 — ollama serve 실행 필요"
    fi
fi

# MySQL 연결 확인 (DISABLE_DB가 아닐 때)
if [ -z "$DISABLE_DB" ]; then
    if command -v mysql &>/dev/null; then
        _DB_USER="${DB_USER:-}"
        _DB_PASS="${DB_PASSWORD:-}"
        _DB_HOST="${DB_HOST:-localhost}"
        if [ -n "$_DB_USER" ] && [ -n "$_DB_PASS" ]; then
            if MYSQL_PWD="$_DB_PASS" mysql -u "$_DB_USER" -h "$_DB_HOST" -e "USE iot_smart_home;" 2>/dev/null; then
                echo "MySQL 연결 확인 완료 (iot_smart_home@${_DB_HOST})"
            else
                echo "경고: MySQL 연결 실패 — DB 로깅이 자동 비활성화됩니다."
                echo "  DB 설정: sudo mysql < scripts/init_db.sql"
            fi
        else
            echo "경고: DB_USER/DB_PASSWORD 미설정 — .env 파일을 확인하세요"
        fi
    else
        echo "경고: mysql 클라이언트 없음 — DB 연결 확인 스킵"
    fi
fi

# SmartGate 상태 표시
if [ -n "$DISABLE_SMARTGATE" ]; then
    echo "SmartGate 2FA: 비활성화 (DISABLE_SMARTGATE=1)"
else
    echo "SmartGate 2FA: 활성화"
fi

echo ""
echo "서버 시작 중... (TCP:9000 / HTTP+WS:8000)"
echo ""

# ── CVE 취약점 자동 스캔 (백그라운드 실행, 서버 시작 차단 안 함) ──
if command -v pip-audit &>/dev/null || pip show pip-audit &>/dev/null 2>&1; then
  echo "[AUDIT] 백그라운드 CVE 스캔 시작..."
  bash scripts/audit.sh >> logs/audit/latest.log 2>&1 &
  echo "[AUDIT] 완료 후 logs/audit/ 에서 결과 확인 가능"
else
  echo "[AUDIT] pip-audit 미설치 — 스킵 (설치: pip install pip-audit)"
fi
echo ""

# ── YouTube → MP3 중계 서버 (relay_server) ──
if [ -n "$RELAY_PORT" ]; then
  # relay venv 자동 생성 (.relay-venv 없으면 생성 후 flask, yt-dlp 설치)
  if [ ! -f ".relay-venv/bin/python3" ]; then
    echo "Relay venv 생성 중 (.relay-venv)..."
    python3 -m venv .relay-venv
    .relay-venv/bin/python3 -m ensurepip --upgrade >/dev/null 2>&1
    .relay-venv/bin/python3 -m pip install --quiet flask yt-dlp
    echo "Relay venv 생성 완료 (flask, yt-dlp 설치됨)"
  fi

  if command -v lsof &>/dev/null && lsof -i :"$RELAY_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Relay 서버: 포트 $RELAY_PORT 이미 사용 중 — 스킵"
  else
    echo "Relay 서버 시작 중 (port $RELAY_PORT)..."
    RELAY_PYTHON=".relay-venv/bin/python3"
    nohup $RELAY_PYTHON scripts/relay_server.py > /tmp/relay_server.log 2>&1 &
    RELAY_PID=$!
    sleep 2
    if command -v lsof &>/dev/null && lsof -i :"$RELAY_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
      echo "Relay 서버 시작 완료 (PID: $RELAY_PID, log: /tmp/relay_server.log)"
    else
      echo "경고: Relay 서버 시작 실패 — /tmp/relay_server.log 확인"
    fi
  fi
else
  echo "Relay 서버: RELAY_PORT 미설정 — 스킵"
fi

# ── BT Speaker 초기 볼륨 설정 ──
BT_SPEAKER_URL="${BT_SPEAKER_URL:-}"
if [ -n "$BT_SPEAKER_URL" ]; then
  BT_DEFAULT_VOL=5
  if curl -s --max-time 3 -X POST "${BT_SPEAKER_URL}/volume" -d "v=${BT_DEFAULT_VOL}" >/dev/null 2>&1; then
    echo "BT Speaker 초기 볼륨: ${BT_DEFAULT_VOL}%"
  else
    echo "BT Speaker 볼륨 설정 스킵 (ESP32 미응답)"
  fi
fi

# uvicorn 백그라운드 실행
uvicorn server.main:app --host 0.0.0.0 --port 8000 &
UVICORN_PID=$!

# 서버 준비 대기 (최대 30초)
echo "서버 준비 대기 중..."
for _ in $(seq 1 60); do
  if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ 2>/dev/null | grep -q 200; then
    echo "서버 준비 완료"
    break
  fi
  sleep 0.5
done

# nginx (HTTPS) 자동 시작 (SSL 인증서 있으면)
DASHBOARD_URL="http://localhost:8000/"
if [ -f "nginx/ssl/iot.pem" ] && [ -f "nginx/ssl/iot-key.pem" ]; then
  if ./scripts/run_nginx.sh; then
    NGINX_STARTED=1
    DASHBOARD_URL="https://localhost/"
    echo "  웹 대시보드: $DASHBOARD_URL (HTTPS)"
  else
    echo "  웹 대시보드: http://localhost:8000/ (nginx 시작 실패)"
  fi
else
  echo "  웹 대시보드: http://localhost:8000/"
  echo "  (HTTPS: ./scripts/ssl_generate_cert.sh 후 재시작)"
fi
echo ""

# 기본 브라우저에서 대시보드 자동 열기 (Linux)
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$DASHBOARD_URL" >/dev/null 2>&1
fi

# uvicorn 종료 대기
wait $UVICORN_PID
