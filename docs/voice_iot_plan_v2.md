# Voice-Controlled IoT System 개발 계획서 v2

> 음성 명령 기반 ESP32 IoT 디바이스 제어 시스템  
> 작성일: 2026-02-20 | 버전: v0.2 | 작성자: Stephen Kong

---

## 변경 이력

| 버전 | 날짜 | 변경 내용 |
|------|------|----------|
| v0.1 | 2026-02-20 | 초기 계획서 (PyQt6 단독) |
| v0.2 | 2026-02-20 | FastAPI 추가, Web App 클라이언트 통합, 아키텍처 재설계 |

---

## 1. 프로젝트 개요

### 1.1 목적

사용자의 음성 명령을 로컬 Whisper STT로 인식하고, 로컬 Ollama LLM으로 JSON 명령을 파싱하여 ESP32 보드에 연결된 LED, 서보모터, 온습도 센서 등 IoT 디바이스를 실시간으로 제어한다.

**클라이언트는 두 가지 형태를 동시 지원한다.**
- **Web App** : 브라우저 접속, WebSocket 기반 실시간 통신
- **PyQt6 Desktop** : 로컬 Whisper STT 포함, 고성능 모니터링

### 1.2 핵심 특징

- 완전 로컬 실행 (Whisper + Ollama, 클라우드 의존 없음)
- FastAPI 단일 서버가 REST / WebSocket / TCP 동시 제공
- TCP 기반 다중 ESP32 클라이언트 연결 지원
- Web App (브라우저) + PyQt6 (데스크탑) 이중 클라이언트
- JSON 표준 프로토콜로 ESP32 디바이스 확장성 확보

---

## 2. 시스템 아키텍처 (v2)

