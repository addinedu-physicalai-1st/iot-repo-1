"""
protocol/schema.py
==================
TCP JSON 메시지 스키마 정의 및 유효성 검증

서버 ↔ ESP32 / 서버 ↔ 브라우저(WebSocket) 메시지 포맷을 중앙 관리.
모든 모듈은 이 파일의 상수/함수를 참조한다.

v2: 단일 ESP32 통합 (esp32_home)
  - DEVICE_HOME = "esp32_home" (5개 공간 통합)
  - room 필드: living/bathroom/bedroom/garage/entrance
  - ROOM_LED_PIN, ROOM_SERVO_PIN: room → GPIO 핀 자동 매핑
  - validate_command: pin 없이 room만으로 검증 가능
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Literal, Optional, Any


# ─────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────

MSG_DELIMITER = b"\n"

CMD_LED   = "led"
CMD_SERVO = "servo"
CMD_QUERY = "query"
CMD_SEG7  = "seg7"

TYPE_REGISTER = "register"
TYPE_ACK      = "ack"
TYPE_SENSOR   = "sensor"
TYPE_ERROR    = "error"

WS_DEVICE_UPDATE = "device_update"
WS_SENSOR_DATA   = "sensor_data"
WS_CMD_RESULT    = "cmd_result"
WS_DEVICE_LIST   = "device_list"

LED_ON  = "on"
LED_OFF = "off"

SENSOR_DHT22   = "dht22"
SENSOR_DS18B20 = "ds18b20"

SERVO_MIN = 0
SERVO_MAX = 180
SERVO_DOOR_CLOSE = 0
SERVO_DOOR_OPEN  = 90

# ── ESP32 디바이스 ID ─────────────────────────────────
# v2: 단일 esp32_home 통합
DEVICE_HOME = "esp32_home"

# 하위 호환용
DEVICE_GARAGE   = "esp32_garage"
DEVICE_BATHROOM = "esp32_bathroom"
DEVICE_BEDROOM  = "esp32_bedroom"
DEVICE_ENTRANCE = "esp32_entrance"
DEVICE_LIVING   = "esp32_living"

ALL_DEVICES = (DEVICE_HOME,)

# ── room 식별자 ───────────────────────────────────────
ROOM_LIVING   = "living"
ROOM_BATHROOM = "bathroom"
ROOM_BEDROOM  = "bedroom"
ROOM_GARAGE   = "garage"
ROOM_ENTRANCE = "entrance"

ALL_ROOMS = (ROOM_LIVING, ROOM_BATHROOM, ROOM_BEDROOM, ROOM_GARAGE, ROOM_ENTRANCE)

# ── room → LED 핀 매핑 ───────────────────────────────
ROOM_LED_PIN: dict[str, int] = {
    ROOM_LIVING:   2,
    ROOM_BATHROOM: 4,
    ROOM_BEDROOM:  5,
    ROOM_GARAGE:   12,
    ROOM_ENTRANCE: 13,
}

# ── room → Servo 핀 매핑 (없는 공간 = None) ──────────
ROOM_SERVO_PIN: dict[str, Optional[int]] = {
    ROOM_LIVING:   None,
    ROOM_BATHROOM: None,
    ROOM_BEDROOM:  14,
    ROOM_GARAGE:   15,
    ROOM_ENTRANCE: 16,
}

# ── room 한국어 라벨 ─────────────────────────────────
ROOM_LABEL: dict[str, str] = {
    ROOM_LIVING:   "거실",
    ROOM_BATHROOM: "욕실",
    ROOM_BEDROOM:  "침실",
    ROOM_GARAGE:   "차고",
    ROOM_ENTRANCE: "현관",
}


# ─────────────────────────────────────────────
# 서버 → ESP32 명령 메시지
# ─────────────────────────────────────────────

@dataclass
class CmdLed:
    pin: int
    state: Literal["on", "off"]
    cmd: str = CMD_LED
    room: str = ""

    def validate(self) -> bool:
        return isinstance(self.pin, int) and self.pin >= 0 and self.state in (LED_ON, LED_OFF)


@dataclass
class CmdServo:
    pin: int
    angle: int
    cmd: str = CMD_SERVO
    room: str = ""

    def validate(self) -> bool:
        return isinstance(self.pin, int) and self.pin >= 0 and SERVO_MIN <= self.angle <= SERVO_MAX


@dataclass
class CmdQuery:
    sensor: str = "seg7"
    cmd: str = CMD_QUERY

    def validate(self) -> bool:
        return True


@dataclass
class CmdSeg7:
    pin_clk: int
    pin_dio: int
    mode: Literal["temp", "humidity", "number", "off", "current_temp", "target_temp"]
    value: Optional[float] = None
    cmd: str = CMD_SEG7

    def validate(self) -> bool:
        if self.mode not in ("temp", "humidity", "number", "off", "current_temp", "target_temp"):
            return False
        if self.mode != "off" and self.value is None:
            return False
        return isinstance(self.pin_clk, int) and isinstance(self.pin_dio, int)


# ─────────────────────────────────────────────
# ESP32 → 서버 응답 메시지
# ─────────────────────────────────────────────

@dataclass
class MsgRegister:
    device_id: str
    caps: list[str]
    type: str = TYPE_REGISTER

    def validate(self) -> bool:
        return bool(self.device_id) and isinstance(self.caps, list)


@dataclass
class MsgAck:
    cmd: str
    status: Literal["ok", "fail"]
    type: str = TYPE_ACK

    def validate(self) -> bool:
        return self.cmd in (CMD_LED, CMD_SERVO, CMD_QUERY, CMD_SEG7)


@dataclass
class MsgSensor:
    device: str
    type: str = TYPE_SENSOR
    temp: Optional[float] = None
    humidity: Optional[float] = None

    def validate(self) -> bool:
        return bool(self.device)


@dataclass
class MsgError:
    msg: str
    type: str = TYPE_ERROR

    def validate(self) -> bool:
        return bool(self.msg)


# ─────────────────────────────────────────────
# WebSocket 브로드캐스트 메시지
# ─────────────────────────────────────────────

@dataclass
class WsDeviceUpdate:
    device_id: str
    state: dict[str, Any]
    type: str = WS_DEVICE_UPDATE


@dataclass
class WsSensorData:
    device_id: str
    type: str = WS_SENSOR_DATA
    temp: Optional[float] = None
    humidity: Optional[float] = None
    room: Optional[str] = None


@dataclass
class WsCmdResult:
    status: Literal["ok", "fail", "unknown"]
    msg: str
    type: str = WS_CMD_RESULT


@dataclass
class WsDeviceList:
    devices: list[dict[str, Any]]
    type: str = WS_DEVICE_LIST


# ─────────────────────────────────────────────
# 직렬화 / 역직렬화 유틸
# ─────────────────────────────────────────────

def to_bytes(data: dataclass) -> bytes:
    d = asdict(data)
    # room="" 인 경우 TCP 전송에서 제외
    if "room" in d and not d["room"]:
        del d["room"]
    return (json.dumps(d, ensure_ascii=False) + "\n").encode("utf-8")


def to_json_str(data: dataclass) -> str:
    return json.dumps(asdict(data), ensure_ascii=False)


def parse_tcp_message(raw: str) -> dict:
    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return {}


def parse_ws_message(raw: str) -> dict:
    return parse_tcp_message(raw)


def validate_esp32_message(data: dict) -> tuple[bool, str]:
    msg_type = data.get("type")
    if not msg_type:
        return False, "missing 'type' field"

    if msg_type == TYPE_REGISTER:
        if not data.get("device_id"):
            return False, "register: missing device_id"
        if not isinstance(data.get("caps"), list):
            return False, "register: caps must be a list"

    elif msg_type == TYPE_ACK:
        if not data.get("cmd"):
            return False, "ack: missing cmd"
        if data.get("status") not in ("ok", "fail"):
            return False, "ack: status must be 'ok' or 'fail'"

    elif msg_type == TYPE_SENSOR:
        if not data.get("device"):
            return False, "sensor: missing device"

    elif msg_type == TYPE_ERROR:
        if not data.get("msg"):
            return False, "error: missing msg"

    else:
        return False, f"unknown type: {msg_type}"

    return True, ""


def validate_command(data: dict) -> tuple[bool, str]:
    """
    v2: room 필드 기반 검증
      - room 있으면 pin 없어도 OK (ROOM_LED_PIN/ROOM_SERVO_PIN 에서 자동 결정)
      - room 없으면 기존 pin 방식 유지
    """
    cmd  = data.get("cmd")
    room = data.get("room", "")

    if not cmd:
        return False, "missing 'cmd' field"

    if cmd == CMD_LED:
        if room and room not in ALL_ROOMS:
            return False, f"led: unknown room '{room}'"
        if not room and not isinstance(data.get("pin"), int):
            return False, "led: pin must be int (or provide 'room')"
        if data.get("state") not in (LED_ON, LED_OFF):
            return False, "led: state must be 'on' or 'off'"

    elif cmd == CMD_SERVO:
        if room and room not in ALL_ROOMS:
            return False, f"servo: unknown room '{room}'"
        if not room and not isinstance(data.get("pin"), int):
            return False, "servo: pin must be int (or provide 'room')"
        angle = data.get("angle")
        if not isinstance(angle, int) or not (SERVO_MIN <= angle <= SERVO_MAX):
            return False, f"servo: angle must be int {SERVO_MIN}~{SERVO_MAX}"

    elif cmd == CMD_QUERY:
        pass  # room 기반 자동 처리

    elif cmd == CMD_SEG7:
        if data.get("mode") not in ("temp", "humidity", "number", "off", "current_temp", "target_temp"):
            return False, "seg7: mode must be 'temp'|'humidity'|'number'|'off'|'current_temp'|'target_temp'"
        if data.get("mode") != "off" and data.get("value") is None:
            return False, "seg7: value required when mode != 'off'"

    else:
        return False, f"unknown cmd: {cmd}"

    return True, ""


# ─────────────────────────────────────────────
# 빠른 빌더 함수
# ─────────────────────────────────────────────

def cmd_led(pin: int, state: str, room: str = "") -> bytes:
    return to_bytes(CmdLed(pin=pin, state=state, room=room))

def cmd_servo(pin: int, angle: int, room: str = "") -> bytes:
    return to_bytes(CmdServo(pin=pin, angle=angle, room=room))

def cmd_query(sensor: str = "seg7") -> bytes:
    return to_bytes(CmdQuery(sensor=sensor))

def ws_cmd_result(status: str, msg: str) -> str:
    return to_json_str(WsCmdResult(status=status, msg=msg))

def ws_sensor_data(device_id: str, temp: float = None, humidity: float = None, room: str = None) -> str:
    return to_json_str(WsSensorData(device_id=device_id, temp=temp, humidity=humidity, room=room))

def ws_device_update(device_id: str, state: dict) -> str:
    return to_json_str(WsDeviceUpdate(device_id=device_id, state=state))

def ws_device_list(devices: list) -> str:
    return to_json_str(WsDeviceList(devices=devices))

def cmd_seg7(pin_clk: int, pin_dio: int, mode: str, value: float = None) -> bytes:
    return to_bytes(CmdSeg7(pin_clk=pin_clk, pin_dio=pin_dio, mode=mode, value=value))

def cmd_seg7_temp(pin_clk: int, pin_dio: int, value: float) -> bytes:
    return cmd_seg7(pin_clk, pin_dio, "temp", value)

def cmd_seg7_humidity(pin_clk: int, pin_dio: int, value: float) -> bytes:
    return cmd_seg7(pin_clk, pin_dio, "humidity", value)

def cmd_seg7_off(pin_clk: int, pin_dio: int) -> bytes:
    return cmd_seg7(pin_clk, pin_dio, "off")
