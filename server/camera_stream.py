"""
camera_stream.py — ESP-CAM UDP 수신 + MJPEG WebSocket 스트리밍
v2.2 | Voice IoT Controller
- v1.1: SOI/EOI 자동 조립, FPS/품질 최적화
- v1.2: 디버그 로그 강화 (패킷 수신 확인용)
- v1.3: cam_verdict WS 브로드캐스트 (매 분석마다 Command Log 기록)
- v1.4: 디버그 로그 정리, known/unknown Command Log 항상 기록, verdict 쿨다운 추가
- v1.5: SmartGate 연동 — push_frame() / set_smartgate_manager() 추가, verdict 타입 방어
- v1.6: SmartGate 프레임 공급을 매 프레임마다 수행 (ANALYZE_EVERY 무관하게)
         Liveness(눈 깜빡임/고개 회전) + 제스처(손가락 시퀀스) 정상 동작 보장
- v1.7: SmartGate 상태를 웹 클라이언트 전용 WS 브로드캐스트 (type: smartgate_overlay)
         MJPEG 오버레이는 verdict 전용, SmartGate 지시는 웹 UI에서 한글 표시
         상태+챌린지 변경 감지로 실시간 Sync 개선
- v1.8: SmartGate 인증 후 쿨다운 중 intruder→clear 강제 변환 (오탐 억제)
         verdict 캐시·cam_verdict·cam_alert·TTS·서버 로그 모두 억제
         MJPEG 오버레이도 쿨다운 중 CLEAR 유지
- v1.9: 보안모드(dnd) 알람 억제, SmartGate 활성 시 frame_analyzer 분석 skip
- v2.1: dnd 모드 시 unknown 로그 + intruder 알람/TTS 완전 억제
- v2.0: known 알림 "귀가"→"인식" 변경, known 쿨다운 10초→60초
- v2.2: UDP IP 화이트리스트 (MEDIUM-5) — 비인가 IP 패킷 차단
         simple/multipart 수신 모두 적용, .env CAM_ALLOWED_IPS 또는
         settings.yaml camera.allowed_ips 로 관리

ESP-CAM → UDP (JPEG 프레임) → Python 수신
    → frame_analyzer 분석 (매 ANALYZE_EVERY 프레임)
    → SmartGate 프레임 공급 (매 프레임)
    → WebSocket 브로드캐스트 (영상 + 알람 verdict + SmartGate 상태)
    → FastAPI MJPEG HTTP 스트림 (/camera/entrance/stream)
"""

import asyncio
import logging
import os
import socket
import struct
import threading
import time
from collections import deque
from typing import Optional, Set

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────
# v2.2: UDP IP 화이트리스트
# ──────────────────────────────────────────
# 허용 IP 집합 (main.py startup에서 set_allowed_cam_ips()로 주입)
# 빈 집합이면 필터링 비활성화 (경고 로그 출력)
_allowed_cam_ips: Set[str] = set()
# 비인가 IP별 차단 횟수 (로그 폭발 방지용)
_blocked_ip_counter: dict = {}


def set_allowed_cam_ips(ips: Set[str]) -> None:
    """허용 IP 집합 주입 (main.py startup에서 호출)"""
    global _allowed_cam_ips
    _allowed_cam_ips = set(ips)
    if _allowed_cam_ips:
        logger.info("[CameraStream] UDP IP 화이트리스트 설정: %s", _allowed_cam_ips)
    else:
        logger.warning("[CameraStream] UDP IP 화이트리스트 미설정 — IP 필터링 비활성화 (보안 위험)")