```
┌─────────────────────────────────────────────────────────────────┐
│                        INPUT PIPELINE                           │
│   🎙️ 마이크  →  Whisper STT  →  Ollama LLM  →  JSON Command   │
└─────────────────────────────┬───────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│               CORE SERVER  (FastAPI · Python)                   │
│                                                                 │
│   ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│   │ REST API     │  │ WebSocket    │  │ TCP Server         │  │
│   │ :8000        │  │ /ws          │  │ :9000              │  │
│   └──────────────┘  └──────────────┘  └────────────────────┘  │
│                                                                 │
│   tcp_server.py · websocket_hub.py · command_router.py         │
│   stt_engine.py · llm_engine.py · api_routes.py · schema.py   │
└──────────────┬──────────────────────────┬───────────────────────┘
               │ WebSocket                │ TCP Socket
               ↓                          ↓
┌──────────────────────┐    ┌─────────────────────────────────────┐
│   WEB CLIENT         │    │   ESP32 CLIENTS                     │
│                      │    │                                     │
│  Browser (HTML/JS)   │    │  ESP32 #1  ESP32 #2  ESP32 #N      │
│  - Web Speech API    │    │  거실유닛  침실유닛   확장유닛       │
│  - 센서 실시간 차트  │    │  LED/서보  LED/DHT22  릴레이/센서   │
│  - 디바이스 상태 UI  │    │                                     │
│  - 수동 명령 입력    │    │  (JSON over TCP · 양방향 통신)      │
└──────────────────────┘    └─────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│   DESKTOP CLIENT  (PyQt6)                                       │
│   로컬 Whisper STT · pyqtgraph 차트 · Alert 로그 · 디바이스 관리│
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 클라이언트별 데이터 흐름

**Web App 흐름**
```
브라우저 Web Speech API
→ WebSocket → FastAPI
→ Ollama LLM 명령 파싱
→ TCP → ESP32 제어
→ ACK/센서 데이터 → WebSocket → 브라우저 UI 업데이트
```

**PyQt6 Desktop 흐름**
```
로컬 마이크 + Whisper STT
→ FastAPI REST API
→ Ollama LLM 명령 파싱
→ TCP → ESP32 제어
→ 센서 데이터 → pyqtgraph 실시간 차트
```

---

## 3. 기술 스택

| 분류 | 기술 | 버전 | 역할 |
|------|------|------|------|
| 백엔드 프레임워크 | FastAPI | 0.110+ | REST + WebSocket + 앱 서버 |
| 비동기 런타임 | asyncio / uvicorn | - | 비동기 TCP + HTTP 동시 처리 |
| STT | faster-whisper | Latest | 로컬 음성 인식 |
| LLM | Ollama (EXAONE 3.5) | Latest | 자연어 → JSON 명령 파싱 |
| 데스크탑 GUI | PyQt6 + pyqtgraph | 6.x | 로컬 모니터링 대시보드 |
| Web 프론트 | HTML5 + Vanilla JS | - | 브라우저 기반 제어 UI |
| IoT 클라이언트 | ESP32 (Arduino) | IDF 5.x | TCP 클라이언트 + 디바이스 제어 |
| 프로토콜 | JSON over TCP | - | 서버 ↔ ESP32 메시지 포맷 |
| 오디오 캡처 | sounddevice | Latest | 마이크 PCM 스트림 |
| 설정 관리 | PyYAML | - | settings.yaml |

---

## 4. 프로젝트 디렉터리 구조

```
voice_iot_controller/
├── server/
│   ├── main.py                # 진입점 (FastAPI 앱 + TCP 서버 동시 실행)
│   ├── tcp_server.py          # asyncio TCP 서버 (다중 ESP32 클라이언트)
│   ├── websocket_hub.py       # WebSocket 연결 관리 + 브로드캐스트
│   ├── api_routes.py          # FastAPI REST 엔드포인트
│   ├── stt_engine.py          # Whisper STT 모듈
│   ├── llm_engine.py          # Ollama LLM 명령 파싱
│   └── command_router.py      # JSON 명령 → ESP32 라우팅
│
├── web/
│   ├── index.html             # Web App 메인 UI
│   ├── app.js                 # WebSocket 클라이언트 + Web Speech API
│   └── style.css              # 다크 테마 스타일
│
├── gui/
│   └── dashboard.py           # PyQt6 데스크탑 대시보드
│
├── protocol/
│   └── schema.py              # JSON 메시지 스키마 + 유효성 검증
│
├── esp32/
│   └── esp32_client.ino       # Arduino ESP32 TCP 클라이언트
│
├── config/
│   └── settings.yaml          # 포트, 모델명, GPIO 핀 설정
│
├── docs/
│   ├── voice_iot_plan_v2.md   # 개발 계획서 (현재 문서)
│   └── DEV_LOG.md             # 일별 개발 로그
│
└── requirements.txt
```

---

## 5. TCP 프로토콜 설계

모든 메시지는 JSON + 개행문자(`\n`) 구분.

### 5.1 서버 → ESP32 (명령)

```json
{"cmd": "led",   "pin": 2,  "state": "on"}
{"cmd": "led",   "pin": 2,  "state": "off"}
{"cmd": "servo", "pin": 18, "angle": 90}
{"cmd": "query", "sensor": "dht22"}
```

### 5.2 ESP32 → 서버 (응답 / 센서)

```json
{"type": "ack",    "cmd": "led",   "status": "ok"}
{"type": "sensor", "device": "dht22", "temp": 24.5, "humidity": 60.2}
{"type": "error",  "msg": "pin not found"}
```

### 5.3 ESP32 등록 (접속 시)

```json
{"type": "register", "device_id": "esp32_living_room", "caps": ["led", "servo"]}
```

---

## 6. FastAPI 엔드포인트 설계

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | Web App index.html 서빙 |
| GET | `/devices` | 연결된 ESP32 목록 조회 |
| POST | `/command` | 직접 명령 전송 (수동) |
| POST | `/voice` | STT 오디오 업로드 → 명령 실행 |
| GET | `/ws` | WebSocket 연결 (브라우저 실시간) |

---

## 7. WebSocket 메시지 포맷

### 브라우저 → 서버

```json
{"type": "voice_text", "text": "거실 불 켜줘"}
{"type": "manual_cmd", "cmd": "led", "pin": 2, "state": "on"}
```

### 서버 → 브라우저 (브로드캐스트)

```json
{"type": "device_update", "device_id": "esp32_living_room", "state": {"led_2": "on"}}
{"type": "sensor_data",   "device_id": "esp32_bedroom", "temp": 24.5, "humidity": 60}
{"type": "cmd_result",    "status": "ok", "msg": "LED GPIO2 ON"}
```

---

## 8. LLM 시스템 프롬프트

```
You are an IoT command parser. Convert Korean natural language to JSON only.

