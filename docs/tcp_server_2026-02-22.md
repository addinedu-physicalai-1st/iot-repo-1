# tcp_server.py — 개발 로그

## 수정일: 2026-02-22

---

## 변경 요약

PIR 이벤트 TCP 수신 처리 추가 (v3.0 → v3.1)

---

## 버그 원인

ESP32 `sendPirEvent()`는 TCP 소켓으로 전송하는데,
서버는 HTTP POST `/pir-event`로만 수신 대기 중이어서
이벤트가 서버에 도달하지 못하고 유실되던 문제.

```
ESP32 → TCP send  (pir_event JSON)
서버  → HTTP POST /pir-event 대기  ← 경로 불일치 → 유실
```

---

## 수정 내용

### 1. _handle_client() — pir_event 분기 추가

```python
elif msg_type == "pir_event":
    await self._on_pir_event(client, data)
```

### 2. validate_esp32_message() — pir_event 우회

schema에 pir_event 타입이 없어서 validate 실패하던 문제 해결:

```python
if not ok:
    if data.get("type") == "pir_event":
        pass  # 우회
    else:
        continue
```

### 3. _on_pir_event() 핸들러 신규 추가

| event | context | WS 메시지 |
|-------|---------|-----------|
| guard_alert    | away  | 🚨 외출 중 침입 감지! |
| guard_alert    | sleep | 🚨 취침 중 거실 침입 감지! |
| presence_alert | home  | ⚠️ 장시간 움직임 없음 — 괜찮으신가요? |

WS 브로드캐스트 포맷:
```json
{"type":"pir_alert","msg":"🚨 외출 중 침입 감지!","event":"guard_alert","context":"away"}
```

---

## 수정 후 정상 흐름

```
ESP32 PIR 감지
  → tcpClient.print(pir_event JSON)
  → TCPServer._handle_client() 수신
  → _on_pir_event() 호출
  → WS 브로드캐스트 (pir_alert)
  → 브라우저 PIR 카드 ⚠ 감지됨! 점멸
  → 로그 패널 빨간 텍스트 출력
```
