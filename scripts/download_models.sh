#!/usr/bin/env bash
# 대용량 모델 파일을 models/ 에 다운로드합니다. (Git에는 올리지 않음)
# 사용법: ./scripts/download_models.sh
# 자세한 설명: MODELS.md

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODELS_DIR="$REPO_ROOT/models"
mkdir -p "$MODELS_DIR"
cd "$MODELS_DIR"

echo "[models] 디렉터리: $MODELS_DIR"

# Kokoro TTS ONNX (약 310MB) — Hugging Face
KOKORO_ONNX="kokoro-v0_19.onnx"
KOKORO_URL="https://huggingface.co/thewh1teagle/Kokoro/resolve/main/kokoro-v0_19.onnx"
if [[ -f "$KOKORO_ONNX" ]]; then
  echo "[models] 이미 존재: $KOKORO_ONNX (건너뜀)"
else
  echo "[models] 다운로드 중: $KOKORO_ONNX"
  if command -v wget &>/dev/null; then
    wget -O "$KOKORO_ONNX" "$KOKORO_URL"
  elif command -v curl &>/dev/null; then
    curl -L -o "$KOKORO_ONNX" "$KOKORO_URL"
  else
    echo "wget 또는 curl이 필요합니다. 수동으로 받아서 $MODELS_DIR/$KOKORO_ONNX 에 넣으세요: $KOKORO_URL"
    exit 1
  fi
  echo "[models] 완료: $KOKORO_ONNX"
fi

# Porcupine .ppn / .pv — Picovoice Console에서 수동 다운로드 필요
echo ""
echo "[models] Porcupine 웨이크 워드 파일은 Git에 포함되지 않습니다."
echo "  - 자비스야_ko_linux_v4_0_0.ppn  → https://console.picovoice.ai/ 에서 생성 후 다운로드"
echo "  - porcupine_params_ko.pv        → Picovoice 문서/리포에서 다운로드 후 models/ 에 저장"
echo "  자세한 내용은 MODELS.md 를 참고하세요."
