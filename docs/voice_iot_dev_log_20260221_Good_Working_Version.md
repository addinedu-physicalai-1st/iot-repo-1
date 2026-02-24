# Voice IoT Controller — 개발 작업 기록

> 작성일: 2026-02-21  
> 프로젝트: `~/dev_ws/voice_iot_controller`  
> 기술 스택: FastAPI, asyncio TCP, WebSocket, ESP32, Porcupine, Whisper, Ollama (EXAONE 3.5)

---

## 1. 버그수정 #1 — 웨이크 워드 정상 작동

**문제**  
한국어 웨이크 워드 `"헤이 코코"` 가 Porcupine에서 인식 불가.

**원인**  
Porcupine 커스텀 모델이 `"자비스야"` 로 학습됨. 웨이크 워드 불일치.

**수정 파일**: `config/settings.yaml`, `server/stt_engine.py`  
**결과**: `wake_word: "자비스야"`, `.ppn` 경로 수정 후 정상 감지 확인.

---

## 2. 버그수정 #2 — VAD 에너지 임계값 조정

**문제**  
발화 종료가 감지되지 않아 매번 10초 강제 처리됨.

**원인**  
기존 `vad_energy_threshold: 0.02` 가 배경음 에너지(0.054~0.114)보다 낮아  
배경음도 SPEECH로 판정 → VAD 무음 미감지.

**수정 파일**: `config/settings.yaml`

```yaml
# 변경 전
vad_energy_threshold: 0.02

# 변경 후
vad_energy_threshold: 0.06
```

---

## 3. 웹 대시보드 UI 1차 개선 — 상태 뱃지 + 버튼 레이아웃

**요구사항**  
4개 패널(차고, 욕실, 침실, 현관)의 디바이스 상태를 직관적으로 표시.

**수정 파일**: `web/index.html`

**변경 내용**

- `ctrl-row-3` CSS Grid 3열 레이아웃 추가  
  → `[ 상태 뱃지 ]  [ 켜기 / 열기 ]  [ 끄기 / 닫기 ]`
- 버튼 텍스트 단순화: `"전등 : 켜짐"` → `"전등 켜기"`
- 상태 뱃지 텍스트 통일: `"켜짐/꺼짐"` → `"ON/OFF"`

---

## 4. 웹 대시보드 UI 2차 개선 — 상태 색상 강조

**요구사항**  
ON/열림은 눈에 바로 띄도록, 색상으로 명확하게 구분.

**수정 파일**: `web/index.html`

**변경 내용**

| 상태 | 폰트 | 색상 | 배경 |
|------|------|------|------|
| ON / 열림 | 1rem Bold | `var(--green)` + glow | `rgba(0,255,136,0.06)` |
| OFF / 닫힘 | 1rem Bold | `var(--red)` | `rgba(255,64,96,0.05)` |

```css
.status-badge.on   { border-color:rgba(0,255,136,0.35); }
.status-badge.on   .status-val { color:var(--green); text-shadow:0 0 8px rgba(0,255,136,0.5); }
.status-badge.off  { border-color:rgba(255,64,96,0.3); }
.status-badge.off  .status-val { color:var(--red); }
```

---

## 5. 온도 UI 개선 — 현재 온도 조회 + 희망 온도 설정

**요구사항**  
욕실/침실에 현재 온도 조회 버튼과 희망 온도 설정 컨트롤 추가.

**수정 파일**: `web/index.html`

**변경 내용**

```
[ 📊 온습도 조회 ]           ← 현재 온도 요청 버튼
┌──────────┐ ┌──────────┐
│ 현재 온도 │ │ 현재 습도 │   ← 조회 결과 칩
│  --.- °C │ │  --.- %  │
└──────────┘ └──────────┘
┌──────────────────────────────────────┐
│ 희망 온도 설정                        │
│  [ − ]  [ 24.0 ]  [ + ]  °C  [ 적용 ] │
└──────────────────────────────────────┘
```

**JS 함수 추가**

```javascript
function adjustTarget(room, delta)   // ± 버튼 클릭
function applyTarget(room)           // 적용 버튼 → ESP32 전송 + 7세그먼트 반영
```

- 침실 `적용` 클릭 시 희망 온도가 7세그먼트에 표시됨

---

## 6. 모바일 반응형 대응

**요구사항**  
모바일 브라우저에서도 조작 가능하도록 레이아웃 및 터치 UX 개선.

**수정 파일**: `web/index.html`

**변경 내용**

| 항목 | 데스크톱 | 모바일 (≤768px) |
|------|----------|----------------|
| 레이아웃 | `grid 1fr 340px` (2열) | 1열 세로 스택 |
| 디바이스 그리드 | 2열 | 1열 |
| ctrl-row-3 | 3열 | 상태뱃지 전체폭 + 버튼 2열 |
| 슬라이더 thumb | 14px | 22px |
| ±버튼 크기 | 28px | 40px |
| ctrl-btn 패딩 | 7px | 11px |

```css
@media (max-width: 768px) {
  .layout { grid-template-columns: 1fr; }
  .ctrl-row-3 { grid-template-columns: 1fr 1fr; }
  .ctrl-row-3 .status-badge { grid-column: 1 / -1; }
}
@media (max-width: 480px) {
  .logo-text { display: none; }
}
```

---

## 7. 음성 명령 패턴 추가

**요구사항**  
커튼 열기/닫기, 차고문 열기/닫기 음성 명령 지원.

**수정 파일**: `server/command_router.py` (`_simple_parse`)

**추가 패턴**

