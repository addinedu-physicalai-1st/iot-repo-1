# Voice IoT Controller — 실행 및 테스트 가이드

---

## 1. 환경 준비 (최초 1회)

```bash
cd ~/dev_ws/voice_iot_controller

# 가상환경 생성 및 활성화
python -m venv venv
source venv/bin/activate

# 의존성 설치
pip install -r requirements.txt

# ROS2 환경 충돌 해결 (lark 설치)
pip install lark

# 웨이크 워드 모델 다운로드
python -c "from openwakeword.utils import download_models; download_models()"
```

---

## 2. 서버 실행 순서

### 풀 모드 (STT + LLM 포함)

```bash
# 터미널 1: Ollama 먼저 실행 (LLM 사용 시)
ollama serve
ollama pull exaone3.5:latest

# 터미널 2: FastAPI 서버 실행
cd ~/dev_ws/voice_iot_controller
source venv/bin/activate
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

### 빠른 실행 (개발/테스트용, STT·LLM 비활성화)

```bash
cd ~/dev_ws/voice_iot_controller
source venv/bin/activate
DISABLE_STT=1 DISABLE_LLM=1 uvicorn server.main:app --host 0.0.0.0 --port 8000
```

### 서버 준비 완료 확인 로그

```
====================================================
 Voice IoT Controller  v0.3
====================================================
  TCP  : 0.0.0.0:9000
  HTTP : 0.0.0.0:8000
  WS   : ws://0.0.0.0:8000/ws
----------------------------------------------------
  LLM  : ✅ exaone3.5:latest   (또는 ⛔ 비활성화)
  STT  : ✅ base / wake=헤이IoT (또는 ⛔ 비활성화)
====================================================
서버 준비 완료 - 요청 대기 중   ← 이 로그 확인 후 사용
```

---

## 3. 클라이언트 실행 순서

```bash
# 터미널 3: PyQt6 대시보드
cd ~/dev_ws/voice_iot_controller
source venv/bin/activate
python -m gui.dashboard

# Web App: 브라우저에서 접속
http://localhost:8000
```

---

## 4. ESP32 연결 순서

```
1. esp32_client.ino 상단 #define 수정
   #define DEVICE_ID  "esp32_bedroom"   ← 유닛별 변경
   #define WIFI_SSID  "your_ssid"
   #define WIFI_PASS  "your_password"
   #define SERVER_IP  "192.168.x.x"     ← 서버 PC IP

2. Arduino IDE → 업로드

3. 시리얼 모니터 확인
   WiFi connected. IP: 192.168.x.x
   TCP connected to 192.168.x.x:9000
   Registered as esp32_bedroom

4. 대시보드 카드 → ONLINE 표시 확인
```

---

## 5. 포트 충돌 시 해결

```bash
# 점유 프로세스 확인
fuser 8000/tcp
fuser 9000/tcp

# 강제 종료
fuser -k 8000/tcp
fuser -k 9000/tcp
```

---

## 6. 테스트 실행 순서

### Step 1 — Mock 테스트 (서버 불필요)

서버 없이 스키마, 파싱 로직만 검증합니다.

```bash
cd ~/dev_ws/voice_iot_controller
source venv/bin/activate
python -m pytest tests/test_e2e.py -v -m "no_server"
```

**기대 결과:**
```
tests/test_e2e.py::TestMock::test_mock_schema_led          PASSED
tests/test_e2e.py::TestMock::test_mock_schema_servo        PASSED
tests/test_e2e.py::TestMock::test_mock_schema_invalid_state PASSED
tests/test_e2e.py::TestMock::test_mock_simple_parse_all    PASSED
tests/test_e2e.py::TestMock::test_mock_simple_parse_room   PASSED
5 passed
```

---

### Step 2 — E2E 전체 테스트 (서버 실행 후)

```bash
# 터미널 1: 서버 실행
DISABLE_STT=1 DISABLE_LLM=1 uvicorn server.main:app --host 0.0.0.0 --port 8000

