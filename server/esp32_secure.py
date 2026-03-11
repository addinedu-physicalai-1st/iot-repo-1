"""
server/esp32_secure.py — TCP :9000 HMAC-SHA256 서명 통신
Voice IoT Controller · iot-repo-1
보안 등급: HIGH | NIST SP 800-213 §4.2 | OWASP IoT OT1/OT3

기존 command_router.py의 TCP 송신 로직을 이 모듈로 교체하세요.

환경변수 설정:
  export ESP32_SECRET="랜덤_시크릿_키_최소32자"
  예: export ESP32_SECRET=$(python -c "import secrets; print(secrets.token_hex(16))")

ESP32 측 검증 코드 (Arduino C++):
  #include <mbedtls/md.h>
  // 수신 JSON에서 ts, sig, cmd 파싱 후 HMAC 재계산하여 비교
  // 샘플: docs/esp32_hmac_verify.cpp 참조
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# ── 환경변수 ────────────────────────────────────────────────────
_SECRET: bytes = os.environ.get("ESP32_SECRET", "").encode()
if not _SECRET:
    raise RuntimeError(
        "[ESP32_SECURE] ESP32_SECRET 환경변수가 없습니다.\n"
        "  export ESP32_SECRET=$(python -c \"import secrets; print(secrets.token_hex(16))\")"
    )

TIMESTAMP_TOLERANCE_SEC: int = 30  # 재전송 공격 방지 허용 오차


def _sign(ts: str, payload_bytes: bytes) -> str:
    """HMAC-SHA256 서명 생성"""
    msg = ts.encode() + b"." + payload_bytes
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()


def build_signed_packet(cmd: dict) -> bytes:
    """
    서명된 TCP 패킷 생성.
    형식: {"ts": <unix_ts>, "sig": <hmac_hex>, "cmd": <원본 명령>}
    """
    ts = str(int(time.time()))
    payload = json.dumps(cmd, ensure_ascii=False).encode()
    sig = _sign(ts, payload)
    packet = json.dumps({"ts": ts, "sig": sig, "cmd": cmd}, ensure_ascii=False)
    return (packet + "\n").encode()


def verify_ack(ack_bytes: bytes) -> bool:
    """
    ESP32로부터 받은 ACK 패킷 서명 검증.
    ESP32가 ACK에 서명을 추가한 경우 사용.
    """
    try:
        ack = json.loads(ack_bytes.decode())
        ts = ack.get("ts", "")
        sig = ack.get("sig", "")
        body = json.dumps(ack.get("ack", {}), ensure_ascii=False).encode()

        # 타임스탬프 유효성 검사 (재전송 공격 방지)
        if abs(time.time() - int(ts)) > TIMESTAMP_TOLERANCE_SEC:
            logger.warning("[ESP32_SECURE] ACK 타임스탬프 오차 초과 — 재전송 공격 의심")
            return False

        expected = _sign(ts, body)
        return hmac.compare_digest(expected, sig)
    except Exception as e:
        logger.error(f"[ESP32_SECURE] ACK 검증 오류: {e}")
        return False


class SecureTCPClient:
    """
    기존 command_router.py의 TCP 통신을 교체하는 보안 클라이언트.

    사용 예:
        client = SecureTCPClient("192.168.0.100", 9000)
        await client.send({"action": "led_on", "room": "living_room"})
    """

    def __init__(self, host: str, port: int = 9000, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    async def send(self, cmd: dict) -> str | None:
        """서명된 명령 전송 후 ACK 수신"""
        packet = build_signed_packet(cmd)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
            writer.write(packet)
            await writer.drain()

            ack_raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            writer.close()
            await writer.wait_closed()

            logger.debug(f"[ESP32_SECURE] ACK 수신: {ack_raw.decode().strip()}")
            return ack_raw.decode().strip()

        except asyncio.TimeoutError:
            logger.error(f"[ESP32_SECURE] {self.host}:{self.port} 응답 타임아웃")
            return None
        except OSError as e:
            logger.error(f"[ESP32_SECURE] TCP 연결 실패: {e}")
            return None
