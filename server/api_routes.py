"""
server/api_routes.py
====================
FastAPI REST 엔드포인트 + WebSocket 라우터

엔드포인트:
  GET  /            - Web App index.html 서빙
  GET  /devices     - 연결된 ESP32 목록
  POST /command     - 수동 명령 직접 전송
  POST /voice       - STT 텍스트 → LLM → 명령 실행
  GET  /ws          - WebSocket 연결 (브라우저 실시간)

의존성 주입:
  TCPServer, WebSocketHub, CommandRouter 인스턴스를
  main.py 에서 생성 후 setup_routes() 로 주입
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, WebSocket, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from protocol.schema import validate_command, ws_cmd_result

logger = logging.getLogger(__name__)

# Web App HTML 경로 (프로젝트 루트 기준)
WEB_DIR = Path(__file__).parent.parent / "web"


# ─────────────────────────────────────────────
# Request / Response 모델
# ─────────────────────────────────────────────

class CommandRequest(BaseModel):
    """POST /command 요청 바디"""
    device_id: str
    cmd: str                          # led / servo / query / seg7
    pin: Optional[int] = None
    state: Optional[str] = None       # led: on / off
    angle: Optional[int] = None       # servo: 0~180
    sensor: Optional[str] = None      # query: dht22
    pin_clk: Optional[int] = None     # seg7
    pin_dio: Optional[int] = None     # seg7
    mode: Optional[str] = None        # seg7: temp / humidity / number / off
    value: Optional[float] = None     # seg7


class VoiceRequest(BaseModel):
    """POST /voice 요청 바디"""
    text: str                         # STT 변환된 텍스트


class CommandResponse(BaseModel):
    """공통 응답"""
    status: str                       # ok / fail / unknown
    msg: str


# ─────────────────────────────────────────────
# 라우터 팩토리
# ─────────────────────────────────────────────

def create_router(tcp_server, ws_hub, command_router) -> APIRouter:
    """
    APIRouter 생성 및 엔드포인트 등록

    Parameters
    ----------
    tcp_server      : TCPServer 인스턴스
    ws_hub          : WebSocketHub 인스턴스
    command_router  : CommandRouter 인스턴스
    """
    router = APIRouter()

    # # ── GET / ───────────────────────────────────────────────────────
    # @router.get("/", response_class=HTMLResponse)
    # async def serve_index():
    #     """Web App index.html 서빙"""
    #     index_path = WEB_DIR / "index.html"
    #     if not index_path.exists():
    #         return HTMLResponse(
    #             content="<h2>Web App 준비 중입니다. (web/index.html 없음)</h2>",
    #             status_code=200,
    #         )
    #     return FileResponse(str(index_path))

     # ── GET / ───────────────────────────────────────────────────────
    @router.get("/", response_class=HTMLResponse)
    async def serve_index():
        """첫 페이지: iot/index.html 서빙"""
        index_path = WEB_DIR / "index_main.html"
        if not index_path.exists():
            return HTMLResponse(
                content="<h2>첫 페이지 준비 중입니다. (iot/index.html 없음)</h2>",
                status_code=200,
            )
        return FileResponse(str(index_path))

    # ── GET /dashboard ──────────────────────────────────────────────
    @router.get("/dashboard", response_class=HTMLResponse)
    async def serve_dashboard():
        """두 번째 페이지: web/index.html 서빙"""
        index_path = WEB_DIR / "index_dashboard.html"
        if not index_path.exists():
            return HTMLResponse(
                content="<h2>대시보드 준비 중입니다. (web/index.html 없음)</h2>",
                status_code=200,
            )
        return FileResponse(str(index_path))

    # ── GET /devices ─────────────────────────────────────────────────
    @router.get("/devices")
    async def get_devices():
        """연결된 ESP32 디바이스 목록 반환"""
        devices = tcp_server.get_device_list()
        return {
            "count": len(devices),
            "devices": devices,
            "ws_clients": ws_hub.connected_count,
        }

    # ── POST /command ────────────────────────────────────────────────
    @router.post("/command", response_model=CommandResponse)
    async def post_command(req: CommandRequest):
        """
        수동 명령 직접 전송
        예: {"device_id": "esp32_bedroom", "cmd": "led", "pin": 2, "state": "on"}
        """
        data = req.model_dump(exclude_none=True)
        logger.info(f"[API] POST /command: {data}")

        # 유효성 검사
        ok, err = validate_command(data)
        if not ok:
            raise HTTPException(status_code=422, detail=err)

        result_json = await command_router.execute(data)

        import json
        result = json.loads(result_json)
        return CommandResponse(status=result["status"], msg=result["msg"])

    # ── POST /voice ──────────────────────────────────────────────────
    @router.post("/voice", response_model=CommandResponse)
    async def post_voice(req: VoiceRequest):
        """
        STT 텍스트 → LLM 파싱 → ESP32 명령 실행
        예: {"text": "침실 불 켜줘"}
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="빈 텍스트")

        logger.info(f"[API] POST /voice: '{text}'")

        result_json = await command_router.handle(
            client_id="rest_api",
            data={"type": "voice_text", "text": text},
        )

        import json
        result = json.loads(result_json)
        return CommandResponse(status=result["status"], msg=result["msg"])

    # ── GET /ws ──────────────────────────────────────────────────────
    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """브라우저 WebSocket 연결 진입점"""
        await ws_hub.connect(websocket)

    # ── GET /status ──────────────────────────────────────────────────
    @router.get("/status")
    async def get_status():
        """서버 상태 요약"""
        return {
            "server": "running",
            "tcp_clients": tcp_server.connected_count,
            "ws_clients":  ws_hub.connected_count,
            "devices":     [d["device_id"] for d in tcp_server.get_device_list()],
            "stt_state":   _get_stt_state(),
        }

    # ── POST /stt/activate ───────────────────────────────────────────
    @router.post("/stt/activate", response_model=CommandResponse)
    async def stt_activate():
        """
        버튼 모드 트리거 - STTEngine 을 LISTENING 상태로 즉시 전환.
        Web App 마이크 버튼 / PyQt6 버튼 / 외부 REST 호출 모두 지원.

        웨이크 워드 모드와 공존:
          버튼 클릭 → 즉시 LISTENING
          "헤이 IoT" 발화 → 자동 LISTENING
        """
        import sys
        stt = None
        for mod in sys.modules.values():
            app = getattr(mod, 'app', None)
            if app and hasattr(getattr(app, 'state', None), 'stt_engine'):
                stt = app.state.stt_engine
                break

        if stt is None:
            return CommandResponse(
                status="warn",
                msg="STTEngine 비활성화 상태 (DISABLE_STT=1)"
            )
        if stt.state != "IDLE":
            return CommandResponse(
                status="ok",
                msg=f"STT 이미 활성화 중: {stt.state}"
            )

        stt.activate()
        await ws_hub.broadcast(
            '{"type":"wake_detected","msg":"버튼 트리거 → 명령을 말씀하세요"}'
        )
        return CommandResponse(status="ok", msg="STT LISTENING 활성화")

    def _get_stt_state():
        import sys
        for mod in sys.modules.values():
            app = getattr(mod, 'app', None)
            if app and hasattr(getattr(app, 'state', None), 'stt_engine'):
                stt = app.state.stt_engine
                return getattr(stt, 'state', 'N/A') if stt else 'DISABLED'
        return 'N/A'

    return router
