"""
server/smartgate/manager.py
============================
SmartGateManager — asyncio 기반 상태머신

독립 프로젝트(smartgate/main.py)의 SmartGate 클래스를
iot-repo-1의 비동기 아키텍처에 맞게 변환한 버전

주요 변경점 (독립 → 통합):
  - cv2.VideoCapture → camera_stream.push_frame() 프레임 수신
  - cv2.imshow 렌더링 제거 → WS 브로드캐스트 상태 전송
  - GateController 시리얼 → TCP 방식
  - 동기 while 루프 → asyncio task (_auth_loop)
  - config.yaml → settings.yaml (smartgate 섹션)

v1.1 변경사항:
  - DBLogger 연동 (SR-3.1): 주요 인증 이벤트 DB 로그 기록
    · arm/disarm, 얼굴 인식, liveness 통과/실패
    · 제스처 인증, 2FA 성공, 게이트 오픈/닫힘
    · 잠금(lockout), PIR 환영 트리거

인증 흐름:
  IDLE → (얼굴 인증) → LIVENESS → (챌린지 통과) → FACE_OK
       → (제스처 인증) → GESTURE_OK → (게이트 오픈) → IDLE

상태 전이:
  push_frame(frame_bgr)  ← camera_stream 에서 호출
  _auth_loop()           ← asyncio.create_task() 로 실행
  status                 ← API 응답용 프로퍼티
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# 인증 상태
# ──────────────────────────────────────────────────────────────
class AuthState(Enum):
    IDLE       = auto()   # 대기 (인증 비활성 — arm() 호출 대기)
    ARMED      = auto()   # 인증 준비 (얼굴 감지 중)
    LIVENESS   = auto()   # Liveness Challenge
    FACE_OK    = auto()   # 1팩터 성공 → 제스처 대기
    GESTURE_OK = auto()   # 2팩터 성공 → 게이트 오픈
    LOCKED     = auto()   # 잠금 상태


# ──────────────────────────────────────────────────────────────
# SmartGateManager
# ──────────────────────────────────────────────────────────────
class SmartGateManager:
    """
    asyncio 기반 SmartGate 상태머신

    Parameters
    ----------
    settings    : dict — settings.yaml 전체 (smartgate 섹션 사용)
    tcp_server  : TCPServer 인스턴스 (게이트 서보 제어용)
    db_logger   : DBLogger 인스턴스 (이벤트 로그 기록, optional)
    """

    FACE_HOLD_SEC      = 3.0    # 얼굴 인증 후 제스처 대기 여유 시간
    AUTH_LOOP_INTERVAL = 0.1    # 인증 루프 주기 (초)
    FRAME_QUEUE_SIZE   = 5      # 프레임 큐 크기
    AUTH_COOLDOWN_SEC  = 120.0  # 인증 성공 후 재인증 방지 쿨다운 (초)
    ARM_TIMEOUT_SEC    = 30.0   # ARMED 상태 자동 해제 타임아웃 (초)

    def __init__(self, settings: dict, tcp_server=None, ws_broadcast_fn=None,
                 tts_fn=None, db_logger=None):
        self._cfg = settings.get("smartgate", {})
        self._tcp_server = tcp_server
        self._ws_broadcast = ws_broadcast_fn   # async fn(dict) — 웹 대시보드 이벤트
        self._tts_fn = tts_fn                  # async fn(str) — TTS 발화 콜백
        self._db_logger = db_logger            # DBLogger 인스턴스 (SR-3.1)

        # ── 모듈 초기화 ──────────────────────────────────────────
        self.face_auth = self._init_face_auth()
        self.liveness = self._init_liveness()
        self.gesture_auth = self._init_gesture_auth()
        self.gate_ctrl = self._init_gate_controller()

        # ── 보안 설정 ────────────────────────────────────────────
        _sec = self._cfg.get("security", {})
        self._max_failures = _sec.get("max_failures", 3)
        self._lockout_sec = _sec.get("lockout_sec", 30)

        # ── 런타임 상태 ──────────────────────────────────────────
        self._state: AuthState = AuthState.IDLE
        self._authenticated_user: str = ""
        self._fail_count: int = 0
        self._lockout_until: float = 0.0
        self._face_ok_time: float = 0.0
        self._auth_cooldown_until: float = 0.0  # 인증 성공 후 쿨다운 만료 시각
        self._last_authenticated_user: str = ""  # 쿨다운 동안 사용자 이름 유지
        self._welcome_done: bool = True          # PIR 환영 완료 플래그 (True=대기 없음)
        self._armed_until: float = 0.0           # ARMED 상태 만료 시각

        # 프레임 큐 (camera_stream → push_frame → _auth_loop)
        self._frame_queue: deque = deque(maxlen=self.FRAME_QUEUE_SIZE)

        # asyncio task
        self._auth_task: Optional[asyncio.Task] = None
        self._running: bool = False

        # 배너 출력
        self._print_init_banner()

    # ──────────────────────────────────────────
    # DB 로그 헬퍼 (SR-3.1)
    # ──────────────────────────────────────────
    def _db_log(self, summary: str, *, level: str = "INFO",
                detail: dict | None = None):
        """SmartGate 이벤트를 DB에 기록 (fire-and-forget)"""
        if self._db_logger and self._db_logger.enabled:
            self._db_logger.log(
                "security_alert",
                "smartgate",
                summary,
                device_id=self._cfg.get("gate_device_id", "esp32_entrance"),
                room="entrance",
                detail=detail,
                level=level,
            )

    # ──────────────────────────────────────────
    # 모듈 초기화
    # ──────────────────────────────────────────
    def _init_face_auth(self):
        """FaceAuthenticator 초기화"""
        from server.smartgate.face_auth import FaceAuthenticator

        _fc = self._cfg.get("face_auth", {})
        _face_db = self._cfg.get("face_db_dir", "face_db")

        return FaceAuthenticator(
            face_db_dir=_face_db,
            tolerance=_fc.get("tolerance", 0.40),
            min_face_size=_fc.get("min_face_size", 80),
            model_name=_fc.get("model_name", "buffalo_sc"),
        )

    def _init_liveness(self):
        """LivenessChecker 초기화"""
        from server.smartgate.liveness import LivenessChecker

        _lc = self._cfg.get("liveness", {})
        return LivenessChecker(_lc)

    def _init_gesture_auth(self):
        """GestureAuthenticator 초기화"""
        from server.smartgate.gesture_auth import GestureAuthenticator

        _gc = self._cfg.get("gesture_auth", {})
        return GestureAuthenticator(
            mode=_gc.get("mode", "number"),
            sequence=_gc.get("sequence", [1, 0, 3]),
            timeout_sec=_gc.get("timeout_sec", 7.0),
            hold_frames=_gc.get("hold_frames", 8),
            cooldown_sec=_gc.get("cooldown_sec", 1.5),
        )

    def _init_gate_controller(self):
        """GateController (TCP) 초기화"""
        from server.smartgate.gate_controller import GateController

        return GateController(
            tcp_server=self._tcp_server,
            device_id=self._cfg.get("gate_device_id", "esp32_entrance"),
            open_duration_sec=self._cfg.get("open_duration_sec", 5),
        )

    # ──────────────────────────────────────────
    # ARM / DISARM (인증 시작/취소)
    # ──────────────────────────────────────────
    async def arm(self) -> dict:
        """인증 시작 — IDLE → ARMED 전환 (웹 버튼에서 호출)"""
        if self._state != AuthState.IDLE:
            return {
                "status": "fail",
                "msg": f"현재 상태에서 시작 불가: {self._state.name}",
                "state": self._state.name,
            }

        self._state = AuthState.ARMED
        self._armed_until = time.time() + self.ARM_TIMEOUT_SEC
        self._auth_cooldown_until = 0.0   # ← 쿨다운 해제 (명시적 ARM이므로)
        self._fail_count = 0
        self._authenticated_user = ""
        self.liveness.reset()
        self.gesture_auth._reset_sequence()

        logger.info(
            f"[SmartGate] 🟢 ARMED | "
            f"{self.ARM_TIMEOUT_SEC:.0f}초 내 인증 시작"
        )
        self._db_log(
            "인증 준비 (ARMED)",
            detail={"action": "arm", "timeout_sec": self.ARM_TIMEOUT_SEC},
        )
        await self._broadcast_event(
            "armed",
            timeout_sec=self.ARM_TIMEOUT_SEC,
        )

        return {
            "status": "ok",
            "msg": f"인증 준비 완료 — {self.ARM_TIMEOUT_SEC:.0f}초 내 카메라를 바라보세요",
            "state": "ARMED",
            "timeout_sec": self.ARM_TIMEOUT_SEC,
        }

    async def disarm(self) -> dict:
        """인증 취소 — ARMED/LIVENESS/FACE_OK → IDLE 복귀"""
        prev = self._state.name
        if self._state in (AuthState.ARMED, AuthState.LIVENESS, AuthState.FACE_OK):
            self._state = AuthState.IDLE
            self._authenticated_user = ""
            self.liveness.reset()
            self.gesture_auth._reset_sequence()
            logger.info(f"[SmartGate] 🔴 DISARMED ({prev} → IDLE)")
            self._db_log(
                f"인증 취소 (DISARMED)",
                detail={"action": "disarm", "prev_state": prev},
            )
            await self._broadcast_event("disarmed")
            return {"status": "ok", "msg": "인증 취소됨", "state": "IDLE"}

        return {
            "status": "fail",
            "msg": f"취소할 수 없는 상태: {self._state.name}",
            "state": self._state.name,
        }

    # ──────────────────────────────────────────
    # 라이프사이클
    # ──────────────────────────────────────────
    async def start(self):
        """인증 루프 시작 (main.py lifespan에서 호출)"""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_event_loop()
        self.gate_ctrl._loop = self._loop
        self._auth_task = asyncio.create_task(self._auth_loop())
        logger.info("[SmartGate] 인증 루프 시작")

    async def stop(self):
        """인증 루프 종료"""
        self._running = False
        if self._auth_task and not self._auth_task.done():
            self._auth_task.cancel()
            try:
                await self._auth_task
            except asyncio.CancelledError:
                pass
        await self.gate_ctrl.cleanup()
        logger.info("[SmartGate] 인증 루프 종료")

    # ──────────────────────────────────────────
    # 프레임 수신
    # ──────────────────────────────────────────
    def push_frame(self, frame_bgr: np.ndarray):
        """
        camera_stream에서 호출 — BGR 프레임을 큐에 추가
        thread-safe: deque.append는 GIL 하에서 atomic
        """
        self._frame_queue.append(frame_bgr)

    # ──────────────────────────────────────────
    # 인증 루프 (asyncio task)
    # ──────────────────────────────────────────
    async def _auth_loop(self):
        """
        메인 인증 상태머신 루프
        camera_stream.push_frame()으로 들어온 프레임을 순차 처리
        """
        logger.info("[SmartGate] _auth_loop 시작")

        while self._running:
            await asyncio.sleep(self.AUTH_LOOP_INTERVAL)

            # 프레임 가져오기 (최신 1장)
            if not self._frame_queue:
                continue

            frame_bgr = self._frame_queue[-1]
            self._frame_queue.clear()

            now = time.time()

            try:
                await self._process_state(frame_bgr, now)
            except Exception as e:
                logger.error(f"[SmartGate] 인증 루프 오류: {e}", exc_info=True)

        logger.info("[SmartGate] _auth_loop 종료")

    async def _process_state(self, frame_bgr: np.ndarray, now: float):
        """상태별 처리 분기"""

        # ── LOCKED ────────────────────────────────────────
        if self._state == AuthState.LOCKED:
            remaining = self._lockout_until - now
            if remaining <= 0:
                self._state = AuthState.IDLE
                self._fail_count = 0
                logger.info("[SmartGate] 잠금 해제 → IDLE")
                self._db_log(
                    "잠금 해제 → IDLE",
                    detail={"action": "lockout_end"},
                )
            return

        # ── IDLE: 대기 (인증 비활성 — arm() 대기) ──────
        if self._state == AuthState.IDLE:
            return

        # ── ARMED: 얼굴 인식 활성 ─────────────────────
        if self._state == AuthState.ARMED:
            # ARMED 타임아웃 체크
            if now > self._armed_until:
                self._state = AuthState.IDLE
                logger.info("[SmartGate] ARMED 타임아웃 → IDLE")
                self._db_log(
                    "ARMED 타임아웃 → IDLE",
                    detail={"action": "arm_timeout"},
                    level="WARN",
                )
                await self._broadcast_event("arm_timeout")
                return

            # 인증 성공 후 쿨다운 중이면 skip
            if now < self._auth_cooldown_until:
                return

            # CPU-intensive → executor
            loop = asyncio.get_event_loop()
            success, name, sim = await loop.run_in_executor(
                None, self.face_auth.authenticate, frame_bgr
            )

            # ── 디버그: 매 인식 시도 결과 로그 (v1.2) ──
            if success:
                logger.info(
                    f"[SmartGate] ✅ 얼굴 인식: {name} "
                    f"(sim={sim:.3f})"
                )
                self._authenticated_user = name
                self._db_log(
                    f"얼굴 인식 성공: {name}",
                    detail={"action": "face_recognized", "user": name,
                            "similarity": round(sim, 3)},
                )
                await self._broadcast_event("face_ok", user=name, similarity=round(sim, 3))

                # Liveness 분기
                if self.liveness.enabled and self.liveness.is_ready:
                    self._state = AuthState.LIVENESS
                    self.liveness.start()
                    logger.info("[SmartGate] → LIVENESS 챌린지 시작")
                else:
                    self._state = AuthState.FACE_OK
                    self._face_ok_time = now
                    self.gesture_auth._reset_sequence()
                    logger.info("[SmartGate] → FACE_OK (Liveness 비활성)")
            else:
                logger.info(
                    f"[SmartGate] 🔍 얼굴 인식 시도 | "
                    f"sim={sim:.3f} / threshold={self.face_auth.tolerance} | "
                    f"name={name} | "
                    f"frame={frame_bgr.shape if frame_bgr is not None else 'None'} | "
                    f"known={len(self.face_auth.known_embeddings)}"
                )
            return

        # ── LIVENESS: 생존 인증 ──────────────────────────
        if self._state == AuthState.LIVENESS:
            loop = asyncio.get_event_loop()
            done, failed, msg = await loop.run_in_executor(
                None, self.liveness.process, frame_bgr
            )

            if done:
                self._state = AuthState.FACE_OK
                self._face_ok_time = now
                self.gesture_auth._reset_sequence()
                logger.info("[SmartGate] ✅ Liveness 통과 → FACE_OK")
                self._db_log(
                    f"Liveness 통과: {self._authenticated_user}",
                    detail={"action": "liveness_pass",
                            "user": self._authenticated_user},
                )
            elif failed:
                self._handle_failure("Liveness 타임아웃")
                logger.warning(f"[SmartGate] ❌ Liveness 실패: {msg}")
                self._db_log(
                    f"Liveness 실패: {msg}",
                    detail={"action": "liveness_fail", "reason": msg or "타임아웃",
                            "user": self._authenticated_user},
                    level="WARN",
                )
                await self._broadcast_event("liveness_fail", msg=msg or "타임아웃")
            return

        # ── FACE_OK: 제스처 인식 ─────────────────────────
        if self._state == AuthState.FACE_OK:
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

            loop = asyncio.get_event_loop()
            auth_ok, seq, finger_cnt, _ = await loop.run_in_executor(
                None, self.gesture_auth.process_frame, frame_rgb
            )

            if auth_ok:
                self._state = AuthState.GESTURE_OK
                logger.info(
                    f"[SmartGate] ✅ 제스처 인증 성공: "
                    f"{self.gesture_auth.target_sequence}"
                )
                logger.info(
                    f"[SmartGate] 🔓 2FA 인증 완료 | "
                    f"사용자: {self._authenticated_user}"
                )

                # 게이트 오픈
                self.gate_ctrl.open_gate()
                self._fail_count = 0

                self._db_log(
                    f"2FA 인증 성공: {self._authenticated_user}",
                    detail={
                        "action": "auth_success",
                        "user": self._authenticated_user,
                        "sequence": list(self.gesture_auth.target_sequence),
                    },
                )

                await self._broadcast_event(
                    "auth_success",
                    user=self._authenticated_user,
                    sequence=list(self.gesture_auth.target_sequence),
                )

                # ── PIR 환영 대기 모드 ──
                # 조명+TTS는 현관 PIR 감지 시 trigger_welcome()에서 실행
                self._last_authenticated_user = self._authenticated_user
                self._welcome_done = False
                logger.info(
                    f"[SmartGate] 환영 대기 모드 | "
                    f"사용자: {self._authenticated_user} | PIR 감지 대기 중"
                )

            # 전체 타임아웃 검사
            elif (now - self._face_ok_time) > (
                self.FACE_HOLD_SEC + self.gesture_auth.timeout_sec
            ):
                if not self.gesture_auth.current_sequence:
                    self._handle_failure("제스처 타임아웃")
                    logger.warning("[SmartGate] ❌ 제스처 타임아웃")
                    self._db_log(
                        "제스처 타임아웃",
                        detail={"action": "gesture_timeout",
                                "user": self._authenticated_user},
                        level="WARN",
                    )
            return

        # ── GESTURE_OK: 게이트 열림 상태 ─────────────────
        if self._state == AuthState.GESTURE_OK:
            if not self.gate_ctrl.is_gate_open:
                self._state = AuthState.IDLE
                self._auth_cooldown_until = now + self.AUTH_COOLDOWN_SEC
                logger.info(
                    f"[SmartGate] 게이트 닫힘 → IDLE "
                    f"(쿨다운 {self.AUTH_COOLDOWN_SEC:.0f}초)"
                )
                self._db_log(
                    f"게이트 닫힘 → IDLE (쿨다운 {self.AUTH_COOLDOWN_SEC:.0f}초)",
                    detail={"action": "gate_closed",
                            "cooldown_sec": self.AUTH_COOLDOWN_SEC,
                            "user": self._last_authenticated_user},
                )
                self._authenticated_user = ""
                # 웹 오버레이 초기화
                await self._broadcast_event("gate_closed")
            return

    # ──────────────────────────────────────────
    # 실패 처리
    # ──────────────────────────────────────────
    def _handle_failure(self, reason: str = ""):
        """인증 실패 처리 + 잠금 검사"""
        self._fail_count += 1
        self.gesture_auth._reset_sequence()
        self.liveness.reset()
        self._authenticated_user = ""

        # ARMED 타임아웃 내이면 ARMED로 복귀 (자동 재시도)
        # 타임아웃 만료 시 IDLE로 복귀
        now = time.time()
        if now < self._armed_until:
            self._state = AuthState.ARMED
            logger.warning(
                f"[SmartGate] 실패 ({self._fail_count}/{self._max_failures})"
                f" | 사유: {reason} → ARMED 유지 (자동 재시도)"
            )
        else:
            self._state = AuthState.IDLE
            logger.warning(
                f"[SmartGate] 실패 ({self._fail_count}/{self._max_failures})"
                f" | 사유: {reason} → IDLE (ARM 타임아웃 만료)"
            )

        if self._fail_count >= self._max_failures:
            self._state = AuthState.LOCKED
            self._lockout_until = time.time() + self._lockout_sec
            logger.error(
                f"[SmartGate] 🚫 잠금 활성화 | "
                f"{self._lockout_sec}초 대기"
            )
            self._db_log(
                f"잠금 활성화 ({self._fail_count}회 실패)",
                detail={"action": "lockout_start", "reason": reason,
                        "fail_count": self._fail_count,
                        "lockout_sec": self._lockout_sec},
                level="ERROR",
            )
            # 잠금 이벤트 브로드캐스트
            loop = getattr(self, '_loop', None) or asyncio.get_event_loop()
            asyncio.run_coroutine_threadsafe(
                self._broadcast_event(
                    "lockout_start",
                    lockout_sec=self._lockout_sec,
                    fail_count=self._fail_count,
                ),
                loop,
            )

    # ──────────────────────────────────────────
    # PIR 트리거 환영 동작
    # ──────────────────────────────────────────
    @property
    def welcome_pending(self) -> bool:
        """환영 대기 중 여부 (쿨다운 중 + 환영 미완료)"""
        return (
            not self._welcome_done
            and time.time() < self._auth_cooldown_until
        )

    async def trigger_welcome(self):
        """
        현관 PIR 감지 시 호출 — 조명 ON + TTS 환영 인사
        쿨다운 내 1회만 실행 (중복 방지)
        """
        if self._welcome_done:
            return False

        self._welcome_done = True
        user = self._last_authenticated_user or "사용자"

        logger.info(f"[SmartGate] 🏠 PIR 감지 → 환영 동작 시작 | 사용자: {user}")
        self._db_log(
            f"PIR 환영 트리거: {user}",
            detail={"action": "welcome_triggered", "user": user},
        )

        # 1) 현관 조명 ON
        await self._entrance_light_on()

        # 2) TTS 환영 인사
        if self._tts_fn:
            try:
                await self._tts_fn(f"{user}님 어서오세요. 환영합니다.")
            except Exception as e:
                logger.debug(f"[SmartGate] TTS 인사 오류: {e}")

        # 3) WS 브로드캐스트 (웹 대시보드 알림)
        await self._broadcast_event(
            "welcome_triggered",
            user=user,
        )

        return True

    # ──────────────────────────────────────────
    # 인증 성공 부가 동작
    # ──────────────────────────────────────────
    async def _entrance_light_on(self):
        """인증 성공 시 현관 조명 ON (TCP → ESP32)"""
        if self._tcp_server is None:
            logger.info("[SmartGate] 🔧 시뮬레이션 모드 | 현관 조명 ON (skip)")
            return
        try:
            from protocol.schema import cmd_light
            device_id = self._cfg.get("gate_device_id", "esp32_entrance")
            command = cmd_light(pin=0, state=True, room="entrance")
            result = await self._tcp_server.send_command(device_id, command)
            if result:
                logger.info("[SmartGate] 💡 현관 조명 ON")
            else:
                logger.warning("[SmartGate] 현관 조명 ON 실패 (디바이스 미연결?)")
        except Exception as e:
            logger.error(f"[SmartGate] 현관 조명 제어 오류: {e}")

    # ──────────────────────────────────────────
    # WS 브로드캐스트 헬퍼
    # ──────────────────────────────────────────
    async def _broadcast_event(self, event: str, **kwargs):
        """SmartGate 이벤트를 웹 대시보드에 WS 브로드캐스트"""
        if self._ws_broadcast is None:
            return
        try:
            data = {
                "type": "smartgate",
                "event": event,
                "timestamp": time.time(),
            }
            data.update(kwargs)
            await self._ws_broadcast(data)
        except Exception as e:
            logger.debug(f"[SmartGate] WS 브로드캐스트 오류: {e}")

    # ──────────────────────────────────────────
    # 상태 조회 (API 응답용)
    # ──────────────────────────────────────────
    @property
    def status(self) -> dict:
        """GET /smartgate/status 응답 데이터"""
        _gc = self._cfg.get("gesture_auth", {})
        _lv = self._cfg.get("liveness", {})

        result = {
            "enabled": True,
            "state": self._state.name,
            "authenticated_user": self._authenticated_user or None,
            "fail_count": self._fail_count,
            "max_failures": self._max_failures,
            "mode": _gc.get("mode", "number"),
            "sequence": _gc.get("sequence", []),
            "liveness_profile": _lv.get("active_profile", "laptop"),
            "liveness_enabled": self.liveness.enabled,
            "face_db_ready": self.face_auth.is_ready,
            "gesture_ready": self.gesture_auth.is_ready,
            "liveness_ready": self.liveness.is_ready,
            "gate_open": self.gate_ctrl.is_gate_open,
            "in_cooldown": time.time() < self._auth_cooldown_until,
            "welcome_pending": self.welcome_pending,
        }

        if self._state == AuthState.LOCKED:
            remaining = max(0, self._lockout_until - time.time())
            result["lockout_remaining_sec"] = round(remaining, 1)

        return result

    # ──────────────────────────────────────────
    # 초기화 배너
    # ──────────────────────────────────────────
    def _print_init_banner(self):
        _gc = self._cfg.get("gesture_auth", {})
        _lv = self._cfg.get("liveness", {})
        _mode = _gc.get("mode", "number")
        _seq = _gc.get("sequence", [])
        _profile = _lv.get("active_profile", "laptop")
        _pool = _lv.get("profiles", {}).get(_profile, {}).get(
            "challenges_pool", []
        )

        _users = list(dict.fromkeys(self.face_auth.known_names))

        logger.info(
            f"[SmartGate] 초기화 완료 | "
            f"모드={_mode} | 시퀀스={_seq} | "
            f"Liveness={_profile}"
        )
        logger.info(f"[SmartGate] 등록 사용자: {_users}")
        if _pool:
            logger.info(f"[SmartGate] 챌린지 풀: {_pool}")