```python
# 커튼 열기/닫기 (침실)
"커튼 열", "커튼 열어", "커튼 올려" → servo angle=90, esp32_bedroom
"커튼 닫", "커튼 닫아", "커튼 내려" → servo angle=0,  esp32_bedroom

# 차고문 열기/닫기
"차고문 열", "차고 문 열", "차고 열" → servo angle=90, esp32_garage
"차고문 닫", "차고 문 닫", "차고 닫" → servo angle=0,  esp32_garage
```

---

## 8. 버그수정 #3 — 웹 뱃지 ↔ 실제 LED 상태 미동기화

**증상**  
음성/버튼으로 LED를 제어해도 웹 패널의 뱃지가 갱신되지 않음.  
ONLINE 뱃지도 표시되지 않음.

### 원인 분석

| # | 파일 | 원인 |
|---|------|------|
| 1 | `main.py` | `WebSocketHub()` 생성 시 `on_connect` 콜백 미등록 → 브라우저 접속 시 device_list 수신 불가 |
| 2 | `websocket_hub.py` | `on_connect(client_id)` 시그니처 — 접속한 클라이언트에만 전송하는 `send_fn` 없음 |
| 3 | `tcp_server.py` | `_on_ack()` 에서 msg가 `"esp32_bedroom led → ok"` 형태 — state/angle 정보 없음 |
| 4 | `command_router.py` | TCP 전송 성공 후 `client.state` 미갱신, `device_update` 브로드캐스트 없음 |

### 수정 내용

**`websocket_hub.py`** — `on_connect` 콜백에 `send_fn` 전달

```python
# 접속 직후, 해당 클라이언트에만 전송하는 클로저 생성
async def _send_fn(msg: str):
    await websocket.send_text(msg)
await self._safe_call(self._on_connect, client_id, _send_fn)
```

**`main.py`** — `_on_ws_connect` 콜백 등록

```python
async def _on_ws_connect(client_id: str, send_fn):
    devices = tcp_server.get_device_list()
    await send_fn(ws_device_list(devices))           # ONLINE 뱃지용
    for d in devices:
        await send_fn(ws_device_update(d["device_id"], d["state"]))  # LED/서보 상태용

ws_hub._on_connect = _on_ws_connect
```

**`command_router.py`** — 전송 성공 시 state 즉시 반영

```python
# client.state 즉시 업데이트 (ACK 대기 없이 선반영)
if cmd == CMD_LED:
    client.state[f"led_{pin}"] = 1 if state == "on" else 0
elif cmd == CMD_SERVO:
    client.state[f"servo_{pin}"] = angle

# device_update 브로드캐스트 → 모든 브라우저 뱃지 즉시 반영
await self._tcp._broadcast(ws_device_update(device_id, client.state))
```

**`tcp_server.py`** — ACK 수신 시 state 캐시 갱신 + device_update 추가 브로드캐스트

```python
async def _on_ack(self, client, data):
    if status == "ok":
        # state 캐시 갱신
        if cmd == "led":
            client.state[f"led_{pin}"] = 1 if state == "on" else 0
        elif cmd == "servo":
            client.state[f"servo_{pin}"] = angle
        # device_update 브로드캐스트
        await self._broadcast(ws_device_update(client.device_id, client.state))
    # msg에 state/angle 포함
    msg = f"{device_id} {cmd} 명령 전송 state={state}"  # or angle=
    await self._broadcast(ws_cmd_result(status, msg))
```

**`web/index.html`** — `syncBadgeFromCmdResult()` 추가

```javascript
// cmd_result 수신 시 msg 파싱 → 뱃지 동기화
// "esp32_bedroom led 명령 전송 state=on" → updateLedStatus('bedroom', 'on')
// "esp32_garage servo 명령 전송 angle=90" → updateDoorStatus('garage', 90)
function syncBadgeFromCmdResult(data) { ... }

// cmd_result 핸들러에서 호출
if (isOk) syncBadgeFromCmdResult(data);
```

### 최종 동작 흐름

```
브라우저 접속
  → on_connect → device_list 수신 → ONLINE 뱃지 표시
  → device_update × n → LED/서보 현재 상태 복원

음성 "침실 불 켜줘" / 버튼 클릭
  → command_router: client.state["led_2"] = 1 즉시 저장
  → ws_device_update("esp32_bedroom", state) 브로드캐스트
  → 브라우저 updateDeviceState() → 전등 뱃지 ON (초록) ✅
  → (2중 보장) syncBadgeFromCmdResult() 추가 처리
  → (3중 보장) ESP32 ACK 수신 후 tcp_server._on_ack() 재처리
```

---

## 수정 파일 목록 (최종)

| 파일 | 경로 | 주요 변경 |
|------|------|-----------|
| `index.html` | `web/index.html` | UI 전면 개선, 모바일 반응형, syncBadgeFromCmdResult |
| `command_router.py` | `server/command_router.py` | 음성 패턴 추가, state 즉시 반영, device_update 브로드캐스트 |
| `tcp_server.py` | `server/tcp_server.py` | _on_ack state 캐시 갱신, device_update 브로드캐스트 |
| `websocket_hub.py` | `server/websocket_hub.py` | on_connect send_fn 전달 |
| `main.py` | `server/main.py` | _on_ws_connect 콜백 등록 |
| `settings.yaml` | `config/settings.yaml` | wake_word, vad_energy_threshold, mic_device |

---

## 배포 명령

```bash
cd ~/dev_ws/voice_iot_controller

cp web/index.html           web/index.html.bak
cp server/command_router.py server/command_router.py.bak
cp server/tcp_server.py     server/tcp_server.py.bak
cp server/websocket_hub.py  server/websocket_hub.py.bak
cp server/main.py           server/main.py.bak

# 새 파일 반영 후 서버 재시작
uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
```

---

*끝 — 2026-02-21*
