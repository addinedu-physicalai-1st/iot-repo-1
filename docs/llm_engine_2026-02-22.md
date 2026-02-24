# llm_engine.py — 개발 로그

## 수정일: 2026-02-22

---

## 변경 요약

PIR 모드 4종 명령 추가 (v1.7 → v1.8)

---

## SYSTEM_PROMPT 변경

### Available commands 추가

```
{"cmd": "away_mode",  "device_id": "esp32_home"}  ← 외출 (PIR 방범 ON + 전체 조명 OFF)
{"cmd": "home_mode",  "device_id": "esp32_home"}  ← 귀가 (PIR 재실 감지 ON)
{"cmd": "sleep_mode", "device_id": "esp32_home"}  ← 취침 (PIR 거실 방범 ON)
{"cmd": "wake_mode",  "device_id": "esp32_home"}  ← 기상 (PIR 재실 감지 ON)
```

### Rules 4-3 추가

```
외출 / 나갈게 / 외출해        → away_mode
귀가 / 돌아왔어 / 집에 왔어   → home_mode
잘게 / 취침 / 자러 갈게       → sleep_mode
일어났어 / 기상 / 아침이야    → wake_mode
```

### Examples 추가

| 입력 | 출력 cmd |
|------|----------|
| "외출해 / 나갈게" | `away_mode` |
| "귀가했어 / 집에 왔어" | `home_mode` |
| "잘게 / 취침할게" | `sleep_mode` |
| "일어났어 / 기상" | `wake_mode` |

---

## parse() 변경

PIR 모드 4종 validate 우회 분기 추가

```python
if cmd.get("cmd") in ("away_mode", "home_mode", "sleep_mode", "wake_mode"):
    logger.info(f"[LLM] PIR 모드 명령: {cmd}")
    return cmd
```

---

## 전체 명령 파이프라인 (완성)

```
"자비스야, 외출해"
  → STT(Whisper) → LLMEngine.parse()
  → {"cmd": "away_mode", "device_id": "esp32_home", "tts_response": "외출 모드로 설정했어요..."}
  → CommandRouter._execute_pir_mode()
  → ESP32: {"cmd": "pir_mode", "mode": "guard", "context": "away"}
  → 전체 조명 OFF
  → TTS: "외출 모드로 설정했어요. 안전하게 다녀오세요!"

[외출 중 침입 감지]
  → ESP32 PIR HIGH
  → POST /pir-event {"event": "guard_alert", "context": "away"}
  → WS 브로드캐스트 + Telegram 알림 (예정)
```

---

## 다음 작업

- [ ] `server/telegram_bot.py` 신규 작성 (send_alert 함수)
