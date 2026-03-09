"""
camera_stream.py — ESP-CAM UDP 수신 + MJPEG WebSocket 스트리밍
v1.4 | Voice IoT Controller
- v1.1: SOI/EOI 자동 조립, FPS/품질 최적화
- v1.2: 디버그 로그 강화 (패킷 수신 확인용)
- v1.3: cam_verdict WS 브로드캐스트 (매 분석마다 Command Log 기록)
- v1.4: 디버그 로그 정리, known/unknown Command Log 항상 기록, verdict 쿨다운 추가

ESP-CAM → UDP (JPEG 프레임) → Python 수신
    → frame_analyzer 분석 (매 ANALYZE_EVERY 프레임)
    → WebSocket 브로드캐스트 (영상 + 알람 verdict)
    → FastAPI MJPEG HTTP 스트림 (/camera/entrance/stream)
"""

import asyncio
import logging
import socket
import struct
import threading
import time
from collections import deque
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────
UDP_IP            = "0.0.0.0"
UDP_PORT          = 5005          # ESP-CAM UDP 송신 포트
UDP_BUFFER_SIZE   = 65535         # UDP 최대 수신 버퍼
FRAME_QUEUE_SIZE  = 10            # 최신 프레임 큐 (5→10, 버퍼 여유)
ANALYZE_EVERY     = 20            # 매 N 프레임마다 분석 (CPU 절약)
STREAM_FPS_LIMIT  = 10            # MJPEG HTTP 스트림 FPS (15→10, 끊김 방지)
JPEG_QUALITY      = 70            # 재압축 JPEG 품질 (80→70, 전송량 감소)

# ESP-CAM 패킷 헤더 구조 (Arduino 펌웨어와 일치해야 함)
# [4B magic][4B frame_id][4B total_len][2B part_idx][2B total_parts][data...]
MAGIC             = b'\xAB\xCD\xEF\x01'
HEADER_FMT        = ">4sIIHH"
HEADER_SIZE       = struct.calcsize(HEADER_FMT)  # 16 bytes

# ──────────────────────────────────────────
# 전역 상태
# ──────────────────────────────────────────
_latest_frame: Optional[bytes]   = None          # 최신 JPEG bytes
_frame_lock                      = threading.Lock()
_frame_queue: deque              = deque(maxlen=FRAME_QUEUE_SIZE)
_running                         = False
_udp_thread: Optional[threading.Thread] = None

# 분석 결과 캐시 (frame_analyzer → 웹앱 오버레이용)
_last_verdict: dict = {
    "label":      "clear",    # clear / known / delivery / intruder
    "name":       None,       # 등록 얼굴 이름 (known일 때)
    "confidence": 0.0,
    "timestamp":  0.0,
    "bbox":       [],         # [{x,y,w,h,label}, ...]
}
_verdict_lock = threading.Lock()


