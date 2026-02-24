"""
tests/test_e2e.py
=================
Voice IoT Controller - E2E 통합 테스트

테스트 범위:
  TC-01  서버 헬스 체크 (REST GET /status)
  TC-02  디바이스 목록 조회 (REST GET /devices)
  TC-03  WebSocket 연결 및 메시지 수신
  TC-04  수동 명령 전송 (REST POST /command)
  TC-05  음성 텍스트 파이프라인 (REST POST /voice)
  TC-06  STT 버튼 트리거 (REST POST /stt/activate)
  TC-07  WS manual_cmd 명령 전송
  TC-08  WS voice_text 명령 전송
  TC-09  WS manual_trigger STT 활성화
  TC-10  device_id="all" 전체 브로드캐스트
  TC-11  /status stt_state 필드 확인
  TC-12  오류 응답 검증 (잘못된 cmd, 빈 텍스트 등)
  Mock   서버 없이 실행 가능한 단위 검증

실행:
  # 서버 먼저 실행
  uvicorn server.main:app --host 0.0.0.0 --port 8000

  # 전체 테스트
  cd ~/dev_ws/voice_iot_controller
  python -m pytest tests/test_e2e.py -v

  # 서버 없이 mock 테스트만
  python -m pytest tests/test_e2e.py -v -m "no_server"

  # 특정 케이스만
  python -m pytest tests/test_e2e.py -v -k "TC01 or TC03"
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

import pytest
import pytest_asyncio
import httpx
import websockets

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────

SERVER_HOST = os.getenv("TEST_HOST", "localhost")
SERVER_PORT = int(os.getenv("TEST_PORT", "8000"))
BASE_URL    = f"http://{SERVER_HOST}:{SERVER_PORT}"
WS_URL      = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
TIMEOUT     = 5.0
WS_TIMEOUT  = 3.0


# ─────────────────────────────────────────────
# 픽스처
# ─────────────────────────────────────────────

@pytest_asyncio.fixture
async def http_client():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as client:
        yield client


@pytest_asyncio.fixture
async def server_ready(http_client):
    """서버 준비 확인 (최대 10초 대기)"""
    for _ in range(20):
        try:
            r = await http_client.get("/status")
            if r.status_code == 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    pytest.skip("서버 미실행 → 서버를 먼저 시작하세요: uvicorn server.main:app --port 8000")


# ─────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────

async def ws_collect(n_msgs: int = 1, timeout: float = WS_TIMEOUT) -> list[dict]:
    """WS 연결 후 n개 메시지 수집"""
    msgs = []
    try:
        async with websockets.connect(WS_URL) as ws:
            deadline = time.time() + timeout
            while len(msgs) < n_msgs and time.time() < deadline:
                remaining = deadline - time.time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
                    msgs.append(json.loads(raw))
                except asyncio.TimeoutError:
                    break
    except Exception:
        pass
    return msgs


async def ws_send_recv(
    payload: dict,
    expected_type: Optional[str] = None,
    timeout: float = WS_TIMEOUT,
) -> Optional[dict]:
    """WS 메시지 전송 후 응답 수신"""
    try:
        async with websockets.connect(WS_URL) as ws:
            await ws.send(json.dumps(payload))
            deadline = time.time() + timeout
            while time.time() < deadline:
                remaining = deadline - time.time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
                    data = json.loads(raw)
                    if expected_type is None or data.get("type") == expected_type:
                        return data
                except asyncio.TimeoutError:
                    break
    except Exception:
        pass
    return None


# ════════════════════════════════════════════════════════════════════
# TC-01 ~ TC-06 : REST API 테스트
# ════════════════════════════════════════════════════════════════════

class TestREST:

    @pytest.mark.asyncio
    async def test_TC01_server_status(self, http_client, server_ready):
        """GET /status → 200 + server=running"""
        r = await http_client.get("/status")
        assert r.status_code == 200
        body = r.json()
        assert body.get("server") == "running", f"응답: {body}"
        assert "tcp_clients" in body
        assert "ws_clients"  in body
        print(f"\n  서버 상태: {body}")

    @pytest.mark.asyncio
    async def test_TC02_device_list(self, http_client, server_ready):
        """GET /devices → 200 + devices 리스트"""
        r = await http_client.get("/devices")
        assert r.status_code == 200
        body = r.json()
        assert "devices" in body
        assert isinstance(body["devices"], list)
        print(f"\n  연결 디바이스: {[d['device_id'] for d in body['devices']]}")

    @pytest.mark.asyncio
    async def test_TC04_manual_command(self, http_client, server_ready):
        """POST /command → status ok or fail"""
        r = await http_client.post("/command", json={
            "cmd": "led", "pin": 2, "state": "on", "device_id": "esp32_bedroom"
        })
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") in ("ok", "fail"), f"응답: {body}"
        print(f"\n  명령 결과: {body}")

    @pytest.mark.asyncio
    async def test_TC05_voice_pipeline(self, http_client, server_ready):
        """POST /voice 침실 불 켜줘 → status 존재"""
        r = await http_client.post("/voice", json={"text": "침실 불 켜줘"}, timeout=15.0)
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") in ("ok", "fail", "unknown")
        print(f"\n  음성 파이프라인: {body}")

    @pytest.mark.asyncio
    async def test_TC05b_voice_all_devices(self, http_client, server_ready):
        """POST /voice 전체 불 꺼줘 → device_id=all 브로드캐스트"""
        r = await http_client.post("/voice", json={"text": "전체 불 꺼줘"}, timeout=15.0)
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") in ("ok", "fail", "unknown")
        print(f"\n  전체 명령: {body}")

    @pytest.mark.asyncio
    async def test_TC06_stt_activate(self, http_client, server_ready):
        """POST /stt/activate → status ok or warn"""
        r = await http_client.post("/stt/activate")
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") in ("ok", "warn"), f"응답: {body}"
        print(f"\n  STT 활성화: {body}")

    @pytest.mark.asyncio
    async def test_TC10_all_broadcast_rest(self, http_client, server_ready):
        """POST /command device_id=all → 전체 브로드캐스트"""
        r = await http_client.post("/command", json={
            "cmd": "led", "pin": 2, "state": "off", "device_id": "all"
        })
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") in ("ok", "fail"), f"응답: {body}"
        print(f"\n  전체 브로드캐스트: {body}")

    @pytest.mark.asyncio
    async def test_TC11_stt_state_in_status(self, http_client, server_ready):
        """GET /status → stt_state 필드 존재"""
        r = await http_client.get("/status")
        body = r.json()
        assert "stt_state" in body, f"stt_state 없음: {body}"
        print(f"\n  STT 상태: {body.get('stt_state')}")


# ════════════════════════════════════════════════════════════════════
# TC-03, TC-07 ~ TC-09 : WebSocket 테스트
# ════════════════════════════════════════════════════════════════════

class TestWebSocket:

    @pytest.mark.asyncio
    async def test_TC03_ws_connect(self, server_ready):
        """WebSocket 연결 성공"""
        try:
            async with websockets.connect(WS_URL) as ws:
                # websockets 버전별 연결 상태 확인
                state = getattr(ws, 'state', None)
                is_open = (
                    (state is not None and str(state.name).upper() == "OPEN")
                    or getattr(ws, 'open', False)
                )
                assert is_open
                print("\n  WS 연결 성공")
        except Exception as e:
            pytest.fail(f"WS 연결 실패: {e}")

    @pytest.mark.asyncio
    async def test_TC07_ws_manual_cmd(self, server_ready):
        """WS manual_cmd → cmd_result 수신"""
        result = await ws_send_recv(
            {"type": "manual_cmd", "cmd": "led", "pin": 2,
             "state": "on", "device_id": "esp32_bedroom"},
            expected_type="cmd_result",
        )
        assert result is not None, "cmd_result 응답 없음"
        assert result.get("status") in ("ok", "fail")
        print(f"\n  WS manual_cmd: {result}")

    @pytest.mark.asyncio
    async def test_TC08_ws_voice_text(self, server_ready):
        """WS voice_text → cmd_result 수신"""
        result = await ws_send_recv(
            {"type": "voice_text", "text": "침실 불 켜줘"},
            expected_type="cmd_result",
            timeout=15.0,
        )
        assert result is not None, "cmd_result 응답 없음"
        assert result.get("status") in ("ok", "fail", "unknown")
        print(f"\n  WS voice_text: {result}")

    @pytest.mark.asyncio
    async def test_TC09_ws_manual_trigger(self, server_ready):
        """WS manual_trigger → STT 활성화 응답"""
        result = await ws_send_recv(
            {"type": "manual_trigger"},
            expected_type="cmd_result",
        )
        assert result is not None, "cmd_result 응답 없음"
        assert result.get("status") in ("ok", "warn")
        print(f"\n  WS manual_trigger: {result}")


# ════════════════════════════════════════════════════════════════════
# TC-12 : 오류 처리 테스트
# ════════════════════════════════════════════════════════════════════

class TestErrorHandling:

    @pytest.mark.asyncio
    async def test_TC12a_invalid_cmd(self, http_client, server_ready):
        """cmd 없는 명령 → 422 or status:fail"""
        r = await http_client.post("/command", json={"device_id": "esp32_bedroom", "pin": 2})
        assert r.status_code in (200, 400, 422)
        if r.status_code == 200:
            assert r.json().get("status") == "fail"
        print(f"\n  잘못된 cmd: {r.status_code}")

    @pytest.mark.asyncio
    async def test_TC12b_empty_voice_text(self, http_client, server_ready):
        """POST /voice 빈 텍스트 → 422(FastAPI validation) or status:fail"""
        r = await http_client.post("/voice", json={"text": ""})
        # FastAPI Pydantic 검증 실패(422) 또는 라우터 fail 응답(200) 모두 허용
        assert r.status_code in (200, 422), f"예상치 못한 status: {r.status_code}"
        if r.status_code == 200:
            assert r.json().get("status") in ("fail", "unknown")
        print(f"\n  빈 voice_text: {r.status_code}")

    @pytest.mark.asyncio
    async def test_TC12c_unknown_ws_type(self, server_ready):
        """WS unknown_type → status:fail"""
        result = await ws_send_recv(
            {"type": "unknown_type"},
            expected_type="cmd_result",
        )
        assert result is not None
        assert result.get("status") == "fail"
        print(f"\n  알 수 없는 WS 타입: {result}")


# ════════════════════════════════════════════════════════════════════
# Mock 테스트 (서버 없이 실행 가능)
# ════════════════════════════════════════════════════════════════════

class TestMock:
    """서버 미실행 상태에서도 실행 가능한 단위 검증"""

    @pytest.mark.no_server
    def test_mock_schema_led(self):
        """schema.validate_command - LED 명령 정상"""
        import sys; sys.path.insert(0, ".")
        from protocol.schema import validate_command
        ok, err = validate_command(
            {"cmd": "led", "pin": 2, "state": "on", "device_id": "esp32_bedroom"}
        )
        assert ok, f"검증 실패: {err}"

    @pytest.mark.no_server
    def test_mock_schema_servo(self):
        """schema.validate_command - Servo 명령 정상"""
        import sys; sys.path.insert(0, ".")
        from protocol.schema import validate_command
        ok, err = validate_command(
            {"cmd": "servo", "pin": 18, "angle": 90, "device_id": "esp32_garage"}
        )
        assert ok, f"검증 실패: {err}"

    @pytest.mark.no_server
    def test_mock_schema_invalid_state(self):
        """schema.validate_command - 잘못된 LED state 거부"""
        import sys; sys.path.insert(0, ".")
        from protocol.schema import validate_command
        ok, _ = validate_command(
            {"cmd": "led", "pin": 2, "state": "blink", "device_id": "esp32_bedroom"}
        )
        assert not ok, "잘못된 state가 통과됨"

    @pytest.mark.no_server
    def test_mock_simple_parse_all(self):
        """_simple_parse - 전체 불 꺼줘 → device_id=all"""
        import sys, unittest.mock as mock
        sys.path.insert(0, ".")
        for m in ['fastapi', 'fastapi.responses', 'uvicorn', 'pydantic']:
            sys.modules[m] = mock.MagicMock()
        sys.modules['pydantic'].BaseModel = type('B', (), {
            '__init__': lambda s, **kw: [setattr(s, k, v) for k, v in kw.items()],
            'model_dump': lambda s, **_: s.__dict__,
        })
        import yaml
        cfg = yaml.safe_load(open("config/settings.yaml"))
        from server.command_router import CommandRouter
        router = CommandRouter(tcp_server=mock.MagicMock(), settings=cfg)
        result = router._simple_parse("전체 불 꺼줘")
        assert result is not None
        assert result["device_id"] == "all"
        assert result["state"]     == "off"

    @pytest.mark.no_server
    def test_mock_simple_parse_room(self):
        """_simple_parse - 침실 불 켜줘 → esp32_bedroom"""
        import sys, unittest.mock as mock
        sys.path.insert(0, ".")
        for m in ['fastapi', 'fastapi.responses', 'uvicorn', 'pydantic']:
            sys.modules[m] = mock.MagicMock()
        sys.modules['pydantic'].BaseModel = type('B', (), {
            '__init__': lambda s, **kw: [setattr(s, k, v) for k, v in kw.items()],
            'model_dump': lambda s, **_: s.__dict__,
        })
        import yaml
        cfg = yaml.safe_load(open("config/settings.yaml"))
        from server.command_router import CommandRouter
        router = CommandRouter(tcp_server=mock.MagicMock(), settings=cfg)
        result = router._simple_parse("침실 불 켜줘")
        assert result is not None
        assert result["device_id"] == "esp32_bedroom"
        assert result["state"]     == "on"


# ────────────────────────────────────────────
# 직접 실행
# ────────────────────────────────────────────

if __name__ == "__main__":
    import subprocess, sys
    ret = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    sys.exit(ret.returncode)
