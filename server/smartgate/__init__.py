"""
server/smartgate/__init__.py
============================
SmartGate 2-Factor Authentication 서브패키지
Voice IoT Controller (iot-repo-1) 통합 버전

인증 흐름:
  ESP32-CAM (UDP) → camera_stream.push_frame()
      → SmartGateManager._auth_loop()
          ├─ [IDLE]       FaceAuthenticator.authenticate()
          ├─ [LIVENESS]   LivenessChecker.process()
          ├─ [FACE_OK]    GestureAuthenticator.process_frame()
          ├─ [GESTURE_OK] GateController.open_gate()
          └─ [IDLE]       자동 복귀

사용:
  from server.smartgate import SmartGateManager
"""

from server.smartgate.manager import SmartGateManager

__all__ = ["SmartGateManager"]
