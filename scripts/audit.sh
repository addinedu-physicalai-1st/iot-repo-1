#!/usr/bin/env bash
# ================================================================
# scripts/audit.sh — Python 패키지 CVE 취약점 자동 스캔
# Voice IoT Controller · iot-repo-1
#
# 사용법:
#   ./scripts/audit.sh                  # 기본 실행
#   ./scripts/audit.sh --fix            # 취약 패키지 자동 업그레이드
#   AUDIT_FAIL_ON_VULN=1 ./scripts/audit.sh  # CI: 취약점 발견 시 exit 1
#
# 결과 로그: logs/audit/audit_YYYYMMDD_HHMMSS.log
# ================================================================

set -e
cd "$(dirname "$0")/.."

# ── 설정 ──────────────────────────────────────────────────────
LOG_DIR="logs/audit"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="${LOG_DIR}/audit_${TIMESTAMP}.log"
FAIL_ON_VULN="${AUDIT_FAIL_ON_VULN:-0}"
FIX_MODE=0

# ── 자동 업그레이드 제외 패키지 (공백 구분) ─────────────────
# protobuf: 5.x 업그레이드 시 mediapipe 0.10.14 충돌 → SmartGate 비활성화
SKIP_PACKAGES="protobuf"

for arg in "$@"; do
  case $arg in
    --fix) FIX_MODE=1 ;;
  esac
done

mkdir -p "$LOG_DIR"

# ── pip-audit 설치 확인 ───────────────────────────────────────
if ! command -v pip-audit &>/dev/null; then
  echo "[AUDIT] pip-audit 미설치 → 자동 설치 중..."
  pip install pip-audit --break-system-packages -q
fi

# ── 가상환경 활성화 ───────────────────────────────────────────
if [ -d ".venv" ] && [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

echo "================================================================"
echo " IoT Voice Controller — CVE 보안 스캔"
echo " 날짜: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"
echo ""
echo "[AUDIT] 스캔 중... (ROS2 시스템 패키지는 제외)"
echo ""

# ── pip-audit 실행 (raw 결과를 임시 파일에 저장) ──────────────
RAW_LOG="${LOG_DIR}/.raw_${TIMESTAMP}.log"
set +e
pip-audit --format=columns --skip-editable 2>"$RAW_LOG"
AUDIT_EXIT=$?
set -e

# ── ROS2/시스템 패키지 경고 필터링 ───────────────────────────
NOT_FOUND_COUNT=$(grep -c "could not be audited" "$RAW_LOG" 2>/dev/null || echo "0")
VULN_LINES=$(grep -v "could not be audited" "$RAW_LOG" | grep -v "^$" || true)
VULN_COUNT=$(echo "$VULN_LINES" | grep -c "CVE-" 2>/dev/null || echo "0")

# ── 로그 저장 (전체 raw 포함) ────────────────────────────────
{
  echo "================================================================"
  echo " IoT Voice Controller — CVE 보안 스캔"
  echo " 날짜: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "================================================================"
  echo ""
  cat "$RAW_LOG"
} > "$LOG_FILE"
rm -f "$RAW_LOG"

# ── 결과 출력 ─────────────────────────────────────────────────
echo "================================================================"
echo " 스캔 결과 요약"
echo "================================================================"

if [ "$AUDIT_EXIT" -eq 0 ] && [ "$VULN_COUNT" -eq 0 ]; then
  echo ""
  echo "  ✅ CVE 취약점 없음 — 모든 패키지 안전"
  echo ""
else
  echo ""
  echo "  🚨 CVE 취약점 발견: ${VULN_COUNT}건"
  echo ""
  echo "$VULN_LINES"
  echo ""
  echo "  → 수동 수정: pip install <패키지>==<안전버전>"
  echo "  → 자동 수정: bash scripts/audit.sh --fix"
  echo ""
fi

if [ "$NOT_FOUND_COUNT" -gt 0 ]; then
  echo "  ℹ️  PyPI 미등록 패키지 (ROS2 등 시스템 패키지): ${NOT_FOUND_COUNT}건 — 무시해도 됩니다"
  echo ""
fi

echo "  📄 전체 로그: ${LOG_FILE}"
echo "================================================================"

# ── 자동 수정 모드 ────────────────────────────────────────────
if [ "$FIX_MODE" -eq 1 ] && [ "$VULN_COUNT" -gt 0 ]; then
  echo ""
  echo "[AUDIT] --fix 모드: 취약 패키지 자동 업그레이드 시도..."
  echo "[AUDIT] 제외 패키지: ${SKIP_PACKAGES}"
  echo ""

  # SKIP_PACKAGES에 있는 패키지는 업그레이드 제외
  FIX_TARGETS=$(echo "$VULN_LINES" | grep "CVE-" | awk '{print $1}' | while read pkg; do
    SKIP=0
    for skip_pkg in $SKIP_PACKAGES; do
      if [ "$pkg" = "$skip_pkg" ]; then
        SKIP=1
        break
      fi
    done
    if [ "$SKIP" -eq 0 ]; then
      echo "$pkg"
    else
      echo "[AUDIT] ⚠️  $pkg 업그레이드 제외 (호환성 이슈 — 수동 확인 필요)" >&2
    fi
  done)

  if [ -n "$FIX_TARGETS" ]; then
    echo "$FIX_TARGETS" | xargs pip install --upgrade --break-system-packages 2>&1 | tee -a "$LOG_FILE"
    echo ""
    echo "[AUDIT] 업그레이드 완료 — 서버 재시작 필요"
  else
    echo "[AUDIT] 업그레이드 대상 없음 (모두 제외됨)"
  fi
fi

# ── CI 모드 ───────────────────────────────────────────────────
if [ "$FAIL_ON_VULN" = "1" ] && [ "$VULN_COUNT" -gt 0 ]; then
  echo "[AUDIT] AUDIT_FAIL_ON_VULN=1 — exit 1 반환 (CI 실패 처리)"
  exit 1
fi

# ── 오래된 로그 정리 (30일 이상) ─────────────────────────────
find "$LOG_DIR" -name "audit_*.log" -mtime +30 -delete 2>/dev/null || true

exit 0
