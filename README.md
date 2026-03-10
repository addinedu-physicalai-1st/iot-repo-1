# Voice IoT Controller — 1인 가구 케어 홈 시스템

한국어 음성 명령으로 ESP32 스마트홈 디바이스를 제어하는 IoT 시스템.

> **"자비스야"** 웨이크워드 → STT(Whisper) → LLM(Ollama) → TCP 명령 → ESP32 제어

---

## 주요 기능

- 한국어 음성 인식 (faster-whisper) + 웨이크워드 감지 (Porcupine)
- LLM 자연어 파싱 (Ollama - exaone3.5:7.8b)
- ESP32 TCP 통신 (LED, 서보모터, DHT22, 7세그먼트)
- 웹 대시보드 (홈 평면도, 실시간 센서, 명령 로그)
- TTS 음성 응답 (Microsoft Edge TTS)
- PIR 보안 모드 (외출/취침 시 침입 감지)
- SmartGate 2FA 출입 시스템 (얼굴인식 → 라이브니스 → 제스처 인증)
- MySQL 이벤트 로그 (장기 보관 + 검색/조회 API)
- 패턴 분석 (활동 시각화 + 이상 패턴 탐지)
- PyQt6 GUI 대시보드 (선택)

---

## 시스템 아키텍처

```
[HC User Device]  웹 브라우저 / PyQt6
       │
    HTTP / WebSocket
       │
[HC Service]  FastAPI 서버  ──TCP──  [HC DB]  MySQL
       │                   ──HTTP── [HC-AI Service]  InsightFace / YOLOv8
    UDP :5005  /  TCP :9000
       │
[HC Camera Sender]  ESP32-CAM     [HC Controller]  ESP32
```

---

## 프로젝트 구조

