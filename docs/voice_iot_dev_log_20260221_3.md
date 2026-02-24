# Voice IoT Controller — 작업 기록 #3

> 작성일: 2026-02-21  
> 프로젝트: `~/dev_ws/voice_iot_controller`  
> 작업 범위: STT/LLM 성능 최적화 → OpenAI 전환 시도 → Ollama 원복

---

## 작업 요약

| 단계 | 내용 | 결과 |
|------|------|------|
| 1 | faster-whisper CPU 최적화 | ✅ beam_size=1, cpu_threads=3 |
| 2 | LLM Ollama → OpenAI 전환 | ✅ gpt-4o-mini 연결 성공 |
| 3 | STT OpenAI Whisper API 전환 | ❌ 크레딧 소진으로 실패 |
| 4 | Ollama 원복 | ✅ 정상 동작 확인 |
| 5 | _KO_CORRECTIONS 한국어 오인식 교정 추가 | ✅ |
| 6 | _normalize_types float→int 수정 추가 | ✅ |

---

## 최종 적용 파일 (v0.6 — Ollama 원복)

| 파일 | 변경 내용 |
|------|----------|
| `server/main.py` | v0.3 원본 유지 (Ollama 전용) |
| `server/llm_engine.py` | `_normalize_types()` 추가 |
| `server/stt_engine.py` | `_KO_CORRECTIONS` 추가 |
| `config/settings.yaml` | v0.6, vad_energy_threshold=0.06 유지 |

---

## 작업 중 발생한 문제 및 원인

### 문제 1 — validate_command pin float 오류
**증상**: "명령을 이해하지 못했습니다" — llm_ms=717ms인데 실패  
**원인**: OpenAI JSON 모드가 `{"pin": 2}` 대신 `{"pin": 2.0}` 응답  
→ `isinstance(2.0, int)` = False → validate_command 실패 → None 리턴  
**해결**: `llm_engine.py`에 `_normalize_types()` 추가 — pin/angle/pin_clk/pin_dio를 int로 강제 변환

```python
def _normalize_types(self, data: dict) -> dict:
    for key in ("pin", "angle", "pin_clk", "pin_dio"):
        if key in data:
            val = data[key]
            if isinstance(val, (int, float)):
                data[key] = int(val)
    return data
```

### 문제 2 — STT "침실" → "칭실" 오인식
**증상**: Whisper base가 한국어 단어를 자주 오인식  
**원인**: faster-whisper base 모델 한계 (74M 파라미터)  
**해결**: `_clean_text_debug()`에 `_KO_CORRECTIONS` 딕셔너리 추가

```python
_KO_CORRECTIONS = {
    "칭실": "침실", "잔든": "전등", "켜져": "켜줘",
    "꺼져": "꺼줘", "현과": "현관", ...
}
words = text.split()
corrected = " ".join(_KO_CORRECTIONS.get(w, w) for w in words)
```

### 문제 3 — OpenAI STT "Illegal header value b'Bearer '"
**증상**: stt_ms=54ms, raw='' — API 응답 없음  
**원인**: `openai_api_key`가 빈 문자열 → `Bearer ` (키 없음) → httpx 헤더 오류  
**원인 2**: `settings.yaml`에 `ollama.api_key` 필드 자체가 없었음  
**해결**: settings.yaml에 `api_key` 필드 추가 후 실제 키 입력

### 문제 4 — OpenAI API 429 크레딧 소진
**증상**: `insufficient_quota` 오류  
**원인**: OpenAI 크레딧 소진  
**해결**: STT → local faster-whisper, LLM → Ollama 원복

### 문제 5 — llm_ms=4ms (LLM 미호출)
**증상**: STT 인식 후 LLM 호출이 안 됨  
**원인**: main.py 구버전 배포 — `_llm is None`으로 처리됨  
**해결**: 올바른 main.py 재배포

