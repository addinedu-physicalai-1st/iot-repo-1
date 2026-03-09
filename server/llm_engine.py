"""
server/llm_engine.py
====================
Ollama LLM 연동 - 자연어 → JSON 명령 파싱  v1.8

역할:
  - Ollama REST API 호출 (로컬 http://localhost:11434)
  - 한국어 자연어 → ESP32 JSON 명령 변환
  - 응답 JSON 유효성 검증 및 오류 처리
  - 다중 명령 파싱 지원 ("침실 불 켜고 서보 90도")

v1.8 변경사항:
  - SYSTEM_PROMPT: 욕실 현재온도 조회 명령 추가 (query_bathroom_temp)
  - parse(): query_bathroom_temp validate 우회 처리 추가
v1.9:
  - SYSTEM_PROMPT: 욕실 희망온도 설정 명령 추가 (set_bathroom_temp)
  - parse(): set_bathroom_temp validate 우회 처리 추가
v1.8:
  - SYSTEM_PROMPT: PIR 모드 4종 명령 추가
    · "외출해 / 나갈게"        → cmd: "away_mode"  (PIR guard + 전체 조명 OFF)
    · "귀가했어 / 집에 왔어"   → cmd: "home_mode"  (PIR presence)
    · "잘게 / 취침"            → cmd: "sleep_mode" (PIR guard, 거실 감시)
    · "일어났어 / 기상"        → cmd: "wake_mode"  (PIR presence)
  - parse(): PIR 모드 4종 validate 우회 처리 추가

v1.7 변경사항:
  - SYSTEM_PROMPT: all_off / all_on 명령 추가
    · "전체/모두/다 꺼줘" → cmd: "all_off" (전등 + 음악 동시)
    · "전체/모두/다 켜줘" → cmd: "all_on"  (전등 + 음악 동시)
  - parse(): all_off / all_on validate 우회 처리 추가

v1.6 변경사항:
  - status 명령 추가: 디바이스 상태 조회
    {"cmd": "status", "device_id": "<id>"|"all", "target": "led"|"servo"|"all"}
  - SYSTEM_PROMPT: 상태 조회 패턴 예시 추가 (차고 문 상태, 외출 점검 등)
  - parse(): status 명령 validate 우회 처리

v1.5 변경사항:
  - SYSTEM_PROMPT 날씨 예시 tts_response 문구 수정
    "인터넷 연결이 없어서" → "실시간 날씨 정보를 제공하기 어렵지만"

v1.4 변경사항:
  - tts_response 필드 추가: LLM이 JSON 내에 음성 답변 텍스트 직접 생성
  - SYSTEM_PROMPT: tts_response 필드 규칙 추가
  - parse(): tts_response 파싱 후 반환 dict에 포함

v1.3 변경사항:
  - music 명령 지원 추가: play/pause/next/prev/volume
  - SYSTEM_PROMPT: living_room 디바이스 및 music 스키마 추가
  - parse(): music 명령 validate 우회 처리

지원 모델:
  - Qwen2.5:7b (기본)
  - EXAONE 3.5
  - 기타 Ollama 지원 모델

사용:
  from server.llm_engine import LLMEngine
  engine = LLMEngine(model="qwen2.5:7b", host="http://localhost:11434")
  cmd = await engine.parse("침실 불 켜줘")
  # → {"cmd": "led", "pin": 2, "state": "on", "device_id": "esp32_bedroom",
  #    "tts_response": "침실 전등을 켰어요."}
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from protocol.schema import (
    validate_command,
    CMD_LED, CMD_SERVO, CMD_QUERY, CMD_SEG7,
    ALL_DEVICES,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 시스템 프롬프트
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are an IoT command parser for a Korean smart home system.
Convert Korean natural language into a JSON command. Respond ONLY with valid JSON. No explanation, no markdown.

System: Single ESP32 (device_id: "esp32_home") controls all 5 rooms via "room" field.

Rooms and capabilities:
  - living   : 거실  (led)
  - bathroom : 욕실  (led, seg7)
  - bedroom  : 침실  (led, servo=커튼)
  - garage   : 차고  (led, servo=차고문)
  - entrance : 현관  (led, servo=현관문)

Available commands:
  {"cmd": "led",    "device_id": "esp32_home", "room": "<room>", "state": "on"|"off"}
  {"cmd": "servo",  "device_id": "esp32_home", "room": "<room>", "angle": <0-180>}
  {"cmd": "seg7",   "device_id": "esp32_home", "mode": "number", "value": <float>}
  {"cmd": "music",  "action": "play"|"pause"|"next"|"prev"|"volume", "value": <0-100 (volume only)>}
  {"cmd": "status", "device_id": "esp32_home", "room": "<room>"|"all", "target": "led"|"servo"|"all"}
  {"cmd": "led",    "device_id": "all", "state": "on"|"off"}  ← 전체 전등 제어
  {"cmd": "all_off", "device_id": "all"}  ← 전체 전등 + 음악 동시 끄기
  {"cmd": "all_on",  "device_id": "all"}  ← 전체 전등 + 음악 동시 켜기
  {"cmd": "away_mode",  "device_id": "esp32_home"}  ← 외출 (PIR 방범 ON + 전체 조명 OFF)
  {"cmd": "home_mode",  "device_id": "esp32_home"}  ← 귀가 (PIR 재실 감지 ON)
  {"cmd": "sleep_mode", "device_id": "esp32_home"}  ← 취침 (PIR 거실 방범 ON)
  {"cmd": "wake_mode",  "device_id": "esp32_home"}  ← 기상 (PIR 재실 감지 ON)
  {"cmd": "set_bathroom_temp",   "device_id": "esp32_home", "value": <10.0-40.0>}  ← 욕실 희망온도 설정
  {"cmd": "query_bathroom_temp", "device_id": "esp32_home"}                         ← 욕실 현재온도 조회 (센서 없음 → 희망온도 안내)

Servo angle presets:
  열어 / 열림 → angle: 90
  닫아 / 닫힘 → angle: 0

Music action keywords:
  재생 / 틀어 / 켜줘 (음악) → action: "play"
  정지 / 멈춰 / 꺼줘 (음악) → action: "pause"
  다음 곡 / 다음            → action: "next"
  이전 곡 / 이전            → action: "prev"
  볼륨 / 소리 크게          → action: "volume", value: 80
  볼륨 / 소리 작게          → action: "volume", value: 30

Rules:
  1. Respond ONLY with valid JSON (single command).
  2. Always include "device_id": "esp32_home" for device commands (except "all" broadcast).
  3. Always include "room" field for single-room commands.
  4. For whole-home LED: use "device_id": "all" without "room".
  4-1. For whole-home power OFF (전등+음악 동시): use cmd: "all_off", "device_id": "all".
  4-2. For whole-home power ON  (전등+음악 동시): use cmd: "all_on",  "device_id": "all".
  4-3. PIR mode keywords:
     - 외출 / 나갈게 / 외출해           → cmd: "away_mode",  device_id: "esp32_home"
     - 귀가 / 돌아왔어 / 집에 왔어      → cmd: "home_mode",  device_id: "esp32_home"
     - 잘게 / 취침 / 자러 갈게          → cmd: "sleep_mode", device_id: "esp32_home"
     - 일어났어 / 기상 / 아침이야       → cmd: "wake_mode",  device_id: "esp32_home"
  4-4. Bathroom temperature keywords:
     - 욕실 / 목욕탕 + 온도 / 온도 설정 / 도로 맞춰줘 + <숫자>  → cmd: "set_bathroom_temp", value: <숫자>
     - 예: "욕실 25도로 설정해줘" / "욕실 온도 28도" / "목욕탕 22.5도"
  4-5. Bathroom temperature query keywords (숫자 없이 온도 물어보는 경우):
     - 욕실 / 목욕탕 + 온도 몇 도 / 몇 도야 / 온도 알려줘 / 온도 확인  → cmd: "query_bathroom_temp"
     - 예: "욕실 온도 몇 도야?" / "욕실 지금 몇 도?" / "목욕탕 온도 알려줘"
  5. If the command is completely unknown: {"cmd": "unknown", "msg": "<reason in Korean>", "tts_response": "<friendly Korean apology>"}
  6. Always include "tts_response" field with a short, natural Korean spoken response.
  7. Status query keywords → use cmd: "status":
     - "<공간> 상태 알려줘 / 확인해줘 / 어때?" → room: "<room>", target: "all"
     - "<공간> 불 켜져있어? / 전등 상태?"      → room: "<room>", target: "led"
     - "<공간> 문 열려있어? / 서보 상태?"       → room: "<room>", target: "servo"
     - "전체 상태 / 외출 점검 / 다 확인해줘"   → room: "all",    target: "all"

tts_response rules:
  - Device control success : 30자 이내, 구어체 (예: "침실 전등을 켰어요.", "차고문 열었어요.")
  - Unknown command        : 친근한 사과 (예: "죄송해요, 잘 못 들었어요.")
  - Status command         : 상태 확인 알림 (예: "차고 상태를 확인할게요.")
  - Music command          : 음악 동작 안내 (예: "음악을 재생할게요.")
  - Conversation (no cmd)  : 자유로운 한국어 답변

Examples:
  "침실 불 켜줘"              → {"cmd":"led","device_id":"esp32_home","room":"bedroom","state":"on","tts_response":"침실 전등을 켰어요."}
  "차고문 열어줘"              → {"cmd":"servo","device_id":"esp32_home","room":"garage","angle":90,"tts_response":"차고문 열었어요."}
  "현관 문 닫아"               → {"cmd":"servo","device_id":"esp32_home","room":"entrance","angle":0,"tts_response":"현관문 닫았어요."}
  "커튼 열어줘"                → {"cmd":"servo","device_id":"esp32_home","room":"bedroom","angle":90,"tts_response":"커튼을 열었어요."}
  "욕실 세그먼트 꺼줘"         → {"cmd":"seg7","device_id":"esp32_home","pin_clk":22,"pin_dio":23,"mode":"off","tts_response":"욕실 디스플레이를 껐어요."}
  "전체 불 꺼줘"               → {"cmd":"led","device_id":"all","state":"off","tts_response":"전체 전등을 껐어요."}
  "전체 꺼줘 / 모두 꺼줘 / 다 꺼줘"   → {"cmd":"all_off","device_id":"all","tts_response":"전체 전등과 음악을 껐어요."}
  "전체 켜줘 / 모두 켜줘 / 다 켜줘"   → {"cmd":"all_on","device_id":"all","tts_response":"전체 전등과 음악을 켰어요."}
  "거실 음악 틀어줘"           → {"cmd":"music","action":"play","tts_response":"음악을 재생할게요."}
  "음악 꺼줘"                  → {"cmd":"music","action":"pause","tts_response":"음악을 정지할게요."}
  "다음 곡"                    → {"cmd":"music","action":"next","tts_response":"다음 곡으로 넘길게요."}
  "볼륨 크게"                  → {"cmd":"music","action":"volume","value":80,"tts_response":"볼륨을 키울게요."}
  "차고 문 닫혀있니?"           → {"cmd":"status","device_id":"esp32_home","room":"garage","target":"servo","tts_response":"차고문 상태를 확인할게요."}
  "현관 불 켜져 있어?"          → {"cmd":"status","device_id":"esp32_home","room":"entrance","target":"led","tts_response":"현관 전등 상태를 확인할게요."}
  "침실 상태 확인해줘"          → {"cmd":"status","device_id":"esp32_home","room":"bedroom","target":"all","tts_response":"침실 상태를 확인할게요."}
  "외출 전 전체 점검해줘"       → {"cmd":"status","device_id":"esp32_home","room":"all","target":"all","tts_response":"전체 상태를 점검할게요."}
  "외출해 / 나갈게"             → {"cmd":"away_mode","device_id":"esp32_home","tts_response":"외출 모드로 설정했어요. 안전하게 다녀오세요!"}
  "귀가했어 / 집에 왔어"        → {"cmd":"home_mode","device_id":"esp32_home","tts_response":"어서 오세요! 재실 감지 모드로 전환했어요."}
  "잘게 / 취침할게"             → {"cmd":"sleep_mode","device_id":"esp32_home","tts_response":"잘 자요! 거실 방범 모드를 켰어요."}
  "일어났어 / 기상"             → {"cmd":"wake_mode","device_id":"esp32_home","tts_response":"좋은 아침이에요! 재실 감지 모드로 전환했어요."}
  "욕실 온도 몇 도야?"            → {"cmd":"query_bathroom_temp","device_id":"esp32_home","tts_response":"현재 온도 센서가 없어요. 설정된 희망 온도를 알려드릴게요."}
  "욕실 25도로 설정해줘"          → {"cmd":"set_bathroom_temp","device_id":"esp32_home","value":25.0,"tts_response":"욕실 온도를 25도로 설정했어요."}
  "욕실 온도 28.5도"              → {"cmd":"set_bathroom_temp","device_id":"esp32_home","value":28.5,"tts_response":"욕실 온도를 28.5도로 설정했어요."}
  "오늘 날씨 어때"              → {"cmd":null,"tts_response":"날씨는 날씨 앱에서 확인해보세요!"}
"""


