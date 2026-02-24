# Voice IoT Controller — STT/LLM 최적화 작업 기록

> 작성일: 2026-02-21 (오후)
> 프로젝트: `~/dev_ws/voice_iot_controller`
> 이전 기록: `voice_iot_dev_log_20260221.md`

---

## 1. faster-whisper 추론 최적화

**문제**
음성 인식 후 ESP32 명령 실행까지 체감 속도가 너무 느림.

**원인 분석**
현재 파이프라인:
```
웨이크워드 → Whisper(2~4s) → LLM(1~3s) → ESP32  ≈ 3~7초
```
- `faster-whisper`는 이미 적용되어 있었으나 `cpu_threads` 미설정(기본값 1)
- `beam_size=5` → IoT 단문 명령어에 불필요하게 높음

**수정 파일**: `server/stt_engine.py`

```python
# _init_whisper - cpu_threads, num_workers 추가
def _init_whisper(self) -> WhisperModel:
    import multiprocessing
    cpu_threads = max(2, multiprocessing.cpu_count() - 1)  # 4코어 기준 → 3
    return WhisperModel(
        self.model_size,
        device=self.device,
        compute_type="int8" if self.device == "cpu" else "float16",
        cpu_threads=cpu_threads,  # 기본 1 → CPU 풀 사용
        num_workers=1,
    )

# _run_whisper_with_debug - beam_size 변경
segments, _ = self._whisper.transcribe(
    audio_nr,
    language=self.language,
    beam_size=1,   # 5 → 1: IoT 단문 명령어는 beam 탐색 불필요 (2~3배 빠름)
    best_of=1,     # beam_size=1 시 통일
    ...
)
```

**변경 요약**

| 항목 | 변경 전 | 변경 후 | 효과 |
|------|---------|---------|------|
| `cpu_threads` | 1 (기본) | CPU 코어-1 (3) | 병렬 처리 |
| `beam_size` | 5 | 1 | 2~3배 빠름 |
| `best_of` | 미설정 | 1 | beam=1과 통일 |

---

## 2. LLM 교체 — Ollama(로컬) → OpenAI API

**문제**
로컬 LLM(`exaone3.5:latest`, ~8B)으로는 다양한 한국어 표현 인식이 불안정하고 느림.

**결정**
- 비용이 들더라도 정확도/속도 최우선
- **OpenAI `gpt-4o-mini`** 선택
  - 한국어 이해 최상급
  - 응답속도 0.3~0.5초
  - 비용: IoT 명령 수준에서 하루 수천 번 사용해도 $1 미만

### 수정 내용

**`server/llm_engine.py`** — `provider` 파라미터 추가, OpenAI/Ollama 분기 처리

```python
class LLMEngine:
    def __init__(
        self,
        provider: str = "ollama",   # "openai" | "ollama"
        model: str = "qwen2.5:1.5b",
        api_key: str = "",           # OpenAI API 키
        host: str = "http://localhost:11434",
        timeout: float = 10.0,
    ):
```

호출 분기:
```python
async def _call_api(self, text):
    if self.provider == "openai":
        return await self._call_openai(text)
    return await self._call_ollama(text)
```

OpenAI 호출 핵심 옵션:
```python
payload = {
    "model": self.model,
    "temperature": 0.1,
    "max_tokens": 150,                           # IoT JSON은 짧음 → 빠른 응답
    "response_format": {"type": "json_object"},  # JSON 모드 강제
}
```

**`server/main.py`** — LLMEngine 생성 시 provider, api_key 전달

```python
_llm_cfg  = cfg.get("ollama", {})
_provider = _llm_cfg.get("provider", "ollama")
llm_engine = LLMEngine(
    provider=_provider,
    model=_llm_cfg["model"],
    api_key=_llm_cfg.get("api_key", ""),
    host=_llm_cfg.get("host", "http://localhost:11434"),
    timeout=_llm_cfg.get("timeout", 10),
)
```

**`config/settings.yaml`** — provider, api_key 추가

```yaml
ollama:
  provider: "openai"
  model: "gpt-4o-mini"
  api_key: "sk-..."          # platform.openai.com 에서 발급
  host: "http://localhost:11434"
  timeout: 10
```

---

## 3. 최종 파이프라인

```
[이전] 웨이크워드 → Whisper/beam=5/threads=1 (2~4s)
                → Ollama exaone3.5 (1~3s) → ESP32   ≈ 3~7초

[이후] 웨이크워드 → Whisper/beam=1/threads=3 (0.5~1s)
                → OpenAI gpt-4o-mini (0.3~0.5s) → ESP32   ≈ 1초 이내
```

---

## 4. 적용 확인 로그

```
[INFO] server.main -   LLM  : ✅ [openai] gpt-4o-mini
[INFO] server.main -   STT  : ✅ base (Whisper)
[INFO] server.main -   WAKE : ✅ Porcupine / 자비스야
[INFO] httpx - HTTP Request: GET https://api.openai.com/v1/models "HTTP/1.1 200 OK"
[INFO] server.main - LLM 연결 성공 | provider=openai | 모델: [...]
[INFO] server.tcp_server - [TCP] 등록 완료: esp32_bedroom
```

---

## 5. provider 전환 방법

**OpenAI → Ollama 롤백:**
```yaml
# config/settings.yaml
provider: "ollama"
model: "qwen2.5:1.5b"
```

**Ollama → OpenAI 재전환:**
```yaml
provider: "openai"
model: "gpt-4o-mini"
api_key: "sk-..."
```

코드 변경 없이 `settings.yaml` 2줄만 수정하면 됩니다.

---

## 6. 수정 파일 목록

| 파일 | 경로 | 주요 변경 |
|------|------|-----------|
| `stt_engine.py` | `server/stt_engine.py` | cpu_threads=3, beam_size=1, best_of=1 |
| `llm_engine.py` | `server/llm_engine.py` | provider 분기, _call_openai() 추가, JSON 모드 강제 |
| `main.py` | `server/main.py` | LLMEngine 생성 시 provider/api_key 전달, 배너 업데이트 |
| `settings.yaml` | `config/settings.yaml` | provider/api_key 추가 |

---

*끝 — 2026-02-21*
