# Voice IoT Controller — 전체 부팅 및 실행 순서도

---

## 1. 전체 개요

```
[개발자]
   │
   │  uvicorn server.main:app --host 0.0.0.0 --port 8000
   ▼
[uvicorn]  ─────────────────────────────────────────────────────┐
   │                                                            │
   │  Python import: server.main                               │
   ▼                                                            │
[create_app()]                                                  │
   │  settings.yaml 로드                                        │
   │  인스턴스 생성 & 의존성 연결                                  │
   │  FastAPI 앱 반환                                           │
   ▼                                                            │
[lifespan 시작]                                                  │
   ├─ TCPServer    :9000  ◄─── ESP32 연결 대기                   │
   ├─ LLMEngine           ◄─── Ollama :11434 연결 확인           │
   ├─ STTEngine           ◄─── 마이크 스트림 + 웨이크 워드 대기    │
   └─ HTTP/WS     :8000  ◄─── 브라우저 / PyQt6 연결 대기 ────────┘
```

---

## 2. create_app() 상세 순서

```
uvicorn 실행
    │
    ▼
① settings.yaml 로드
    │  server.host / tcp_port / ws_port
    │  ollama.model / host
    │  whisper.model / language / wake_word
    │  devices / command_keywords
    │
    ▼
② 인스턴스 생성
    │
    ├─ TCPServer(host, port=9000)
    │
    ├─ WebSocketHub()
    │
    ├─ LLMEngine(model, host, timeout)       ← DISABLE_LLM=1 이면 None
    │
    ├─ CommandRouter(tcp, cfg, llm)
    │
    └─ STTEngine(                            ← DISABLE_STT=1 이면 None
           on_result  = _make_stt_callback(router, hub),
           on_wake    = _make_wake_callback(hub),
           on_timeout = _make_timeout_callback(hub),
           model_size, language, wake_word
       )
    │
    ▼
③ 상호 의존성 연결
    │
    ├─ tcp.ws_broadcast  = hub.broadcast
    │    └─ ESP32 센서 수신 → WS 브로드캐스트 자동 연결
    │
    ├─ hub._on_message   = router.handle
    │    └─ 브라우저/PyQt6 WS 메시지 → 명령 라우터 자동 연결
    │
    └─ router._llm       = llm_engine
         └─ 음성 명령 → LLM 파싱 자동 연결
    │
    ▼
④ FastAPI 앱 생성
    │  라우터 등록 (api_routes)
    │  정적 파일 마운트 (web/static)
    │  app.state 바인딩 (tcp, hub, router, llm, stt)
    │
    ▼
⑤ app 반환 → uvicorn이 HTTP/WS :8000 LISTEN 시작
```

---

## 3. lifespan 시작 상세 순서

```
lifespan 진입 (앱 시작 직후 자동 실행)
    │
    ▼
① TCPServer.start()
    │  asyncio.start_server() → TCP :9000 LISTEN
    │  ESP32 연결 대기 상태 진입
    │
    ▼
② LLMEngine.is_available()
    │  GET http://localhost:11434  (Ollama 연결 확인)
    │
    ├─ 성공 → 설치된 모델 목록 로그 출력
    └─ 실패 → "키워드 fallback 동작" 경고 로그
              (서버는 정상 계속 실행)
    │
    ▼
③ STTEngine.start()  (비동기 태스크로 실행)
    │
    ├─ Whisper 모델 로드 (executor, 블로킹 방지)
    │    모델 크기별 로드 시간:
    │    tiny  ≈ 1초  / base  ≈ 2초
    │    small ≈ 5초  / medium ≈ 15초
    │
    ├─ openwakeword 모델 로드
    │    ├─ 성공 → OWW ONNX 추론 사용
    │    └─ 실패 → Whisper tiny 폴백 감지 사용
    │
    ├─ sounddevice InputStream 시작
    │    SR=16000Hz, block=1280샘플(80ms), mono
    │
    └─ 처리 루프 시작 → IDLE 상태 진입
         "헤이 IoT" 웨이크 워드 대기 중
    │
    ▼
④ 서버 Ready 배너 출력
    ════════════════════════════════════════════════════
     Voice IoT Controller  v0.3
    ════════════════════════════════════════════════════
      TCP  : 0.0.0.0:9000
      HTTP : 0.0.0.0:8000
      WS   : ws://0.0.0.0:8000/ws
    ────────────────────────────────────────────────────
      LLM  : ✅ exaone3.5:latest
      STT  : ✅ base / wake=헤이IoT
    ════════════════════════════════════════════════════
    │
    ▼
⑤ 전체 서비스 Ready
    ├─ HTTP  :8000  → REST API 요청 수락
    ├─ WS    :8000  → WebSocket 연결 수락
    └─ TCP   :9000  → ESP32 연결 수락
```

---

## 4. ESP32 부팅 및 서버 등록 순서

