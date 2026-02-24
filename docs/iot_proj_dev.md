# DEV_LOG — Voice IoT Controller

---

## 2026-02-20 | Phase 1~6 구현 + 웨이크 워드 + 버튼 트리거 + UI 버그 수정

### 작업 요약

Voice-Controlled IoT System 신규 프로젝트 착수.  
아키텍처 설계부터 전체 모듈 구현, 웨이크 워드 통합, 버튼/웨이크 공존 트리거,  
UI 이중선 버그 수정, PyQt6 마이크 버튼 실제 연동까지 완료.

---

### 1. 아키텍처 확정 (v2)

- 초기 PyQt6 단독 구성 → FastAPI 통합 서버 구조로 재설계
- FastAPI 단일 서버가 REST / WebSocket / TCP 동시 제공
- Web App (브라우저) + PyQt6 (데스크탑) 이중 클라이언트 구조 확정

```
[마이크] → [STTEngine] → ("헤이 IoT" 웨이크 워드 또는 버튼 트리거)
                ↓
         [Whisper 추론] → [LLMEngine (Ollama)]
                                  ↓
                          [FastAPI Server]
                         /       |        \
                WebSocket     REST API    TCP :9000
                    |                        |
             [Web Browser]             [ESP32 #1~N]
             [PyQt6 Dashboard]
```

---

### 2. 평면도 기반 디바이스 배치 확정

| 공간 | device_id | caps |
|------|-----------|------|
| 차고 | `esp32_garage` | LED, 서보(차고문) |
| 욕실 | `esp32_bathroom` | LED, DS18B20(온도) |
| 침실 | `esp32_bedroom` | LED, DHT22(온습도), 서보, 7세그먼트 |
| 현관 | `esp32_entrance` | LED, 서보(현관문) |

- 침실 ESP32가 가장 복잡한 유닛 (4종 caps)
- 7세그먼트(TM1637): CLK=GPIO22, DIO=GPIO23
- 서보 도어 프리셋: 열기=90도, 닫기=0도

---

### 3. 구현 파일 목록

#### server/

| 파일 | 버전 | 주요 변경 |
|------|------|----------|
| `main.py` | v0.3 | LLMEngine + STTEngine 의존성 주입, 콜백 팩토리, DISABLE_STT/LLM 플래그 |
| `tcp_server.py` | v1.0 | asyncio TCP 서버, 다중 ESP32 관리 |
| `websocket_hub.py` | v1.0 | WebSocket 연결 풀, 브로드캐스트 |
| `command_router.py` | v1.1 | `manual_trigger` WS 타입 추가, STTEngine.activate() 연동 |
| `api_routes.py` | v1.1 | `POST /stt/activate` 엔드포인트 추가 |
| `llm_engine.py` | v1.0 | Ollama LLM 연동, 자연어 → JSON 파싱 |
| `stt_engine.py` | v2.0 | 웨이크 워드 + VAD 상태머신, openwakeword + Whisper 폴백 |

#### web/ · gui/ · esp32/

| 파일 | 버전 | 주요 변경 |
|------|------|----------|
| `index.html` | v1.1 | 이중선 버그 수정, 모드 뱃지, REST+WS 이중 트리거, wake/stt WS 핸들러 |
| `dashboard.py` | v0.3 | outer/inner 패널 분리(이중선 수정), 마이크 버튼 실제 연동, 모드 뱃지 |
| `esp32_client.ino` | v1.0 | Arduino ESP32 TCP 클라이언트 |

---

### 4. 주요 기능 상세

#### STT 상태머신 (stt_engine.py v2.0)

```
IDLE → (웨이크 워드 or 버튼) → LISTENING → (발화 후 침묵 1초) → Whisper 추론 → IDLE
                                    └── 8초 타임아웃 → IDLE
```

| 파라미터 | 값 | 설명 |
|----------|-----|------|
| `BLOCK_SIZE` | 1280 샘플 | 80ms @ 16kHz, OWW 권장 |
| `WAKE_THRESHOLD` | 0.5 | OWW 감지 신뢰도 임계값 |
| `WAKE_CONFIRM_CHUNKS` | 3회 | 연속 감지 오탐 방지 |
| `WAKE_LISTEN_SEC` | 8.0초 | 명령 대기 타임아웃 |
| `VAD_SILENCE_SEC` | 1.0초 | 발화 종료 판단 침묵 시간 |

폴백: openwakeword 미설치 시 Whisper tiny로 웨이크 워드 감지

#### 버튼 + 웨이크 워드 공존 트리거

