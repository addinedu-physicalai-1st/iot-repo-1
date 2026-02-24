# Voice IoT Controller — 작업 기록 #4
# STT/LLM 업그레이드 및 VAD 최적화

> 작성일: 2026-02-21
> 프로젝트: `~/dev_ws/voice_iot_controller`
> 작업 범위: 명령 인식률 향상을 위한 STT/LLM 업그레이드 + VAD 응답속도 최적화

---

## 작업 요약

| 단계 | 내용 | 결과 |
|------|------|------|
| 1 | 시스템 리소스 확인 (RAM 14GB, CPU 16코어) | ✅ 업그레이드 가능 확인 |
| 2 | qwen2.5:7b Ollama 다운로드 | ✅ |
| 3 | faster-whisper small 사전 캐시 | ✅ |
| 4 | settings.yaml v0.7 — STT/LLM 설정 업그레이드 | ✅ |
| 5 | stt_engine.py v4.2 — beam_size/cpu_threads/num_workers 최적화 | ✅ |
| 6 | llm_engine.py v1.2 — 모델/타임아웃/시스템 프롬프트 수정 | ✅ |
| 7 | VAD thresh 0.15 테스트 | ❌ 발화 미감지 → 0.06 원복 |
| 8 | stt_engine.py v4.3 — VAD 대기시간 최적화 | ✅ 체감 속도 대폭 개선 |
| 9 | main.py v0.4 — LLM 워밍업 추가 | ✅ 콜드 스타트 완전 제거 |

---

## 최종 적용 파일

| 파일 | 버전 | 변경 내용 |
|------|------|----------|
| `config/settings.yaml` | v0.7 | STT model_size: small, LLM: qwen2.5:7b, timeout: 30 |
| `server/stt_engine.py` | v4.3 | beam_size: 3, cpu_threads: 6, num_workers: 2, VAD 대기시간 단축 |
| `server/llm_engine.py` | v1.2 | 기본 모델: qwen2.5:7b, timeout: 30s, 시스템 프롬프트 pin integer 명시 |
| `server/main.py` | v0.4 | LLM 워밍업 추가 (_warmup_llm), 콜드 스타트 제거 |

---

## 변경 상세

### settings.yaml v0.6 → v0.7

```yaml
# STT
model_size: "base"  →  "small"     # 인식률 향상
cpu_threads: 3      →  6           # 16코어 환경 최적화

# LLM
model: "qwen2.5:1.5b"  →  "qwen2.5:7b"   # 명령 이해도 향상
timeout: 10            →  30              # 7b 응답 시간 대비
```

### stt_engine.py v4.1 → v4.2 → v4.3

```python
# v4.2: WhisperModel 초기화
cpu_threads=6,    # 3 → 6
num_workers=2,    # 1 → 2

# v4.2: transcribe()
beam_size=3,      # 1 → 3
best_of=3,        # 1 → 3

# v4.3: VAD 상수
VAD_MAX_SPEECH_SEC = 5    # 10 → 5초 (강제 종료 대기 단축)
VAD_SILENCE_SEC    = 0.8  # 1.2 → 0.8초 (무음 종료 판정 단축)
```

### llm_engine.py v1.1 → v1.2

```python
# 기본 모델 변경
model: str = "qwen2.5:7b"   # exaone3.5:latest → qwen2.5:7b

# 타임아웃 변경
timeout: float = 30.0        # 10.0 → 30.0

# 시스템 프롬프트 추가
"Output only JSON. Ensure 'pin' values are integers, not floats (e.g., 2, not 2.0)."
```

### main.py v0.3 → v0.4

```python
# _warmup_llm() 함수 추가
async def _warmup_llm(llm_engine: LLMEngine):
    logger.info("[LLM] 워밍업 시작 — 모델 메모리 적재 중...")
    t0 = time.time()
    try:
        await llm_engine.parse("테스트")   # 더미 호출로 7b 모델 메모리 적재
    except Exception as e:
        logger.warning(f"[LLM] 워밍업 중 예외 (무시): {e}")
    logger.info(f"[LLM] 워밍업 완료 — {elapsed:.0f}ms")

# lifespan 내 호출 위치
available = await llm_engine.is_available()
if available:
    models = await llm_engine.list_models()
    await _warmup_llm(llm_engine)   # ← 추가
```

---

## 로그 분석 결과 (stt_debug_20260221_220333.log)

### 파이프라인 성능 (v4.2 기준, VAD 10초 강제 종료)

| 명령어 | whisper_ms | llm_ms | total_ms |
|--------|-----------|--------|---------|
| 침실 전등 켜줘 | 1,371ms | 5,693ms | 7,134ms |
| 침실 커튼 열어줘 | 1,364ms | 7,526ms | 8,957ms |
| 침실하고 전등 다 꺼줘 | 1,370ms | 582ms | 2,026ms |
| 커튼 닫아줘 | 1,285ms | 595ms | 1,953ms |

### 발견된 이슈

**LLM 콜드 스타트**: 첫 1~2회 호출 시 5~7초 소요 → 이후 600ms 이하로 안정화
- 원인: qwen2.5:7b 첫 로딩 시 메모리 적재 시간 포함
- 해결: main.py v0.4 — 서버 시작 시 `_warmup_llm()` 더미 호출로 완전 제거 ✅

**VAD 발화 미감지 (speech_dur=10080ms)**:
- 배경음 energy: 0.113~0.148, thresh=0.06 → 배경음이 thresh 초과
- VAD가 발화 종료를 감지 못해 10초 강제 처리
- VAD thresh 0.15 테스트 → 발화도 SILENT 판정 → 명령 인식 불가
- 해결: VAD_MAX_SPEECH_SEC 10→5초, VAD_SILENCE_SEC 1.2→0.8초 단축

---

## 작업 중 발생한 문제 및 원인

### 문제 1 — VAD thresh 0.15 설정 시 명령 인식 불가
**증상**: 웨이크 워드 감지 후 IDLE로 즉시 전환, 명령 미인식
**원인**: 이 환경의 발화 energy(0.127~0.130)가 배경음(0.113~0.148)과 구분 불가
→ thresh=0.15 설정 시 발화도 SILENT로 판정됨
**해결**: 0.06 원복 + VAD 대기시간 단축으로 우회
**교훈**: 단순 energy 임계값은 배경음이 높은 환경에서 한계 존재. 근본 해결은 Silero VAD 교체 필요

---

## 수정 원칙 (기존 유지)

1. **원인 확정 후 수정** — 로그에서 근본 원인 100% 확인 전 코드 수정 금지
2. **한 번에 하나씩** — 여러 파일 동시 수정 시 원인 파악이 어려워짐
3. **배포 순서 준수** — 파일 복사 → 설정 수정 → 서버 재시작
4. **원복 시 원본 파일 기준** — 수정본이 꼬였을 때는 원본 파일부터 시작

---

## 현재 시스템 구성

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
Ollama qwen2.5:7b (로컬, timeout=30s)
    ↓
validate_command + _normalize_types
    ↓
TCP → ESP32 (mic=11)
```

---

## 향후 계획

- VAD 근본 개선 검토:
  - 방법 A: VAD_MAX_SPEECH_SEC/SILENCE_SEC 추가 튜닝
  - 방법 B: Silero VAD 교체 (딥러닝 기반, 배경음/발화 구분 정확)
- OpenAI 크레딧 충전 후 whisper-1 + gpt-4o-mini 재전환 검토
- _KO_CORRECTIONS 패턴 지속 수집 및 확장

---

*끝 — 2026-02-21*
