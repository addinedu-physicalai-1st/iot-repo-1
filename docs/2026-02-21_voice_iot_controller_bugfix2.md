# Voice IoT Controller — 버그수정 #2
**날짜:** 2026-02-21
**태그:** `bugfix#2` `debug-mode` `vad` `threshold`
**결과:** ⏸️ 홀딩 — 현재 환경 한계 확인, 추후 재시도

---

## 최종 버전 현황

| 파일 | 버전 | 변경 |
|------|------|------|
| `stt_engine.py` | v4.1-debug | 디버그 모드 추가, vad_energy_threshold 0.06 고정 |
| `main.py` | v0.5 | energy_threshold 파라미터 연동 추가 |
| `settings.yaml` | v0.5 | vad_energy_threshold: 0.06 추가, debug_mode 추가 |

---

## 작업 흐름

### 1단계 — 디버그 모드 구축

`stt_engine.py v4.1-debug` 작성. 아래 태그로 상세 로그 출력 + 파일 저장.

| 태그 | 내용 |
|------|------|
| `[DBG-WAKE]` | 웨이크워드 감지 타임스탬프, 모드(porcupine/button) |
| `[DBG-VAD]` | 매 청크 energy, SPEECH/SILENT, 무음 경과시간 |
| `[DBG-QUEUE]` | 오디오 큐 백로그 크기 |
| `[DBG-NR]` | NR 전/후 RMS, 처리 시간 |
| `[DBG-STT]` | Whisper raw 텍스트, 정제 결과, 필터 이유 |
| `[DBG-PIPE]` | 전 구간 ms 타임라인 |
| `[DBG-STAT]` | 세션 누적 통계 |

활성화:
```yaml
# config/settings.yaml
stt:
  debug_mode: true
```

로그 파일: `logs/stt_debug_YYYYMMDD_HHMMSS.log` 자동 저장

---

### 2단계 — 1차 디버그 로그 분석 결과

**세션 통계 (7회):**
- 성공률: 100%
- 평균 Whisper: 627ms
- 평균 파이프라인: 1996ms

**핵심 문제 발견:**

```
speech_dur = 10048ms (7회 전부 최대 발화길이 강제처리)
LISTEN [SILENT] → 단 한 번도 없음
```

**원인:** 배경음 energy(0.054~0.114)가 VAD_ENERGY_THRESH(0.02)보다 높아서
말을 멈춰도 SILENT 판정이 안 됨 → 10초 강제처리 반복

---

### 3단계 — vad_energy_threshold 0.02 → 0.06 상향

`settings.yaml`에 `vad_energy_threshold: 0.06` 추가
`main.py` STTEngine 생성 시 파라미터 연동

**2차 디버그 로그 결과 (4회):**

| 세션 | speech_dur | 처리 |
|------|-----------|------|
| 1 | 7.84s | ✅ 정상 종료 |
| 2 | 10.05s | ❌ 강제처리 (배경음 순간 상승) |
| 3 | 7.94s | ✅ 정상 종료 |
| 4 | 7.07s | ✅ 정상 종료 |

4회 중 3회 정상. 배경음이 순간 올라가면 여전히 뚫림.

---

### 4단계 — 동적 threshold 시도 (× 1.5, × 1.2)

IDLE 구간 noise_profile RMS를 배경음 기준으로 사용, 자동 조정 시도.

**× 1.5 결과:**
```
noise_profile rms=0.09406 → dyn_thresh=0.14110
발화 energy=0.11  →  thresh 못 넘음  →  전부 SILENT → 인식 불가
```

**× 1.2 결과:**
```
noise_profile rms=0.11082 → dyn_thresh=0.13299
발화 energy=0.11  →  thresh 못 넘음  →  전부 SILENT → 인식 불가
```

**결론:** 이 환경은 배경음(0.11)과 발화(0.11~0.14) 에너지 차이가 0.02~0.03 수준으로 너무 작아 배율 방식 자체가 동작하지 않음. 매번 원복.

---

### 5단계 — alsamixer 마이크 볼륨 조정 시도

**80% → 60% 조정 결과:**
```
배경음 rms: 0.11 → 0.020  (배경음은 잘 내려감)
발화 energy: 0.038  →  thresh=0.06 아래로 내려감  →  인식 불가
```

볼륨을 너무 낮추면 발화도 같이 작아지는 문제. 최적 볼륨 지점 탐색 필요.

**현재 상태:** alsamixer 80% 원복, 홀딩

---

## 현재 안정 버전 파라미터

```yaml
stt:
  vad_energy_threshold: 0.06   # 0.02 → 0.06 (bugfix#2)
  noise_reduction: true
  noise_prop_decrease: 0.85
  debug_mode: false             # 평상시 false
  mic_device: 11
```

---

## 다음 작업 후보 (홀딩)

- [ ] alsamixer 최적 볼륨 지점 탐색 (75% 근방)
- [ ] 마이크 위치 개선 (입에 더 가까이)
- [ ] USB 마이크/헤드셋 교체 검토
- [ ] VAD를 에너지 기반 → WebRTC VAD 등 모델 기반으로 교체 검토
