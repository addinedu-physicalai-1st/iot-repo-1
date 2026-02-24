# Pipeline Monitor — 사용 가이드

> Voice IoT Controller 파이프라인 성능 모니터링 도구  
> Wake Word → STT → LLM → TTS 각 단계 소요 시간 추적

---

## 개요

`pipeline_monitor.py`는 Voice IoT Controller의 음성 처리 파이프라인을 실시간으로 모니터링한다.  
각 단계(웨이크워드, STT, LLM, TTS)의 소요 시간을 측정하고, 통계와 성능 등급을 제공한다.

```
"자비스야"  →  Whisper STT  →  Ollama LLM  →  edge-tts  →  ESP32
   Wake          STT            LLM           TTS
   ??ms          ??ms           ??ms          ??ms
```

---

## 설치

### 필수

```bash
# 추가 패키지 없음 — Python 3.10+ 표준 라이브러리만 사용
python pipeline_monitor.py --manual
```

### 선택 (WebSocket 모드)

```bash
pip install websocket-client
```

---

## 실행 모드

### 1. 수동 입력 모드

터미널에서 직접 측정값을 입력한다. 서버 로그를 보면서 수동 기록할 때 유용.

```bash
python pipeline_monitor.py --manual
```

**사용 흐름:**
```
명령어 입력 (q=종료, c=CSV저장, r=초기화): (Enter)
음성 명령 텍스트: 침실 불 켜줘
Wake Word (ms): 50
STT (ms): 1300
LLM (ms): 600
TTS (ms): 400
STT 모델 (기본: small): small
LLM 모델 (기본: qwen2.5:7b): qwen2.5:7b
성공? (Y/n): y
```

**단축키:**
- `q` — 종료 (CSV 자동 저장)
- `c` — CSV 즉시 저장
- `r` — 기록 초기화
- `Enter` — 새 기록 입력

---

### 2. 서버 로그 모니터링 모드

Voice IoT Controller 서버 로그를 `tail -f` 방식으로 실시간 추적한다.

```bash
python pipeline_monitor.py --log ~/dev_ws/voice_iot_controller/logs/server.log
```

**서버 로그 형식 요구사항:**

모니터가 자동 파싱하는 로그 패턴:

```
# Wake Word
[INFO] wake word detected ... 45ms

# STT
[INFO] stt inference complete ... 1320ms

# LLM
[INFO] llm parse complete ... 580ms

# TTS
[INFO] tts generation complete ... 390ms

# 전체
[INFO] pipeline total ... 2335ms
```

서버 코드에 위 형식의 로그가 없다면, 아래 **서버 코드 연동** 섹션을 참고하여 추가한다.

---

### 3. WebSocket 모드

서버의 WebSocket에 직접 연결하여 이벤트를 수신한다.

```bash
python pipeline_monitor.py --ws ws://localhost:8000/ws
```

**서버가 전송해야 하는 WebSocket 메시지:**

```json
{"type": "wake_detected",    "latency_ms": 45}
{"type": "stt_result",       "latency_ms": 1320, "text": "침실 불 켜줘", "model": "small"}
{"type": "llm_result",       "latency_ms": 580,  "model": "qwen2.5:7b"}
{"type": "tts_done",         "latency_ms": 390}
{"type": "cmd_result",       "status": "ok", "pipeline_ms": 2335}
```

또는 한 번에 보내는 형식도 지원:

```json
{
  "type": "pipeline_timing",
  "text": "침실 불 켜줘",
  "wake_ms": 45,
  "stt_ms": 1320,
  "llm_ms": 580,
  "tts_ms": 390,
  "total_ms": 2335,
  "stt_model": "small",
  "llm_model": "qwen2.5:7b",
  "success": true
}
```

---

### 4. 통계 조회 모드

기존 CSV 파일에서 통계만 출력한다.

```bash
python pipeline_monitor.py --stats --csv pipeline_metrics.csv
```

---

## 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--manual` | - | 수동 입력 모드 |
| `--log FILE` | - | 서버 로그 파일 경로 |
| `--ws URL` | - | WebSocket 서버 URL |
| `--stats` | - | 기존 CSV 통계 출력 |
| `--csv FILE` | `pipeline_metrics.csv` | CSV 저장/로드 경로 |
| `--window N` | `50` | 통계 계산 윈도우 크기 |

---

## 출력 화면

### 대시보드 예시

```
╔══════════════════════════════════════════════════════════════════╗
║  🎙️  Voice IoT Pipeline Monitor                  2026-02-22    ║
╚══════════════════════════════════════════════════════════════════╝

  [ 최근 파이프라인 기록 ]
   #        명령            Wake      STT      LLM      TTS    Total  Bar
  ─────────────────────────────────────────────────────────────────────────
  ✓ 1  침실 불 켜줘          50ms   1300ms    600ms    400ms   2350ms  ██████████████░░░░░░░░░░░░░░░░░░
  ✓ 2  차고문 열어줘          48ms   1280ms    580ms    410ms   2318ms  █████████████░░░░░░░░░░░░░░░░░░░
  ✓ 3  전체 불 꺼줘          52ms   1350ms    620ms    380ms   2402ms  ██████████████░░░░░░░░░░░░░░░░░░

  [ 통계 — 최근 3건 ]
       Stage      Avg    Median      P95      Min      Max  Count
  ──────────────────────────────────────────────────────────────────
   Wake Word      50ms      50ms      52ms      48ms      52ms      3건
         STT    1310ms    1300ms    1350ms    1280ms    1350ms      3건
         LLM     600ms     600ms     620ms     580ms     620ms      3건
         TTS     397ms     400ms     410ms     380ms     410ms      3건
       Total    2357ms    2350ms    2402ms    2318ms    2402ms      3건

  Performance Grade: ✓  GOOD (1.5~2.5s)
```

