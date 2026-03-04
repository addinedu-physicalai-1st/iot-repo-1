"""
server/smartgate/gate_controller.py
====================================
TCP 기반 게이트 컨트롤러 (iot-repo-1 통합 버전)

독립 프로젝트의 시리얼 방식(gate_controller_serial.py)과 달리
iot-repo-1의 TCPServer.send_command()를 통해 ESP32에 서보 명령 전송

동작:
  open_gate()  → tcp_server.send_command(device_id, cmd_servo(angle=90))
  close_gate() → tcp_server.send_command(device_id, cmd_servo(angle=0))
  자동 닫힘   → open_duration_sec 후 close_gate() 호출
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GateController:
    """
    TCP 기반 게이트 컨트롤러

    Parameters
    ----------
    tcp_server      : TCPServer 인스턴스 (None이면 시뮬레이션 모드)
    device_id       : ESP32 디바이스 ID (settings.yaml → smartgate.gate_device_id)
    open_duration_sec : 게이트 열림 유지 시간 (초)
    servo_open_angle  : 열림 각도 (기본 90°)
    servo_close_angle : 닫힘 각도 (기본 0°)
    """

    def __init__(
        self,
        tcp_server=None,
        device_id: str = "esp32_entrance",
        open_duration_sec: int = 5,
        servo_open_angle: int = 90,
        servo_close_angle: int = 0,
        servo_pin: int = 2,
        servo_room: str = "entrance",
    ):
        self._tcp_server = tcp_server
        self._device_id = device_id
        self._open_duration = open_duration_sec
        self._servo_open = servo_open_angle
        self._servo_close = servo_close_angle
        self._servo_pin = servo_pin
        self._servo_room = servo_room

        self._is_open: bool = False
        self._close_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ──────────────────────────────────────────
    # 게이트 제어
    # ──────────────────────────────────────────
    def open_gate(self):
        """게이트 열기 (sync — manager._auth_loop에서 호출)"""
        if self._is_open:
            return True
        self._is_open = True  # 즉시 설정 (GESTURE_OK 상태 유지)
        loop = self._loop or asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(self._async_open(), loop)
        return True

    async def _async_open(self):
        """게이트 열기 (async 실제 처리)"""
        success = await self._send_servo(self._servo_open)
        if success:
            logger.info(
                f"[GateCtrl] 🚪 게이트 OPEN | {self._device_id} "
                f"| {self._open_duration}초 후 자동 닫힘"
            )
        else:
            logger.warning(
                f"[GateCtrl] ⚠️ 서보 전송 실패 — "
                f"{self._open_duration}초 후 상태 초기화 예약"
            )

        # 기존 타이머 취소
        if self._close_task and not self._close_task.done():
            self._close_task.cancel()

        # 자동 닫힘 스케줄 (TCP 실패 시에도 상태 초기화 보장)
        self._close_task = asyncio.create_task(
            self._auto_close()
        )

        return success

    async def close_gate(self) -> bool:
        """게이트 닫기"""
        success = await self._send_servo(self._servo_close)
        self._is_open = False
        logger.info(f"[GateCtrl] 🚪 게이트 CLOSE | {self._device_id}")
        return success

    async def _auto_close(self):
        """자동 닫힘 타이머"""
        try:
            await asyncio.sleep(self._open_duration)
            await self.close_gate()
        except asyncio.CancelledError:
            pass

    def close(self):
        """동기 정리 (manager.stop()에서 호출 가능)"""
        if self._close_task and not self._close_task.done():
            self._close_task.cancel()

    # ──────────────────────────────────────────
    # TCP 명령 전송
    # ──────────────────────────────────────────
    async def _send_servo(self, angle: int) -> bool:
        """TCPServer를 통해 서보 명령 전송"""
        if self._tcp_server is None:
            logger.info(
                f"[GateCtrl] 🔧 시뮬레이션 모드 | {self._device_id} "
                f"servo → {angle}° (room={self._servo_room})"
            )
            return True

        try:
            from protocol.schema import cmd_servo
            # cmd_servo(pin: int, angle: int, room: str = "")
            # ESP32: resolveServo(room) → servoEntrance.write(angle)
            command = cmd_servo(
                pin=self._servo_pin,
                angle=angle,
                room=self._servo_room,
            )
            result = await self._tcp_server.send_command(
                self._device_id, command
            )
            if result:
                logger.debug(
                    f"[GateCtrl] TCP 전송 성공: {self._device_id} "
                    f"servo={angle}°"
                )
                return True
            else:
                logger.warning(
                    f"[GateCtrl] TCP 전송 실패: {self._device_id} "
                    f"(디바이스 미연결?)"
                )
                return False
        except Exception as e:
            logger.error(f"[GateCtrl] TCP 전송 오류: {e}")
            return False

    # ──────────────────────────────────────────
    # 상태
    # ──────────────────────────────────────────
    @property
    def is_gate_open(self) -> bool:
        return self._is_open

    async def cleanup(self):
        """리소스 정리"""
        if self._close_task and not self._close_task.done():
            self._close_task.cancel()
        if self._is_open:
            await self.close_gate()