```
ESP32 전원 ON
    │
    ▼
① WiFi 연결
    │  SSID / PASSWORD → 연결 시도
    │
    ├─ 실패 (30초) → ESP.restart() 자동 재시작
    └─ 성공 → IP 할당 확인
    │
    ▼
② TCP 서버 접속
    │  connect(SERVER_HOST, 9000)
    │
    ├─ 실패 → 3초 대기 → 재시도 (loop에서 재연결)
    └─ 성공 → 소켓 연결 완료
    │
    ▼
③ register 메시지 전송
    │
    │  esp32_bedroom 예시:
    │  {"type":"register","device_id":"esp32_bedroom",
    │   "caps":["led","dht22","servo","seg7"]}
    │
    ▼
④ 서버 측 처리 (TCPServer._on_register)
    │  device_registry 등록
    │  WS broadcast → {"type":"device_list", "devices":[...]}
    │  브라우저 / PyQt6 카드 → ONLINE 표시
    │
    ▼
⑤ ESP32 정상 루프 진입
    │
    ├─ 명령 수신 대기 (JSON + '\n' 파싱)
    │
    └─ 10초 주기 센서 전송
         DHT22  → {"type":"sensor","device":"dht22","temp":24.5,"humidity":60.2}
         DS18B20→ {"type":"sensor","device":"ds18b20","temp":23.1}
              │
              ▼
         TCPServer._on_sensor()
              │
              ▼
         WS broadcast → {"type":"sensor_data",...}
              │
              ▼
         브라우저 / PyQt6 센서 값 + 차트 업데이트
```

---

## 5. 런타임 명령 처리 흐름

### 경로 A — 음성 명령 (웨이크 워드)

```
마이크 입력 (상시)
    │
    ▼
STTEngine IDLE 상태
    │  openwakeword 추론 (80ms 단위)
    │
    │  "헤이 IoT" 감지 (3회 연속 확인)
    ▼
LISTENING 상태 전환
    │  WS broadcast: {type:"wake_detected"}
    │  브라우저/PyQt6 → 뱃지 "🎙 LISTENING"
    │
    ▼
VAD 발화 수집
    │  에너지 > 0.01 → 발화 시작
    │  1.0초 침묵    → 발화 종료
    │  최대 10초     → 강제 종료
    │
    ▼
Whisper 추론 (executor)
    │  faster-whisper → 텍스트 변환
    │  웨이크 워드 앞부분 제거
    │  환각 패턴 필터
    │
    ▼
on_result("침실 불 켜줘")
    │
    ├─ WS broadcast: {type:"stt_result", text:"침실 불 켜줘"}
    │
    └─ CommandRouter.handle("stt_engine", {type:"voice_text",...})
              │
              ▼
         LLMEngine.parse("침실 불 켜줘")
              │  POST http://localhost:11434/api/chat
              │  ← Ollama exaone3.5
              │
              ▼
         {"cmd":"led","device_id":"esp32_bedroom","pin":2,"state":"on"}
              │
              ▼
         TCPServer.send_command("esp32_bedroom", data)
              │
              ▼
         ESP32 수신 → LED GPIO2 ON → ACK 전송
              │
              ▼
         WS broadcast: {type:"cmd_result", status:"ok"}
              │
              ▼
    브라우저 / PyQt6 로그 표시
    STTEngine → IDLE 복귀
```

### 경로 B — 버튼 트리거

```
마이크 버튼 클릭 (PyQt6 or Web App)
    │
    ├─① REST POST /stt/activate
    │       │  STTEngine.activate()
    │       └─ WS broadcast: {type:"wake_detected"}
    │
    └─② WS {type:"manual_trigger"}
            │  CommandRouter._handle_manual_trigger()
            └─ STTEngine.activate()
    │
    ▼
LISTENING 상태 전환 (경로 A와 합류)
```

### 경로 C — 수동 텍스트 입력

```
텍스트 입력 후 전송
    │
    ├─ WS   : {type:"voice_text", text:"침실 불 켜줘"}
    └─ REST : POST /voice {"text":"침실 불 켜줘"}
    │
    ▼
CommandRouter._handle_voice()  (경로 A LLM 파싱과 합류)
```

---

## 6. 종료 순서

```
Ctrl+C 입력
    │
    ▼
① uvicorn SIGINT 수신
    │
    ▼
② lifespan 종료 구간 진입
    │
    ├─ STTEngine.stop()
    │    마이크 스트림 종료
    │    처리 루프 태스크 취소
    │
    ├─ LLMEngine.close()
    │    httpx AsyncClient 종료
    │
    └─ TCPServer.stop()
         ESP32 소켓 전체 close
         asyncio 서버 종료
    │
    ▼
③ uvicorn 종료
    "Voice IoT Controller 종료 완료" 로그
```

---

## 7. 포트 및 프로토콜 요약

| 포트 | 프로토콜 | 대상 | 용도 |
|------|---------|------|------|
| 8000 | HTTP | 브라우저 / PyQt6 | REST API, Web App 서빙 |
| 8000 | WebSocket | 브라우저 / PyQt6 | 실시간 상태 동기화 |
| 9000 | TCP | ESP32 | 명령 전송 / 센서 수신 |
| 11434 | HTTP | Ollama (로컬) | LLM 추론 |

---

## 8. 환경 변수 플래그

| 변수 | 값 | 동작 |
|------|-----|------|
| `DISABLE_STT` | `1` | STTEngine 생성 안 함 → 수동 입력만 사용 |
| `DISABLE_LLM` | `1` | LLMEngine 생성 안 함 → 키워드 fallback 사용 |

```bash
# 서버만 빠르게 실행 (개발/테스트용)
DISABLE_STT=1 DISABLE_LLM=1 uvicorn server.main:app --port 8000
```

---

*Voice IoT Controller · BOOT_SEQUENCE · 2026-02-20*