# ──────────────────────────────────────────
# 내부 유틸
# ──────────────────────────────────────────
def _build_overlay(frame_bgr: np.ndarray) -> np.ndarray:
    """최신 verdict 기반 오버레이 텍스트/bbox를 프레임에 그림"""
    with _verdict_lock:
        verdict = dict(_last_verdict)

    label      = verdict["label"]
    name       = verdict["name"]
    confidence = verdict["confidence"]
    bboxes     = verdict["bbox"]

    # 색상 매핑
    color_map = {
        "clear":    (180, 180, 180),
        "known":    (0,   200,  80),
        "delivery": (0,   180, 255),
        "intruder": (0,    40, 220),
    }
    color = color_map.get(label, (180, 180, 180))

    # bbox 그리기
    for b in bboxes:
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        bl = b.get("label", "")
        cv2.rectangle(frame_bgr, (x, y), (x + w, y + h), color, 2)
        cv2.putText(frame_bgr, bl, (x, y - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

    # 상태 배너 (상단)
    banner_map = {
        "clear":    "CLEAR",
        "known":    f"KNOWN: {name or ''}",
        "delivery": "DELIVERY DETECTED",
        "intruder": "!! INTRUDER ALERT !!",
    }
    banner = banner_map.get(label, label.upper())
    cv2.rectangle(frame_bgr, (0, 0), (frame_bgr.shape[1], 32), (20, 20, 20), -1)
    cv2.putText(frame_bgr, banner, (8, 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)

    # 신뢰도
    if confidence > 0:
        conf_text = f"{confidence:.0%}"
        cv2.putText(frame_bgr, conf_text,
                    (frame_bgr.shape[1] - 70, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)

    # 타임스탬프
    ts = time.strftime("%H:%M:%S")
    cv2.putText(frame_bgr, ts,
                (8, frame_bgr.shape[0] - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 120, 120), 1, cv2.LINE_AA)

    return frame_bgr


def _decode_frame(data: bytes) -> Optional[np.ndarray]:
    """JPEG bytes → BGR ndarray"""
    arr = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return frame  # None이면 디코드 실패


# ──────────────────────────────────────────
# UDP 수신 스레드 (단순 단일 패킷 모드)
# ESP-CAM가 한 패킷에 JPEG 전체를 보내는 경우
# ──────────────────────────────────────────
def _udp_simple_receiver():
    """
    단순/분할 통합 모드:
    - 단일 패킷(SOI~EOI 완전한 JPEG): 바로 사용
    - 분할 패킷(1460 bytes MTU 분할): SOI/EOI 기반 자동 조립
    """
    global _latest_frame

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)  # 수신 버퍼 1MB
    sock.settimeout(1.0)
    sock.bind((UDP_IP, UDP_PORT))
    logger.info(f"[CameraStream] UDP 수신 시작 — {UDP_IP}:{UDP_PORT}")

    frame_count = 0
    frame_buf   = b""
    last_pkt_ts = time.time()
    pkt_count   = 0          # 수신 패킷 총 카운터 (디버그용)

    while _running:
        try:
            data, addr = sock.recvfrom(UDP_BUFFER_SIZE)
        except socket.timeout:
            if frame_buf:
                frame_buf = b""  # 불완전 프레임 폐기
            continue
        except OSError:
            break

        pkt_count += 1
        now = time.time()

        # ── 단일 완전 JPEG (SOI + EOI 모두 포함) ──
        if data[:2] == b'\xff\xd8' and data[-2:] == b'\xff\xd9':
            frame_buf = b""
            with _frame_lock:
                _latest_frame = data
            _frame_queue.append((frame_count, data))
            frame_count += 1
            last_pkt_ts = now
            continue

        # ── 분할 패킷: 새 프레임 시작 (SOI 마커) ──
        if data[:2] == b'\xff\xd8':
            frame_buf   = data
            last_pkt_ts = now
            continue

        # ── 분할 패킷: 중간/끝 조각 ──
        if frame_buf:
            # 0.5초 초과 시 손상 프레임으로 폐기
            if now - last_pkt_ts > 0.5:
                logger.debug("[CameraStream] 조립 타임아웃 — 버퍼 초기화")
                frame_buf = b""
                continue

            frame_buf  += data
            last_pkt_ts = now

            # EOI 마커 확인 → 조립 완료
            if frame_buf[-2:] == b'\xff\xd9':
                jpeg_bytes = frame_buf
                frame_buf  = b""
                if len(jpeg_bytes) > 100:
                    with _frame_lock:
                        _latest_frame = jpeg_bytes
                    _frame_queue.append((frame_count, jpeg_bytes))
                    frame_count += 1
                    if frame_count % 50 == 0:
                        logger.info(f"[CameraStream] 프레임 {frame_count} "
                                    f"| 크기: {len(jpeg_bytes)} bytes")

    sock.close()
    logger.info("[CameraStream] UDP 수신 스레드 종료")


# ──────────────────────────────────────────
# UDP 수신 스레드 (멀티파트 조립 모드)
# ESP-CAM가 큰 JPEG를 여러 UDP 패킷으로 분할 전송하는 경우
# ──────────────────────────────────────────
def _udp_multipart_receiver():
    """
    멀티파트 모드: MAGIC + 헤더로 프레임 조립
    [magic 4B][frame_id 4B][total_len 4B][part_idx 2B][total_parts 2B][JPEG chunk...]
    """
    global _latest_frame

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    sock.bind((UDP_IP, UDP_PORT))
    logger.info(f"[CameraStream] UDP 멀티파트 수신 시작 — {UDP_IP}:{UDP_PORT}")

    # 조립 버퍼: {frame_id: {part_idx: bytes}}
    assembly: dict = {}
    frame_count = 0

    while _running:
        try:
            data, addr = sock.recvfrom(UDP_BUFFER_SIZE)
        except socket.timeout:
            continue
        except OSError:
            break

        if len(data) < HEADER_SIZE:
            continue

        header = data[:HEADER_SIZE]
        payload = data[HEADER_SIZE:]

        try:
            magic, frame_id, total_len, part_idx, total_parts = \
                struct.unpack(HEADER_FMT, header)
        except struct.error:
            continue

        if magic != MAGIC:
            continue

        # 조립
        if frame_id not in assembly:
            assembly[frame_id] = {}
        assembly[frame_id][part_idx] = payload

        # 오래된 frame_id 정리 (최대 5프레임 보관)
        old_ids = [fid for fid in assembly if fid < frame_id - 5]
        for fid in old_ids:
            del assembly[fid]

        # 조립 완료 확인
        parts = assembly[frame_id]
        if len(parts) == total_parts:
            jpeg_bytes = b"".join(parts[i] for i in range(total_parts))
            del assembly[frame_id]

            with _frame_lock:
                _latest_frame = jpeg_bytes
            _frame_queue.append((frame_count, jpeg_bytes))
            frame_count += 1

    sock.close()
    logger.info("[CameraStream] UDP 멀티파트 수신 스레드 종료")


# ──────────────────────────────────────────
# 분석 루프 (비동기 — asyncio task)
# ──────────────────────────────────────────
async def analysis_loop(ws_broadcast_fn, tts_fn=None):
    """
    매 ANALYZE_EVERY 프레임마다 frame_analyzer 호출
    verdict에 따라 WS 브로드캐스트 + TTS 트리거

    Args:
        ws_broadcast_fn: async fn(dict) — websocket_hub.broadcast
        tts_fn:          async fn(str)  — tts_engine.speak (optional)
    """
    from server.frame_analyzer import FrameAnalyzer

    analyzer = FrameAnalyzer()
    await analyzer.load()
    logger.info("[CameraStream] FrameAnalyzer 로드 완료")

    last_analyzed = -1
    # 알람 쿨다운
    cooldown: dict = {"intruder": 0.0, "delivery": 0.0, "known": 0.0, "unknown": 0.0}
    COOLDOWN_SEC   = {"intruder": 30.0, "delivery": 60.0, "known": 10.0, "unknown": 10.0}

    while _running:
        await asyncio.sleep(0.05)  # 50ms 폴링

        if not _frame_queue:
            continue

        frame_no, jpeg_bytes = _frame_queue[-1]  # 최신 프레임

        if frame_no == last_analyzed:
            continue
        if frame_no % ANALYZE_EVERY != 0:
            continue

        last_analyzed = frame_no

        # 분석 (CPU 집중 → executor에서 실행)
        loop = asyncio.get_event_loop()
        try:
            verdict = await loop.run_in_executor(
                None, analyzer.analyze, jpeg_bytes
            )
        except Exception as e:
            logger.warning(f"[CameraStream] 분석 오류: {e}")
            continue

        # verdict 캐시 업데이트
        with _verdict_lock:
            _last_verdict.update(verdict)
            _last_verdict["timestamp"] = time.time()

        label = verdict.get("label", "clear")
        _name = verdict.get("name") or ""
        _conf = verdict.get("confidence", 0.0)

        # ── cam_verdict 브로드캐스트 (Command Log + 배지 업데이트) ──
        now = time.time()

        # 라벨별 표시 텍스트 및 다음 액션 힌트
        _verdict_meta = {
            "clear":    {"icon": "⬜", "text": "감지 없음",                 "action": "none"},
            "known":    {"icon": "✅", "text": f"등록 인물: {_name}",       "action": "unlock"},
            "delivery": {"icon": "📦", "text": "택배 감지",                "action": "notify"},
            "intruder": {"icon": "🚨", "text": "미등록 인물 감지 (UNKNOWN)", "action": "alarm"},
        }
        meta = _verdict_meta.get(label, {"icon": "❓", "text": label, "action": "none"})

        # clear 제외 모든 라벨 브로드캐스트
        # clear 는 이전 라벨이 clear 가 아닐 때만 브로드캐스트 (상태 초기화)
        with _verdict_lock:
            _prev_label = _last_verdict.get("label", "clear")
        if label != "clear" or _prev_label != "clear":
            await ws_broadcast_fn({
                "type":       "cam_verdict",
                "label":      label,
                "icon":       meta["icon"],
                "text":       meta["text"],
                "action":     meta["action"],
                "name":       verdict.get("name"),
                "confidence": _conf,
                "timestamp":  now,
            })

        # ── known / unknown 서버 로그 (10초 쿨다운) ──
        if label == "known":
            if now - cooldown["known"] > COOLDOWN_SEC["known"]:
                cooldown["known"] = now
                logger.info(f"[CameraStream] ✅ KNOWN: {_name} (conf={_conf:.0%})")
        elif label == "intruder":
            if now - cooldown["unknown"] > COOLDOWN_SEC["unknown"]:
                cooldown["unknown"] = now
                logger.warning(f"[CameraStream] ❓ UNKNOWN 인물 감지 (conf={_conf:.0%})")

        # ── 알람 브로드캐스트 ──
        if label == "intruder":
            if now - cooldown["intruder"] > COOLDOWN_SEC["intruder"]:
                cooldown["intruder"] = now
                await ws_broadcast_fn({
                    "type":       "cam_alert",
                    "level":      "intruder",
                    "msg":        "🚨 현관 미등록 인물 감지!",
                    "confidence": verdict.get("confidence", 0.0),
                    "timestamp":  now,
                })
                if tts_fn:
                    await tts_fn("현관에 미등록 인물이 감지되었습니다. 확인해 주세요.")
                logger.warning("[CameraStream] 🚨 INTRUDER DETECTED")

        elif label == "delivery":
            if now - cooldown["delivery"] > COOLDOWN_SEC["delivery"]:
                cooldown["delivery"] = now
                await ws_broadcast_fn({
                    "type":       "cam_alert",
                    "level":      "delivery",
                    "msg":        "📦 현관 택배 도착",
                    "confidence": verdict.get("confidence", 0.0),
                    "timestamp":  now,
                })
                if tts_fn:
                    await tts_fn("현관에 택배가 도착했습니다.")
                logger.info("[CameraStream] 📦 DELIVERY DETECTED")

        elif label == "known":
            await ws_broadcast_fn({
                "type":  "cam_notify",
                "level": "known",
                "msg":   f"✅ {verdict.get('name', '등록 얼굴')} 귀가",
                "timestamp": now,
            })
            logger.info(f"[CameraStream] ✅ KNOWN: {verdict.get('name')}")


# ──────────────────────────────────────────
# MJPEG HTTP 스트림 제너레이터
# FastAPI StreamingResponse에서 사용
# ──────────────────────────────────────────
async def mjpeg_generator():
    """
    FastAPI StreamingResponse용 async generator
    최신 프레임에 오버레이를 그려 multipart/x-mixed-replace로 스트리밍
    """
    interval    = 1.0 / STREAM_FPS_LIMIT
    last_sent   = None      # 동일 프레임 중복 전송 방지
    placeholder = _make_placeholder()

    while True:
        await asyncio.sleep(interval)

        with _frame_lock:
            jpeg_bytes = _latest_frame

        # 프레임 없을 때 → placeholder (1초에 1회만 전송)
        if jpeg_bytes is None:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" +
                   placeholder + b"\r\n")
            await asyncio.sleep(1.0)
            continue

        # 동일 프레임 재전송 스킵 (브라우저 깜박임 방지)
        if jpeg_bytes is last_sent:
            continue
        last_sent = jpeg_bytes

        # BGR로 디코딩 → 오버레이 → 재압축
        frame = _decode_frame(jpeg_bytes)
        if frame is None:
            continue

        frame = _build_overlay(frame)
        ok, buf = cv2.imencode(
            ".jpg", frame,
            [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
        )
        if not ok:
            continue

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" +
               buf.tobytes() + b"\r\n")


def _make_placeholder() -> bytes:
    """카메라 연결 대기 중 표시할 플레이스홀더 이미지"""
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    img[:] = (20, 20, 20)
    cv2.putText(img, "Connecting...", (80, 115),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100, 100, 100), 1)
    cv2.putText(img, f"UDP :{UDP_PORT}", (105, 140),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 60), 1)
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes()


# ──────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────
def start(multipart: bool = False):
    """UDP 수신 스레드 시작"""
    global _running, _udp_thread
    if _running:
        return
    _running = True
    target = _udp_multipart_receiver if multipart else _udp_simple_receiver
    _udp_thread = threading.Thread(target=target, daemon=True, name="udp-cam")
    _udp_thread.start()
    logger.info(f"[CameraStream] 시작 (mode={'multipart' if multipart else 'simple'})")


def stop():
    """UDP 수신 스레드 종료"""
    global _running
    _running = False
    if _udp_thread:
        _udp_thread.join(timeout=3)
    logger.info("[CameraStream] 종료")


def get_latest_jpeg() -> Optional[bytes]:
    """현재 최신 JPEG 프레임 반환 (외부 참조용)"""
    with _frame_lock:
        return _latest_frame


def update_verdict(verdict: dict):
    """frame_analyzer 외부에서 verdict 직접 업데이트 (테스트용)"""
    with _verdict_lock:
        _last_verdict.update(verdict)
        _last_verdict["timestamp"] = time.time()
