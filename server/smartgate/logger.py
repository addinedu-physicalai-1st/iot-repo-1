"""
SmartGate Logger Module
인증 이벤트 로깅 (파일 + 콘솔)
"""

import logging
import os
from datetime import datetime
from pathlib import Path


def setup_logger(log_dir: str = "logs/", log_file: str = "smartgate.log", level: str = "INFO") -> logging.Logger:
    """SmartGate 전용 로거 설정"""
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = os.path.join(log_dir, log_file)

    logger = logging.getLogger("SmartGate")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        logger.handlers.clear()

    # 파일 핸들러
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    # 콘솔 핸들러
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


class AuthLogger:
    """인증 이벤트 전용 로거"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def face_detected(self, name: str, confidence: float):
        self.logger.info(f"[FACE] ✅ 얼굴 인식 성공 | 이름: {name} | 신뢰도: {confidence:.3f}")

    def face_failed(self, confidence: float):
        self.logger.warning(f"[FACE] ❌ 얼굴 인식 실패 | 신뢰도: {confidence:.3f}")

    def face_unknown(self):
        self.logger.warning("[FACE] ❌ 미등록 얼굴 감지")

    def gesture_detected(self, count: int, sequence_so_far: list):
        self.logger.info(f"[GESTURE] 👆 제스처 인식: {count} | 현재 시퀀스: {sequence_so_far}")

    def gesture_success(self, sequence: list):
        self.logger.info(f"[GESTURE] ✅ 수신호 시퀀스 인증 성공 | 시퀀스: {sequence}")

    def gesture_failed(self, reason: str = ""):
        self.logger.warning(f"[GESTURE] ❌ 수신호 시퀀스 실패 | {reason}")

    def auth_success(self, name: str):
        self.logger.info(f"[AUTH] 🔓 2팩터 인증 성공 | 사용자: {name} | 시각: {datetime.now().strftime('%H:%M:%S')}")

    def auth_failed(self, fail_count: int, max_failures: int):
        self.logger.warning(f"[AUTH] 🔒 인증 실패 ({fail_count}/{max_failures})")

    def lockout_start(self, lockout_sec: int):
        self.logger.error(f"[AUTH] 🚫 잠금 활성화 | {lockout_sec}초 대기")

    def lockout_end(self):
        self.logger.info("[AUTH] 🔓 잠금 해제 | 인증 재시도 가능")

    def gate_open(self):
        self.logger.info("[GATE] 🚪 게이트 열림 신호 전송")

    def gate_close(self):
        self.logger.info("[GATE] 🚪 게이트 닫힘 신호 전송")

    def esp32_error(self, error: str):
        self.logger.error(f"[ESP32] ⚠️ 통신 오류: {error}")