# ─────────────────────────────────────────────
# LLM 엔진
# ─────────────────────────────────────────────

class LLMEngine:
    """
    Ollama LLM 기반 자연어 → JSON 명령 파싱 엔진

    Parameters
    ----------
    model   : Ollama 모델명 (기본: qwen2.5:7b)
    host    : Ollama 서버 주소 (기본: http://localhost:11434)
    timeout : 요청 타임아웃 (초)
    """

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        host: str = "http://localhost:11434",
        timeout: float = 30.0,
    ):
        self.model   = model
        self.host    = host.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    # ── 공개 API ────────────────────────────────────────────────────

    async def parse(self, text: str) -> Optional[dict]:
        """
        자연어 텍스트 → JSON 명령 dict 파싱
        Returns: 명령 dict (tts_response 포함) 또는 None (파싱 실패 시)
        """
        text = text.strip()
        if not text:
            return None

        logger.info(f"[LLM] 파싱 요청: '{text}'")

        raw_response = await self._call_ollama(text)
        if not raw_response:
            return None

        cmd = self._extract_json(raw_response)
        if not cmd:
            logger.warning(f"[LLM] JSON 추출 실패: {raw_response}")
            return None

        # 숫자 타입 정규화 (float → int)
        cmd = self._normalize_types(cmd)

        # unknown 명령 처리 (tts_response는 살려서 반환)
        if cmd.get("cmd") == "unknown":
            logger.info(f"[LLM] unknown 명령: {cmd.get('msg')}")
            # tts_response가 있으면 음성 답변을 위해 반환
            if cmd.get("tts_response"):
                return {"cmd": None, "tts_response": cmd["tts_response"]}
            return None

        # cmd=null 자유 대화 처리 (tts_response만 사용)
        if cmd.get("cmd") is None:
            tts = cmd.get("tts_response", "")
            if tts:
                logger.info(f"[LLM] 자유 대화 응답: '{tts[:30]}'")
                return {"cmd": None, "tts_response": tts}
            return None

        # music 명령 처리 (TCP 불필요 → validate 우회)
        if cmd.get("cmd") == "music":
            action = cmd.get("action", "")
            valid_actions = {"play", "pause", "next", "prev", "volume"}
            if action not in valid_actions:
                logger.warning(f"[LLM] music 잘못된 action: {action}")
                return None
            logger.info(f"[LLM] music 명령: {cmd}")
            return cmd

        # status 명령 처리 (TCP 불필요 → validate 우회)
        if cmd.get("cmd") == "status":
            valid_targets = {"led", "servo", "all"}
            target = cmd.get("target", "all")
            if target not in valid_targets:
                cmd["target"] = "all"
            logger.info(f"[LLM] status 명령: {cmd}")
            return cmd

        # all_off / all_on 명령 처리 (TCP 불필요 → validate 우회)
        if cmd.get("cmd") in ("all_off", "all_on"):
            logger.info(f"[LLM] 전체 전원 명령: {cmd}")
            return cmd

        # PIR 모드 명령 처리 (validate 우회)
        if cmd.get("cmd") in ("away_mode", "home_mode", "sleep_mode", "wake_mode"):
            logger.info(f"[LLM] PIR 모드 명령: {cmd}")
            return cmd

        # 욕실 희망온도 설정 명령 처리 (validate 우회)
        if cmd.get("cmd") == "set_bathroom_temp":
            value = cmd.get("value")
            try:
                value = float(value)
            except (TypeError, ValueError):
                logger.warning(f"[LLM] set_bathroom_temp 잘못된 value: {value}")
                return None
            if not (10.0 <= value <= 40.0):
                logger.warning(f"[LLM] set_bathroom_temp 범위 초과: {value}")
                return None
            cmd["value"] = value
            logger.info(f"[LLM] 욕실 온도 설정 명령: {value}°C")
            return cmd

        # 욕실 현재온도 조회 명령 처리 (validate 우회)
        if cmd.get("cmd") == "query_bathroom_temp":
            logger.info(f"[LLM] 욕실 온도 조회 명령: {cmd}")
            return cmd

        # 전체 디바이스 명령 처리
        if cmd.get("device_id") == "all":
            logger.info(f"[LLM] 전체 디바이스 명령: {cmd}")
            return cmd

        # 유효성 검사
        ok, err = validate_command(cmd)
        if not ok:
            logger.warning(f"[LLM] 유효성 오류: {err} | cmd={cmd}")
            return None

        logger.info(f"[LLM] 파싱 성공: {cmd}")
        return cmd

    async def is_available(self) -> bool:
        """Ollama 서버 연결 확인"""
        try:
            resp = await self._client.get(f"{self.host}/api/tags", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        """설치된 모델 목록 조회"""
        try:
            resp = await self._client.get(f"{self.host}/api/tags")
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.warning(f"[LLM] 모델 목록 조회 실패: {e}")
            return []

    async def close(self):
        """HTTP 클라이언트 종료"""
        await self._client.aclose()

    # ── 내부 처리 ────────────────────────────────────────────────────

    async def _call_ollama(self, text: str) -> Optional[str]:
        """Ollama /api/chat 호출"""
        url = f"{self.host}/api/chat"
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system",  "content": SYSTEM_PROMPT},
                {"role": "user",    "content": text},
            ],
            "options": {
                "temperature": 0.1,
                "top_p": 0.9,
            },
        }

        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data["message"]["content"].strip()
            logger.debug(f"[LLM] 원본 응답: {content}")
            return content

        except httpx.TimeoutException:
            logger.error(f"[LLM] 타임아웃 ({self.timeout}s): '{text}'")
            return None
        except httpx.HTTPStatusError as e:
            logger.error(f"[LLM] HTTP 오류: {e.response.status_code}")
            return None
        except Exception as e:
            logger.error(f"[LLM] 요청 실패: {e}")
            return None

    def _normalize_types(self, data: dict) -> dict:
        """
        LLM 응답에서 정수 필드가 float으로 올 경우 int로 강제 변환
        예: {"pin": 2.0} → {"pin": 2}  → validate_command 통과
        """
        for key in ("pin", "angle", "pin_clk", "pin_dio"):
            if key in data:
                val = data[key]
                if isinstance(val, (int, float)):
                    data[key] = int(val)
                elif isinstance(val, str) and val.lstrip("-").isdigit():
                    data[key] = int(val)
        return data

    def _extract_json(self, text: str) -> Optional[dict]:
        """
        LLM 응답 문자열에서 JSON 추출
        - 순수 JSON 응답
        - ```json ... ``` 마크다운 코드블록 포함 응답
        - 앞뒤 설명 텍스트가 붙은 응답
        """
        text = text.strip()

        # 1. 직접 파싱 시도
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2. 마크다운 코드블록 제거 후 파싱
        md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if md_match:
            try:
                return json.loads(md_match.group(1))
            except json.JSONDecodeError:
                pass

        # 3. 중괄호 범위 추출 후 파싱
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass

        return None
