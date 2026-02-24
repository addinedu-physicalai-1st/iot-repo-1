# Voice IoT Controller — 버그수정 #1
**날짜:** 2026-02-21
**태그:** `bugfix#1` `porcupine` `noisereduce` `wake-word`
**결과:** ✅ "자비스야" 웨이크워드 정상 작동 확인

---

## 최종 버전 현황

| 파일 | 버전 | 경로 |
|------|------|------|
| `stt_engine.py` | v4.0 | `server/stt_engine.py` |
| `main.py` | v0.5 | `server/main.py` |
| `settings.yaml` | v0.5 | `config/settings.yaml` |

---

## 작업 흐름 요약

### 1단계 — noisereduce 설치 및 효과 검증
`noisereduce`, `scipy`, `soundfile` 설치 후 `test_noisereduce.py` 스크립트로 실측

```
배경음 RMS        : 0.08807
발화(원본) RMS    : 0.11062
노이즈 감쇄율     : 74.1%  (prop_decrease=0.75 기준)
처리 시간         : 112ms
```

**prop_decrease 스윕 결과:**

| 값 | 감쇄율 | 판정 |
|----|--------|------|
| 0.50 | 49.4% | 약함 |
| 0.75 | 74.1% | 기존 기본값 |
| **0.85** | **~85%** | **✅ 권장 (발화 보호 + 억제 균형)** |
| 1.00 | 96.5% | 과억제 위험 |

→ `noise_prop_decrease: 0.85` 확정

---

### 2단계 — 웨이크워드 감지 지연 원인 분석

**증상:** 서버/dashboard 시작 직후 "자비스야" 불러도 즉시 반응 안 함

**원인 (v3.2 구조 문제):**
1. Whisper 모델 로드(2~5초) 중 마이크 스트림이 이미 켜져 있음
2. 로드 완료 후 IDLE 루프 진입 시 큐 백로그 드레인으로 쌓인 청크 전부 폐기
3. Whisper 슬라이딩 버퍼(`_wake_buf`)가 비어있어 `WAKE_FALLBACK_BUF_SEC=1.2초` 채울 때까지 감지 불가
4. 결과적으로 시작 직후 3~4초 감지 불능 구간 존재

**근본 원인:** v3.2는 Whisper 폴백으로 웨이크워드 감지 → 슬라이딩 버퍼 구조 자체의 한계

---

### 3단계 — stt_engine.py v4.0 Porcupine 버전으로 재작성

**핵심 변경:**

| 항목 | v3.2 (이전) | v4.0 (현재) |
|------|------------|------------|
| 웨이크워드 방식 | Whisper 슬라이딩 버퍼 폴백 | **Porcupine 네이티브 .ppn** |
| 마이크 스트림 타입 | `InputStream(float32)` | `RawInputStream(int16)` |
| blocksize | 1280 (80ms) | **512 (32ms, Porcupine 고정)** |
| 마이크 시작 시점 | 모델 로드 전 | **모델 로드 완료 후** |
| 초기 감지 지연 | 3~4초 불능 구간 존재 | **제거** |

**Porcupine 모델 경로:**
```
models/자비스야_ko_linux_v4_0_0.ppn
models/porcupine_params_ko.pv
AccessKey: LsJ1ppPYfYDqEDiSh5skCfTnWFZEEoh2xopF0lr6U/NBqLtT3ZIKmA==
```

---

### 4단계 — main.py v0.5 업데이트

STTEngine 생성 시 Porcupine 파라미터 전달 추가:

```python
stt_engine = STTEngine(
    porcupine_access_key  = _stt.get("porcupine_access_key", ""),
    porcupine_model_path  = _stt.get("porcupine_model_path", ""),
    porcupine_params_path = _stt.get("porcupine_params_path", ""),
    mic_device            = _stt.get("mic_device"),
    noise_reduction       = _stt.get("noise_reduction", True),
    noise_prop_decrease   = _stt.get("noise_prop_decrease", 0.85),
    ...
)
```

배너 변경:
```
# 이전 (v0.4)
  STT  : ✅ base / wake=자비스야

# 현재 (v0.5)
  STT  : ✅ base (Whisper)
  WAKE : ✅ Porcupine / 자비스야
  NR   : ✅ prop_decrease=0.85
```

---

### 5단계 — settings.yaml v0.5 업데이트

```yaml
stt:
  wake_word: "자비스야"          # 로그 표시용 (신규 추가)
  mic_device: 11                 # 10(pipewire) → 11(default)
  noise_reduction: true          # 신규 추가
  noise_prop_decrease: 0.85      # 신규 추가
```

---

## 최종 기동 로그 (정상 확인)

```
✅ LLM  : exaone3.5:latest
✅ STT  : base (Whisper)
✅ WAKE : Porcupine / 자비스야
✅ NR   : prop_decrease=0.85
...
[STT] noisereduce 사용 가능 → 노이즈 억제 활성화
[STT] Whisper 로드 완료
[STT] Porcupine 로드 완료 (frame_length=512, sample_rate=16000)
[STT] 마이크 스트림 시작 (SR=16000Hz, frame=512, device=11)
[STT] 노이즈 억제: 활성 prop=0.85
[STT] Porcupine 웨이크 워드 대기 중: '자비스야'
[NR]  배경음 프로파일 초기 수집 완료 (0.5초)
```

---

## 전체 파이프라인

```
마이크(int16, 512frame)
    │
    ├─ IDLE  → Porcupine.process() → "자비스야" 감지
    │                                      │
    └─ LISTENING → VAD(energy>0.02)        │
                    → 발화 수집(float32)   │
                    → noisereduce(0.85) ←──┘
                    → Whisper base
                    → LLMEngine(exaone3.5)
                    → TCPServer → ESP32
```

---

## 다음 작업 후보

- [ ] "자비스야" 호출 후 명령 인식률 실측 테스트
- [ ] esp32_garage / esp32_bathroom / esp32_entrance TCP 연결 테스트
- [ ] WAKE → LISTENING 전환 시 WebSocket UI 상태 표시 확인
- [ ] 버그수정 #2 준비