def load_allowed_cam_ips(settings: dict) -> Set[str]:
    """
    허용 IP 목록 로드.
    우선순위: 환경변수 CAM_ALLOWED_IPS > settings.yaml camera.allowed_ips
    main.py startup에서 호출 후 set_allowed_cam_ips()로 주입.

    예)
        .env:           CAM_ALLOWED_IPS=192.168.0.50,192.168.0.51
        settings.yaml:  camera.allowed_ips: ["192.168.0.50", "192.168.0.51"]
    """
    env_val = os.getenv("CAM_ALLOWED_IPS", "").strip()
    if env_val:
        ips = {ip.strip() for ip in env_val.split(",") if ip.strip()}
        logger.info("[CameraStream] IP 화이트리스트 로드 (env): %s", ips)
        return ips

    yaml_ips = settings.get("camera", {}).get("allowed_ips", [])
    if yaml_ips:
        ips = set(yaml_ips)
        logger.info("[CameraStream] IP 화이트리스트 로드 (yaml): %s", ips)
        return ips

    logger.warning("[CameraStream] CAM_ALLOWED_IPS 미설정 — UDP IP 필터링 비활성화")
    return set()


def _check_allowed_ip(src_ip: str) -> bool:
    """
    IP 화이트리스트 검사.
    허용 목록이 비어 있으면 항상 True (필터링 비활성).
    비인가 IP는 100회마다 1회 WARNING 로그.
    """
    if not _allowed_cam_ips:
        return True
    if src_ip in _allowed_cam_ips:
        return True
    cnt = _blocked_ip_counter.get(src_ip, 0) + 1
    _blocked_ip_counter[src_ip] = cnt
    if cnt % 100 == 1:
        logger.warning("[CameraStream] 비인가 IP 차단: %s (누적 %d회)", src_ip, cnt)
    return False

# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────
UDP_IP            = "0.0.0.0"
UDP_PORT          = 5005          # ESP-CAM UDP 송신 포트
UDP_BUFFER_SIZE   = 65535         # UDP 최대 수신 버퍼
UDP_RCVBUF_BYTES  = 1024 * 1024   # SO_RCVBUF 1MB (네트워크 스택 버퍼링 최소화)
FRAME_QUEUE_SIZE  = 10            # 최신 프레임 큐 (분석용, 지연 민감 시 ANALYZE_EVERY↑)
ANALYZE_EVERY     = 20            # 매 N 프레임마다 분석 (CPU 절약, 키우면 지연 영향↓)
STREAM_FPS_LIMIT  = 10            # MJPEG HTTP 스트림 FPS (15→10, 끊김 방지)
JPEG_QUALITY      = 70            # 재압축 JPEG 품질 (오버레이 스트림용)
SMARTGATE_EVERY   = 1             # v1.6: 매 N 프레임마다 SmartGate에 공급

# ESP-CAM 패킷 헤더 구조 (Arduino 펌웨어와 일치해야 함)
# [4B magic][4B frame_id][4B total_len][2B part_idx][2B total_parts][data...]
MAGIC             = b'\xAB\xCD\xEF\x01'
HEADER_FMT        = ">4sIIHH"
HEADER_SIZE       = struct.calcsize(HEADER_FMT)  # 16 bytes

# 하이브리드 재전송: frame_id 기준 손실률 → ESP32에 quality_down/up UDP 명령
ESP32_UDP_CMD_PORT = 5006         # esp32_cam.ino LOCAL_PORT (명령 수신 포트)
LOSS_RATE_THRESHOLD = 0.15        # 15% 이상 손실 시 quality_down
LOSS_RATE_RECOVERY  = 0.05       # 5% 이하로 유지 시 quality_up
LOSS_WINDOW_SIZE    = 50         # 손실률 계산 윈도우 (프레임 수)

# ──────────────────────────────────────────
# 전역 상태
# ──────────────────────────────────────────
_latest_frame: Optional[bytes]   = None          # 최신 JPEG bytes
_frame_lock                      = threading.Lock()
_frame_queue: deque              = deque(maxlen=FRAME_QUEUE_SIZE)
_running                         = False
_udp_thread: Optional[threading.Thread] = None

# ── v1.5: SmartGate 연동 ──────────────────────────────────────
_smartgate_manager = None

# ── v1.9: 보안모드 상태 참조 ──────────────────────────────────
# main.py에서 set_security_mode_fn()으로 콜백 주입
# 콜백 반환값: "armed" | "dnd" | "away" | "off" 등
_get_security_mode = None


