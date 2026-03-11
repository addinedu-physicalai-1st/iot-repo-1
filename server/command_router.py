"""
server/command_router.py
========================
LLM/수동 JSON 명령 → ESP32 라우팅  v2.4

v2.2 변경사항:
  - PIR 모드 제어 액션 4종 추가:
      · away_mode  : 외출 (PIR guard + 전체 조명 OFF)
      · home_mode  : 귀가 (PIR presence)
      · sleep_mode : 취침 (PIR guard, 거실 감시)
      · wake_mode  : 기상 (PIR presence)
  - execute(): 위 4개 cmd → _execute_pir_mode() 위임
  - _execute_pir_mode(): ESP32에 pir_mode 명령 전송
  - _simple_parse(): 욕실 온도 조회 키워드 파싱 추가 (query_bathroom_temp)
  - execute(): query_bathroom_temp 분기 추가
  - _execute_query_bathroom_temp(): 희망온도 TTS 응답 (센서 없음 안내)
v2.3:
  - _simple_parse(): 욕실 온도 설정 키워드 파싱 추가 (set_bathroom_temp)
  - execute(): set_bathroom_temp 분기 추가
v2.2:
  - _simple_parse(): 외출/귀가/취침/기상 키워드 파싱 추가

v2.1 변경사항:
  - all_on 명령 추가: 전체 전등 켜기 + 음악 play
  - _simple_parse(): "전체/모두 켜줘" → cmd="all_on"
  - asyncio.sleep(0.4) 딜레이 적용 (WS 큐 타이밍 보장)

v2.0 변경사항:
  - _execute_all_broadcast(): cmd="all_off" 분기 추가
    · LED 전체 끄기 + 음악 pause 순차 처리
    · "전체 꺼줘", "모두 꺼줘", "다 꺼줘" 명령 지원
  - execute(): cmd="all_off" → validate 우회 후 _execute_all_broadcast() 위임
  - _simple_parse(): "전체/모두/다 + 꺼" 조합 → cmd="all_off" 반환

v1.9 버그픽스:
  - execute(): device_id="all" 체크를 validate_command() 호출 앞으로 이동
    · 전체 전등 켜/끄기 명령 시 room/pin 없어 validate 실패하던 문제 수정
    · "led: pin must be int (or provide 'room')" 오류 해소

v1.6 변경사항:
  - WS_TYPE_MUSIC_STATE = "music_state" 상수 추가
  - handle(): music_state 타입 분기 추가 → _handle_music_state() 호출
  - _handle_music_state(): 브라우저 ytPlayer 상태 → UnifiedStateManager 반영
      · playing/title/genre/volume 부분 업데이트 지원
      · web_clients 수 동기화 (ws_hub.connected_count)
  - _handle_status(): UnifiedStateManager 음악 상태 포함 응답

v1.5 변경사항:
  - _naturalize_status() 추가
  - _handle_status(): tts_out 생성을 _naturalize_status() 로 위임

역할:
  - WebSocket / REST 로 들어온 명령 dict 를 검증
  - device_id 결정 (명령에 포함 or settings.yaml 키워드 매핑)
  - TCPServer.send_command() 호출
  - 결과를 ws_cmd_result 문자열로 반환

흐름:
  [브라우저 WS]  -> websocket_hub -> CommandRouter.handle(client_id, data)
  [REST POST]    -> api_routes   -> CommandRouter.execute(data)
  [LLM 파싱결과] -> llm_engine   -> CommandRouter.execute(data)

사용:
  from server.command_router import CommandRouter
  router = CommandRouter(tcp_server=srv, settings=cfg, ws_hub=hub)
"""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from protocol.schema import (
    validate_command,
    ws_cmd_result,
    cmd_led, cmd_servo, cmd_query, cmd_seg7,
    CMD_LED, CMD_SERVO, CMD_QUERY, CMD_SEG7,
    DEVICE_HOME, DEVICE_HOME1, DEVICE_HOME2, ALL_DEVICES,
    ROOM_LED_PIN, ROOM_SERVO_PIN, ROOM_LABEL,
    ALL_ROOMS,
)

if TYPE_CHECKING:
    from server.tcp_server import TCPServer

logger = logging.getLogger(__name__)

# ── PIR 모드 상태 영속화 파일 ─────────────────────────────────────────
_PIR_STATE_FILE = Path("data/pir_mode.json")

# ── HMAC 서명 헬퍼 (esp32_secure 선택적 로드) ────────────────────────
try:
    from server.esp32_secure import build_signed_packet as _build_signed_packet
    _HMAC_ENABLED = True
    logger.info("[Security] TCP HMAC 서명 활성화")
except ImportError:
    _HMAC_ENABLED = False
    logger.warning("[Security] esp32_secure 모듈 없음 — 평문 TCP 전송")


def _sign_payload(payload: bytes) -> bytes:
    """
    평문 payload bytes 그대로 반환.
    HMAC 서명은 tcp_server.send_command()에서 일괄 처리.
    """
    return payload


# ─────────────────────────────────────────────
# WebSocket 메시지 타입 상수
# ─────────────────────────────────────────────

WS_TYPE_VOICE_TEXT    = "voice_text"      # 브라우저 STT 결과
WS_TYPE_MANUAL_CMD    = "manual_cmd"      # 브라우저 수동 명령
WS_TYPE_LLM_CMD       = "llm_cmd"         # LLM 파싱 결과
WS_TYPE_MANUAL_TRIGGER = "manual_trigger" # 버튼 모드: 서버 STT 활성화 요청
WS_TYPE_MUSIC_STATE   = "music_state"     # 브라우저 ytPlayer 상태 보고 (v1.6)
WS_TYPE_AUDIO_CHUNK   = "audio_chunk"     # 브라우저 마이크 오디오 스트림 (wake_source=browser)


