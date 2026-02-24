# command_router.py / main.py — 개발 로그

## 수정일: 2026-02-22

---

## 변경 요약

PIR 모드 제어 액션 4종 추가 (command_router v2.2 / main v0.7)

---

## command_router.py (v2.1 → v2.2)

### 추가된 액션 4종

| cmd | PIR 모드 | context | 부가 동작 |
|-----|----------|---------|-----------|
| `away_mode`  | guard    | away  | 전체 조명 OFF |
| `home_mode`  | presence | home  | -         |
| `sleep_mode` | guard    | sleep | -         |
| `wake_mode`  | presence | wake  | -         |

### execute() 분기 추가

```python
if data.get("cmd") in ("away_mode", "home_mode", "sleep_mode", "wake_mode"):
    return await self._execute_pir_mode(data)
```

### _execute_pir_mode() 신규 메서드

```python
# ESP32로 전송되는 명령
{"cmd": "pir_mode", "mode": "guard", "context": "away"}
```

- `away_mode` 시 `all_off` 추가 실행 (0.2초 딜레이)
- `tts_response` 있으면 결과에 포함

### _simple_parse() 키워드 추가

```python
외출/나갈게/나간다/외출해  → away_mode
귀가/돌아왔어/집에 왔어/귀가했어 → home_mode
잘게/잠자리/취침/자러 갈게  → sleep_mode
일어났어/기상/아침이야/일어났다 → wake_mode
```

---

## main.py (v0.6 → v0.7)

### 추가된 엔드포인트: POST /pir-event

ESP32 PIR 이벤트 수신 처리

```
ESP32 감지
  └─ POST /pir-event
       {"type":"pir_event","event":"guard_alert","detail":"motion_detected","context":"away"}
           │
           ├─ 이벤트별 메시지 생성
           ├─ WS 브로드캐스트 → 프론트엔드 알림
           └─ Telegram 알림 (telegram_bot 모듈 존재 시)
```

### 이벤트 메시지 매핑

| event | context | 메시지 |
|-------|---------|--------|
| guard_alert    | away  | 🚨 외출 중 침입 감지! |
| guard_alert    | sleep | 🚨 취침 중 거실 침입 감지! |
| presence_alert | home  | ⚠️ 장시간 움직임 없음 — 괜찮으신가요? |

### Telegram 연동 준비

`telegram_bot` 모듈 미존재 시 스킵 (ImportError 처리)
→ `server/telegram_bot.py` 구현 시 자동 활성화

---

## 전체 PIR 흐름 (완성 후)

```
"자비스야, 외출해"
  → STT → LLM → command_router (away_mode)
  → ESP32: pir_mode=guard + context=away
  → 전체 조명 OFF

[외출 중 침입 발생]
  → ESP32 PIR 감지
  → POST /pir-event (guard_alert, context=away)
  → WS 브로드캐스트 + Telegram 알림
```

---

## 다음 작업

- [ ] `server/telegram_bot.py` 신규 작성 (send_alert 함수)
- [ ] `llm_engine.py` SYSTEM_PROMPT에 away/home/sleep/wake 모드 추가
- [ ] ESP32 펌웨어: context 필드 pir_event 전송 시 포함
