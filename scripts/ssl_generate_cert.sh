#!/usr/bin/env bash
# ================================================================
# Voice IoT Controller - 자체 서명 SSL 인증서 생성
# ================================================================
# nginx HTTPS 역방향 프록시용. 원격 접속 시 브라우저 마이크 사용 가능.
#
# 사용법:
#   ./scripts/ssl_generate_cert.sh
#
# 참고: 자체 서명 인증서는 브라우저에서 "안전하지 않음" 경고가 뜹니다.
#       "고급" → "계속 진행"으로 접속 가능.
#       Let's Encrypt 사용 시 docs/NGINX_HTTPS_SETUP.md 참조.
# ================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NGINX_DIR="$PROJECT_ROOT/nginx"
SSL_DIR="$NGINX_DIR/ssl"
LOGS_DIR="$NGINX_DIR/logs"

mkdir -p "$SSL_DIR"
mkdir -p "$LOGS_DIR"

CERT="$SSL_DIR/iot.pem"
KEY="$SSL_DIR/iot-key.pem"

# 이미 존재하면 덮어쓸지 확인
if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    echo "인증서가 이미 존재합니다: $CERT"
    read -p "덮어쓰시겠습니까? (y/N): " -r
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "취소됨"
        exit 0
    fi
fi

# 호스트명: 로컬 IP 또는 localhost
# SAN(Subject Alternative Name)에 여러 호스트 포함
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
OPENSSL_CNF="$SSL_DIR/openssl.cnf"

cat > "$OPENSSL_CNF" << EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
req_extensions = v3_req

[dn]
CN = iot.local

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
DNS.2 = iot.local
IP.1 = 127.0.0.1
IP.2 = $HOST_IP
EOF

echo "자체 서명 인증서 생성 중..."
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$KEY" -out "$CERT" \
    -config "$OPENSSL_CNF" \
    -extensions v3_req

chmod 600 "$KEY"
chmod 644 "$CERT"
rm -f "$OPENSSL_CNF"

echo ""
echo "✅ 인증서 생성 완료:"
echo "   인증서: $CERT"
echo "   개인키: $KEY"
echo ""
echo "다음으로 nginx를 실행하세요:"
echo "  sudo nginx -c $NGINX_DIR/nginx.conf -p $NGINX_DIR"
echo ""
echo "접속: https://localhost 또는 https://$HOST_IP"
echo "(자체 서명이므로 브라우저에서 '고급' → '계속 진행' 필요)"
