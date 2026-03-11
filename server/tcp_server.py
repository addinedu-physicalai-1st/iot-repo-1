"""
server/tcp_server.py
====================
asyncio 기반 TCP 서버 - 다중 ESP32 클라이언트 관리  v3.2

v3.2 변경사항:
  - send_command() HMAC-SHA256 서명 패킷 래핑 추가
    · ESP32_SECRET 환경변수 필수 (없으면 서명 스킵 + 경고)
    · 기존 bytes payload → {"ts":..., "sig":..., "cmd":{...}} JSON 래핑
    · NIST SP 800-213 §4.2 / OWASP IoT OT1/OT3 대응
    · ESP32 측 검증: docs/esp32_hmac_verify.cpp 참조
    · ESP32 → TCP로 전송되는 pir_event 타입 메시지 수신
    · guard_alert / presence_alert 이벤트 → WS 브로드캐스트
    · context(away/sleep/home) 별 메시지 생성
    · validate_esp32_message() pir_event 타입 허용 처리

역할:
  - ESP32 클라이언트 연결/해제 관리 (device_registry)
  - JSON 메시지 수신 → 파싱 → 핸들러 라우팅
  - 명령 전송 (서버 → 특정 ESP32)
  - 센서/ACK 데이터 수신 → WebSocket 브로드캐스트 콜백 호출
  - UnifiedStateManager: ESP32 + 음악 + 웹앱 상태 통합 관리

v3.0 변경사항 (StateManager → UnifiedStateManager):
  - music 상태 추가 (playing, title, genre, volume)
      · update_music_state(playing, title, genre, volume)
      · get_music_state() 조회
  - web_app 상태 추가 (connected_clients)
      · update_web_clients(count)
  - get_snapshot() 반환값에 music / web_app 섹션 포함
  - TCPServer.unified_state_manager 프로퍼티 노출 (하위 호환: state_manager alias)

v2.0 변경사항:
  - StateManager 클래스 추가
  - TCPServer.start_polling(): 주기적 센서 폴링 태스크 (기본 30초)

사용:
  from server.tcp_server import TCPServer
  srv = TCPServer(host="0.0.0.0", port=9000, ws_broadcast=ws_hub.broadcast)
  await srv.start()
  poll_task = asyncio.create_task(srv.start_polling(interval=30))

  # 음악 상태 업데이트 (command_router.py 에서 호출)
  srv.unified_state_manager.update_music_state(playing=True, title="곡명", volume=70)

  # 전체 스냅샷 조회
  snap = srv.unified_state_manager.get_snapshot()
  # snap["_music"]    → 음악 상태
  # snap["_web_app"]  → 웹앱 상태
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

from protocol.schema import (
    MSG_DELIMITER,
    TYPE_REGISTER, TYPE_ACK, TYPE_SENSOR, TYPE_ERROR,
    parse_tcp_message, validate_esp32_message,
    ws_cmd_result, ws_sensor_data, ws_device_update, ws_device_list,
    CMD_SEG7,
    cmd_seg7_temp, cmd_seg7_humidity,
    cmd_query,
    ROOM_LED_PIN, ROOM_SERVO_PIN, ALL_ROOMS,
)

# ── HMAC-SHA256 서명 (v3.2) ────────────────────────────────────────
import hashlib
import hmac as _hmac
import json as _json
import os as _os
import time as _time

_ESP32_SECRET: bytes = _os.environ.get("ESP32_SECRET", "").encode()
if not _ESP32_SECRET:
    logging.getLogger(__name__).warning(
        "[TCP] ESP32_SECRET 환경변수 없음 — HMAC 서명 비활성화 (평문 전송)"
    )

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 디바이스 메타 정보
# ─────────────────────────────────────────────

DEVICE_LABEL: dict[str, str] = {
    "esp32_home1": "스마트홈1",
    "esp32_home2": "스마트홈2",
}

# 폴링 대상 없음 (현재 구성에 자동 폴링 센서 없음)
SENSOR_CAP_MAP: dict[str, str] = {}


# ─────────────────────────────────────────────
# UnifiedStateManager — ESP32 + 음악 + 웹앱 통합 상태 관리
# ─────────────────────────────────────────────

class UnifiedStateManager:
    """
    전체 시스템 상태 통합 관리

    ESP32 디바이스 상태:
      - 연결 시    : register() — LED off, servo 0 기본값
      - 명령 실행  : update_command() — 낙관적 선반영
      - ACK 수신   : update_ack() — 확정 반영
      - 센서 수신  : update_sensor() — temp/humidity 반영
      - 해제 시    : remove()

    음악 재생 상태 (브라우저 ytPlayer → WS → 서버):
      - update_music_state(playing, title, genre, volume)

    웹앱 연결 상태:
      - update_web_clients(count)

    조회:
      - get_snapshot(device_id="all") → 전체 또는 특정 디바이스
      - get_music_state()             → 음악 상태 dict
    """

    def __init__(self):
        # device_id → state dict
        self._states: dict[str, dict] = {}
        self._updated_at: dict[str, float] = {}

        # 음악 재생 상태
        self._music: dict = {
            "playing": False,
            "title":   "",
            "genre":   "",
            "volume":  60,
            "updated_at": 0.0,
        }

        # 웹앱 연결 상태
        self._web_app: dict = {
            "connected_clients": 0,
            "updated_at": 0.0,
        }

    # ── 등록 / 해제 ──────────────────────────────────────────────

    def register(self, device_id: str, caps: list[str]):
        """연결 시 초기 state 등록"""
        import time
        existing = self._states.get(device_id, {})
        state = {}

        if device_id in ("esp32_home", "esp32_home1"):
            # esp32_home1: 침실 서보(GPIO2), 욕실 7seg/DHT11
            # LED 핀 없음 — 초기화하지 않아야 esp32_home2 LED 상태를 덮어쓰지 않음
            state["servo_2"] = existing.get("servo_2", 0)  # 침실 커튼 서보 GPIO2

        elif device_id == "esp32_home2":
            # esp32_home2: LED 5개 + 서보 2개(차고GPIO15, 현관GPIO16)
            for room in ALL_ROOMS:
                led_pin = ROOM_LED_PIN.get(room)
                if led_pin is not None:
                    state[f"led_{led_pin}"] = existing.get(f"led_{led_pin}", 0)
            for servo_pin in [15, 16]:  # 차고, 현관 서보
                state[f"servo_{servo_pin}"] = existing.get(f"servo_{servo_pin}", 0)

        else:
            # 하위 호환: 기존 개별 디바이스
            state = {
                "led_2":    existing.get("led_2",    0),
                "servo_18": existing.get("servo_18", 0),
            }

        self._states[device_id]     = state
        self._updated_at[device_id] = time.time()
        logger.info(f"[State] 등록: {device_id} | 초기={state}")

    def remove(self, device_id: str):
        self._states.pop(device_id, None)
        self._updated_at.pop(device_id, None)
        logger.info(f"[State] 제거: {device_id}")

    # ── 업데이트 ─────────────────────────────────────────────────

    def update_command(self, device_id: str, cmd: str, data: dict):
        """명령 전송 직후 선반영 (ACK 전 낙관적 업데이트)"""
        import time
        state = self._states.setdefault(device_id, {})
        room  = data.get("room", "")

        if cmd == "led":
            pin = ROOM_LED_PIN.get(room) if room else data.get("pin", 2)
            if pin is not None:
                state[f"led_{pin}"] = 1 if data.get("state") == "on" else 0
        elif cmd == "servo":
            pin = ROOM_SERVO_PIN.get(room) if room else data.get("pin", 18)
            if pin is not None and data.get("angle") is not None:
                state[f"servo_{pin}"] = data["angle"]
        self._updated_at[device_id] = time.time()

    def update_ack(self, device_id: str, cmd: str, data: dict):
        """ACK 수신 시 확정 반영"""
        import time
        state = self._states.setdefault(device_id, {})
        if cmd == "led":
            pin = data.get("pin", 2)
            s   = data.get("state", "")
            if s:
                state[f"led_{pin}"] = 1 if s == "on" else 0
        elif cmd == "servo":
            pin   = data.get("pin")
            angle = data.get("angle")
            if angle is not None and pin is not None:
                state[f"servo_{pin}"] = angle
        self._updated_at[device_id] = time.time()

    def update_sensor(self, device_id: str, temp=None, humidity=None):
        """센서 수신 시 반영"""
        import time
        state = self._states.setdefault(device_id, {})
        if temp     is not None: state["temp"]     = temp
        if humidity is not None: state["humidity"] = humidity
        self._updated_at[device_id] = time.time()

    # ── 조회 ────────────────────────────────────────────────────

    def get_snapshot(self, device_id: str = "all") -> dict:
        """
        상태 스냅샷 반환
        device_id="all" → ESP32 전체 + _music + _web_app 포함
        device_id 지정   → 해당 디바이스만 (음악/웹앱 제외)
        """
        if device_id == "all":
            snap = {
                did: {
                    "label":      DEVICE_LABEL.get(did, did),
                    "state":      dict(s),
                    "updated_at": self._updated_at.get(did, 0),
                }
                for did, s in self._states.items()
            }
            # 음악 / 웹앱 상태를 예약 키로 포함
            snap["_music"]   = dict(self._music)
            snap["_web_app"] = dict(self._web_app)
            return snap

        if device_id in self._states:
            return {
                device_id: {
                    "label":      DEVICE_LABEL.get(device_id, device_id),
                    "state":      dict(self._states[device_id]),
                    "updated_at": self._updated_at.get(device_id, 0),
                }
            }
        return {}

    # ── 음악 상태 ────────────────────────────────────────────────

    def update_music_state(
        self,
        playing: Optional[bool] = None,
        title:   Optional[str]  = None,
        genre:   Optional[str]  = None,
        volume:  Optional[int]  = None,
    ):
        """
        브라우저 ytPlayer 이벤트 → WS music_state 수신 시 호출
        None 인 필드는 유지 (부분 업데이트 허용)
        """
        import time
        if playing is not None: self._music["playing"] = playing
        if title   is not None: self._music["title"]   = title
        if genre   is not None: self._music["genre"]   = genre
        if volume  is not None: self._music["volume"]  = int(volume)
        self._music["updated_at"] = time.time()
        logger.debug(
            f"[State] 음악 상태 업데이트: "
            f"playing={self._music['playing']} title='{self._music['title']}' "
            f"volume={self._music['volume']}"
        )

    def get_music_state(self) -> dict:
        """음악 재생 상태 반환"""
        return dict(self._music)

    # ── 웹앱 연결 상태 ───────────────────────────────────────────

    def update_web_clients(self, count: int):
        """WebSocketHub 연결 클라이언트 수 반영"""
        import time
        self._web_app["connected_clients"] = count
        self._web_app["updated_at"] = time.time()
        logger.debug(f"[State] 웹앱 클라이언트 수: {count}명")

    def all_device_ids(self) -> list[str]:
        return list(self._states.keys())


# ─────────────────────────────────────────────
# 클라이언트 정보 dataclass
# ─────────────────────────────────────────────

@dataclass
class ESP32Client:
    device_id: str
    caps: list[str]
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    addr: tuple

    # 마지막으로 수신한 상태 캐시
    state: dict = field(default_factory=dict)

    @property
    def ip(self) -> str:
        return self.addr[0]

    def has_cap(self, cap: str) -> bool:
        return cap in self.caps

    async def send(self, data: bytes) -> bool:
        """ESP32로 bytes 전송. 실패 시 False 반환"""
        try:
            self.writer.write(data)
            await self.writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.warning(f"[{self.device_id}] send 실패: {e}")
            return False

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


# ─────────────────────────────────────────────
# TCP 서버
# ─────────────────────────────────────────────

class TCPServer:
    """
    asyncio 기반 TCP 서버

    Parameters
    ----------
    host        : 바인드 주소 (기본 0.0.0.0)
    port        : TCP 포트 (기본 9000)
    ws_broadcast: WebSocket 브로드캐스트 콜백 async def(msg: str)
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
        ws_broadcast: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.host = host
        self.port = port
        self.ws_broadcast = ws_broadcast
        self.db_logger = None  # DBLogger (SR-3.1 이벤트 로그, main.py에서 설정)

        # device_id → ESP32Client
        self._registry: dict[str, ESP32Client] = {}
        self._server: Optional[asyncio.Server] = None

        # 전체 상태 통합 관리자 (v3.0)
        self._unified_state_manager = UnifiedStateManager()

    @property
    def unified_state_manager(self) -> UnifiedStateManager:
        return self._unified_state_manager

    @property
    def state_manager(self) -> UnifiedStateManager:
        """하위 호환 alias (v2.0 코드가 state_manager 로 접근 시 동작)"""
        return self._unified_state_manager

    # ── 공개 API ────────────────────────────────────────────────────

    async def start(self):
        """TCP 서버 시작"""
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.host,
            port=self.port,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info(f"[TCP] 서버 시작: {addr[0]}:{addr[1]}")

    async def stop(self):
        """TCP 서버 종료"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("[TCP] 서버 종료")

    async def start_polling(self, interval: int = 30):
        """
        주기적 센서 폴링 태스크 (asyncio.create_task 로 실행)

        센서 cap(dht22/ds18b20) 보유 디바이스에만 query 전송
        interval: 폴링 주기 (초, 기본 30)
        """
        logger.info(f"[State] 센서 폴링 시작 (interval={interval}s)")
        while True:
            await asyncio.sleep(interval)
            for device_id, client in list(self._registry.items()):
                for cap, sensor in SENSOR_CAP_MAP.items():
                    if client.has_cap(cap):
                        payload = cmd_query(sensor)
                        ok = await client.send(payload)
                        if ok:
                            logger.debug(f"[State] 폴링 쿼리: {device_id} → {sensor}")
                        break  # 디바이스당 1회만

    async def send_command(self, device_id: str, data: bytes) -> bool:
        """
        특정 ESP32에 명령 전송 (v3.2: HMAC-SHA256 서명 래핑)

        ESP32_SECRET 환경변수가 있으면:
          기존 bytes payload → JSON 파싱 → {"ts":..,"sig":..,"cmd":{..}} 래핑 후 전송
        없으면:
          기존 방식 그대로 전송 (하위 호환, 경고 로그)

        Returns: 전송 성공 여부
        """
        client = self._registry.get(device_id)
        if not client:
            logger.warning(f"[TCP] 디바이스 없음: {device_id}")
            return False

        # ── HMAC 서명 래핑 ────────────────────────────────────────
        if _ESP32_SECRET:
            try:
                cmd_dict = _json.loads(data.decode("utf-8").strip().rstrip(b"\n".decode()))
                ts = str(int(_time.time()))
                payload_bytes = _json.dumps(cmd_dict, ensure_ascii=False, separators=(',', ':')).encode()
                msg = ts.encode() + b"." + payload_bytes
                sig = _hmac.new(_ESP32_SECRET, msg, hashlib.sha256).hexdigest()
                signed = _json.dumps(
                    {"ts": ts, "sig": sig, "cmd": cmd_dict},
                    ensure_ascii=False,
                ) + "\n"
                send_data = signed.encode()
            except Exception as e:
                logger.warning(f"[TCP] HMAC 서명 실패, 평문 전송: {e}")
                send_data = data
        else:
            send_data = data
        # ─────────────────────────────────────────────────────────

        success = await client.send(send_data)
        if success:
            logger.info(f"[TCP] → {device_id}: {send_data.decode().strip()}")
        return success

    async def broadcast_command(self, data: bytes) -> tuple:
        """
        연결된 모든 ESP32에 명령 전송
        Returns: (ok_cnt, fail_cnt)
        """
        ok_cnt = fail_cnt = 0
        for device_id in list(self._registry.keys()):
            success = await self.send_command(device_id, data)
            if success:
                ok_cnt += 1
            else:
                fail_cnt += 1
        return ok_cnt, fail_cnt

    def get_device_list(self) -> list[dict]:
        """연결된 디바이스 목록 반환 (REST/WS 응답용)"""
        return [
            {
                "device_id": c.device_id,
                "ip": c.ip,
                "caps": c.caps,
                "state": c.state,
            }
            for c in self._registry.values()
        ]

    def get_device(self, device_id: str) -> Optional[ESP32Client]:
        return self._registry.get(device_id)

    @property
    def connected_count(self) -> int:
        return len(self._registry)

    # ── 내부 핸들러 ─────────────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        addr = writer.get_extra_info("peername")
        logger.info(f"[TCP] 새 연결: {addr}")

        client: Optional[ESP32Client] = None

        try:
            while True:
                raw = await reader.readuntil(MSG_DELIMITER)
                if not raw:
                    break

                text = raw.decode("utf-8").strip()
                data = parse_tcp_message(text)

                if not data:
                    logger.warning(f"[TCP] JSON 파싱 실패: {text}")
                    continue

                ok, err = validate_esp32_message(data)
                if not ok:
                    # pir_event 는 schema 에 없으므로 validate 우회
                    if data.get("type") == "pir_event":
                        pass
                    else:
                        logger.warning(f"[TCP] 유효성 오류: {err} | raw={text}")
                        continue

                msg_type = data.get("type")

                # ── 등록 메시지 처리 ────────────────────────────
                if msg_type == TYPE_REGISTER:
                    client = await self._on_register(data, reader, writer, addr)

                # ── 등록 전 메시지는 무시 ────────────────────────
                elif client is None:
                    logger.warning(f"[TCP] 미등록 클라이언트 메시지 무시: {addr}")
                    continue

                # ── ACK ─────────────────────────────────────────
                elif msg_type == TYPE_ACK:
                    await self._on_ack(client, data)

                # ── 센서 데이터 ──────────────────────────────────
                elif msg_type == TYPE_SENSOR:
                    await self._on_sensor(client, data)

                # ── PIR 이벤트 (v3.1) ────────────────────────────
                elif msg_type == "pir_event":
                    await self._on_pir_event(client, data)

                # ── 에러 ────────────────────────────────────────
                elif msg_type == TYPE_ERROR:
                    await self._on_error(client, data)

        except asyncio.IncompleteReadError:
            logger.info(f"[TCP] 연결 종료: {addr}")
        except Exception as e:
            logger.error(f"[TCP] 오류 ({addr}): {e}")
        finally:
            await self._on_disconnect(client, addr)

    async def _on_register(
        self,
        data: dict,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        addr: tuple,
    ) -> ESP32Client:
        device_id = data["device_id"]
        caps = data.get("caps", [])

        # 기존 연결 정리 (재접속 처리)
        if device_id in self._registry:
            old = self._registry[device_id]
            old.close()
            logger.info(f"[TCP] 재접속 처리: {device_id}")

        client = ESP32Client(
            device_id=device_id,
            caps=caps,
            reader=reader,
            writer=writer,
            addr=addr,
        )
        self._registry[device_id] = client

        # UnifiedStateManager 에 등록 (초기 state 세팅)
        self._unified_state_manager.register(device_id, caps)
        # ESP32Client.state 도 동기화
        client.state = self._unified_state_manager._states[device_id]

        logger.info(f"[TCP] 등록 완료: {device_id} | caps={caps} | ip={addr[0]}")

        # DB 로그 (SR-3.1)
        if self.db_logger:
            self.db_logger.log("device_connect", "tcp_server",
                               f"{device_id} 연결 | caps={caps} | ip={addr[0]}",
                               device_id=device_id,
                               detail={"caps": caps, "ip": addr[0]})

        # WebSocket 브로드캐스트 - 디바이스 목록 갱신
        await self._broadcast(ws_device_list(self.get_device_list()))
        return client

    async def _on_ack(self, client: ESP32Client, data: dict):
        cmd    = data.get("cmd")
        status = data.get("status")
        logger.info(f"[TCP] ← {client.device_id} ACK: cmd={cmd} status={status}")

        # ── state 캐시 업데이트 + 뱃지 동기화용 detail ──────────────
        detail = ""
        if status == "ok":
            if cmd == "led":
                state = data.get("state", "")
                pin   = data.get("pin", 2)
                if state:
                    client.state[f"led_{pin}"] = 1 if state == "on" else 0
                    detail = f" state={state}"
            elif cmd == "servo":
                angle = data.get("angle")
                pin   = data.get("pin")
                if angle is not None and pin is not None:
                    client.state[f"servo_{pin}"] = angle
                    detail = f" angle={angle} pin={pin}"

            # UnifiedStateManager 확정 반영
            self._unified_state_manager.update_ack(client.device_id, cmd, data)

            # device_update 브로드캐스트 → 프론트 updateDeviceState() 즉시 반영
            await self._broadcast(ws_device_update(client.device_id, client.state))

        msg = (f"{client.device_id} {cmd} 명령 전송{detail}"
               if status == "ok" else f"{client.device_id} {cmd} → {status}")
        await self._broadcast(ws_cmd_result(status, msg))

        # DB 로그 (SR-3.1)
        if self.db_logger:
            self.db_logger.log("device_ack", "tcp_server",
                               f"{client.device_id} ACK: cmd={cmd} status={status}",
                               device_id=client.device_id, detail=data)

    async def _on_sensor(self, client: ESP32Client, data: dict):
        device_type = data.get("device")
        temp     = data.get("temp")
        humidity = data.get("humidity")
        room     = data.get("room")

        logger.info(
            f"[TCP] ← {client.device_id} SENSOR: "
            f"device={device_type} room={room} temp={temp} humidity={humidity}"
        )

        if temp     is not None: client.state["temp"]     = temp
        if humidity is not None: client.state["humidity"] = humidity

        self._unified_state_manager.update_sensor(
            client.device_id, temp=temp, humidity=humidity
        )

        await self._broadcast(
            ws_sensor_data(client.device_id, temp=temp, humidity=humidity, room=room)
        )

        # DB 로그 (SR-3.1)
        if self.db_logger:
            self.db_logger.log("sensor_data", "tcp_server",
                               f"{client.device_id} 센서: room={room} temp={temp} humidity={humidity}",
                               device_id=client.device_id,
                               detail={"device": device_type, "room": room, "temp": temp, "humidity": humidity})

    async def _on_error(self, client: ESP32Client, data: dict):
        msg = data.get("msg", "unknown error")
        logger.error(f"[TCP] ← {client.device_id} ERROR: {msg}")
        await self._broadcast(ws_cmd_result("fail", f"{client.device_id}: {msg}"))

    async def _on_pir_event(self, client: ESP32Client, data: dict):
        """
        PIR 이벤트 수신 처리 (v3.2)
        ESP32 → TCP {"type":"pir_event","location":"pir_living_room","event":"motion_detected",...}
        → WS 브로드캐스트 → 브라우저 pir_alert 수신
        """
        event    = data.get("event",    "")
        detail   = data.get("detail",   "")
        context  = data.get("context",  "unknown")
        location = data.get("location", "")

        # ── location 기반 메시지 매핑 ──────────────────────────────
        location_msg_map = {
            # esp32_home1 (거실 PIR)
            ("pir_living_room", "motion_detected"): "🏠 실내(거실) 움직임 감지",
            # esp32_home2 (게이트/입구 PIR)
            ("pir_gate", "motion_detected"):  "🚪 게이트 움직임 감지",
            ("pir_gate", "guard_alert"):      "🚨 방범 모드 — 게이트 침입 감지!",
            ("pir_gate", "presence_alert"):   "⚠️ 장시간 움직임 없음 (게이트)",
        }

        # ── 기존 event+context 기반 메시지 매핑 ───────────────────
        event_msg_map = {
            ("guard_alert",    "away"):  "🚨 외출 중 침입 감지!",
            ("guard_alert",    "sleep"): "🚨 취침 중 거실 침입 감지!",
            ("presence_alert", "home"):  "⚠️ 장시간 움직임 없음 — 괜찮으신가요?",
        }

        # location 우선, 없으면 event+context 조합으로 fallback
        alert_msg = (
            location_msg_map.get((location, event))
            or event_msg_map.get((event, context))
            or f"🔔 PIR 감지: {event} ({location or context})"
        )

        logger.warning(
            f"[TCP] ← {client.device_id} PIR: {alert_msg} "
            f"| location={location} event={event} detail={detail}"
        )

        # 일반 motion_detected 는 로그만 남기고 웹 알림을 보내지 않음
        # guard_alert / presence_alert 만 웹 브로드캐스트
        if event == "motion_detected":
            return

        import json as _j
        ws_msg = _j.dumps({
            "type":     "pir_alert",
            "msg":      alert_msg,
            "event":    event,
            "location": location,
            "context":  context,
        }, ensure_ascii=False)
        await self._broadcast(ws_msg)

        # DB 로깅: 보안 이벤트
        if self.db_logger:
            self.db_logger.log(
                "security_alert", "tcp_server", alert_msg,
                device_id=client.device_id, level="WARN",
                detail={"event": event, "location": location,
                        "context": context, "detail": detail},
            )

    async def _on_disconnect(self, client: Optional[ESP32Client], addr: tuple):
        if client:
            # 재접속으로 새 클라이언트가 이미 등록된 경우,
            # 이전 연결 해제 시 새 클라이언트를 제거하지 않음
            current = self._registry.get(client.device_id)
            if current is client:
                self._registry.pop(client.device_id, None)
                self._unified_state_manager.remove(client.device_id)
            client.close()
            logger.info(f"[TCP] 해제: {client.device_id} ({addr})")
            await self._broadcast(ws_device_list(self.get_device_list()))

            # DB 로깅: 디바이스 연결 해제
            if self.db_logger:
                self.db_logger.log(
                    "device_disconnect", "tcp_server",
                    f"{client.device_id} 연결 해제",
                    device_id=client.device_id,
                    detail={"addr": f"{addr[0]}:{addr[1]}"},
                )
        else:
            logger.info(f"[TCP] 미등록 연결 해제: {addr}")

    # ── 유틸 ────────────────────────────────────────────────────────

    async def _broadcast(self, msg: str):
        """WebSocket 허브로 브로드캐스트 (콜백이 없으면 스킵)"""
        if self.ws_broadcast:
            try:
                await self.ws_broadcast(msg)
            except Exception as e:
                logger.warning(f"[TCP] ws_broadcast 오류: {e}")
