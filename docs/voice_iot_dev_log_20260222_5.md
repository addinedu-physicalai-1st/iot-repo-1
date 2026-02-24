# Voice IoT Controller — 작업 기록 #5
# TTS 음성 답변 추가 (Kokoro / ElevenLabs)

> 작성일: 2026-02-22
> 프로젝트: `~/dev_ws/voice_iot_controller`
> 작업 범위: LLM 음성 답변 기능 추가 — TTS 엔진 통합

---

## 작업 요약

| 단계 | 내용 | 결과 |
|------|------|------|
| 1 | DEV_RULES 확인 및 최신 파일 기준 작업 | ✅ |
| 2 | tts_engine.py v1.0 신규 작성 | ✅ |
| 3 | llm_engine.py v1.3 → v1.4 (tts_response 필드 추가) | ✅ |
| 4 | main.py v0.4 → v0.5 (TTS 초기화 + speak 호출) | ✅ |
| 5 | settings.yaml v0.7 → v0.8 (tts 블록 추가) | ✅ |

---

## 최종 적용 파일

| 파일 | 버전 | 변경 내용 |
|------|------|----------|
| `server/tts_engine.py` | v1.0 **신규** | Kokoro/ElevenLabs 공통 인터페이스, asyncio Lock, run_in_executor |
| `server/llm_engine.py` | v1.4 | SYSTEM_PROMPT에 tts_response 규칙 추가, parse() 반환값 포함 |
| `server/main.py` | v0.5 | TTSEngine 초기화, _make_stt_callback에 speak() 비동기 호출 추가 |
| `config/settings.yaml` | v0.8 | tts 블록 추가 (provider/kokoro/elevenlabs 설정) |

---

## 변경 상세

### tts_engine.py v1.0 (신규)

```
TTSEngine 클래스
  - provider: "kokoro" | "elevenlabs"
  - initialize(): 비동기 초기화 (모델 로드)
  - speak(text): 비동기 발화, asyncio.Lock으로 동시 발화 방지
  - CPU 블로킹 작업(ONNX 추론/오디오 재생) → run_in_executor 처리
  - is_available(): 초기화 상태 확인
  - DISABLE_TTS=1 환경 변수로 비활성화 가능
```

### llm_engine.py v1.3 → v1.4

```python
# SYSTEM_PROMPT 추가 규칙 (Rule 6 + tts_response rules)
"Always include 'tts_response' field with a short, natural Korean spoken response"

# 응답 JSON 구조 변경
# 기존: {"cmd":"led","device_id":"esp32_bedroom","pin":2,"state":"on"}
# 변경: {"cmd":"led","device_id":"esp32_bedroom","pin":2,"state":"on",
#        "tts_response":"침실 전등을 켰어요."}

# parse() 변경:
# - cmd=null 자유 대화 처리 추가 (tts_response만 반환)
# - unknown 명령에서 tts_response 추출 후 반환
```

### main.py v0.4 → v0.5

```python
# 추가: TTSEngine import 및 인스턴스 생성
from server.tts_engine import TTSEngine
tts_engine = TTSEngine(provider=..., ...)   # settings.yaml tts 블록 기반

# 추가: lifespan에서 TTS 초기화
tts_ok = await tts_engine.initialize()

# 추가: _make_stt_callback에 TTS 호출
tts_text = _extract_tts_response(result)
if tts_text and tts_engine:
    asyncio.create_task(tts_engine.speak(tts_text))  # 논블로킹

# 추가: _extract_tts_response() 헬퍼 함수
# 추가: 배너에 TTS 상태 출력
# 추가: app.state.tts_engine 바인딩
# 추가: DISABLE_TTS=1 환경 변수 지원
```

### settings.yaml v0.7 → v0.8

```yaml
# 추가된 tts 블록
tts:
  provider: "kokoro"        # "kokoro" | "elevenlabs"
  kokoro:
    model_path:  "models/kokoro-v0_19.onnx"
    voices_path: "models/voices.bin"
    voice: "af_heart"
    speed: 1.0
    lang: "ko"
  elevenlabs:
    api_key:  ""
    voice_id: ""
    model_id: "eleven_multilingual_v2"
```

---

## 설치 필요 패키지

```bash
source venv/bin/activate

# Kokoro TTS (로컬)
pip install kokoro-onnx sounddevice soundfile

# Kokoro 모델 파일 다운로드
cd models/
wget https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx
wget https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin

# ElevenLabs (API, 테스트 후 선택)
pip install elevenlabs
```

---

## 파이프라인 변경 후 구성

```
마이크 (device=11)
    ↓
Porcupine 웨이크 워드 ("자비스야")
    ↓
VAD (energy_threshold=0.06, max=5s, silence=0.8s) + noisereduce (prop=0.85)
    ↓
faster-whisper small (beam_size=3, cpu_threads=6, num_workers=2)
    ↓
_KO_CORRECTIONS 오인식 교정
    ↓
Ollama qwen2.5:7b → JSON + tts_response 생성
    ↓
    ├── validate_command + _normalize_types
    │       ↓
    │   TCP → ESP32 (pin 제어)
    │
    └── tts_response 추출
            ↓
        TTSEngine.speak() [비동기, 논블로킹]
            ↓
        스피커 출력 (Kokoro 또는 ElevenLabs)
```

---

## 테스트 시나리오

```bash
# 1단계: Kokoro 독립 테스트
python3 -c "
import asyncio
import sys
sys.path.insert(0, '.')
from server.tts_engine import TTSEngine

async def test():
    engine = TTSEngine(provider='kokoro',
                       model_path='models/kokoro-v0_19.onnx',
                       voices_path='models/voices.bin')
    ok = await engine.initialize()
    print(f'init: {ok}')
    if ok:
        await engine.speak('침실 전등을 켰어요.')

asyncio.run(test())
"

# 2단계: LLM tts_response 확인
python3 -c "
import asyncio
from server.llm_engine import LLMEngine

async def test():
    e = LLMEngine()
    result = await e.parse('침실 불 켜줘')
    print(result)
    print('TTS:', result.get('tts_response'))

asyncio.run(test())
"

# 3단계: 서버 통합 테스트
./run_server.sh
# → 자비스야 + "침실 불 켜줘" → 음성 답변 확인
```

---

## 향후 계획

- [ ] Kokoro 설치 및 모델 다운로드 후 음질 테스트
- [ ] ElevenLabs 교체 테스트 (settings.yaml provider: "elevenlabs")
- [ ] 음질 비교 후 최종 provider 결정
- [ ] VAD 근본 개선 검토 (Silero VAD 교체)
- [ ] OpenAI 크레딧 충전 후 whisper-1 + gpt-4o-mini 재전환 검토

---

*끝 — 2026-02-22*
