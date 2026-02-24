# esp32_client_final.ino — 개발 로그

## 수정일: 2026-02-22

---

## 변경 요약

PIR 센서(HC-SR501) 통합 — 재실 감지 및 방범 모드 기능 추가

---

## 추가된 하드웨어

| 센서 | 핀 | 역할 |
|------|----|------|
| HC-SR501 PIR | GPIO 27 | 움직임 감지 (재실/방범) |

> GPIO 14는 침실 서보가 기존 사용 중이므로 GPIO 27 사용

---

## 추가된 상수 및 변수

```cpp
#define PIN_PIR                27
#define PIR_MODE_OFF           0
#define PIR_MODE_PRESENCE      1   // 재실 감지
#define PIR_MODE_GUARD         2   // 방범 모드
#define PIR_STATIC_TIMEOUT_MS  (4UL * 60 * 60 * 1000)  // 4시간
#define PIR_ALERT_COOLDOWN_MS  (30UL * 1000)            // 30초

int           pirMode        = PIR_MODE_OFF;
unsigned long lastMotionTime = 0;
unsigned long lastAlertTime  = 0;
bool          pirAlertSent   = false;
```

---

## 추가된 함수

| 함수 | 설명 |
|------|------|
| `handlePir()` | loop()에서 매 사이클 호출, 모드별 PIR 처리 |
| `cmdPirMode(mode)` | presence / guard / off 모드 전환 |
| `allLightsOn()` | 방범 알림 시 전체 조명 ON |
| `sendPirEvent(event, detail)` | 서버로 PIR 이벤트 TCP 전송 |

---

## 명령 프로토콜 추가

### 서버 → ESP32 (모드 설정)

```json
{"cmd": "pir_mode", "mode": "presence"}   // 재실 감지 ON
{"cmd": "pir_mode", "mode": "guard"}      // 방범 모드 ON
{"cmd": "pir_mode", "mode": "off"}        // 비활성
```

### ESP32 → 서버 (이벤트 전송)

```json
// 방범 모드: 움직임 감지 시
{"type": "pir_event", "event": "guard_alert", "detail": "motion_detected", "device_id": "esp32_home"}

// 재실 모드: 4시간 정적 감지 시
{"type": "pir_event", "event": "presence_alert", "detail": "static_too_long", "device_id": "esp32_home"}
```

---

## 동작 로직

### 재실 모드 (PIR_MODE_PRESENCE)
```
움직임 감지 → lastMotionTime 갱신, pirAlertSent 초기화
움직임 없음 → 4시간 경과 시 presence_alert 서버 전송 (1회)
```

### 방범 모드 (PIR_MODE_GUARD)
```
움직임 감지 → 전체 조명 ON + guard_alert 서버 전송
              쿨다운 30초 적용 (중복 전송 방지)
움직임 없음 → 대기
```

---

## 연동 예정 (서버 측)

- `command_router.py` : `away_mode` 명령 → ESP32 `pir_mode: guard` 전송
- `main.py` (FastAPI) : `/pir-event` 엔드포인트 → Telegram 알림 발송
- `telegram_bot.py` : 신규 모듈 (미구현)

---

## 다음 작업

- [ ] FastAPI `/pir-event` 엔드포인트 추가
- [ ] `command_router.py` `away_mode` / `home_mode` 액션 추가
- [ ] `llm_engine.py` SYSTEM_PROMPT 업데이트
- [ ] `telegram_bot.py` 알림 모듈 신규 작성
