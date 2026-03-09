#!/usr/bin/env bash
# key-gen.sh — ESP32_SECRET 키 생성 및 config.h / .env 자동 적용

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_H="$PROJECT_ROOT/esp32/config.h"
ENV_FILE="$PROJECT_ROOT/.env"

# 키 생성
KEY=$(python3 -c "import secrets; print(secrets.token_hex(16))")
echo "[KEY] 생성된 ESP32_SECRET: $KEY"

# ── config.h 업데이트 ──────────────────────────────────────────
if [ -f "$CONFIG_H" ]; then
    sed -i "s|#define ESP32_SECRET.*|#define ESP32_SECRET   \"$KEY\"|" "$CONFIG_H"
    echo "[OK] config.h 업데이트 완료: $CONFIG_H"
else
    echo "[ERROR] config.h 파일 없음: $CONFIG_H"
    exit 1
fi

# ── .env 업데이트 ──────────────────────────────────────────────
if [ -f "$ENV_FILE" ]; then
    if grep -q "ESP32_SECRET" "$ENV_FILE"; then
        sed -i "s|^ESP32_SECRET=.*|ESP32_SECRET=$KEY|" "$ENV_FILE"
        echo "[OK] .env 업데이트 완료 (기존 값 교체)"
    else
        echo "ESP32_SECRET=$KEY" >> "$ENV_FILE"
        echo "[OK] .env 추가 완료"
    fi
else
    echo "[WARN] .env 파일 없음 — config.h 만 업데이트됨"
fi

echo ""
echo "적용 완료! 다음 단계:"
echo "  1) Arduino IDE에서 esp32_home1.ino / esp32_home2.ino 업로드"
echo "  2) 서버 재시작: ./run_server.sh"
