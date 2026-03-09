#!/usr/bin/env bash
# ================================================================
# Voice IoT Controller - nginx 역방향 프록시 실행
# ================================================================
# 사전 요구: ./scripts/ssl_generate_cert.sh 실행
#           Python 서버 실행 중 (./run_server.sh)
#
# 사용법:
#   ./scripts/run_nginx.sh        # nginx 시작
#   ./scripts/run_nginx.sh stop   # nginx 중지
# ================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NGINX_DIR="$PROJECT_ROOT/nginx"
CONFIG="$NGINX_DIR/nginx.conf"

if [ "$1" = "stop" ]; then
    echo "nginx 중지 중..."
    sudo nginx -s stop -p "$NGINX_DIR" 2>/dev/null || true
    echo "nginx 중지됨"
    exit 0
fi

# 인증서 확인
if [ ! -f "$NGINX_DIR/ssl/iot.pem" ] || [ ! -f "$NGINX_DIR/ssl/iot-key.pem" ]; then
    echo "오류: SSL 인증서가 없습니다. 먼저 실행하세요:"
    echo "  ./scripts/ssl_generate_cert.sh"
    exit 1
fi

# 설정 검증
echo "nginx 설정 검증 중..."
sudo nginx -t -c "$CONFIG" -p "$NGINX_DIR"

echo "nginx 시작 중..."
sudo nginx -c "$CONFIG" -p "$NGINX_DIR"

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
echo ""
echo "✅ nginx 실행됨"
echo "   로컬:  https://localhost"
echo "   원격:  https://$HOST_IP"
echo "   중지:  ./scripts/run_nginx.sh stop"
echo ""
echo "원격 접속이 안 되면 방화벽 확인: sudo ufw allow 443/tcp && sudo ufw reload"