def set_security_mode_fn(fn):
    """보안모드 조회 함수 주입 (main.py에서 호출)
    fn() → str: 현재 보안모드 반환 ("armed", "dnd", "away", "off" 등)
    """
    global _get_security_mode
    _get_security_mode = fn


def set_smartgate_manager(manager):
    """SmartGateManager 인스턴스 주입 (main.py lifespan에서 호출)"""
    global _smartgate_manager
    _smartgate_manager = manager


def push_frame(frame_bgr):
    """BGR ndarray를 SmartGateManager 인증 루프에 공급"""
    if _smartgate_manager is not None:
        _smartgate_manager.push_frame(frame_bgr)


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
    """최신 verdict 오버레이를 프레임에 그림 (SmartGate 지시는 웹 클라이언트에서 표시)"""
    h, w = frame_bgr.shape[:2]

    # ── verdict 오버레이 (기존) ──
    _draw_verdict_overlay(frame_bgr, h, w)

    # 타임스탬프 (항상 표시)
    ts = time.strftime("%H:%M:%S")
    cv2.putText(frame_bgr, ts,
                (8, h - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (120, 120, 120), 1, cv2.LINE_AA)

    return frame_bgr


def _draw_verdict_overlay(frame_bgr: np.ndarray, h: int, w: int):
    """기존 verdict 기반 오버레이 (IDLE 시)"""
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
        x, y, bw, bh = b["x"], b["y"], b["w"], b["h"]
        bl = b.get("label", "")
        cv2.rectangle(frame_bgr, (x, y), (x + bw, y + bh), color, 2)
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
    cv2.rectangle(frame_bgr, (0, 0), (w, 32), (20, 20, 20), -1)
    cv2.putText(frame_bgr, banner, (8, 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)

    # 신뢰도
    if confidence > 0:
        conf_text = f"{confidence:.0%}"
        cv2.putText(frame_bgr, conf_text,
                    (w - 70, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)


def _decode_frame(data: bytes) -> Optional[np.ndarray]:
    """JPEG bytes → BGR ndarray (좌우 반전 적용)"""
    arr = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is not None:
        frame = cv2.flip(frame, 1)  # 1 = horizontal flip (좌우 반전)
    return frame


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

        # ── v2.2: IP 화이트리스트 검사 ──
        if not _check_allowed_ip(addr[0]):
            continue

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
    frame_id 기준 손실률 계산 → 일정 이상 시 ESP32에 quality_down/up UDP 명령 전송
    """
    global _latest_frame

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RCVBUF_BYTES)
    sock.settimeout(1.0)
    sock.bind((UDP_IP, UDP_PORT))
    logger.info(f"[CameraStream] UDP 멀티파트 수신 시작 — {UDP_IP}:{UDP_PORT}")

    # ESP32 명령 전송용 소켓 (별도, bind 불필요)
    try:
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        send_sock = None

    frame_count = 0
    frame_parts = {}         # {frame_id: {part_idx: data, ...}}
    frame_meta  = {}         # {frame_id: (total_len, total_parts)}

    # 하이브리드 재전송: 손실률 추적
    last_seen_frame_id = None
    total_lost = 0
    total_received = 0
    last_quality_cmd = ""    # "down" | "up" | ""
    frames_since_cmd = 0

    while _running:
        try:
            data, addr = sock.recvfrom(UDP_BUFFER_SIZE)
        except socket.timeout:
            continue
        except OSError:
            break

        if len(data) < HEADER_SIZE:
            continue

        # ── v2.2: IP 화이트리스트 검사 ──
        if not _check_allowed_ip(addr[0]):
            continue

        magic, frame_id, total_len, part_idx, total_parts = \
            struct.unpack(HEADER_FMT, data[:HEADER_SIZE])

        if magic != MAGIC:
            continue

        esp32_addr = (addr[0], ESP32_UDP_CMD_PORT)  # ESP32 수신 포트로 명령 전송
        payload = data[HEADER_SIZE:]

        if frame_id not in frame_parts:
            frame_parts[frame_id] = {}
            frame_meta[frame_id]  = (total_len, total_parts)

        frame_parts[frame_id][part_idx] = payload

        # 모든 파트 수신 완료 → 조립
        if len(frame_parts[frame_id]) == total_parts:
            jpeg_bytes = b""
            for i in range(total_parts):
                jpeg_bytes += frame_parts[frame_id].get(i, b"")

            if len(jpeg_bytes) > 100:
                with _frame_lock:
                    _latest_frame = jpeg_bytes
                _frame_queue.append((frame_count, jpeg_bytes))
                frame_count += 1

                # 손실률 계산 (frame_id 갭 기반)
                if last_seen_frame_id is not None and frame_id > last_seen_frame_id + 1:
                    lost = frame_id - last_seen_frame_id - 1
                    total_lost += lost
                total_received += 1
                last_seen_frame_id = frame_id
                frames_since_cmd += 1

                # 윈도우마다 손실률 체크 → quality_down/up 전송
                n = total_received + total_lost
                if n >= LOSS_WINDOW_SIZE and frames_since_cmd >= 20 and send_sock:
                    loss_rate = total_lost / max(1, n)
                    if loss_rate >= LOSS_RATE_THRESHOLD and last_quality_cmd != "down":
                        try:
                            send_sock.sendto(b"quality_down", esp32_addr)
                            last_quality_cmd = "down"
                            frames_since_cmd = 0
                            logger.info(f"[CameraStream] 손실률 {loss_rate:.0%} → quality_down 전송")
                        except OSError:
                            pass
                    elif loss_rate <= LOSS_RATE_RECOVERY and last_quality_cmd == "down":
                        try:
                            send_sock.sendto(b"quality_up", esp32_addr)
                            last_quality_cmd = ""
                            frames_since_cmd = 0
                            logger.info(f"[CameraStream] 손실률 {loss_rate:.0%} → quality_up 전송")
                        except OSError:
                            pass
                    total_lost = 0
                    total_received = 0

            del frame_parts[frame_id]
            del frame_meta[frame_id]

        # 오래된 미완성 프레임 정리
        old_ids = [fid for fid in frame_parts if fid < frame_id - 5]
        for fid in old_ids:
            del frame_parts[fid]
            if fid in frame_meta:
                del frame_meta[fid]

    if send_sock:
        try:
            send_sock.close()
        except OSError:
            pass
    sock.close()
    logger.info("[CameraStream] UDP 멀티파트 수신 스레드 종료")


# ──────────────────────────────────────────
# 분석 루프 (비동기 — asyncio task)
# ──────────────────────────────────────────
async def analysis_loop(ws_broadcast_fn, tts_fn=None):
    """
    매 ANALYZE_EVERY 프레임마다 frame_analyzer 호출
    verdict에 따라 WS 브로드캐스트 + TTS 트리거

    v1.6: SmartGate 프레임 공급을 ANALYZE_EVERY와 분리
    v1.7: SmartGate 상태를 WS 브로드캐스트 (type: smartgate_overlay)

    Args:
        ws_broadcast_fn: async fn(dict) — websocket_hub.broadcast
        tts_fn:          async fn(str)  — tts_engine.speak (optional)
    """
    from server.frame_analyzer import FrameAnalyzer

    analyzer = FrameAnalyzer()
    await analyzer.load()
    logger.info("[CameraStream] FrameAnalyzer 로드 완료")

    last_analyzed = -1
    last_smartgate_frame = -1   # v1.6: SmartGate 마지막 공급 프레임 번호
    last_sg_state = ""          # v1.7: SmartGate 상태 변경 감지용
    # 알람 쿨다운
    cooldown: dict = {"intruder": 0.0, "delivery": 0.0, "known": 0.0, "unknown": 0.0}
    COOLDOWN_SEC   = {"intruder": 30.0, "delivery": 60.0, "known": 60.0, "unknown": 10.0}
    # v2.0: known 감지 후 intruder 억제
    _last_known_time = 0.0          # 마지막 known 감지 시각
    _KNOWN_SUPPRESS_SEC = 60.0      # known 감지 후 60초간 intruder 억제

    while _running:
        await asyncio.sleep(0.05)  # 50ms 폴링

        if not _frame_queue:
            continue

        frame_no, jpeg_bytes = _frame_queue[-1]  # 최신 프레임

        if frame_no == last_analyzed and frame_no == last_smartgate_frame:
            continue

        # ── v1.6: SmartGate 프레임 공급 (ANALYZE_EVERY와 독립) ──
        if frame_no != last_smartgate_frame and frame_no % SMARTGATE_EVERY == 0:
            last_smartgate_frame = frame_no
            frame_bgr = _decode_frame(jpeg_bytes)
            if frame_bgr is not None:
                push_frame(frame_bgr)

        # ── v1.7: SmartGate 상태 WS 브로드캐스트 (웹 오버레이용) ──
        if _smartgate_manager is not None:
            sg_status = _smartgate_manager.status
            sg_state  = sg_status.get("state", "IDLE")

            # 한글 메시지 생성
            _sg_msg = ""
            _sg_challenge = ""

            if sg_state == "ARMED":
                _sg_msg = "카메라를 정면으로 바라봐 주세요"
            elif sg_state == "LIVENESS" and hasattr(_smartgate_manager, 'liveness'):
                lv = _smartgate_manager.liveness
                _sg_challenge = getattr(lv, 'current_challenge', '') or ''
                _idx = getattr(lv, '_current_idx', 0)
                _total = len(getattr(lv, '_challenges', []))
                if _sg_challenge:
                    _sg_msg = f"({_idx + 1}/{_total}) 챌린지 진행 중"
                else:
                    _sg_msg = "챌린지 준비 중..."
            elif sg_state == "FACE_OK":
                seq = sg_status.get("sequence", [])
                _sg_msg = f"제스처 시퀀스: [{', '.join(str(s) for s in seq)}]"
            elif sg_state == "GESTURE_OK":
                _sg_msg = "게이트 열림"
            elif sg_state == "LOCKED":
                remain = sg_status.get("lockout_remaining_sec", 0)
                _sg_msg = f"잠금 ({remain:.0f}초)"

            # WS 브로드캐스트 (매 폴링 — 상태+챌린지 변경 감지)
            _sg_key = f"{sg_state}:{_sg_challenge}:{_sg_msg}"
            if _sg_key != last_sg_state:
                last_sg_state = _sg_key
                try:
                    await ws_broadcast_fn({
                        "type":      "smartgate_overlay",
                        "state":     sg_state,
                        "msg":       _sg_msg,
                        "user":      sg_status.get("user", "") or sg_status.get("authenticated_user", "") or "",
                        "challenge": _sg_challenge,
                        "sequence":  sg_status.get("sequence", []),
                        "fail_count": sg_status.get("fail_count", 0),
                        "gate_open": sg_status.get("gate_open", False),
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass

        # ── frame_analyzer 분석 (기존 로직 유지) ──
        # v1.9: SmartGate 인증 활성 또는 쿨다운 중이면 분석 skip
        # → CPU를 SmartGate에 집중 + 미등록 오탐 방지
        if _smartgate_manager is not None:
            _sg_st = _smartgate_manager.status
            _sg_state = _sg_st.get("state", "IDLE")
            if _sg_state != "IDLE" or _sg_st.get("in_cooldown", False):
                continue

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

        # v1.5: verdict 타입 방어 (string 반환 시 skip)
        if not isinstance(verdict, dict):
            continue

        # verdict 캐시 업데이트
        with _verdict_lock:
            _last_verdict.update(verdict)
            _last_verdict["timestamp"] = time.time()

        label = verdict.get("label", "clear")
        _name = verdict.get("name") or ""
        _conf = verdict.get("confidence", 0.0)

        # ── v1.8: 쿨다운 중 intruder 오탐 억제 ──
        # ── v1.9: 방해 금지(dnd) 모드 시 intruder 알람 전체 억제 ──
        # ── v2.0: known 감지 후 60초간 intruder 억제 ──
        # 인증 성공 후 120초 동안은 intruder → clear 강제 변환
        # (인증된 사용자가 현관에 머무를 때 미등록 오탐 방지)
        _suppress_intruder = False
        _suppress_reason   = ""

        if label == "intruder":
            # 1) 보안모드가 방해 금지(dnd)이면 억제
            if _get_security_mode is not None:
                try:
                    _sec_mode = _get_security_mode()
                    if _sec_mode == "dnd":
                        _suppress_intruder = True
                        _suppress_reason   = "dnd"
                        logger.debug(
                            f"[CameraStream] 보안모드 '{_sec_mode}' → intruder 억제"
                        )
                except Exception:
                    pass

            # 2) SmartGate 쿨다운 중이면 억제
            if not _suppress_intruder and _smartgate_manager is not None:
                sg_status = _smartgate_manager.status
                if sg_status.get("in_cooldown", False):
                    _suppress_intruder = True
                    _suppress_reason   = "cooldown"
                    logger.debug(
                        "[CameraStream] 쿨다운 중 intruder → clear 억제 "
                        f"(conf={_conf:.0%})"
                    )

            # 3) v2.0: 최근 known 감지 후 60초 이내이면 억제
            if not _suppress_intruder and _last_known_time > 0:
                if (time.time() - _last_known_time) < _KNOWN_SUPPRESS_SEC:
                    _suppress_intruder = True
                    _suppress_reason   = "known_recent"
                    logger.debug(
                        f"[CameraStream] known 감지 후 {time.time() - _last_known_time:.0f}s → "
                        f"intruder 억제 (conf={_conf:.0%})"
                    )

            if _suppress_intruder:
                # dnd 억제 시: cooldown 갱신 + 로그 기록 후 label → clear
                # (cooldown 미갱신 시 30초 후 억제 없이 알람 재발생하는 버그 방지)
                if _suppress_reason == "dnd":
                    if now - cooldown["intruder"] > COOLDOWN_SEC["intruder"]:
                        cooldown["intruder"] = now
                        logger.debug(
                            "[CameraStream] 방해금지 모드 — intruder 쿨다운 갱신 + 알람/TTS 억제"
                        )
                label = "clear"
                verdict["label"] = "clear"
                with _verdict_lock:
                    _last_verdict["label"] = "clear"

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

        # ── known / unknown 서버 로그 (60초 쿨다운) ──
        if label == "known":
            _last_known_time = now    # v2.0: intruder 억제 기준 시각 갱신
            if now - cooldown["known"] > COOLDOWN_SEC["known"]:
                cooldown["known"] = now
                logger.info(f"[CameraStream] ✅ KNOWN: {_name} (conf={_conf:.0%})")
        elif label == "intruder":
            # dnd 억제는 위에서 처리됨 → 여기 도달 시 억제 없는 실제 intruder
            if now - cooldown["unknown"] > COOLDOWN_SEC["unknown"]:
                cooldown["unknown"] = now
                logger.warning(f"[CameraStream] ❓ UNKNOWN 인물 감지 (conf={_conf:.0%})")

        # ── 알람 브로드캐스트 ──
        # dnd 억제는 위 _suppress_intruder 블록에서 처리됨
        # 이 블록 도달 시 label == "intruder" 이면 반드시 알람 발생 대상
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
                "msg":   f"✅ {verdict.get('name', '등록 얼굴')} 인식",
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


# ──────────────────────────────────────────
# Raw MJPEG 제너레이터 (지연 최소)
# 디코드/오버레이/재인코딩 없이 _latest_frame 바이트를 그대로 전송
# → /camera/entrance/raw 에서 사용
# ──────────────────────────────────────────
async def mjpeg_raw_generator():
    """원본 JPEG을 재인코딩 없이 MJPEG으로 전송 — 지연 최소"""
    interval    = 1.0 / STREAM_FPS_LIMIT
    last_sent   = None
    placeholder = _make_placeholder()

    while True:
        await asyncio.sleep(interval)

        with _frame_lock:
            jpeg_bytes = _latest_frame

        if jpeg_bytes is None:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" +
                   placeholder + b"\r\n")
            await asyncio.sleep(1.0)
            continue

        if jpeg_bytes is last_sent:
            continue
        last_sent = jpeg_bytes

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" +
               jpeg_bytes + b"\r\n")


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