### 문제 6 — VAD 발화 미감지
**증상**: 웨이크 워드 감지 후 8~10초 대기, raw='' 리턴  
**원인 A**: `vad_energy_threshold: 0.06` → 배경음(rms=0.09~0.11)과 발화 에너지 구분 불가  
**원인 B**: OpenAI STT에 무음 오디오 전송  
**해결**: 근본 원인은 OpenAI 크레딧 소진, Ollama 원복으로 해결

---

## 작업 중 실수 기록 (반성)

### 실수 1 — stt_engine.py __init__ 파라미터 누락
`_transcribe()`와 `self.stt_provider` 속성은 추가했지만  
`__init__` 파라미터(`stt_provider`, `openai_api_key`)를 빠뜨림  
→ `TypeError: STTEngine.__init__() got an unexpected keyword argument 'stt_provider'`  
**교훈**: 속성 추가 시 반드시 `__init__` 파라미터 → `self.xxx = xxx` 세트로 확인

### 실수 2 — main.py 모듈 레벨 코드 안내 오류
환경변수 처리를 위해 `os.getenv()` 코드를 모듈 레벨에 추가하도록 안내  
→ `cfg`가 함수 내부에서만 정의되므로 `NameError: name 'cfg' is not defined`  
**교훈**: main.py 구조상 `cfg`는 `create_app()` 함수 내부에서만 유효. 모듈 레벨 수정 금지

### 실수 3 — API 키 노출
사용자가 settings.yaml에 실제 API 키를 입력한 채로 업로드  
Claude가 이를 즉시 감지하고 마스킹했어야 하는데 1회 지연됨  
**교훈**: 업로드 파일에서 `sk-` 패턴 즉시 감지 → 경고 + 마스킹 처리 철저히

### 실수 4 — 원인 파악 전 수정 반복
llm_ms=4ms 원인을 정확히 파악하기 전에 여러 파일을 수정  
→ 오히려 더 꼬이는 상황 발생  
**교훈**: 서버 터미널 로그 → stt_debug 로그 → 코드 순서로 원인 100% 확인 후 수정

### 실수 5 — 배포 순서 안내 오류
파일 복사 전에 서버 재시작을 안내  
**교훈**: 항상 "파일 복사 → 설정 수정 → 서버 재시작" 순서 준수

---

## 수정 원칙 (오늘 확립)

1. **원인 확정 후 수정** — 로그에서 근본 원인 100% 확인 전 코드 수정 금지
2. **한 번에 하나씩** — 여러 파일 동시 수정 시 원인 파악이 어려워짐
3. **__init__ 세트 원칙** — 속성/파라미터 추가 시 파라미터 → self 할당 반드시 세트로
4. **모듈 레벨 코드 금지** — main.py에서 cfg는 create_app() 내부에서만 유효
5. **API 키 즉시 마스킹** — 업로드 파일의 sk- 패턴 감지 즉시 경고 + 마스킹
6. **배포 순서 준수** — 파일 복사 → 설정 수정 → 서버 재시작
7. **원복 시 원본 파일 기준** — 수정본이 꼬였을 때는 업로드된 원본 파일부터 시작

---

## 현재 시스템 구성 (원복 완료)

```
마이크 (device=11)
    ↓
Porcupine 웨이크 워드 ("자비스야")
    ↓
VAD (energy_threshold=0.06) + noisereduce (prop=0.85)
    ↓
faster-whisper base (beam_size=1, cpu_threads=3)
    ↓
_KO_CORRECTIONS 오인식 교정
    ↓
Ollama qwen2.5:1.5b (로컬)
    ↓
validate_command + _normalize_types
    ↓
TCP → ESP32
```

---

## 향후 계획

- OpenAI 크레딧 충전 후 → STT: whisper-1, LLM: gpt-4o-mini 재전환 예정
- _KO_CORRECTIONS 패턴 지속 수집 및 확장
- VAD 에너지 임계값 환경별 자동 조정 검토

---

*끝 — 2026-02-21*