class CommandRouter:
    """
    명령 라우터

    Parameters
    ----------
    tcp_server : TCPServer 인스턴스
    settings   : settings.yaml 로드된 dict
    llm_engine : LLMEngine 인스턴스 (Phase 3 연동, 선택)
    """

    def __init__(
        self,
        tcp_server: "TCPServer",
        settings: dict,
        llm_engine=None,
        ws_hub=None,
        db_logger=None,
    ):
        self._tcp = tcp_server
        self._settings = settings
        self._llm = llm_engine
        self._ws_hub = ws_hub   # WebSocketHub (connected_count 조회용)
        self._db = db_logger    # DBLogger (SR-3.1 이벤트 로그)

        # 공간명 → device_id 키워드 매핑 (settings.yaml)
        self._keyword_map: dict[str, str] = (
            settings.get("command_keywords", {})
        )

        # 현재 PIR 모드 상태 — 서버 재시작 후에도 마지막 상태 복원
        # None | "away_mode" | "home_mode" | "sleep_mode" | "wake_mode" | "dnd_mode"
        self._current_pir_mode: Optional[str] = self._load_pir_mode()

    # ── PIR 모드 영속화 ─────────────────────────────────────────────
    def _load_pir_mode(self) -> Optional[str]:
        """서버 시작 시 마지막 PIR 모드 복원"""
        try:
            if _PIR_STATE_FILE.exists():
                data = _json.loads(_PIR_STATE_FILE.read_text())
                mode = data.get("pir_mode")
                if mode:
                    logger.info(f"[Router] PIR 모드 복원: {mode}")
                return mode
        except Exception as e:
            logger.warning(f"[Router] PIR 모드 복원 실패: {e}")
        return None

    def _save_pir_mode(self, mode: Optional[str]) -> None:
        """PIR 모드 변경 시 파일 저장"""
        try:
            _PIR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PIR_STATE_FILE.write_text(_json.dumps({"pir_mode": mode}))
        except Exception as e:
            logger.warning(f"[Router] PIR 모드 저장 실패: {e}")

    async def handle(self, client_id: str, data: dict) -> str:
        """
        WebSocketHub on_message 콜백
        브라우저에서 수신한 메시지를 타입별로 처리
        Returns: ws_cmd_result JSON 문자열
        """
        msg_type = data.get("type")
        logger.debug(f"[Router] handle() 진입 — client={client_id} type={msg_type} data={data}")

        # 음성 텍스트 → LLM 파싱 후 실행
        if msg_type == WS_TYPE_VOICE_TEXT:
            text = data.get("text", "")
            if not text:
                return ws_cmd_result("fail", "빈 음성 텍스트")
            return await self._handle_voice(text)

        # 수동 명령 직접 실행
        elif msg_type == WS_TYPE_MANUAL_CMD:
            return await self.execute(data)

        # LLM 파싱 결과 직접 실행
        elif msg_type == WS_TYPE_LLM_CMD:
            logger.debug(f"[Router] llm_cmd 수신 → execute() 호출 — cmd={data.get('cmd')}")
            result = await self.execute(data)
            logger.debug(f"[Router] execute() 완료 — result={result}")
            return result

        # 버튼 모드: 서버 STTEngine LISTENING 활성화
        elif msg_type == WS_TYPE_MANUAL_TRIGGER:
            return await self._handle_manual_trigger()

        # 브라우저 ytPlayer 상태 보고 → UnifiedStateManager 반영 (v1.6)
        elif msg_type == WS_TYPE_MUSIC_STATE:
            return await self._handle_music_state(data)

        # 브라우저 마이크 오디오 스트림 (wake_source=browser 시 Porcupine/VAD용)
        elif msg_type == WS_TYPE_AUDIO_CHUNK:
            await self._handle_audio_chunk(data)
            return None  # 브로드캐스트 불필요

        else:
            logger.warning(f"[Router] 알 수 없는 WS type: {msg_type}")
            return ws_cmd_result("fail", f"unknown message type: {msg_type}")

    async def execute(self, data: dict) -> str:
        """
        명령 dict 검증 → device_id 결정 → TCP 전송
        device_id="all" 이면 execute_all() 로 자동 위임
        REST API / LLM 엔진에서 직접 호출 가능
        Returns: ws_cmd_result JSON 문자열
        """
        # 1. music / status 는 validate_command 우회 (TCP 불필요)
        if data.get("cmd") == "music":
            return await self._handle_music(data)

        if data.get("cmd") == "status":
            return await self._handle_status(data)

        # cmd="all_off"/"all_on" → LED 전체 + 음악 동시 제어
        if data.get("cmd") in ("all_off", "all_on"):
            return await self._execute_all_broadcast(data)

        # cmd="away_mode"/"home_mode"/"sleep_mode"/"wake_mode" → PIR 모드 제어
        if data.get("cmd") in ("away_mode", "home_mode", "sleep_mode", "wake_mode", "dnd_mode"):
            logger.debug(f"[Router] execute() → _execute_pir_mode() 진입 — cmd={data.get('cmd')}")
            return await self._execute_pir_mode(data)

        # cmd="pir_dismiss" → PIR 침입 알림 해제 (LED 처리)
        if data.get("cmd") == "pir_dismiss":
            return await self._execute_pir_dismiss(data)

        # cmd="set_bathroom_temp" → 욕실 희망온도 설정
        if data.get("cmd") == "set_bathroom_temp":
            return await self._execute_set_bathroom_temp(data)

        # cmd="heating" → 욕실 난방 ON/OFF (UI 상태 동기화 + DB 기록)
        if data.get("cmd") == "heating":
            return await self._execute_heating(data)

        # cmd="query_bathroom_temp" → 욕실 현재온도 조회 (센서 없음 → 희망온도 안내)
        if data.get("cmd") == "query_bathroom_temp":
            return await self._execute_query_bathroom_temp(data)

        # 2. device_id "all" → validate_command 전에 먼저 처리
        #    (전체 명령은 room/pin 없이 오기 때문에 validate 통과 불가)
        raw_id = data.get("device_id", "")
        if raw_id == "all":
            return await self._execute_all_broadcast(data)

        # 3. 명령 유효성 검사 (단일 디바이스 명령만)
        ok, err = validate_command(data)
        if not ok:
            logger.warning(f"[Router] 유효성 오류: {err} | data={data}")
            return ws_cmd_result("fail", err)

        # 3. device_id 결정
        device_id = self._resolve_device(data)
        if not device_id:
            return ws_cmd_result("fail", "device_id 를 특정할 수 없습니다")

        # 4. ESP32 연결 확인
        client = self._tcp.get_device(device_id)
        if not client:
            return ws_cmd_result("fail", f"{device_id} 미연결")

        # 5. 명령 bytes 빌드
        payload = self._build_payload(data)
        if not payload:
            return ws_cmd_result("fail", f"알 수 없는 cmd: {data.get('cmd')}")

        # 6. TCP 전송
        success = await self._tcp.send_command(device_id, _sign_payload(payload))
        if success:
            logger.info(f"[Router] 전송 성공: {device_id} ← {data}")
            cmd    = data.get("cmd")
            detail = ""

            # ── client.state 즉시 업데이트 (ACK 대기 없이 선반영) ──
            if cmd == CMD_LED:
                state = data.get("state", "")
                room  = data.get("room", "")
                from protocol.schema import ROOM_LED_PIN
                pin   = ROOM_LED_PIN.get(room) if room else data.get("pin", 2)
                if state and pin is not None:
                    client.state[f"led_{pin}"] = 1 if state == "on" else 0
                    detail = f" state={state}"
            elif cmd == CMD_SERVO:
                angle = data.get("angle")
                room  = data.get("room", "")
                from protocol.schema import ROOM_SERVO_PIN
                pin   = ROOM_SERVO_PIN.get(room) if room else data.get("pin", 18)
                if angle is not None and pin is not None:
                    client.state[f"servo_{pin}"] = angle
                    detail = f" angle={angle}"

            # ── device_update 브로드캐스트 → 프론트 뱃지 즉시 반영 ─
            from protocol.schema import ws_device_update
            await self._tcp._broadcast(ws_device_update(device_id, client.state))

            # DB 로그 (SR-3.1)
            if self._db:
                self._db.log("device_control", "command_router",
                             f"{device_id} {cmd} 명령 전송{detail}",
                             device_id=device_id, room=data.get("room"),
                             detail=data)

            # tts_response 있으면 결과에 포함
            result_msg = f"{device_id} {cmd} 명령 전송{detail}"
            tts = data.get("tts_response")
            if tts:
                return _json.dumps({
                    "type": "cmd_result",
                    "status": "ok",
                    "msg": result_msg,
                    "tts_response": tts,
                }, ensure_ascii=False)

            return ws_cmd_result("ok", result_msg)
        else:
            return ws_cmd_result("fail", f"{device_id} 전송 실패")

    async def execute_all(self, data: dict) -> list[str]:
        """
        연결된 모든 ESP32에 동일 명령 실행
        Returns: 각 디바이스별 결과 리스트
        """
        results = []
        connected = [d["device_id"] for d in self._tcp.get_device_list()]
        if not connected:
            return [ws_cmd_result("fail", "연결된 디바이스 없음")]
        for device_id in connected:
            d = {**data, "device_id": device_id}
            result = await self.execute(d)
            results.append(result)
        return results

    # ── 내부 처리 ────────────────────────────────────────────────────

    async def _execute_all_broadcast(self, data: dict) -> str:
        """
        device_id="all" 처리 — esp32_home 단일 구조
        room 없이 전체 전송 시 ALL_ROOMS 순회하여 각 room별 개별 전송

        led  : ALL_ROOMS 5개 순차 전송
        servo: 서보 보유 room(bedroom/garage/entrance)만 전송
        """
        cmd   = data.get("cmd")
        state = data.get("state")
        angle = data.get("angle")

        # LED/all_off/all_on 은 esp32_home2, servo는 home1(bedroom) + home2
        led_device_id = DEVICE_HOME2 if self._tcp.get_device(DEVICE_HOME2) else DEVICE_HOME
        client = self._tcp.get_device(led_device_id)
        if not client:
            return ws_cmd_result("fail", "esp32_home2 미연결")

        ok_cnt = 0

        if cmd == CMD_LED:
            for room in ALL_ROOMS:
                payload = self._build_payload({"cmd": CMD_LED, "room": room, "state": state})
                if payload and await self._tcp.send_command(led_device_id, _sign_payload(payload)):
                    ok_cnt += 1
                    pin = ROOM_LED_PIN.get(room)
                    if pin is not None:
                        client.state[f"led_{pin}"] = 1 if state == "on" else 0

        elif cmd == CMD_SERVO:
            for room, pin in ROOM_SERVO_PIN.items():
                if pin is None:
                    continue
                target = DEVICE_HOME1 if room == "bedroom" else DEVICE_HOME2
                if not self._tcp.get_device(target):
                    target = DEVICE_HOME  # 하위 호환
                payload = self._build_payload({"cmd": CMD_SERVO, "room": room, "angle": angle})
                if payload and await self._tcp.send_command(target, _sign_payload(payload)):
                    ok_cnt += 1
                    t_client = self._tcp.get_device(target)
                    if t_client:
                        t_client.state[f"servo_{pin}"] = angle

        elif cmd == "all_off":
            for room in ALL_ROOMS:
                payload = self._build_payload({"cmd": CMD_LED, "room": room, "state": "off"})
                if payload and await self._tcp.send_command(led_device_id, _sign_payload(payload)):
                    ok_cnt += 1
                    pin = ROOM_LED_PIN.get(room)
                    if pin is not None:
                        client.state[f"led_{pin}"] = 0
            import asyncio
            await asyncio.sleep(0.4)
            await self._handle_music({"cmd": "music", "action": "pause"})
            logger.info("[Router] all_off: 음악 pause 전송")

        elif cmd == "all_on":
            for room in ALL_ROOMS:
                payload = self._build_payload({"cmd": CMD_LED, "room": room, "state": "on"})
                if payload and await self._tcp.send_command(led_device_id, _sign_payload(payload)):
                    ok_cnt += 1
                    pin = ROOM_LED_PIN.get(room)
                    if pin is not None:
                        client.state[f"led_{pin}"] = 1

        else:
            return ws_cmd_result("fail", f"all 브로드캐스트: 지원하지 않는 cmd={cmd}")

        from protocol.schema import ws_device_update
        await self._tcp._broadcast(ws_device_update(led_device_id, client.state))

        if cmd == CMD_LED:
            total = len(ALL_ROOMS)
        elif cmd == CMD_SERVO:
            total = len([p for p in ROOM_SERVO_PIN.values() if p])
        else:  # all_off / all_on
            total = len(ALL_ROOMS)
        msg    = f"전체 {cmd} → {ok_cnt}/{total} 전송 완료"
        logger.info(f"[Router] {msg}")
        return ws_cmd_result("ok" if ok_cnt > 0 else "fail", msg)

    async def _execute_pir_mode(self, data: dict) -> str:
        """
        PIR 모드 제어 (v2.4)
        away_mode  → LED 상태 저장 → PIR guard  + 전체 조명 OFF
        home_mode  → PIR presence  → LED 이전 상태 복원
        sleep_mode → LED 상태 저장 → PIR guard  (context: sleep)
        wake_mode  → PIR presence  → LED 이전 상태 복원
        """
        import asyncio
        import json as _j

        cmd = data.get("cmd")
        tts = data.get("tts_response", "")

        # cmd → PIR 모드 + context 매핑
        pir_map = {
            "away_mode":  ("guard",    "away"),
            "home_mode":  ("presence", "home"),
            "sleep_mode": ("guard",    "sleep"),
            "wake_mode":  ("presence", "wake"),
            "dnd_mode":   ("dnd",      "dnd"),
        }
        pir_mode, context = pir_map[cmd]

        # ── 모드 상태 세팅: ESP32 연결 여부와 무관하게 항상 먼저 저장 ──
        # (ESP32 미연결 시에도 camera_stream dnd 억제가 동작해야 하므로)
        self._current_pir_mode = cmd
        self._save_pir_mode(cmd)
        logger.debug(f"[Router] _current_pir_mode 설정 완료: {self._current_pir_mode}")

        # PIR 모드 명령: LED 제어 포함 → esp32_home2 (LED 보유)
        pir_device = DEVICE_HOME2 if self._tcp.get_device(DEVICE_HOME2) else DEVICE_HOME
        client = self._tcp.get_device(pir_device)
        if not client:
            # ESP32 미연결이어도 모드는 세팅됐으므로 camera_stream 억제는 동작함
            logger.warning(f"[Router] esp32_home2 미연결 — 모드={cmd} 상태는 저장됨")
            if pir_mode == "dnd":
                if self._tcp.ws_broadcast:
                    await self._tcp.ws_broadcast(_j.dumps({"type": "pir_mode", "mode": cmd}, ensure_ascii=False))
                return _j.dumps({"type": "cmd_result", "status": "ok", "msg": "방해금지 모드 설정 완료 (ESP32 미연결 — 알람 억제 적용)", "cmd": cmd}, ensure_ascii=False)
            return ws_cmd_result("fail", "esp32_home2 미연결")

        # ── dnd_mode: ESP32에 pir_mode:dnd 전송 (LED 켜지 않도록) ──
        if pir_mode == "dnd":
            logger.debug(f"[Router] dnd_mode 분기 진입 — pir_device={pir_device}")
            payload = (_j.dumps({
                "cmd": "pir_mode",
                "mode": "dnd",
                "context": "dnd",
            }) + "\n").encode()
            await self._tcp.send_command(pir_device, _sign_payload(payload))
            logger.info("[Router] 방해금지 모드 — ESP32 pir_mode:dnd 전송 (LED 차단)")
            if self._tcp.ws_broadcast:
                await self._tcp.ws_broadcast(_j.dumps({"type": "pir_mode", "mode": cmd}, ensure_ascii=False))
            return _j.dumps({"type": "cmd_result", "status": "ok", "msg": "방해금지 모드 설정 완료 — 모든 알람·팝업 무시, 로그 기록 유지", "cmd": cmd}, ensure_ascii=False)

        # ── guard 모드 진입: LED 상태 스냅샷 저장 ──────────────────
        if pir_mode == "guard":
            from protocol.schema import ROOM_LED_PIN
            snapshot = {}
            for room, pin in ROOM_LED_PIN.items():
                if pin is not None:
                    snapshot[f"led_{pin}"] = client.state.get(f"led_{pin}", 0)
            client.state["_led_snapshot"] = snapshot
            logger.info(f"[Router] PIR guard 진입 — LED 스냅샷 저장: {snapshot}")

        # ESP32에 pir_mode 명령 전송
        payload = (_j.dumps({
            "cmd": "pir_mode",
            "mode": pir_mode,
            "context": context,
        }) + "\n").encode()

        success = await self._tcp.send_command(pir_device, _sign_payload(payload))
        if not success:
            return ws_cmd_result("fail", f"PIR 모드 설정 실패: {pir_mode}")

        logger.info(f"[Router] PIR 모드 설정: {pir_mode} (context={context})")

        # DB 로그 (SR-3.1)
        if self._db:
            self._db.log("pir_mode", "command_router",
                         f"PIR {pir_mode} 모드 설정 (context={context})",
                         device_id=pir_device,
                         detail={"cmd": cmd, "pir_mode": pir_mode, "context": context})
        # ── away_mode / sleep_mode: 전체 조명 OFF ──────────────────
        if cmd in ("away_mode", "sleep_mode"):
            await asyncio.sleep(0.2)
            await self._execute_all_broadcast({"cmd": "all_off"})
            logger.info(f"[Router] {cmd}: 전체 조명 OFF")

        # ── home_mode / wake_mode: 이전 LED 상태 복원 ──────────────
        elif cmd in ("home_mode", "wake_mode"):
            snapshot = client.state.pop("_led_snapshot", None)
            if snapshot:
                await asyncio.sleep(0.2)
                from protocol.schema import ROOM_LED_PIN
                restored = 0
                for room, pin in ROOM_LED_PIN.items():
                    if pin is None:
                        continue
                    prev_state = snapshot.get(f"led_{pin}", 0)
                    state_str  = "on" if prev_state == 1 else "off"
                    payload_led = self._build_payload({
                        "cmd": "led", "room": room, "state": state_str
                    })
                    if payload_led and await self._tcp.send_command(pir_device, _sign_payload(payload_led)):
                        client.state[f"led_{pin}"] = prev_state
                        restored += 1
                logger.info(f"[Router] {cmd}: LED 이전 상태 복원 ({restored}개)")
            else:
                logger.info(f"[Router] {cmd}: LED 스냅샷 없음 — 복원 생략")

            from protocol.schema import ws_device_update
            await self._tcp._broadcast(ws_device_update(pir_device, client.state))

        msg = f"PIR {pir_mode} 모드 설정 완료 (context={context})"
        if self._tcp.ws_broadcast:
            await self._tcp.ws_broadcast(_j.dumps({"type": "pir_mode", "mode": cmd}, ensure_ascii=False))
        payload = {"type": "cmd_result", "status": "ok", "msg": msg, "cmd": cmd}
        if tts:
            payload["tts_response"] = tts
            return _j.dumps(payload, ensure_ascii=False)
        return _j.dumps(payload, ensure_ascii=False)

    async def _execute_pir_dismiss(self, data: dict) -> str:
        """
        PIR 침입 알림 해제 (v2.4)
        guard 모드(외출/취침) → LED 스냅샷 복원, 없으면 all_off
        presence 모드(귀가/기상) → all_off (이미 복원 완료 상태)
        """
        import asyncio
        import json as _j

        pir_device = DEVICE_HOME2 if self._tcp.get_device(DEVICE_HOME2) else DEVICE_HOME
        client = self._tcp.get_device(pir_device)
        if not client:
            return ws_cmd_result("fail", "esp32_home2 미연결")

        snapshot = client.state.pop("_led_snapshot", None)

        if snapshot:
            await asyncio.sleep(0.1)
            from protocol.schema import ROOM_LED_PIN
            restored = 0
            for room, pin in ROOM_LED_PIN.items():
                if pin is None:
                    continue
                prev_state = snapshot.get(f"led_{pin}", 0)
                state_str  = "on" if prev_state == 1 else "off"
                payload_led = self._build_payload({
                    "cmd": "led", "room": room, "state": state_str
                })
                if payload_led and await self._tcp.send_command(pir_device, _sign_payload(payload_led)):
                    client.state[f"led_{pin}"] = prev_state
                    restored += 1
            logger.info(f"[Router] pir_dismiss: LED 스냅샷 복원 ({restored}개)")
        else:
            await self._execute_all_broadcast({"cmd": "all_off"})
            logger.info("[Router] pir_dismiss: 스냅샷 없음 — all_off 실행")

        from protocol.schema import ws_device_update
        await self._tcp._broadcast(ws_device_update(pir_device, client.state))

        return ws_cmd_result("ok", "PIR 알림 해제 — LED 복원 완료")

    async def _execute_query_bathroom_temp(self, data: dict) -> str:
        """
        욕실 현재온도 조회 (v2.4)
        온도 센서 없음 → 웹앱에 설정된 희망온도를 TTS로 안내
        """
        import json as _j
        tts = data.get("tts_response", "")

        # 웹앱 희망온도 조회 요청 브로드캐스트
        ws_payload = _j.dumps({
            "type": "query_bathroom_temp",
            "tts": tts,
        }, ensure_ascii=False)
        if self._tcp.ws_broadcast:
            await self._tcp.ws_broadcast(ws_payload)

        msg = "욕실 온도 조회 요청 전송"
        logger.info(f"[Router] {msg}")

        if tts:
            return _j.dumps({
                "type": "cmd_result",
                "status": "ok",
                "msg": msg,
                "tts_response": tts,
            }, ensure_ascii=False)

        return ws_cmd_result("ok", msg)

    async def _execute_set_bathroom_temp(self, data: dict) -> str:
        """
        욕실 희망온도 설정 (v2.3)
        ESP32에 set_temp 명령 전송 + 웹앱 7세그먼트 표시용 ws 브로드캐스트
        """
        import json as _j
        value = data.get("value")
        tts   = data.get("tts_response", "")
        try:
            value = float(value)
        except (TypeError, ValueError):
            return ws_cmd_result("fail", "온도 값이 올바르지 않습니다")

        if not (10.0 <= value <= 40.0):
            return ws_cmd_result("fail", f"온도 범위 초과: {value}°C (10~40°C)")

        # ESP32 seg7 명령 전송 (esp32_home1에 7세그먼트 있음)
        seg7_device = DEVICE_HOME1 if self._tcp.get_device(DEVICE_HOME1) else DEVICE_HOME
        client = self._tcp.get_device(seg7_device)
        if client:
            payload = {
                "cmd": "seg7",
                "mode": "number",
                "value": float(value)
            }
            message = (_j.dumps(payload) + "\n").encode()
            await self._tcp.send_command(seg7_device, _sign_payload(message))
        else:
            logger.warning("[Router] DEVICE_HOME1 연결 안됨 — seg7 전송 생략")

        logger.info(f"[Router] SEG7 전송 완료: {value:.1f}°C")

        # 희망온도 설정 시 난방 자동 ON → 웹앱에 heating_state 브로드캐스트
        heating_payload = _j.dumps({
            "type":  "heating_state",
            "room":  "bathroom",
            "state": "on",
        }, ensure_ascii=False)
        if self._tcp.ws_broadcast:
            await self._tcp.ws_broadcast(heating_payload)
        logger.info("[Router] 온도 설정 → 난방 자동 ON 브로드캐스트")

        # 웹앱 동기화용 브로드캐스트 (ESP32 연결 여부 무관)
        ws_payload = _j.dumps({
            "type": "bathroom_temp_set",
            "value": value,
            "tts_response": tts,
        }, ensure_ascii=False)
        if self._tcp.ws_broadcast:
            await self._tcp.ws_broadcast(ws_payload)

        msg = f"욕실 난방 ON + 희망온도 {value}°C 설정 완료"
        logger.info(f"[Router] {msg}")

        # DB 로그 (SR-3.1)
        if self._db:
            self._db.log("bathroom_temp", "command_router", msg,
                         device_id=DEVICE_HOME1, room="bathroom",
                         detail={"action": "set_and_heat_on", "value": value})

        if tts:
            return _j.dumps({
                "type": "cmd_result",
                "status": "ok",
                "msg": msg,
                "tts_response": tts,
            }, ensure_ascii=False)

        return ws_cmd_result("ok", msg)

    async def _execute_heating(self, data: dict) -> str:
        """
        욕실 난방 ON/OFF 처리
        - 상태를 WS 브로드캐스트로 모든 클라이언트에 동기화
        - DB 이벤트 기록
        - ESP32 하드웨어 연동이 필요하면 여기에 TCP 명령 추가
        """
        import json as _j

        state = data.get("state", "off").lower()
        is_on = state == "on"

        msg = f"욕실 난방 {'켜기' if is_on else '끄기'}"
        logger.info(f"[Router] {msg}")

        # WS 브로드캐스트 → 다른 클라이언트(모바일 등) 상태 동기화
        ws_payload = _j.dumps({
            "type":  "heating_state",
            "room":  "bathroom",
            "state": state,
        }, ensure_ascii=False)
        if self._tcp.ws_broadcast:
            await self._tcp.ws_broadcast(ws_payload)

        # DB 로그
        if self._db:
            self._db.log("heating", "command_router", msg,
                         device_id=DEVICE_HOME1, room="bathroom",
                         detail={"state": state})

        tts = data.get("tts_response", "")
        if tts:
            return _j.dumps({
                "type": "cmd_result", "status": "ok",
                "msg": msg, "tts_response": tts,
            }, ensure_ascii=False)

        return ws_cmd_result("ok", msg)

    async def _handle_status(self, data: dict) -> str:
        """
        v3: esp32_home1 + esp32_home2 이중 구성 → room별 상태 분리 표시
        """
        target = data.get("target", "all")
        room   = data.get("room", "all")

        sm       = self._tcp.state_manager
        snapshot = sm.get_snapshot("all")

        # home1(서보/온도) + home2(LED/서보) 상태 병합
        home1_state = snapshot.get(DEVICE_HOME1, {}).get("state", {})
        home2_state = snapshot.get(DEVICE_HOME2, {}).get("state", {})
        # 구버전 하위 호환
        if not home1_state and not home2_state:
            home1_state = snapshot.get(DEVICE_HOME, {}).get("state", {})
        home_state = {**home2_state, **home1_state}  # home1이 우선 (servo/temp)

        if not home_state:
            msg = "스마트홈이 연결되어 있지 않습니다."
            return _json.dumps({
                "type": "cmd_result", "status": "status",
                "msg": msg, "tts_response": msg, "status_data": {},
            }, ensure_ascii=False)

        music = snapshot.get("_music", {})

        # room별 상태 분리
        status_data = {}
        for r in ALL_ROOMS:
            if room != "all" and r != room:
                continue

            label    = ROOM_LABEL[r]
            led_pin  = ROOM_LED_PIN[r]
            servo_pin = ROOM_SERVO_PIN.get(r)
            r_state  = {}
            parts    = []

            if target in ("led", "all"):
                led_val = home_state.get(f"led_{led_pin}")
                if led_val is not None:
                    r_state[f"led_{led_pin}"] = led_val
                    parts.append("전등 켜짐" if led_val == 1 else "전등 꺼짐")

            if target in ("servo", "all") and servo_pin:
                servo_val = home_state.get(f"servo_{servo_pin}")
                if servo_val is not None:
                    r_state[f"servo_{servo_pin}"] = servo_val
                    if r == "garage":
                        parts.append("차고문 열림" if servo_val > 0 else "차고문 닫힘")
                    elif r == "entrance":
                        parts.append("현관문 열림" if servo_val > 0 else "현관문 닫힘")
                    elif r == "bedroom":
                        parts.append("커튼 열림" if servo_val > 0 else "커튼 닫힘")

            if parts:
                status_data[r] = {"label": label, "state": r_state, "summary": parts}

        # 음악 상태
        if target in ("all", "music") and music:
            music_desc = (
                f"거실 음악 재생 중: {music.get('title', '')}"
                if music.get("playing") else "거실 음악 정지"
            )
            status_data["_music"] = {
                "label": "거실 음악", "state": music, "summary": [music_desc]
            }

        tts_out = self._build_status_sentence(status_data) if status_data else "현재 상태 정보가 없습니다."

        logger.info(f"[Router] status 조회 ({len(status_data)}개 공간): {tts_out[:80]}")

        return _json.dumps({
            "type":         "cmd_result",
            "status":       "status",
            "msg":          f"상태 조회 완료 ({len(status_data)}개 공간)",
            "tts_response": tts_out,
            "status_data":  status_data,
            "pir_mode":     self._current_pir_mode,
        }, ensure_ascii=False)

    def _build_status_sentence(self, status_data: dict) -> str:
        """
        v2: room 키 기반 자연어 문장 생성
        공간 순서: 거실 → 침실 → 차고 → 현관 → 욕실 → 음악
        """
        ROOM_ORDER = ["living", "bedroom", "garage", "entrance", "bathroom", "_music"]

        def led_phrase(val) -> str:
            return "전등은 켜져 있어요" if val == 1 else "전등은 꺼져 있어요"

        def servo_phrase(room, val) -> str:
            if room == "garage":
                return "차고문은 열려 있어요" if val > 0 else "차고문은 닫혀 있어요"
            elif room == "entrance":
                return "현관문은 열려 있어요" if val > 0 else "현관문은 닫혀 있어요"
            elif room == "bedroom":
                return "커튼은 열려 있어요" if val > 0 else "커튼은 닫혀 있어요"
            return f"서보는 {val}도예요"

        def music_phrase(state: dict) -> str:
            if state.get("playing"):
                title = state.get("title", "")
                return "음악은 재생 중이에요." + (f" {title}." if title else "")
            return "음악은 재생 중이 아니에요."

        sentences = []
        for room in ROOM_ORDER:
            info = status_data.get(room)
            if not info:
                continue

            label = info["label"]
            state = info["state"]

            if room == "_music":
                sentences.append(music_phrase(state))
                continue

            from protocol.schema import ROOM_LED_PIN, ROOM_SERVO_PIN
            led_pin   = ROOM_LED_PIN.get(room)
            servo_pin = ROOM_SERVO_PIN.get(room)

            phrases = []
            if led_pin is not None:
                led_val = state.get(f"led_{led_pin}")
                if led_val is not None:
                    phrases.append(led_phrase(led_val))
            if servo_pin is not None:
                servo_val = state.get(f"servo_{servo_pin}")
                if servo_val is not None:
                    phrases.append(servo_phrase(room, servo_val))

            if not phrases:
                continue

            if len(phrases) == 1:
                sentence = f"{label} {phrases[0]}."
            elif len(phrases) == 2:
                first = phrases[0].replace("있어요", "있고")
                sentence = f"{label} {first}, {phrases[1]}."
            else:
                first = phrases[0].replace("있어요", "있고")
                rest  = " ".join(p + "." for p in phrases[2:])
                sentence = f"{label} {first}, {phrases[1]}. {rest}".strip()

            sentences.append(sentence)

        return " ".join(sentences) if sentences else "현재 상태 정보가 없습니다."

    async def _handle_music(self, data: dict) -> str:
        """
        music 명령 → WebSocket 브로드캐스트
        브라우저 JS의 ytPlayer를 직접 제어
        {"cmd":"music","action":"play"|"pause"|"next"|"prev"|"volume","value":<int>}
        """
        action = data.get("action", "")
        value  = data.get("value")

        valid_actions = {"play", "pause", "next", "prev", "volume"}
        if action not in valid_actions:
            return ws_cmd_result("fail", f"알 수 없는 music action: {action}")

        # WS 메시지 생성
        msg = {"type": "music_control", "action": action}
        if action == "volume" and value is not None:
            msg["value"] = int(value)

        # WebSocket 브로드캐스트 (TCP 불필요)
        await self._tcp.ws_broadcast(_json.dumps(msg, ensure_ascii=False))

        action_label = {
            "play": "▶ 재생", "pause": "⏸ 정지",
            "next": "⏭ 다음 곡", "prev": "⏮ 이전 곡",
            "volume": f"🔊 볼륨 {value}%",
        }.get(action, action)

        logger.info(f"[Router] 거실 음악 제어: {action_label}")

        # DB 로그 (SR-3.1)
        if self._db:
            self._db.log("music_control", "command_router",
                         f"거실 음악 {action_label}",
                         room="living",
                         detail={"action": action, "value": value})

        # tts_response 있으면 결과에 포함
        tts = data.get("tts_response")
        if tts:
            return _json.dumps({
                "type": "cmd_result",
                "status": "ok",
                "msg": f"거실 음악 {action_label}",
                "tts_response": tts,
            }, ensure_ascii=False)

        return ws_cmd_result("ok", f"거실 음악 {action_label}")

    async def _handle_music_state(self, data: dict) -> str:
        """
        브라우저 ytPlayer 이벤트 → UnifiedStateManager.update_music_state() (v1.6)

        수신 형식 (index.html 에서 전송):
          {"type": "music_state", "action": "play"|"pause"|"stop",
           "title": "곡명", "genre": "장르", "volume": 70}

        action 별 처리:
          play   → playing=True,  title/genre/volume 업데이트
          pause  → playing=False, title 유지
          stop   → playing=False, title=""
          volume → volume만 업데이트
          track  → title/genre만 업데이트 (곡 변경 알림)
        """
        action = data.get("action", "")
        title  = data.get("title")
        genre  = data.get("genre")
        volume = data.get("volume")

        usm = self._tcp.unified_state_manager

        # 웹 클라이언트 수 동기화 (ws_hub 있을 때)
        if self._ws_hub is not None:
            usm.update_web_clients(self._ws_hub.connected_count)

        if action == "play":
            usm.update_music_state(playing=True, title=title, genre=genre, volume=volume)
            logger.info(f"[Router] 음악 상태: ▶ 재생 | title='{title}' volume={volume}")

        elif action == "pause":
            usm.update_music_state(playing=False)
            logger.info("[Router] 음악 상태: ⏸ 일시정지")

        elif action == "stop":
            usm.update_music_state(playing=False, title="", genre="")
            logger.info("[Router] 음악 상태: ⏹ 정지")

        elif action == "volume":
            usm.update_music_state(volume=volume)
            logger.info(f"[Router] 음악 상태: 🔊 볼륨={volume}")

        elif action == "track":
            # 곡 변경 (play/pause 상태 유지, title/genre만 갱신)
            usm.update_music_state(title=title, genre=genre)
            logger.info(f"[Router] 음악 상태: 🎵 트랙 변경 | title='{title}'")

        else:
            logger.warning(f"[Router] music_state 알 수 없는 action: {action}")
            return ws_cmd_result("fail", f"unknown music_state action: {action}")

        # 응답은 no-op (브라우저에 따로 알릴 필요 없음)
        return ws_cmd_result("ok", f"music_state 반영: {action}")

    async def _handle_manual_trigger(self) -> str:
        """
        WS manual_trigger 처리 - STTEngine.activate() 호출
        버튼 클릭 시 WS 경로로 즉시 LISTENING 전환
        """
        try:
            from server.main import app
            stt = getattr(app.state, 'stt_engine', None)
        except Exception:
            stt = None
        if stt is None:
            return ws_cmd_result("warn", "STTEngine 비활성화")
        if stt.state != "IDLE":
            return ws_cmd_result("ok", f"STT 이미 활성화: {stt.state}")
        stt.activate()
        logger.info("[Router] WS 버튼 트리거 → STT LISTENING 활성화")
        return ws_cmd_result("ok", "STT LISTENING 활성화")

    async def _handle_audio_chunk(self, data: dict) -> None:
        """
        브라우저 오디오 청크 → STTEngine.feed_audio (wake_source=browser 시)
        """
        import base64
        raw = data.get("data", "")
        if not raw:
            return
        try:
            chunk = base64.b64decode(raw)
        except Exception:
            return
        try:
            from server.main import app
            stt = getattr(app.state, 'stt_engine', None)
            if stt and getattr(stt, 'wake_source', 'server') == "browser":
                stt.feed_audio(chunk)
        except Exception:
            pass

    async def _handle_voice(self, text: str) -> str:
        """
        음성 텍스트 → LLM 파싱 → 명령 실행

        v1.2: cmd=None 자유 대화 분기 처리
          - LLM이 {"cmd": None, "tts_response": "..."} 반환 시
            execute() 우회 → tts_response만 담아 반환
        """
        # STT 오인식 교정 (LLM/fallback 공통 적용)
        normalized = self._normalize_stt(text)
        if normalized != text:
            logger.info(f"[Router] STT 정규화: '{text}' → '{normalized}'")
        text = normalized

        if self._llm is None:
            logger.warning(f"[Router] LLM 비활성화 — Ollama 실행 필요: '{text}'")
            return ws_cmd_result("warn", "Ollama가 실행되지 않았습니다. ollama serve 후 다시 시도해 주세요.")

        logger.info(f"[Router] LLM 파싱 요청: {text}")
        data = await self._llm.parse(text)

        if not data:
            logger.warning(f"[Router] 명령 파싱 실패: '{text}'")
            return ws_cmd_result("unknown", f"명령을 이해하지 못했습니다: '{text}'")

        logger.info(f"[Router] 음성 명령 파싱: '{text}' → cmd={data.get('cmd')} action={data.get('action')}")

        # DB 로그: LLM 파싱 결과 (SR-3.1)
        if self._db:
            self._db.log("llm_parse", "command_router",
                         f"LLM 파싱: '{text}' → cmd={data.get('cmd')}",
                         detail={"input": text, "output": data})

        # ── v1.2: cmd=None 자유 대화 처리 ───────────────────────────
        # LLM이 IoT 명령 없이 tts_response만 반환한 경우
        # execute() → validate_command() 에서 'missing cmd' 오류 방지
        if data.get("cmd") is None:
            tts = data.get("tts_response", "")
            logger.info(f"[Router] 자유 대화 응답: '{tts[:30]}'")
            return _json.dumps({
                "type": "cmd_result",
                "status": "conversation",
                "msg": "자유 대화 응답",
                "tts_response": tts,
                "original_text": text,
                "display_text": text,  # 자유 대화는 원문 그대로
            }, ensure_ascii=False)

        result = await self.execute(data)
        return self._wrap_voice_result(result, text, data)

    def _wrap_voice_result(self, result: str, original_text: str, parsed_data: dict) -> str:
        """
        voice_text 처리 결과에 original_text, display_text 추가.
        - 성공 시: display_text = LLM 해석 결과 (오인식 교정)
        - 실패/unknown 시: display_text 없음 → 프론트는 original_text 사용
        - ESP32 미연결 시에도 UI 반영을 위해 cmd/room/state/angle 항상 포함
        """
        try:
            obj = _json.loads(result)
        except Exception:
            return result
        if obj.get("type") != "cmd_result":
            return result
        obj["original_text"] = original_text
        status = obj.get("status", "")
        if status == "ok":
            display = self._cmd_to_display_text(parsed_data)
            if display:
                obj["display_text"] = display
        # ESP32 미연결(fail) 시에도 UI에 의도된 상태 반영
        cmd = parsed_data.get("cmd")
        if cmd:
            obj["cmd"] = cmd
            if cmd in ("all_on", "all_off"):
                obj["state"] = "on" if cmd == "all_on" else "off"
            elif cmd == "led" and "room" in parsed_data and "state" in parsed_data:
                obj["room"] = parsed_data.get("room")
                obj["state"] = parsed_data.get("state")
            elif cmd == "servo" and "room" in parsed_data and parsed_data.get("angle") is not None:
                obj["room"] = parsed_data.get("room")
                obj["angle"] = parsed_data.get("angle")
            elif cmd == "set_bathroom_temp" and parsed_data.get("value") is not None:
                obj["value"] = parsed_data.get("value")
            elif cmd == "heating" and "room" in parsed_data and "state" in parsed_data:
                obj["room"] = parsed_data.get("room")
                obj["state"] = parsed_data.get("state")
        return _json.dumps(obj, ensure_ascii=False)

    def _resolve_device(self, data: dict) -> Optional[str]:
        """
        v3: esp32_home1 / esp32_home2 이중 구성
          - led          → esp32_home2 (LED 5개)
          - servo        → room=bedroom → esp32_home1 (침실 커튼)
                           room=garage/entrance → esp32_home2
          - seg7         → esp32_home1 (욕실 7세그)
          - 명시된 device_id가 있으면 그대로 사용
          - 구버전 esp32_home 연결 시 하위 호환 지원
        """
        if data.get("device_id") == "all":
            return None

        # 명시적 device_id 지정 (구버전 "esp32_home" 은 legacy로 무시 → cmd+room 라우팅)
        explicit = data.get("device_id", "")
        if explicit and explicit not in ("all", DEVICE_HOME):
            return explicit

        cmd  = data.get("cmd", "")
        room = data.get("room", "")

        # LED → home2
        if cmd == CMD_LED:
            return DEVICE_HOME2

        # 서보: 침실 커튼 → home1 / 차고·현관 → home2
        if cmd == CMD_SERVO:
            if room == "bedroom":
                return DEVICE_HOME1
            return DEVICE_HOME2

        # 7세그 → home1 (욕실)
        if cmd == CMD_SEG7:
            return DEVICE_HOME1

        # 구버전 esp32_home 연결 시 하위 호환
        if self._tcp.get_device(DEVICE_HOME):
            return DEVICE_HOME

        # 기본: home1 (seg7/heating/온도 관련)
        return DEVICE_HOME1

    def _build_payload(self, data: dict) -> Optional[bytes]:
        """
        v2: room → pin 자동 매핑
          - room 있으면 ROOM_LED_PIN/ROOM_SERVO_PIN 에서 핀 결정
          - room 없으면 data['pin'] 사용 (기존 호환)
        """
        cmd  = data.get("cmd")
        room = data.get("room", "")

        if cmd == CMD_LED:
            pin = ROOM_LED_PIN.get(room) if room else data.get("pin", 2)
            if pin is None:
                logger.warning(f"[Router] LED: room '{room}' 에 핀 없음")
                return None
            return cmd_led(pin, data["state"], room)

        elif cmd == CMD_SERVO:
            pin = ROOM_SERVO_PIN.get(room) if room else data.get("pin", 18)
            if pin is None:
                logger.warning(f"[Router] Servo: room '{room}' 에 서보 없음")
                return None
            return cmd_servo(pin, data["angle"], room)

        elif cmd == CMD_QUERY:
            return cmd_query("seg7")

        elif cmd == CMD_SEG7:
            return cmd_seg7(
                pin_clk=data.get("pin_clk", 22),
                pin_dio=data.get("pin_dio", 23),
                mode=data["mode"],
                value=data.get("value"),
            )

        return None

    def _normalize_stt(self, text: str) -> str:
        """
        Whisper STT 한국어 오인식 교정
        - 음성과 발음이 유사하지만 다르게 인식되는 단어를 올바른 형태로 치환
        - LLM/fallback 파싱 전에 공통 적용
        """
        import re as _re

        # ── 난방 관련 오인식 ──────────────────────────────────────────
        # 남방/냄방/난빵/냄빵/남빵/란방/남바/남빵 → 난방
        text = _re.sub(r'[남냄난란][방빵바]', '난방', text)
        # 난 방/반/망 켜/꺼 (STT "난방" 오인식) → 난방
        text = _re.sub(r'난\s*(방|반|망)\s*(켜|꺼|끄|키라고|켜줘|꺼줘|틀어)', r'난방 \2', text)
        text = _re.sub(r'난\s*(방|반|망)(켜|꺼|끄)', r'난방 \2', text)
        text = _re.sub(r'난방(켜)(?![줘요])', r'난방 켜', text)
        text = _re.sub(r'난방(꺼|끄)(?![줘요])', r'난방 꺼', text)

        # ── 보일러 오인식 ─────────────────────────────────────────────
        # 뵈일러/보이러/보일로/뵈일로 → 보일러
        text = _re.sub(r'뵈일[러로]', '보일러', text)
        text = _re.sub(r'보이[러를르]', '보일러', text)
        text = _re.sub(r'보일[로루]', '보일러', text)

        # ── 켜줘/켜 오인식 ───────────────────────────────────────────
        # 켜줘요/케줘/켜주 → 켜줘
        text = text.replace('켜줘요', '켜줘')
        text = text.replace('케줘', '켜줘')
        text = text.replace('켜주세요', '켜줘')
        text = text.replace('꺼주세요', '꺼줘')
        text = text.replace('꺼줘요', '꺼줘')

        # ── 커튼 오인식 ──────────────────────────────────────────────
        # 거튼/꺼튼/컨튼 → 커튼
        text = _re.sub(r'[거꺼컨][튼튼]', '커튼', text)

        # ── 침실 오인식 ──────────────────────────────────────────────
        text = text.replace('침실', '침실')  # placeholder for future
        text = text.replace('칩실', '침실')
        text = text.replace('침씰', '침실')

        # ── 욕실 오인식 ──────────────────────────────────────────────
        text = text.replace('욕씰', '욕실')
        text = text.replace('옥실', '욕실')

        # ── 거실 오인식 ──────────────────────────────────────────────
        text = text.replace('거씰', '거실')
        text = text.replace('걱실', '거실')

        # ── 현관 오인식 ──────────────────────────────────────────────
        text = text.replace('현간', '현관')
        text = text.replace('현완', '현관')

        return text

    def _cmd_to_display_text(self, data: dict) -> str:
        """
        실행된 명령을 사람이 읽기 쉬운 음성 명령 형태로 변환.
        voice command 디스플레이용 (STT 오인식 시 LLM 해석 결과 표시)
        """
        cmd = data.get("cmd", "")
        room = data.get("room", "")
        room_label = ROOM_LABEL.get(room, room) if room else ""

        if cmd == "set_bathroom_temp":
            val = data.get("value")
            if val is not None:
                return f"욕실 난방 {float(val):.0f}도로 설정해줘"
            return "욕실 난방 온도 설정해줘"
        if cmd == "heating":
            s = data.get("state", "off").lower()
            return f"욕실 난방 {'켜줘' if s == 'on' else '꺼줘'}"
        if cmd == CMD_LED:
            state = data.get("state", "on").lower()
            on_off = "켜줘" if state == "on" else "꺼줘"
            if room_label:
                return f"{room_label} 불 {on_off}"
            return f"불 {on_off}"
        if cmd == CMD_SERVO:
            angle = data.get("angle", 0)
            open_close = "열어줘" if angle >= 45 else "닫아줘"
            if room == "bedroom":
                return f"침실 커튼 {open_close}"
            if room_label:
                return f"{room_label} 문 {open_close}"
            return f"커튼 {open_close}"
        if cmd == "music":
            action = data.get("action", "play").lower()
            labels = {"play": "음악 켜줘", "pause": "음악 일시정지", "stop": "음악 꺼줘", "next": "다음 곡", "prev": "이전 곡"}
            return labels.get(action, "음악 켜줘")
        if cmd == "all_on":
            return "전체 켜줘"
        if cmd == "all_off":
            return "전체 꺼줘"
        if cmd and (cmd.startswith("pir_") or cmd.endswith("_mode")):
            mode = cmd.replace("pir_", "").replace("_mode", "")
            labels = {"away": "외출", "home": "귀가", "sleep": "취침", "wake": "기상", "dnd": "방해금지"}
            return f"{labels.get(mode, mode)} 모드"
        return ""

    # ── _simple_parse 비활성화: STT 결과는 무조건 LLM 파싱 ─────────────────
    def _simple_parse(self, text: str) -> Optional[dict]:
        """[비활성화] STT 결과는 무조건 LLM으로 파싱"""
        return None

    def _simple_parse_disabled(self, text: str) -> Optional[dict]:
        """
        [비활성화] LLM 없을 때 키워드 기반 단순 파싱 (fallback)
        v2: device_id → esp32_home, room 필드 추가
        """
        text = self._normalize_stt(text.strip())

        # room 결정
        room = None
        room_keywords = {
            "거실": "living",  "living": "living",
            "욕실": "bathroom","bathroom": "bathroom",
            "침실": "bedroom", "bedroom": "bedroom",
            "차고": "garage",  "garage": "garage",
            "현관": "entrance","entrance": "entrance",
        }
        for kw, r in room_keywords.items():
            if kw in text:
                room = r
                break

        is_all    = any(k in text for k in ["전체", "모두", "전부", "다"])
        is_status = any(k in text for k in [
            "상태", "확인", "켜져있", "꺼져있", "열려있", "닫혀있",
            "어때", "어떻게", "점검", "알려줘"
        ])

        # base: room만 포함, device_id는 _resolve_device()가 cmd+room 기반 자동 결정
        base = {}
        if room:
            base["room"] = room

        if is_status:
            target = "all"
            if any(k in text for k in ["불", "전등", "조명"]): target = "led"
            elif any(k in text for k in ["문", "서보", "커튼"]): target = "servo"
            r = room if (room and not is_all) else "all"
            return {"cmd": "status", "room": r, "target": target}

        # ── 욕실 난방 (LED/전원 키워드보다 먼저 체크) ────────────────────
        if any(k in text for k in ["난방 켜", "난방 키라고", "난방 틀어", "난방 시작", "보일러 켜", "보일러 틀어", "욕실 따뜻"]):
            return {"cmd": "heating", "room": "bathroom", "state": "on",
                    "tts_response": "욕실 난방을 켰어요."}
        if any(k in text for k in ["난방 꺼", "난방 끄", "난방 정지", "보일러 꺼", "보일러 끄"]):
            return {"cmd": "heating", "room": "bathroom", "state": "off",
                    "tts_response": "욕실 난방을 껐어요."}

        # ── 음악 (LED "꺼"보다 먼저 체크) ─────────────────────────────────
        if any(k in text for k in ["음악 틀어", "음악 틀어줘", "음악 틀어요", "음악 켜", "음악 재생", "음악 재생해", "노래 틀어", "노래 재생"]):
            return {"cmd": "music", "action": "play"}
        if any(k in text for k in ["음악 꺼", "음악 꺼줘", "음악 정지", "음악 멈춰", "노래 꺼"]):
            return {"cmd": "music", "action": "pause"}
        if any(k in text for k in ["다음 곡", "다음곡"]):
            return {"cmd": "music", "action": "next"}
        if any(k in text for k in ["이전 곡", "이전곡"]):
            return {"cmd": "music", "action": "prev"}

        if any(k in text for k in ["켜", "켜줘", "불 켜", "조명 켜"]):
            if is_all:
                return {"cmd": "all_on", "device_id": "all"}
            if not room:
                return {
                    "cmd": None,
                    "tts_response": "어떤 방의 불을 켜드릴까요? 거실, 침실, 욕실, 차고, 현관 중 말씀해주세요.",
                }
            return {**base, "cmd": CMD_LED, "state": "on"}

        is_power_off = any(k in text for k in ["꺼", "꺼줘", "전원 꺼", "다 꺼", "모두 꺼", "전체 꺼"])
        is_led_only  = any(k in text for k in ["불 꺼", "조명 꺼", "전등 꺼"])

        if is_power_off or is_led_only:
            if is_all:
                return {"cmd": "all_off", "device_id": "all"}
            if not room:
                return {"cmd": CMD_LED, "state": "off", "device_id": "all"}
            return {**base, "cmd": CMD_LED, "state": "off"}

        if any(k in text for k in ["커튼 열", "커튼 올려"]):
            return {"cmd": CMD_SERVO, "angle": 90, "room": "bedroom"}
        if any(k in text for k in ["커튼 닫", "커튼 내려"]):
            return {"cmd": CMD_SERVO, "angle": 0, "room": "bedroom"}

        if any(k in text for k in ["차고문 열", "차고 열"]):
            return {"cmd": CMD_SERVO, "angle": 90, "room": "garage"}
        if any(k in text for k in ["차고문 닫", "차고 닫"]):
            return {"cmd": CMD_SERVO, "angle": 0, "room": "garage"}

        if any(k in text for k in ["현관문 열", "현관 열"]):
            return {"cmd": CMD_SERVO, "angle": 90, "room": "entrance"}
        if any(k in text for k in ["현관문 닫", "현관 닫"]):
            return {"cmd": CMD_SERVO, "angle": 0, "room": "entrance"}

        if any(k in text for k in ["열어", "열어줘", "문 열"]) and room:
            return {**base, "cmd": CMD_SERVO, "angle": 90}
        if any(k in text for k in ["닫아", "닫아줘", "문 닫"]) and room:
            return {**base, "cmd": CMD_SERVO, "angle": 0}

        # ── PIR 모드 키워드 ───────────────────────────────────────────
        # PIR 명령은 _execute_pir_mode() 에서 pir_device(home2)로 직접 라우팅
        if any(k in text for k in ["외출", "나갈게", "나간다", "외출해"]):
            return {"cmd": "away_mode"}
        if any(k in text for k in ["귀가", "돌아왔어", "집에 왔어", "귀가했어"]):
            return {"cmd": "home_mode"}
        if any(k in text for k in ["잘게", "잠자리", "취침", "자러 갈게"]):
            return {"cmd": "sleep_mode"}
        if any(k in text for k in ["일어났어", "기상", "아침이야", "일어났다"]):
            return {"cmd": "wake_mode"}

        # ── 욕실 현재온도 조회 키워드 ─────────────────────────────────
        if any(k in text for k in ["욕실", "목욕탕"]) and any(k in text for k in ["몇 도", "몇도", "온도 알려", "온도 확인", "온도 어때", "온도야", "온도 몇"]):
            return {"cmd": "query_bathroom_temp"}

        # ── 욕실 희망온도 설정 키워드 ─────────────────────────────────
        if any(k in text for k in ["욕실", "목욕탕"]) and any(k in text for k in ["온도", "도로", "도 설정", "도 맞춰"]):
            import re as _re
            m = _re.search(r'(\d+(?:\.\d+)?)\s*도', text)
            if m:
                val = float(m.group(1))
                if 10.0 <= val <= 40.0:
                    return {"cmd": "set_bathroom_temp", "value": val}

        return None
