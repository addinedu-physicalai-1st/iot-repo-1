"""
SmartGate Gate Controller Module
ESP32 시리얼 통신으로 게이트 GPIO 제어
테스트 모드 지원 (ESP32 없이 시뮬레이션)
"""

import time
import threading
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    print("[WARNING] pyserial 미설치. 설치: pip install pyserial")


class GateController:
    """
    ESP32 시리얼 통신 게이트 컨트롤러
    
    ESP32 수신 명령:
        "GATE_OPEN\n"  → GPIO HIGH (릴레이 ON)
        "GATE_CLOSE\n" → GPIO LOW (릴레이 OFF)
    """

    def __init__(
        self,
        enabled: bool = False,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        open_signal: str = "GATE_OPEN\n",
        close_signal: str = "GATE_CLOSE\n",
        open_duration_sec: int = 5,
    ):
        self.enabled = enabled
        self.port = port
        self.baudrate = baudrate
        self.open_signal = open_signal
        self.close_signal = close_signal
        self.open_duration_sec = open_duration_sec

        self._serial: Optional[serial.Serial] = None
        self._is_open: bool = False
        self._close_timer: Optional[threading.Timer] = None

        if self.enabled and SERIAL_AVAILABLE:
            self._connect()

    # ──────────────────────────────────────────
    # 연결
    # ──────────────────────────────────────────
    def _connect(self):
        """ESP32 시리얼 연결"""
        try:
            self._serial = serial.Serial(self.port, self.baudrate, timeout=1)
            time.sleep(2)  # ESP32 부팅 대기
            print(f"[GateCtrl] ✅ ESP32 연결 성공: {self.port} @ {self.baudrate}bps")
        except Exception as e:
            print(f"[GateCtrl] ❌ ESP32 연결 실패: {e}")
            print(f"[GateCtrl] 사용 가능 포트: {self.list_ports()}")
            self._serial = None

    def list_ports(self) -> list:
        """사용 가능한 시리얼 포트 목록"""
        if not SERIAL_AVAILABLE:
            return []
        return [str(p) for p in serial.tools.list_ports.comports()]

    # ──────────────────────────────────────────
    # 게이트 제어
    # ──────────────────────────────────────────
    def open_gate(self) -> bool:
        """게이트 열기 (open_duration_sec 후 자동 닫힘)"""
        if self._is_open:
            return True  # 이미 열려있음

        success = self._send(self.open_signal)
        if success or not self.enabled:
            self._is_open = True
            print(f"[GateCtrl] 🚪 게이트 OPEN | {self.open_duration_sec}초 후 자동 닫힘")

            # 자동 닫힘 타이머
            if self._close_timer:
                self._close_timer.cancel()
            self._close_timer = threading.Timer(self.open_duration_sec, self.close_gate)
            self._close_timer.daemon = True
            self._close_timer.start()

        return success or not self.enabled

    def close_gate(self) -> bool:
        """게이트 닫기"""
        success = self._send(self.close_signal)
        self._is_open = False
        print("[GateCtrl] 🚪 게이트 CLOSE")
        return success or not self.enabled

    def _send(self, signal: str) -> bool:
        """시리얼 신호 전송"""
        if not self.enabled:
            print(f"[GateCtrl] 🔧 테스트 모드 | 신호: {signal.strip()}")
            return True

        if not self._serial or not self._serial.is_open:
            print("[GateCtrl] ❌ 시리얼 포트 미연결")
            return False

        try:
            self._serial.write(signal.encode("utf-8"))
            self._serial.flush()
            return True
        except Exception as e:
            print(f"[GateCtrl] ❌ 전송 실패: {e}")
            return False

    # ──────────────────────────────────────────
    # 상태
    # ──────────────────────────────────────────
    @property
    def is_gate_open(self) -> bool:
        return self._is_open

    def close(self):
        """리소스 정리"""
        if self._close_timer:
            self._close_timer.cancel()
        if self._serial and self._serial.is_open:
            self.close_gate()
            self._serial.close()