```
[버튼 클릭]                        [헤이 IoT 발화]
  ├─ WS: {type:"manual_trigger"}      └─ OWW 자동 감지
  └─ REST: POST /stt/activate               ↓
                  ↓                  STTEngine LISTENING
         STTEngine.activate()
                  ↓
     WS broadcast: {type:"wake_detected"}
```

#### WS 메시지 타입 (서버 → 클라이언트)

| type | 발생 시점 | UI 반응 |
|------|----------|---------|
| `wake_detected` | 웨이크/버튼 트리거 | 뱃지 `LISTENING`, 마이크 초록 |
| `stt_result` | Whisper 인식 완료 | 텍스트 표시, 뱃지 `IDLE` 복귀 |
| `wake_timeout` | 8초 초과 | 뱃지 `IDLE` 복귀, warn 로그 |
| `cmd_result` | ESP32 명령 결과 | 결과 텍스트 표시 |
| `sensor_data` | ESP32 센서 수신 | 센서 값 + 차트 업데이트 |
| `device_list` | 디바이스 연결/해제 | ONLINE/OFFLINE 뱃지 |

#### API 엔드포인트 (v1.1)

```
GET  /              - Web App 서빙
GET  /devices       - ESP32 목록 + STT 상태
GET  /status        - 서버 상태 (stt_state 포함)
POST /command       - 수동 명령 전송
POST /voice         - STT 텍스트 → LLM → 명령 실행
POST /stt/activate  - 버튼 모드 트리거
GET  /ws            - WebSocket 연결
```

#### UI 이중선 버그 수정

원인: PyQt6/CSS에서 `border: 1px` + `border-top: 2px` 동시 지정 시 두 선 중첩됨

```
web/index.html  : border-top accent 선 + box-shadow 보완
dashboard.py    : QFrame#voiceOuter(cyan 2px 노출)
                    └── QFrame#voiceInner(border:none)
```

#### PyQt6 마이크 버튼 실제 연동

```
클릭 → RestPoller.activate_stt() → POST /stt/activate
  ├─ None        : 서버 미연결 안내
  ├─ ok/warn     : UI 즉시 LISTENING 선반영
  └─ fail        : 활성화 실패 로그
WS wake_detected 수신 시 정식 상태 동기화
```

---

### 5. 프로젝트 디렉터리 구조

```
voice_iot_controller/
├── server/
│   ├── __init__.py
│   ├── main.py             v0.3
│   ├── tcp_server.py       v1.0
│   ├── websocket_hub.py    v1.0
│   ├── command_router.py   v1.1
│   ├── api_routes.py       v1.1
│   ├── llm_engine.py       v1.0
│   └── stt_engine.py       v2.0
├── gui/
│   └── dashboard.py        v0.3
├── web/
│   └── index.html          v1.1
├── protocol/
│   ├── __init__.py
│   └── schema.py           v1.0
├── esp32/
│   └── esp32_client.ino    v1.0
├── config/
│   └── settings.yaml       v1.0
├── docs/
│   ├── voice_iot_plan_v2.md
│   └── DEV_LOG.md
└── requirements.txt
```

---

### 6. requirements.txt

```
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
websockets>=12.0
httpx>=0.27.0
faster-whisper>=1.0.0
sounddevice>=0.4.6
numpy>=1.24.0
ollama>=0.1.9
openwakeword>=0.6.0
PyQt6>=6.6.0
pyqtgraph>=0.13.0
pyyaml>=6.0.1
requests>=2.31.0
```

---

### 7. 실행 방법

```bash
# 의존성 설치
pip install -r requirements.txt

# 웨이크 워드 모델 다운로드
python -c "from openwakeword.utils import download_models; download_models()"

# 풀 모드 실행 (STT + LLM)
cd ~/dev_ws/voice_iot_controller
uvicorn server.main:app --host 0.0.0.0 --port 8000

# 서버만 실행 (STT/LLM 비활성화)
DISABLE_STT=1 DISABLE_LLM=1 uvicorn server.main:app --host 0.0.0.0 --port 8000

# PyQt6 대시보드
python -m gui.dashboard

# Web App
http://localhost:8000
```

---

### 8. 다음 작업 (TODO)

- [ ] `server/__init__.py`, `protocol/__init__.py` 생성
- [ ] `device_id: "all"` 전체 명령 브로드캐스트 처리
- [ ] Phase 7: E2E 통합 테스트
- [ ] ESP32 4개 유닛 실제 업로드 및 연결 테스트
- [ ] 커스텀 "헤이 IoT" 웨이크 워드 모델 학습
- [ ] Web App 반응형 모바일 레이아웃 개선

---

*Voice IoT Controller · DEV_LOG · 2026-02-20*