```
iot-repo-1/
├── server/                          # Python 백엔드
│   ├── main.py                      # FastAPI 앱 & lifespan
│   ├── tcp_server.py                # ESP32 TCP 통신
│   ├── websocket_hub.py             # 브라우저 WebSocket 허브
│   ├── command_router.py            # 명령 파싱 & 라우팅 + HMAC 서명
│   ├── api_routes.py                # REST/WS 엔드포인트 (JWT 보호, 총 21개)
│   ├── auth.py                      # JWT 토큰 발급 & 검증
│   ├── esp32_secure.py              # TCP HMAC-SHA256 서명 유틸
│   ├── llm_engine.py                # Ollama LLM 연동
│   ├── stt_engine.py                # STT + 웨이크워드
│   ├── tts_engine.py                # TTS 엔진
│   ├── camera_stream.py             # UDP MJPEG 스트림 수신 (IP 화이트리스트 적용)
│   ├── face_db.py                   # 얼굴 DB 헬퍼
│   ├── face_store.py                # 얼굴 임베딩 암호화 저장/로드 (Fernet)
│   ├── frame_analyzer.py            # 프레임 분석 (YOLOv8 / InsightFace)
│   ├── db_logger.py                 # MySQL 이벤트 로그 + 보안 감사 로그
│   └── smartgate/                   # SmartGate 2FA 서브패키지
├── protocol/
│   └── schema.py                    # TCP/WS 메시지 스키마
├── web/
│   └── index_dashboard.html         # 대시보드 (홈맵 + 센서 + 로그)
├── esp32/                           # ESP32 펌웨어 소스
│   ├── config.h.example             # WiFi/IP/HMAC 설정 템플릿 (→ config.h로 복사)
│   ├── config.h                     # WiFi/IP/HMAC 설정 (Git 미포함)
│   ├── esp32_cam/                   # ESP32-CAM MJPEG 스트리밍
│   │   └── esp32_cam.ino
│   ├── esp32_home1_hmac/            # 홈 컨트롤러 #1 (침실서보/욕실seg7·DHT11)
│   │   └── esp32_home1_hmac.ino
│   ├── esp32_home2_hmac/            # 홈 컨트롤러 #2 (LED5개/차고·현관서보/PIR)
│   │   └── esp32_home2_hmac.ino
│   └── esp32_bt_speaker/            # BT 스피커 음악 스트리밍 (ESP32-WROVER)
│       ├── esp32_bt_speaker.ino
│       └── config.h                 # WiFi/BT/중계서버 설정 (Git 미포함)
├── gui/
│   └── dashboard.py                 # PyQt6 대시보드
├── config/
│   └── settings.yaml                # 전체 설정 (민감값 → .env 분리)
├── face_db/                         # 얼굴 임베딩 DB (Git 미포함, chmod 700)
│   ├── encodings.pkl
│   └── known/
├── models/                          # 대용량 모델 (Git 미포함, MODELS.md 참조)
│   ├── 자비스야_ko_linux_v4_0_0.ppn  # Porcupine 웨이크워드 모델
│   ├── porcupine_params_ko.pv
│   └── voices.bin / voices_en.bin   # TTS 음성 모델
├── logs/
│   ├── audit/                       # 보안 감사 로그
│   └── stt_debug_*.log
├── scripts/
│   ├── init_db.sql                  # MySQL 스키마 생성
│   ├── audit.sh                     # 보안 감사 실행 스크립트
│   ├── key-gen.sh                   # 키 생성 스크립트
│   ├── token_test.sh                # JWT 토큰 테스트
│   ├── download_models.sh           # 모델 파일 다운로드
│   ├── ssl_generate_cert.sh         # HTTPS 자체 서명 인증서 생성
│   ├── run_nginx.sh                 # nginx 역방향 프록시 실행
│   └── relay_server.py              # YouTube → MP3 중계 서버 (BT Speaker용)
├── nginx/
│   ├── nginx.conf                   # HTTPS 역방향 프록시 설정
│   ├── ssl/                         # 인증서 (Git 미포함)
│   └── logs/
├── docs/                            # 개발 문서
│   ├── NGINX_HTTPS_SETUP.md
│   ├── BOOT_SEQUENCE.md
│   ├── RUN_AND_TEST.md
│   └── ESP32_UPLOAD_GUIDE_Final.md
├── tests/
│   ├── conftest.py
│   ├── test_e2e.py
│   └── test_tts_debug.py
├── data/
│   └── pir_mode.json                # PIR 보안 모드 상태 저장
├── .github/
│   └── workflows/
│       └── security.yml             # CVE 자동 스캔 (pip-audit, 매주 월요일)
├── yolov8n.pt                       # YOLOv8 객체 감지 모델
├── pipeline_monitor.py              # 파이프라인 성능 모니터
├── PIPELINE_MONITOR_GUIDE.md
├── MODELS.md
├── .env_example                     # 환경변수 템플릿 (→ .env로 복사)
├── requirements.txt
├── run_server.sh
└── kill_server.sh
```

---

## 요구사항

- Python 3.12+
- MySQL 8.0+
- Ollama (LLM 서버)
- ESP32 디바이스 (실물 또는 시뮬레이션)

---

## 설치 & 실행

### 1. Python 패키지 설치

```bash
pip install -r requirements.txt
```

### 2. 모델 파일 준비

```bash
./scripts/download_models.sh
```

### 3. 환경 변수 설정 (.env)

```bash
cp .env_example .env
nano .env
```

