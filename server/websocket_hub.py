"""
server/websocket_hub.py
=======================
WebSocket 연결 관리 및 브로드캐스트 허브

역할:
  - 브라우저 WebSocket 클라이언트 연결/해제 관리
  - 전체 클라이언트 브로드캐스트 (ESP32 상태 변경, 센서 데이터 등)
  - 브라우저 → 서버 메시지 수신 → 명령 라우팅 콜백 호출

메시지 흐름:
  [ESP32 이벤트]  → TCPServer → ws_hub.broadcast()  → 모든 브라우저
  [브라우저 명령] → ws_hub   → on_message 콜백      → command_router

사용:
  from server.websocket_hub import WebSocketHub
  hub = WebSocketHub(on_message=command_router.handle)
  
  # FastAPI 라우터에 등록
  @app.websocket("/ws")
  async def ws_endpoint(websocket: WebSocket):
      await hub.connect(websocket)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable, Optional

from fastapi import WebSocket, WebSocketDisconnect

from protocol.schema import (
    parse_ws_message,
    ws_cmd_result,
    WS_DEVICE_LIST,
    WS_SENSOR_DATA,
    WS_DEVICE_UPDATE,
    WS_CMD_RESULT,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# WebSocket 허브
# ─────────────────────────────────────────────

class WebSocketHub:
    """
    FastAPI WebSocket 연결 풀 관리자

    Parameters
    ----------
    on_message : 브라우저에서 메시지 수신 시 호출할 콜백
                 async def on_message(client_id: str, data: dict) -> str | None
                 반환값이 있으면 해당 클라이언트에게만 응답 전송
    on_connect : 클라이언트 접속 시 콜백 (선택)
                 async def on_connect(client_id: str, send_fn)
                 send_fn: 해당 클라이언트에만 전송하는 async 함수
    on_disconnect : 클라이언트 해제 시 콜백 (선택)
                    async def on_disconnect(client_id: str)
    """

    def __init__(
        self,
        on_message: Optional[Callable[[str, dict], Awaitable[Optional[str]]]] = None,
        on_connect: Optional[Callable[[str], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self._on_message    = on_message
        self._on_connect    = on_connect
        self._on_disconnect = on_disconnect

        # client_id → WebSocket
        self._clients: dict[str, WebSocket] = {}
        self._counter = 0                          # client_id 생성용 카운터

        # DB 로깅 (main.py 에서 주입)
        self.db_logger = None

    # ── 공개 API ────────────────────────────────────────────────────

    async def connect(self, websocket: WebSocket):
        """
        WebSocket 연결 수락 후 메시지 루프 진입.
        FastAPI @app.websocket 핸들러에서 호출.
        """
        await websocket.accept()

        client_id = self._next_id()
        self._clients[client_id] = websocket
        logger.info(f"[WS] 연결: {client_id} | 총 {len(self._clients)}개")

        # DB 로깅: WebSocket 연결
        if self.db_logger:
            self.db_logger.log(
                "ws_connect", "websocket_hub",
                f"{client_id} 연결 (총 {len(self._clients)}개)",
                detail={"client_id": client_id, "total": len(self._clients)},
            )

        # 접속 콜백 - send_fn 함께 전달 (접속 직후 초기 데이터 push용)
        if self._on_connect:
            async def _send_fn(msg: str):
                try:
                    await websocket.send_text(msg)
                except Exception:
                    pass
            await self._safe_call(self._on_connect, client_id, _send_fn)

        try:
            while True:
                raw = await websocket.receive_text()
                await self._handle_message(client_id, websocket, raw)

        except WebSocketDisconnect:
            logger.info(f"[WS] 정상 해제: {client_id}")
        except Exception as e:
            logger.error(f"[WS] 오류 ({client_id}): {e}")
        finally:
            await self._disconnect(client_id)

    async def broadcast(self, message: str):
        """연결된 모든 브라우저 클라이언트에게 메시지 전송"""
        if not self._clients:
            return

        dead: list[str] = []
        for client_id, ws in list(self._clients.items()):
            try:
                await ws.send_text(message)
            except Exception as e:
                logger.warning(f"[WS] broadcast 실패 ({client_id}): {e}")
                dead.append(client_id)

        # 끊긴 클라이언트 정리
        for cid in dead:
            await self._disconnect(cid)

    async def send_to(self, client_id: str, message: str) -> bool:
        """특정 클라이언트에게만 메시지 전송"""
        ws = self._clients.get(client_id)
        if not ws:
            return False
        try:
            await ws.send_text(message)
            return True
        except Exception as e:
            logger.warning(f"[WS] send_to 실패 ({client_id}): {e}")
            await self._disconnect(client_id)
            return False

    @property
    def connected_count(self) -> int:
        return len(self._clients)

    @property
    def client_ids(self) -> list[str]:
        return list(self._clients.keys())

    # ── 내부 핸들러 ─────────────────────────────────────────────────

    async def _handle_message(
        self,
        client_id: str,
        websocket: WebSocket,
        raw: str,
    ):
        """브라우저에서 수신한 메시지 처리"""
        data = parse_ws_message(raw)

        if not data:
            logger.warning(f"[WS] JSON 파싱 실패 ({client_id}): {raw}")
            await websocket.send_text(
                ws_cmd_result("fail", "invalid JSON format")
            )
            return

        msg_type = data.get("type", "unknown")
        logger.info(f"[WS] ← {client_id}: type={msg_type}")

        # on_message 콜백 호출
        if self._on_message:
            try:
                response = await self._on_message(client_id, data)
                # 콜백이 응답 문자열을 반환하면 해당 클라이언트에만 전송
                if response:
                    await websocket.send_text(response)
            except Exception as e:
                logger.error(f"[WS] on_message 오류 ({client_id}): {e}")
                await websocket.send_text(
                    ws_cmd_result("fail", f"server error: {str(e)}")
                )
        else:
            # 콜백 없을 때 echo 응답
            await websocket.send_text(
                ws_cmd_result("ok", f"received: {msg_type}")
            )

    async def _disconnect(self, client_id: str):
        """클라이언트 연결 해제 처리"""
        ws = self._clients.pop(client_id, None)
        if ws:
            try:
                await ws.close()
            except Exception:
                pass

        logger.info(f"[WS] 해제 완료: {client_id} | 남은 {len(self._clients)}개")

        # DB 로깅: WebSocket 해제
        if self.db_logger:
            self.db_logger.log(
                "ws_disconnect", "websocket_hub",
                f"{client_id} 해제 (남은 {len(self._clients)}개)",
                detail={"client_id": client_id, "remaining": len(self._clients)},
            )

        if self._on_disconnect:
            await self._safe_call(self._on_disconnect, client_id)

    def _next_id(self) -> str:
        self._counter += 1
        return f"ws_client_{self._counter:04d}"

    @staticmethod
    async def _safe_call(fn: Callable, *args):
        """콜백 예외를 안전하게 처리"""
        try:
            await fn(*args)
        except Exception as e:
            logger.warning(f"[WS] 콜백 오류: {e}")