### 성능 등급 기준

| 등급 | 평균 Total | 의미 |
|------|-----------|------|
| ⚡ EXCELLENT | < 1,500ms | 즉각 반응 수준 |
| ✓ GOOD | 1,500~2,500ms | 실사용 가능 |
| △ FAIR | 2,500~4,000ms | 개선 필요 |
| ✗ SLOW | > 4,000ms | 최적화 필수 |

---

## CSV 파일 형식

자동 저장되는 `pipeline_metrics.csv` 구조:

```csv
timestamp,command_text,wake_ms,stt_ms,llm_ms,tts_ms,total_ms,stt_model,llm_model,success
2026-02-22T15:30:00,침실 불 켜줘,50,1300,600,400,2350,small,qwen2.5:7b,True
2026-02-22T15:31:00,차고문 열어줘,48,1280,580,410,2318,small,qwen2.5:7b,True
```

CSV 데이터를 활용하여:
- 최적화 전후 비교
- 모델 변경 효과 분석
- 시간대별 성능 트렌드 확인
- Jupyter Notebook에서 시각화

---

## 서버 코드 연동

서버에서 각 단계의 소요 시간을 측정하고 로그/WebSocket으로 전송하는 코드 예시.

### stt_engine.py — STT 타이밍

```python
import time

async def _transcribe(self, audio):
    t0 = time.perf_counter()
    result = await self._run_whisper(audio)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    
    logger.info(f"stt inference complete {elapsed_ms:.0f}ms")
    
    # WebSocket 전송
    await self.ws_hub.broadcast({
        "type": "stt_result",
        "text": result.text,
        "latency_ms": round(elapsed_ms),
        "model": self.model_size,
    })
    return result
```

### llm_engine.py — LLM 타이밍

```python
async def parse(self, text: str):
    t0 = time.perf_counter()
    result = await self._call_ollama(text)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    
    logger.info(f"llm parse complete {elapsed_ms:.0f}ms")
    
    await self.ws_hub.broadcast({
        "type": "llm_result",
        "latency_ms": round(elapsed_ms),
        "model": self.model_name,
    })
    return result
```

### tts_engine.py — TTS 타이밍

```python
async def speak(self, text: str):
    t0 = time.perf_counter()
    await self._generate(text)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    
    logger.info(f"tts generation complete {elapsed_ms:.0f}ms")
    
    await self.ws_hub.broadcast({
        "type": "tts_done",
        "latency_ms": round(elapsed_ms),
    })
```

### main.py — 전체 파이프라인 타이밍

```python
async def handle_voice_command(text: str):
    t_start = time.perf_counter()
    
    # STT → LLM → ESP32 → TTS
    stt_result = await stt_engine.transcribe(audio)
    llm_result = await llm_engine.parse(stt_result.text)
    await tcp_server.send_command(llm_result)
    await tts_engine.speak(llm_result.get("tts_response", ""))
    
    total_ms = (time.perf_counter() - t_start) * 1000
    logger.info(f"pipeline total {total_ms:.0f}ms")
    
    await ws_hub.broadcast({
        "type": "cmd_result",
        "status": "ok",
        "pipeline_ms": round(total_ms),
    })
```

---

## 활용 시나리오

### 1. 모델 변경 전후 비교

```bash
# 변경 전 측정 (10회)
python pipeline_monitor.py --manual --csv before_upgrade.csv

# 모델 변경 후 측정 (10회)
python pipeline_monitor.py --manual --csv after_upgrade.csv

# 각각 통계 확인
python pipeline_monitor.py --stats --csv before_upgrade.csv
python pipeline_monitor.py --stats --csv after_upgrade.csv
```

### 2. 장시간 안정성 테스트

```bash
# 서버 로그를 하루 종일 모니터링
python pipeline_monitor.py --log ~/dev_ws/voice_iot_controller/logs/server.log --csv daily_test.csv
```

### 3. Jupyter에서 시각화

```python
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv("pipeline_metrics.csv")
df[["wake_ms", "stt_ms", "llm_ms", "tts_ms"]].plot.box()
plt.title("Pipeline Stage Distribution")
plt.ylabel("ms")
plt.show()
```

---

## 파일 위치

```
~/dev_ws/voice_iot_controller/
├── pipeline_monitor.py        ← 모니터링 스크립트
├── pipeline_metrics.csv       ← 자동 생성되는 측정 기록
├── PIPELINE_MONITOR_GUIDE.md  ← 본 가이드
└── server/
    ├── stt_engine.py          ← STT 타이밍 로그 추가
    ├── llm_engine.py          ← LLM 타이밍 로그 추가
    ├── tts_engine.py          ← TTS 타이밍 로그 추가
    └── main.py                ← 전체 파이프라인 타이밍
```

---

*Pipeline Monitor · Voice IoT Controller · 2026-02-22*