Available commands:
  {"cmd": "led",   "pin": <int>,  "state": "on"|"off"}
  {"cmd": "servo", "pin": <int>,  "angle": <0-180>}
  {"cmd": "query", "sensor": "dht22"}

Rules:
  - Respond ONLY with valid JSON. No explanation.
  - If unclear: {"cmd": "unknown", "msg": "<reason>"}

Examples:
  "거실 LED 켜줘"     → {"cmd": "led", "pin": 2, "state": "on"}
  "서보 90도로 돌려"  → {"cmd": "servo", "pin": 18, "angle": 90}
  "온도 알려줘"       → {"cmd": "query", "sensor": "dht22"}
```

---

## 9. 개발 단계별 계획 (Phase)

| Phase | 명칭 | 주요 내용 | 우선순위 |
|-------|------|----------|---------|
| Phase 1 | FastAPI + TCP 서버 | asyncio TCP + FastAPI 통합, 디바이스 레지스트리 | 🔴 최우선 |
| Phase 2 | ESP32 클라이언트 | Arduino TCP + LED/서보/DHT22 제어 | 🔴 최우선 |
| Phase 3 | LLM 명령 파싱 | Ollama 연동, 자연어 → JSON 변환 엔진 | 🟠 높음 |
| Phase 4 | Whisper STT | faster-whisper + VAD 마이크 스트림 | 🟠 높음 |
| Phase 5 | Web App 프론트 | HTML/JS + WebSocket + Web Speech API | 🟡 중간 |
| Phase 6 | PyQt6 대시보드 | 센서 차트, 명령 히스토리, 상태 패널 | 🟡 중간 |
| Phase 7 | 통합 테스트 | 전체 파이프라인 E2E 테스트 + 최적화 | 🟢 마무리 |

### Phase 1: FastAPI + TCP 서버

- FastAPI 앱 생성, uvicorn 실행
- asyncio TCP 서버 (`:9000`) FastAPI와 동시 구동
- 다중 클라이언트 연결/해제 관리 (`device_registry`)
- JSON 메시지 수신 · 파싱 · 라우팅
- WebSocket 허브 (`/ws`) - 브라우저 실시간 연결

### Phase 2: ESP32 클라이언트

- WiFi 연결 + TCP 소켓 (자동 재연결)
- 접속 시 `register` 메시지 전송
- LED `digitalWrite` / PWM 밝기
- 서보모터 `ESP32Servo` 각도 제어
- DHT22 온습도 주기 전송

### Phase 3: LLM 명령 파싱

- `ollama` Python 클라이언트 연동
- System Prompt 기반 JSON 변환
- 응답 유효성 검증 (`schema.py`)
- 오류 시 fallback 처리

### Phase 4: Whisper STT

- `faster-whisper` 로컬 추론
- `sounddevice` VAD 발화 구간 감지
- STT 결과 → LLM 엔진 연결

### Phase 5: Web App 프론트

- 다크 테마 단일 HTML 파일
- Web Speech API STT → WebSocket 전송
- 실시간 센서 차트 (Chart.js)
- 디바이스 상태 카드 UI
- 수동 명령 입력 패널

### Phase 6: PyQt6 대시보드

- FastAPI REST 폴링 or WebSocket 수신
- pyqtgraph 온습도 실시간 그래프
- 디바이스 상태 패널
- 명령 히스토리 로그

---

## 10. Python 의존성 (requirements.txt)

```
fastapi
uvicorn[standard]
websockets
faster-whisper
sounddevice
ollama
PyQt6
pyqtgraph
pyyaml
```

---

## 11. 개발 원칙

- 모듈 단위 분리, 단일 책임 원칙(SRP) 준수
- FastAPI 서버가 TCP + WebSocket + REST 통합 허브 역할
- ESP32 핀 번호는 `settings.yaml` 중앙 관리
- 프로토콜 변경 시 `schema.py` 먼저 수정
- 하루 작업 종료 시 `DEV_LOG.md` 기록
- Phase 완료 시 해당 모듈 `.md` 스냅샷 저장

---

*Voice IoT Controller · v0.2 · 2026-02-20*