# 서버 준비 확인 후 터미널 2에서 실행
python -m pytest tests/test_e2e.py -v
```

**테스트 케이스 목록:**

| # | 케이스 | 내용 | ESP32 필요 |
|---|--------|------|-----------|
| TC-01 | `test_TC01_server_status` | GET /status → server=running | ❌ |
| TC-02 | `test_TC02_device_list` | GET /devices → 리스트 반환 | ❌ |
| TC-04 | `test_TC04_manual_command` | POST /command LED 명령 | ⚠️ 없으면 fail |
| TC-05 | `test_TC05_voice_pipeline` | POST /voice 침실 불 켜줘 | ⚠️ 없으면 fail |
| TC-05b | `test_TC05b_voice_all_devices` | POST /voice 전체 불 꺼줘 | ⚠️ 없으면 fail |
| TC-06 | `test_TC06_stt_activate` | POST /stt/activate | ❌ |
| TC-07 | `test_TC07_ws_manual_cmd` | WS manual_cmd | ⚠️ 없으면 fail |
| TC-08 | `test_TC08_ws_voice_text` | WS voice_text | ⚠️ 없으면 fail |
| TC-09 | `test_TC09_ws_manual_trigger` | WS manual_trigger | ❌ |
| TC-10 | `test_TC10_all_broadcast_rest` | POST /command device_id=all | ⚠️ 없으면 fail |
| TC-11 | `test_TC11_stt_state_in_status` | /status stt_state 필드 | ❌ |
| TC-12a | `test_TC12a_invalid_cmd` | 잘못된 cmd → fail | ❌ |
| TC-12b | `test_TC12b_empty_voice_text` | 빈 텍스트 → 422/fail | ❌ |
| TC-12c | `test_TC12c_unknown_ws_type` | unknown WS type → fail | ❌ |
| Mock×5 | `TestMock` | 스키마/파싱 단위 검증 | ❌ |

> ⚠️ ESP32 미연결 시 `status: fail` 응답 → 테스트는 PASSED (fail 응답도 정상 처리로 설계)

**기대 결과 (서버만 실행, ESP32 없음):**
```
20 passed in 0.54s
```

---

### Step 3 — 특정 케이스만 실행

```bash
# REST 테스트만
python -m pytest tests/test_e2e.py -v -k "TestREST"

# WS 테스트만
python -m pytest tests/test_e2e.py -v -k "TestWebSocket"

# 특정 TC만
python -m pytest tests/test_e2e.py -v -k "TC01 or TC02 or TC06"

# 실패한 케이스만 재실행
python -m pytest tests/test_e2e.py -v --lf
```

---

### Step 4 — 다른 서버 주소로 테스트

```bash
TEST_HOST=192.168.0.10 TEST_PORT=8000 python -m pytest tests/test_e2e.py -v
```

---

## 7. 전체 실행 체크리스트

```
[ ] venv 활성화 확인 (프롬프트에 (venv) 표시)
[ ] 포트 충돌 없음 (8000, 9000)
[ ] 서버 "준비 완료" 로그 확인
[ ] Mock 테스트 5/5 통과
[ ] E2E 전체 테스트 20/20 통과
[ ] (선택) ESP32 연결 후 ONLINE 카드 확인
[ ] (선택) 음성 명령 실제 동작 확인
```

---

## 8. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `Address already in use` | 이전 서버 프로세스 잔존 | `fuser -k 8000/tcp 9000/tcp` |
| `ModuleNotFoundError: lark` | ROS2 환경 충돌 | `pip install lark` |
| `Event loop is closed` | pytest-asyncio 버전 이슈 | pytest.ini `asyncio_mode=auto` 확인 |
| `20 deselected / 0 selected` | `-m no_server` 마커 인식 못함 | pytest.ini markers 항목 확인 |
| ESP32 OFFLINE 유지 | SERVER_IP 불일치 | `ip addr` 로 서버 IP 재확인 후 ino 수정 |

---

*Voice IoT Controller · RUN_AND_TEST · 2026-02-20*
