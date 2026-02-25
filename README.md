# Voice IoT Controller - 1인 가구 케어 홈 시스템

한국어 음성 명령으로 ESP32 스마트홈 디바이스를 제어하는 IoT 시스템.

"자비스야" 웨이크워드 → STT(Whisper) → LLM(Ollama) → TCP 명령 → ESP32 제어

## 주요 기능

- 한국어 음성 인식 (faster-whisper) + 웨이크워드 감지 (Porcupine)
- LLM 자연어 파싱 (Ollama - exaone3.5:7.8b)
- ESP32 TCP 통신 (LED, 서보모터, DHT22, 7세그먼트)
- 웹 대시보드 (홈 평면도, 실시간 센서, 명령 로그)
- TTS 음성 응답 (Microsoft Edge TTS)
- PIR 보안 모드 (외출/취침 시 침입 감지)
- MySQL 이벤트 로그 (장기 보관 + 검색/조회 API)
- 패턴 분석 (활동 시각화 + 이상 패턴 탐지)
- PyQt6 GUI 대시보드 (선택)

## 프로젝트 구조

```
iot-repo-1/
├── server/                 # Python 백엔드
│   ├── main.py             # FastAPI 앱 & lifespan
│   ├── tcp_server.py       # ESP32 TCP 통신
│   ├── websocket_hub.py    # 브라우저 WebSocket 허브
│   ├── command_router.py   # 명령 파싱 & 라우팅
│   ├── api_routes.py       # REST/WS 엔드포인트
│   ├── llm_engine.py       # Ollama LLM 연동
│   ├── stt_engine.py       # STT + 웨이크워드
│   ├── tts_engine.py       # TTS 엔진
│   └── db_logger.py        # MySQL 이벤트 로그 + 패턴 분석 (SR-3.1/3.2/3.3)
├── protocol/
│   └── schema.py           # TCP/WS 메시지 스키마
├── web/
│   ├── index_main.html     # 메인 페이지
│   └── index_dashboard.html # 대시보드 (홈맵 + 센서 + 로그 + 패턴 분석)
├── esp32/                  # ESP32 관련 문서
├── gui/
│   └── dashboard.py        # PyQt6 대시보드
├── config/
│   └── settings.yaml       # 전체 설정 (서버/STT/TTS/LLM/디바이스/DB)
├── scripts/
│   ├── init_db.sql         # MySQL 스키마 생성
│   └── download_models.sh  # 모델 파일 다운로드
├── models/                 # 대용량 모델 (Git 미포함, MODELS.md 참조)
├── tests/                  # 테스트
├── docs/                   # 개발 문서
├── esp32_client_last.ino   # ESP32 클라이언트 펌웨어
├── requirements.txt        # Python 의존성
├── run_server.sh           # 서버 시작 스크립트
└── kill_server.sh          # 서버 종료 스크립트
```

## 요구사항

- Python 3.12+
- MySQL 8.0+ (이벤트 로그용)
- Ollama (LLM 서버)
- ESP32 디바이스 (실물 또는 시뮬레이션)

## 설치 & 실행

### 1. Python 패키지 설치

```bash
pip install -r requirements.txt
```

### 2. 모델 파일 준비

MODELS.md를 참고하여 `models/` 디렉터리에 필요한 파일을 배치합니다.

```bash
./scripts/download_models.sh
```

### 3. MySQL 데이터베이스 설정

```bash
# MySQL 스키마 생성 (DB + 테이블 + 사용자)
sudo mysql < scripts/init_db.sql
```

생성되는 항목:
- DB: `iot_smart_home`
- 사용자: `iot_user` / `iot_password`
- 테이블: `event_logs`, `security_media`

### 4. Ollama LLM 실행

```bash
ollama run exaone3.5:7.8b
```

### 5. 서버 실행

```bash
./run_server.sh
```

### 6. 서버 종료

```bash
./kill_server.sh
```

### 환경 변수 (선택)

| 변수 | 설명 |
|------|------|
| `DISABLE_STT=1` | STT/웨이크워드 비활성화 |
| `DISABLE_TTS=1` | TTS 비활성화 |
| `DISABLE_DB=1`  | MySQL 로깅 비활성화 |

```bash
# 예: STT/TTS 없이 서버만 실행
DISABLE_STT=1 DISABLE_TTS=1 ./run_server.sh
```

## 웹 인터페이스

서버 실행 후 브라우저에서 접속:

| URL | 페이지 |
|-----|--------|
| `http://localhost:8000/` | 메인 페이지 |
| `http://localhost:8000/dashboard` | 대시보드 (홈맵 + 센서 + 로그) |

### 대시보드 기능

- **HOUSE MAP** — 3D 홈 평면도, 디바이스 상태 실시간 표시
- **SENSOR PANEL** — DHT22 온도/습도, PIR 보안 상태
- **COMMAND LOG** — 명령 실행 이력
- **DB EVENT LOG** — MySQL 이벤트 로그 검색/조회
  - 즉시 필터링 (카테고리/레벨/디바이스/날짜/키워드)
  - 패턴 분석 탭 (일별 타임라인, 시간대별 분포, 카테고리/디바이스 분포, 평일vs주말, 이상 패턴 탐지)

## REST API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET    | `/devices` | 연결된 ESP32 목록 |
| POST   | `/command` | 수동 명령 전송 |
| POST   | `/voice`   | 음성 텍스트 → 명령 실행 |
| GET    | `/status`  | 서버 상태 요약 |
| POST   | `/stt/activate` | STT 수동 활성화 |

### 이벤트 로그 API (SR-3.2)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/logs/search` | 이벤트 로그 검색 (필터: category, level, device_id, date_from, date_to, keyword) |
| GET | `/logs/categories` | 사용된 카테고리 목록 |
| GET | `/logs/stats` | 로그 통계 요약 |
| GET | `/logs/{id}` | 로그 상세 조회 |

### 패턴 분석 API (SR-3.3)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/logs/pattern/hourly` | 시간대별 활동 분포 (day_type: weekday/weekend) |
| GET | `/logs/pattern/daily` | 일별 이벤트 타임라인 |
| GET | `/logs/pattern/categories` | 카테고리별 분포 |
| GET | `/logs/pattern/devices` | 디바이스별 활동량 |
| GET | `/logs/pattern/anomalies` | 이상 패턴 탐지 (threshold 파라미터) |

### API 사용 예시

```bash
# 로그 검색
curl "http://localhost:8000/logs/search?category=device_control&limit=10"
curl "http://localhost:8000/logs/search?date_from=2026-02-01&device_id=esp32_bedroom"
curl "http://localhost:8000/logs/search?keyword=LED"

# 통계
curl "http://localhost:8000/logs/stats"

# 패턴 분석
curl "http://localhost:8000/logs/pattern/hourly?day_type=weekday"
curl "http://localhost:8000/logs/pattern/daily?date_from=2026-02-01"
curl "http://localhost:8000/logs/pattern/anomalies?threshold=2.0"
```

## 디바이스 배치 (settings.yaml)

| 디바이스 ID | 위치 | 기능 |
|-------------|------|------|
| `esp32_garage` | 차고 | LED, 서보(문) |
| `esp32_bathroom` | 욕실 | LED, 온도 센서 |
| `esp32_bedroom` | 침실 | LED, DHT22, 7세그먼트, 서보 |
| `esp32_entrance` | 현관 | LED, 서보(문) |

## TCP 포트

| 포트 | 용도 |
|------|------|
| 8000 | FastAPI (HTTP + WebSocket) |
| 9000 | ESP32 TCP 서버 |