| 변수 | 설명 | 예시 |
|------|------|------|
| `MIC_DEVICE` | 마이크 디바이스 인덱스 | `11` |
| `PORCUPINE_ACCESS_KEY` | Picovoice 웨이크워드 AccessKey | `xxxxxxxx==` |
| `DB_HOST` | MySQL 호스트 | `localhost` |
| `DB_USER` | MySQL 사용자 | `iot_user` |
| `DB_PASSWORD` | MySQL 비밀번호 | `iot_password` |
| `OLLAMA_HOST` | Ollama 서버 URL | `http://localhost:11434` |
| `LLM_MODEL` | LLM 모델명 | `exaone3.5:latest` |
| `JWT_SECRET` | JWT 서명 키 (최초 1회 자동 생성) | `run_server.sh` 자동 처리 |
| `ESP32_SECRET` | TCP HMAC 서명 공유 키 | `my-secret-key` |
| `SMARTGATE_SEQUENCE` | SmartGate 제스처 인증 시퀀스 | `1,0,3` |
| `CAM_ALLOWED_IPS` | UDP 카메라 허용 IP 목록 (쉼표 구분, settings.yaml 오버라이드) | `192.168.0.19,192.168.0.20` |
| `RELAY_PORT` | YouTube 중계 서버 포트 | `8080` |
| `RELAY_ALLOWED_IPS` | 중계 서버 접근 허용 IP (ESP32 WiFi IP, 쉼표 구분) | `192.168.0.xx` |
| `BT_SPEAKER_URL` | ESP32 BT 스피커 웹서버 URL | `http://192.168.0.xx` |

> `.env`는 `.gitignore`에 포함되어 Git에 커밋되지 않습니다.

### 4. ESP32 펌웨어 설정 (config.h)

```bash
cd esp32
cp config.h.example config.h
nano config.h
```

| 항목 | 설명 | 예시 |
|------|------|------|
| `WIFI_SSID` | WiFi 네트워크 이름 | `"MyWiFi"` |
| `WIFI_PASSWORD` | WiFi 비밀번호 | `"MyPassword"` |
| `SERVER_IP` | Python 서버 IP | `"192.168.0.100"` |
| `ESP32_SECRET` | HMAC 서명 공유 키 (서버 `.env`와 동일) | `"my-secret-key"` |

> `config.h`는 각 펌웨어 폴더(`esp32_cam/`, `esp32_home1_hmac/`, `esp32_home2_hmac/`)에 복사해야 합니다.

### 5. 파일 권한 초기화

```bash
./scripts/harden_setup.sh
```

> `settings.yaml` (600), `.env` (600), `face_db/` (700) 권한이 자동으로 설정됩니다.

### 6. MySQL 데이터베이스 설정

```bash
sudo mysql < scripts/init_db.sql
```

생성되는 항목:
- DB: `iot_smart_home`
- 테이블: `event_logs`, `security_media`, `security_audit`

### 7. Ollama LLM 실행

```bash
ollama run exaone3.5:7.8b
```

### 8. 서버 실행 / 종료

```bash
./run_server.sh    # 서버 시작 (JWT_SECRET 최초 1회 자동 생성)
./kill_server.sh   # 서버 종료
```

### 기능 비활성화 (선택)

| 변수 | 설명 |
|------|------|
| `DISABLE_STT=1` | STT/웨이크워드 비활성화 |
| `DISABLE_TTS=1` | TTS 비활성화 |
| `DISABLE_DB=1`  | MySQL 로깅 비활성화 |

```bash
DISABLE_STT=1 DISABLE_TTS=1 ./run_server.sh
```

---

## 웹 인터페이스

| URL | 페이지 |
|-----|--------|
| `http://localhost:8000/` | 메인 페이지 |
| `http://localhost:8000/dashboard` | 대시보드 (홈맵 + 센서 + 로그) |

**원격 접속 시 HTTPS 필요** (브라우저 마이크 권한). 설정: [docs/NGINX_HTTPS_SETUP.md](docs/NGINX_HTTPS_SETUP.md)

### 대시보드 기능

- **HOUSE MAP** — 3D 홈 평면도, 디바이스 상태 실시간 표시
- **SENSOR PANEL** — DHT22 온도/습도, PIR 보안 상태
- **BT 스피커** — YouTube 음악 스트리밍 제어 (재생/정지 토글, 이전/다음, 볼륨, 플레이리스트, BT 수동 연결/해제)
- **COMMAND LOG** — 명령 실행 이력
- **DB EVENT LOG** — MySQL 이벤트 로그 검색/조회 + 패턴 분석

