# Voice IoT Controller — 일일 작업 요약
**날짜:** 2026-02-21  
**프로젝트:** `~/dev_ws/voice_iot_controller`  
**버전:** main v0.3 · stt_engine v3.2 · settings v0.4

---

## 오늘 작업 파일

| 파일 | 경로 | 주요 변경 |
|------|------|-----------|
| `main.py` | `server/main.py` | FastAPI lifespan, STT/LLM 파이프라인 통합 |
| `stt_engine.py` | `server/stt_engine.py` | Whisper 폴백 웨이크 워드 v3.2 최적화 |
| `settings.yaml` | `config/settings.yaml` | `whisper` 섹션 → `stt` 통합, Porcupine 설정 추가 |

---

## 실행 결과 (13:16 기준)

```
✅ Ollama 연결 성공 — exaone3.5:latest
✅ STTEngine — base / wake=자비스야
✅ TCP 서버 — 0.0.0.0:9000
✅ ESP32 연결 — esp32_bedroom (192.168.35.229)
✅ WebSocket 클라이언트 — ws_client_0001
⚠️  noisereduce 미설치 → 노이즈 억제 비활성화
```

---

## stt_engine.py v3.2 주요 변경사항

### 웨이크 워드 전략 변경
- **OWW(openwakeword) 비활성화** (`use_oww=False` 기본값)  
  → 한국어 커스텀 모델 부재로 오탐률 높음
- **Whisper 폴백 주력 모드** 로 전환
  - 슬라이딩 버퍼: `1.5s → 1.2s` (더 빠른 감지)
  - 슬라이딩 스텝: `0.5s → 0.3s` (더 촘촘한 탐지)
  - 웨이크 추론: `beam_size=3 → 1` (최속 그리디 디코딩)

### 웨이크 워드 변경 이력
| 버전 | 웨이크 워드 | 이유 |
|------|------------|------|
| v3.0 | `헤이 코코` | 한국어 시도 |
| v3.1 | `Hey Jarvis` | OWW 영어 호환 |
| v3.2 | `자비스야` | Whisper 폴백 전용, OWW 비활성 |

### 인식 변형 패턴 테이블 (v3.2 신규)
```python
wake_variants = [
    "자비스야", "자비야", "자비스",
    "쟈비스야", "쟈비야", "쟈비스",
    "재비스야", "재비스", "재비야",
    "자부스야", "자부스",
    "jabisya", "jabis ya", "jabis",
    "jarvis ya", "jarvisya",
    "어 자비스야", "야 자비스야",
]
```

### VAD 파라미터 조정 (v3.0 → 현재)
| 파라미터 | 이전 | 현재 | 목적 |
|---------|------|------|------|
| `VAD_ENERGY_THRESH` | 0.01 | 0.02 | 배경음 오탐 방지 |
| `VAD_SILENCE_SEC` | 1.0 | 1.2 | 발화 종료 오탐 방지 |
| `WAKE_FALLBACK_BUF_SEC` | 1.5 | 1.2 | 감지 지연 감소 |
| `WAKE_FALLBACK_STEP_SEC` | 0.5 | 0.3 | 슬라이딩 촘촘히 |

---

## main.py v0.3 구조

### 전체 파이프라인
```
마이크 → STTEngine("자비스야") → LLMEngine(Ollama: exaone3.5)
                                       ↓
WebSocket/REST → CommandRouter → TCPServer → ESP32
```

### 핵심 구성요소 연결
```python
tcp_server.ws_broadcast = ws_hub.broadcast      # TCP → WS 브로드캐스트
ws_hub._on_message = command_router.handle       # WS → 명령 라우터
```

### 환경 변수 플래그
| 변수 | 효과 |
|------|------|
| `DISABLE_STT=1` | STT 비활성화, 수동 입력만 사용 |
| `DISABLE_LLM=1` | LLM 비활성화, 키워드 fallback 사용 |

### STT 콜백 타이머 구조
```
[TIMER] [1] Whisper 추론
[TIMER] [2] on_result → CommandRouter(LLM→ESP32)
[TIMER] 전체: 오디오 수집 + Whisper + LLM + ESP32 합산
```

---

## settings.yaml v0.4 구조

### STT 섹션 (신규 통합)
```yaml
stt:
  model_size: "base"
  language: "ko"
  device: "cpu"
  porcupine_access_key: "..."
  porcupine_model_path: "models/자비스야_ko_linux_v4_0_0.ppn"
  porcupine_params_path: "models/porcupine_params_ko.pv"
  mic_device: 10
```

> **Note:** Porcupine 설정은 향후 네이티브 웨이크 워드 전환 시 사용 예정.  
> 현재는 `use_oww=False` 이므로 실제 로드되지 않음.

### 등록된 ESP32 디바이스
| ID | 위치 | 캡 |
|----|------|----|
| `esp32_garage` | 차고 | led, servo |
| `esp32_bathroom` | 욕실 | led, temp |
| `esp32_bedroom` | 침실 | led, dht22, seg7, servo |
| `esp32_entrance` | 현관 | led, servo |

---

## 미해결 이슈 / 다음 작업

- [ ] `noisereduce` 설치: `pip install noisereduce scipy`
- [ ] Porcupine `.ppn` 모델 다운로드 및 경로 확인
- [ ] `esp32_garage`, `esp32_bathroom`, `esp32_entrance` TCP 연결 테스트
- [ ] Whisper 폴백 `"자비스야"` 실제 인식률 측정 (오탐/미탐 통계)
- [ ] `stt_engine.py` → Porcupine 네이티브 연동 전환 검토 (v4.0)

---

## 실행 명령

```bash
cd ~/dev_ws/voice_iot_controller

# 전체 실행
./run_server.sh

# STT 없이 서버만
DISABLE_STT=1 uvicorn server.main:app --host 0.0.0.0 --port 8000

# LLM 없이 (키워드 fallback)
DISABLE_LLM=1 uvicorn server.main:app --host 0.0.0.0 --port 8000
```