---

## REST API

모든 엔드포인트는 JWT Bearer 토큰 인증이 필요합니다.

```
Authorization: Bearer <token>
```

### 기본 API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET    | `/devices` | 연결된 ESP32 목록 |
| POST   | `/command` | 수동 명령 전송 |
| POST   | `/voice`   | 음성 텍스트 → 명령 실행 |
| GET    | `/status`  | 서버 상태 요약 |
| POST   | `/stt/activate` | STT 수동 활성화 |

### 이벤트 로그 API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/logs/search` | 이벤트 로그 검색 (category / level / device_id / date / keyword) |
| GET | `/logs/categories` | 사용된 카테고리 목록 |
| GET | `/logs/stats` | 로그 통계 요약 |
| GET | `/logs/{id}` | 로그 상세 조회 |

### 패턴 분석 API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/logs/pattern/hourly` | 시간대별 활동 분포 |
| GET | `/logs/pattern/daily` | 일별 이벤트 타임라인 |
| GET | `/logs/pattern/categories` | 카테고리별 분포 |
| GET | `/logs/pattern/devices` | 디바이스별 활동량 |
| GET | `/logs/pattern/anomalies` | 이상 패턴 탐지 |

### BT 스피커 API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/bt-speaker/status` | BT 스피커 상태 (WiFi, BT, 스트리밍, 볼륨) |
| GET | `/bt-speaker/playlist` | 플레이리스트 조회 |
| POST | `/bt-speaker/playlist/add` | YouTube URL 추가 (`url`, `title`) |
| POST | `/bt-speaker/playlist/del` | 트랙 삭제 (`idx`) |
| POST | `/bt-speaker/playlist/clear` | 플레이리스트 초기화 |
| POST | `/bt-speaker/play` | 트랙 재생 (`idx`) |
| POST | `/bt-speaker/stop` | 재생 정지 |
| POST | `/bt-speaker/prev` | 이전 트랙 |
| POST | `/bt-speaker/next` | 다음 트랙 |
| POST | `/bt-speaker/volume` | 볼륨 설정 (`v`: 0-100) |
| POST | `/bt-speaker/bt-connect` | BT 스피커 블루투스 수동 연결 |
| POST | `/bt-speaker/bt-disconnect` | BT 스피커 블루투스 수동 해제 |

### SmartGate API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET  | `/smartgate/status` | SmartGate 현재 상태 |
| GET  | `/smartgate/registered-faces` | 등록된 얼굴 목록 |
| POST | `/smartgate/reload-faces` | 얼굴 DB 리로드 |

---

## 디바이스 배치 (settings.yaml)

| 디바이스 ID | 위치 | 기능 |
|-------------|------|------|
| `esp32_home1` | 홈 컨트롤러 #1 | 침실 서보(커튼), 욕실 7세그먼트, DHT11 |
| `esp32_home2` | 홈 컨트롤러 #2 | LED 5개, 차고·현관 서보, PIR 센서 |
| `esp32_cam`   | 현관 카메라 | MJPEG UDP 스트리밍 (SmartGate) |
| `esp32_bt_speaker` | BT 스피커 (ESP32-WROVER) | WiFi + BT A2DP 음악 스트리밍 |

### TCP 포트

| 포트 | 용도 |
|------|------|
| 8000 | FastAPI (HTTP + WebSocket) |
| 9000 | ESP32 TCP 서버 |
| 5005 | ESP32-CAM UDP MJPEG 스트림 |
| 8080 | YouTube → MP3 중계 서버 (BT Speaker) |

---

## BT 스피커 음악 스트리밍 (ESP32-WROVER)

ESP32-WROVER(CH-340)가 WiFi + Bluetooth A2DP로 PLEIGO BS15 스피커에 YouTube 음악을 스트리밍합니다.

### 아키텍처

```
[웹 대시보드] → FastAPI /bt-speaker/* (JWT) → [ESP32-WROVER :80] → HTTP → [PC 중계서버 :8080]
                                                      │                           │
                                                BT A2DP Source              yt-dlp + ffmpeg
                                                      │                     (YouTube → MP3)
                                               [BT 스피커]
```

### 하드웨어

- ESP32-WROVER (CH-340 USB, PSRAM 탑재)
- PLEIGO BS15 (Bluetooth 5.3 스피커)

### 아두이노 IDE 설정

| 항목 | 설정 |
|------|------|
| 보드 | `ESP32 Wrover Kit` |
| Partition Scheme | `Huge APP (3MB No OTA/1MB SPIFFS)` |
| PSRAM | `Enabled` |
| Upload Speed | `921600` |

### 필요 라이브러리

- [ESP32-A2DP](https://github.com/pschatzmann/ESP32-A2DP) — Sketch → Include Library → Add .ZIP Library
- [arduino-libhelix](https://github.com/pschatzmann/arduino-libhelix) — 동일 방법

### ESP32 펌웨어 설정

```bash
cd esp32/esp32_bt_speaker
nano config.h  # WiFi SSID/PW, BT 스피커 이름, 중계서버 IP 설정
```

### 중계 서버 실행

```bash
# 최초 1회: venv 생성 및 패키지 설치
python3 -m venv .relay-venv
.relay-venv/bin/pip install flask yt-dlp

# .env에 RELAY_PORT, RELAY_ALLOWED_IPS 설정 후
# run_server.sh가 자동으로 relay 서버도 함께 시작합니다.
./run_server.sh    # 전체 서버 시작 (relay 포함)
./kill_server.sh   # 전체 서버 종료 (relay 포함)
```

### 사용

1. ESP32-WROVER에 펌웨어 업로드 후 시리얼 모니터에서 IP 확인
2. `.env`에 `BT_SPEAKER_URL=http://<ESP32_IP>` 설정
3. `./run_server.sh`로 서버 시작 (relay 서버 자동 포함)
4. 웹 대시보드 BT 스피커 카드에서 YouTube URL 추가 → 재생 → BT 스피커 출력

---

## 보안 아키텍처

본 프로젝트는 **NIST SP 800-213** 및 **OWASP IoT Top 10** 기반으로 보안 조치를 적용합니다.

### 보안 조치 현황

| # | 우선순위 | 항목 | 구현 방법 | 상태 |
|---|----------|------|-----------|------|
| 1 | HIGH | TCP :9000 통신 암호화 | HMAC-SHA256 서명 (`esp32_secure.py`) | ✅ 완료 |
| 2 | HIGH | FastAPI 엔드포인트 인증 | JWT Bearer 토큰 전체 34개 엔드포인트 적용 (`auth.py`) | ✅ 완료 |
| 3 | HIGH | 얼굴 임베딩 벡터 암호화 | Fernet 대칭 암호화 (`face_store.py`) | ✅ 코드 완료 / 통합 테스트 필요 |
| 4 | MEDIUM | ESP32 OTA 서명 검증 | ESP-IDF Secure Boot v2 | ⛔ 제외 (ESP-IDF 별도) |
| 5 | MEDIUM | UDP MJPEG 스트림 IP 필터링 | IP 화이트리스트 (`camera_stream.py` v2.2) | ✅ 완료 |
| 6 | MEDIUM | settings.yaml 접근 권한 제한 | chmod 600 + 민감값 `.env` 분리 | ✅ 완료 |
| 7 | LOW | 의존성 CVE 스캔 자동화 | pip-audit + GitHub Actions (매주 월요일) | ✅ 완료 |
| 8 | LOW | ESP32 JTAG 포트 비활성화 | ESP-IDF eFuse JTAG_DISABLE | ⛔ 제외 (ESP-IDF 별도) |
| 9 | LOW | 보안 이벤트 감사 로그 분리 | `security_audit` 테이블 + SHA-256 체인 해시 (`db_logger.py`) | ✅ 완료 |

> 진행률: HIGH 100% (3/3) · MEDIUM 67% (2/3) · LOW 67% (2/3)

### 통신 구간별 보안

| 구간 | 프로토콜 | 보안 조치 |
|------|----------|-----------|
| UI → Server | HTTP / WebSocket | JWT Bearer 인증 |
| Server → ESP32 | TCP :9000 | HMAC-SHA256 서명 + 타임스탬프 |
| Server → ESP32-WROVER | HTTP (프록시) | JWT 인증 + 내부 네트워크 |
| ESP32-CAM → Server | UDP :5005 | IP 화이트리스트 (`settings.yaml camera.allowed_ips`) |
| ESP32-WROVER → Relay | HTTP :8080 | IP 화이트리스트 (`.env RELAY_ALLOWED_IPS`) |

### TCP HMAC 서명 포맷

```json
{
  "ts": "1741305600",
  "sig": "<HMAC-SHA256 hex>",
  "cmd": { ... }
}
```

### JWT 인증 흐름

```
클라이언트                   FastAPI 서버
    │── POST /auth/token ──▶  JWT 발급 (HS256)
    │◀─ { access_token } ──  
    │
    │── GET /devices ────────▶ verify_token() 검증
    │   Authorization: Bearer  ◀─ 200 OK / 401 Unauthorized
```

### SmartGate 2FA 인증 상태 머신

```
IDLE → ARMED → FACE_OK → LIVENESS → GESTURE_OK → 게이트 열림
                                                       │
                                              5초 후 자동 잠금
                                            120초 쿨다운 적용
```

- **라이브니스 챌린지**: blink / yaw (ESP-CAM 환경 최적화)
- **제스처 시퀀스**: `SMARTGATE_SEQUENCE` 환경변수로 관리 (기본값 `.env`)
- **Lockout**: 인증 실패 시 `lockout_sec` 동안 잠금

### 보안 관련 파일

| 파일 | 역할 |
|------|------|
| `server/auth.py` | JWT 토큰 발급 및 `verify_token()` 의존성 |
| `server/esp32_secure.py` | TCP HMAC-SHA256 서명 생성 유틸 |
| `server/face_store.py` | 얼굴 임베딩 Fernet 암호화 저장/로드/마이그레이션 |
| `server/db_logger.py` | `security_audit` 테이블 감사 로그 |
| `scripts/harden_setup.sh` | 파일 권한 초기화 (`chmod 600/700`) |
| `.github/workflows/security.yml` | CVE 자동 스캔 워크플로우 |
| `docs/esp32_hmac_verify.cpp` | ESP32 펌웨어 측 HMAC 검증 참조 코드 |

---

## API 사용 예시

```bash
# 토큰 발급
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "..."}'

# 디바이스 목록 (JWT 필요)
curl http://localhost:8000/devices \
  -H "Authorization: Bearer <token>"

# 명령 전송
curl -X POST http://localhost:8000/command \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"device_id": "esp32_home", "action": "led_on", "room": "living"}'

# 로그 검색
curl "http://localhost:8000/logs/search?category=security&limit=10" \
  -H "Authorization: Bearer <token>"

# 이상 패턴 탐지
curl "http://localhost:8000/logs/pattern/anomalies?threshold=2.0" \
  -H "Authorization: Bearer <token>"
```

---

## 참조 문서

| 문서 | 경로 |
|------|------|
| HTTPS nginx 설정 | `docs/NGINX_HTTPS_SETUP.md` |
| ESP32 HMAC 검증 코드 | `docs/esp32_hmac_verify.cpp` |
| IoT 보안 조치 계획 | `IoT_Security_Plan.xlsx` |
| 모델 파일 목록 | `MODELS.md` |

---

*iot-repo-1 · Voice IoT Controller · Stephen · 2026-03-10*
