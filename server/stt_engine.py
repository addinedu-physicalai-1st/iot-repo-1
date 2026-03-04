"""
server/stt_engine.py  v4.3
==================================
Porcupine 웨이크 워드 + Whisper STT + VAD + noisereduce
+ 디버그 모드 (음성 인식 품질 분석용)

v4.1-debug 추가사항:
  debug_mode=True 시 아래 태그로 상세 로그 출력 + 파일 저장

v4.3 변경사항:
  VAD_MAX_SPEECH_SEC: 10 → 5초 (강제 종료 대기 단축)
  VAD_SILENCE_SEC: 1.2 → 0.8초 (무음 종료 판정 단축)

  [DBG-WAKE]  웨이크워드 감지 타임스탬프, 모드(porcupine / button)
  [DBG-VAD]   매 청크 energy, 발화시작/종료, 무음 경과시간
  [DBG-QUEUE] 오디오 큐 백로그 크기 → 처리 지연 감지
  [DBG-NR]    노이즈 억제 전/후 RMS, 처리 시간(ms)
  [DBG-STT]   Whisper raw 텍스트, 정제 후 텍스트, 필터 히트 원인
  [DBG-PIPE]  전체 파이프라인 구간별 ms 타임라인
              wake_to_speech / speech_dur / nr_ms / whisper_ms / llm_ms / total_ms
  [DBG-STAT]  세션 누적 통계 (시도/성공/무음필터/환각필터/평균지연)

로그 파일: logs/stt_debug_YYYYMMDD_HHMMSS.log (자동 생성)

활성화 방법 (둘 중 하나):
  1) settings.yaml:  stt.debug_mode: true
  2) 환경 변수:      STT_DEBUG=1 ./run_server.sh

로그 분석 포인트:
  [DBG-QUEUE] qsize > 5          → 처리 병목, 청크 누적 중
  [DBG-VAD]   energy 값          → VAD_ENERGY_THRESH 튜닝 참고
  [DBG-NR]    nr_ms > 150        → NR이 병목
  [DBG-PIPE]  whisper_ms         → Whisper 추론 병목
  [DBG-STT]   filtered=halluc    → 환각 패턴 확인
  [DBG-STT]   filtered=too_short → VAD_MIN_SPEECH_MS 조정 필요
  [DBG-STAT]  success_rate       → 전체 인식 품질 지표
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable, Optional

import numpy as np
import sounddevice as sd
import whisper

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 기본 설정값
# ─────────────────────────────────────────────

SAMPLE_RATE          = 16000
CHANNELS             = 1
DTYPE_INT16          = "int16"
PORCUPINE_FRAME_SIZE = 512    # 32ms @ 16kHz (고정값)

VAD_ENERGY_THRESH  = 0.02
VAD_MIN_SPEECH_MS  = 300
VAD_MAX_SPEECH_SEC = 5    # v4.3: 10 → 5초 (강제 종료 대기 단축)
VAD_SILENCE_SEC    = 0.8  # v4.3: 1.2 → 0.8초 (무음 종료 판정 단축)
WAKE_LISTEN_SEC    = 8.0

NOISE_PROFILE_SEC  = 0.5
NOISE_PROFILE_SIZE = int(SAMPLE_RATE * NOISE_PROFILE_SEC)

STATE_IDLE      = "IDLE"
STATE_LISTENING = "LISTENING"


# ─────────────────────────────────────────────
# 디버그 전용 파일 로거
# ─────────────────────────────────────────────

def _setup_debug_logger() -> logging.Logger:
    """
    콘솔(DEBUG) + 파일(DEBUG) 이중 출력 로거
    파일: logs/stt_debug_YYYYMMDD_HHMMSS.log
    """
    log_dir  = Path("logs")
    log_dir.mkdir(exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"stt_debug_{ts}.log"

    dbg = logging.getLogger("stt.debug")
    dbg.setLevel(logging.DEBUG)
    dbg.propagate = False

    fmt_file = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(message)s",
        datefmt="%H:%M:%S"
    )
    fmt_con = logging.Formatter(
        "\033[96m%(asctime)s.%(msecs)03d [DBG]\033[0m %(message)s",
        datefmt="%H:%M:%S"
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)
    dbg.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt_con)
    dbg.addHandler(ch)

    logger.info(f"[STT] 🐛 디버그 모드 ON — 로그: {log_path.resolve()}")
    return dbg


# ─────────────────────────────────────────────
# 세션 통계 카운터
# ─────────────────────────────────────────────

class _SessionStats:
    """세션 동안의 인식 시도 누적 통계"""
    def __init__(self):
        self.attempts      = 0   # 웨이크→발화 시도 횟수
        self.success       = 0   # on_result 까지 도달한 횟수
        self.filtered_short = 0  # VAD too_short 로 버려진 횟수
        self.filtered_halluc = 0 # 환각 필터로 버려진 횟수
        self.filtered_wake  = 0  # 웨이크워드 잔류 제거 후 빈 텍스트
        self.total_whisper_ms: list[float] = []
        self.total_pipeline_ms: list[float] = []
        self.wake_mode_counts = {"porcupine": 0, "button": 0}

    def report(self) -> str:
        total = self.attempts or 1
        avg_w = sum(self.total_whisper_ms) / len(self.total_whisper_ms) if self.total_whisper_ms else 0
        avg_p = sum(self.total_pipeline_ms) / len(self.total_pipeline_ms) if self.total_pipeline_ms else 0
        return (
            f"\n[DBG-STAT] ══════════════════════════════════════\n"
            f"[DBG-STAT]  시도 횟수       : {self.attempts}\n"
            f"[DBG-STAT]  성공 (on_result): {self.success}  ({self.success/total*100:.1f}%)\n"
            f"[DBG-STAT]  발화 너무 짧음  : {self.filtered_short}\n"
            f"[DBG-STAT]  환각 필터       : {self.filtered_halluc}\n"
            f"[DBG-STAT]  웨이크 잔류제거 : {self.filtered_wake}\n"
            f"[DBG-STAT]  웨이크 모드     : porcupine={self.wake_mode_counts['porcupine']}  button={self.wake_mode_counts['button']}\n"
            f"[DBG-STAT]  평균 Whisper    : {avg_w:.0f}ms\n"
            f"[DBG-STAT]  평균 파이프라인 : {avg_p:.0f}ms\n"
            f"[DBG-STAT] ══════════════════════════════════════"
        )


# ═══════════════════════════════════════════════
# STTEngine
# ═══════════════════════════════════════════════

class STTEngine:
    """
    Porcupine 웨이크 워드 + Whisper STT 엔진 v4.1-debug

    Parameters
    ----------
    on_result            : 인식 완료 콜백  async def(text: str)
    on_wake              : 웨이크 워드 감지 콜백 (선택)
    on_timeout           : 명령 대기 타임아웃 콜백 (선택)
    model_size           : Whisper 모델 크기
    language             : 인식 언어 (ko)
    device               : cpu / cuda
    wake_word            : 로그 표시용 이름
    porcupine_access_key : Picovoice AccessKey
    porcupine_model_path : .ppn 경로
    porcupine_params_path: .pv 경로 (한국어)
    mic_device           : sounddevice 장치 번호
    energy_threshold     : VAD 에너지 임계값
    noise_reduction      : noisereduce 활성화
    noise_prop_decrease  : NR 강도 0.0~1.0
    debug_mode           : True → 상세 디버그 로그 + 파일 저장
    """

    def __init__(
        self,
        on_result:             Callable[[str], Awaitable[None]],
        on_wake:               Optional[Callable[[], Awaitable[None]]] = None,
        on_timeout:            Optional[Callable[[], Awaitable[None]]] = None,
        model_size:            str   = "base",
        language:              str   = "ko",
        device:                str   = "cpu",
        wake_word:             str   = "자비스야",
        porcupine_access_key:  str   = "",
        porcupine_model_path:  str   = "",
        porcupine_params_path: str   = "",
        mic_device:            Optional[int] = None,
        energy_threshold:      float = VAD_ENERGY_THRESH,
        noise_reduction:       bool  = True,
        noise_prop_decrease:   float = 0.85,
        debug_mode:            bool  = False,
    ):
        self.on_result             = on_result
        self.on_wake               = on_wake
        self.on_timeout            = on_timeout
        self.model_size            = model_size
        self.language              = language
        self.device                = device
        self.wake_word             = wake_word
        self.porcupine_access_key  = porcupine_access_key
        self.porcupine_model_path  = porcupine_model_path
        self.porcupine_params_path = porcupine_params_path
        self.mic_device            = mic_device
        self.energy_threshold      = energy_threshold
        self.noise_reduction       = noise_reduction
        self.noise_prop_decrease   = noise_prop_decrease

        # debug_mode: settings.yaml 또는 환경변수 STT_DEBUG=1
        self.debug_mode = debug_mode or (os.getenv("STT_DEBUG", "0") == "1")
        self._dbg: Optional[logging.Logger] = None
        self._stats = _SessionStats()

        self._whisper = None  # whisper.load_model() 반환값 (OpenAI whisper)
        self._porcupine = None
        self._stream:   Optional[sd.RawInputStream] = None
        self._loop:     Optional[asyncio.AbstractEventLoop] = None
        self._task:     Optional[asyncio.Task] = None
        self._running   = False

        self._state = STATE_IDLE
        self._audio_queue: asyncio.Queue = asyncio.Queue()

        self._nr_available      = False
        self._noise_profile:    Optional[np.ndarray] = None
        self._noise_frames:     list[np.ndarray] = []
        self._noise_collected   = False
        self._noise_refresh_cnt = 0
        self._idle_chunk_cnt    = 0

        # 파이프라인 타임스탬프 (디버그용)
        self._t_wake:       Optional[float] = None  # 웨이크 감지 시각
        self._t_speech_start: Optional[float] = None  # 발화 시작 시각
        self._wake_mode:    str = "porcupine"        # "porcupine" | "button"

    # ── 디버그 로그 헬퍼 ──────────────────────────────────────────────

    def _d(self, tag: str, msg: str, level: str = "debug"):
        """디버그 모드일 때만 출력"""
        if not self.debug_mode or self._dbg is None:
            return
        full = f"[{tag}] {msg}"
        getattr(self._dbg, level)(full)

    # ════════════════════════════════════════════
    # 공개 API
    # ════════════════════════════════════════════

    async def start(self):
        """Whisper → Porcupine 로드 완료 후 마이크 시작"""
        if self.debug_mode:
            self._dbg = _setup_debug_logger()
            self._d("DBG-INIT", f"debug_mode=ON | VAD_ENERGY_THRESH={self.energy_threshold} | "
                                f"VAD_SILENCE_SEC={VAD_SILENCE_SEC} | WAKE_LISTEN_SEC={WAKE_LISTEN_SEC} | "
                                f"noise_reduction={self.noise_reduction} | prop_decrease={self.noise_prop_decrease}")

        self._check_noisereduce()
        await self._load_models()

        self._loop    = asyncio.get_event_loop()
        self._running = True

        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE_INT16,
            blocksize=PORCUPINE_FRAME_SIZE,
            device=self.mic_device,
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info(
            f"[STT] 마이크 스트림 시작 "
            f"(SR={SAMPLE_RATE}Hz, frame={PORCUPINE_FRAME_SIZE}, device={self.mic_device})"
        )
        logger.info(
            f"[STT] 노이즈 억제: "
            f"{'활성 prop=' + str(self.noise_prop_decrease) if self._nr_available and self.noise_reduction else '비활성'}"
        )

        self._task = asyncio.create_task(self._process_loop())
        logger.info(f"[STT] Porcupine 웨이크 워드 대기 중: '{self.wake_word}'")

        if self.debug_mode:
            self._d("DBG-INIT", "마이크 스트림 시작 완료 — 웨이크워드 대기 중")

    async def stop(self):
        """STT 엔진 종료 + 세션 통계 출력"""
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._porcupine:
            self._porcupine.delete()

        # 세션 종료 시 통계 출력
        if self.debug_mode:
            self._d("DBG-STAT", self._stats.report(), level="info")

        logger.info("[STT] 엔진 종료")

    async def set_mic_device(self, device_index: int) -> bool:
        """
        마이크 장치를 런타임에 교체
        - 현재 스트림을 닫고 새 device_index로 재시작
        - STT 엔진이 구동 중일 때만 동작 (start() 이후)
        """
        if not self._running:
            self.mic_device = device_index
            logger.info(f"[STT] 마이크 설정 변경 (미구동 중): device={device_index}")
            return True

        try:
            # 기존 스트림 중단
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None

            self.mic_device = device_index

            # 새 장치로 스트림 재시작
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE_INT16,
                blocksize=PORCUPINE_FRAME_SIZE,
                device=self.mic_device,
                callback=self._audio_callback,
            )
            self._stream.start()
            logger.info(f"[STT] 마이크 변경 완료: device={device_index}")
            return True
        except Exception as e:
            logger.error(f"[STT] 마이크 변경 실패 device={device_index}: {e}")
            return False

    def activate(self):
        """버튼 모드: 외부 트리거 → 직접 LISTENING 전환"""
        if self._state == STATE_IDLE:
            self._state    = STATE_LISTENING
            self._wake_mode = "button"
            self._t_wake   = time.time()
            self._stats.wake_mode_counts["button"] += 1
            logger.info("[STT] 외부 트리거(버튼) → LISTENING 활성화")
            self._d("DBG-WAKE", f"mode=button | t={self._t_wake:.3f} | IDLE→LISTENING")

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._running

    # ════════════════════════════════════════════
    # 모델 로드
    # ════════════════════════════════════════════

    async def _load_models(self):
        loop = asyncio.get_event_loop()

        logger.info(f"[STT] Whisper 로드: {self.model_size} / {self.device}")
        t0 = time.time()
        self._whisper = await loop.run_in_executor(None, self._init_whisper)
        self._d("DBG-INIT", f"Whisper 로드 완료: {(time.time()-t0)*1000:.0f}ms")
        logger.info("[STT] Whisper 로드 완료")

        logger.info(f"[STT] Porcupine 로드: {self.porcupine_model_path}")
        t0 = time.time()
        self._porcupine = await loop.run_in_executor(None, self._init_porcupine)
        if self._porcupine:
            self._d("DBG-INIT", f"Porcupine 로드 완료: {(time.time()-t0)*1000:.0f}ms | "
                                f"frame_length={self._porcupine.frame_length}")
            logger.info(
                f"[STT] Porcupine 로드 완료 "
                f"(frame_length={self._porcupine.frame_length}, "
                f"sample_rate={self._porcupine.sample_rate})"
            )
        else:
            logger.error("[STT] Porcupine 로드 실패 — 웨이크 워드 감지 불가")

    def _init_whisper(self):
        """OpenAI whisper.load_model() — 반환값은 transcribe() 메서드가 있는 모델 객체."""
        return whisper.load_model(self.model_size, device=self.device)

    def _init_porcupine(self):
        try:
            import pvporcupine
        except ImportError:
            logger.error("[STT] pvporcupine 미설치: pip install pvporcupine")
            return None

        ppn_path = Path(self.porcupine_model_path)
        pv_path  = Path(self.porcupine_params_path)

        if not ppn_path.exists():
            logger.error(f"[STT] .ppn 파일 없음: {ppn_path.resolve()}")
            return None
        if not pv_path.exists():
            logger.error(f"[STT] .pv 파일 없음: {pv_path.resolve()}")
            return None

        try:
            import pvporcupine
            porcupine = pvporcupine.create(
                access_key=self.porcupine_access_key,
                keyword_paths=[str(ppn_path)],
                model_path=str(pv_path),
            )
            return porcupine
        except Exception as e:
            logger.error(f"[STT] Porcupine 초기화 실패: {e}")
            return None

    # ════════════════════════════════════════════
    # 오디오 콜백
    # ════════════════════════════════════════════

    def _audio_callback(self, indata: bytes, frames: int, time_info, status):
        if status:
            logger.warning(f"[STT] 오디오 상태: {status}")
            self._d("DBG-AUDIO", f"callback status: {status}", level="warning")
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._audio_queue.put(bytes(indata)), self._loop
            )

    # ════════════════════════════════════════════
    # 메인 처리 루프
    # ════════════════════════════════════════════

    async def _process_loop(self):
        float_buf:     list[np.ndarray] = []
        silence_start: Optional[float]  = None
        speech_start:  Optional[float]  = None
        listen_start:  Optional[float]  = None

        # VAD 디버그: 연속 에너지 로그 간격 (매 N청크마다 출력)
        _vad_log_every = 10   # 약 0.32초마다
        _vad_log_cnt   = 0

        while self._running:
            try:
                raw: bytes = await asyncio.wait_for(
                    self._audio_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                if self._state == STATE_LISTENING and listen_start:
                    elapsed = time.time() - listen_start
                    if elapsed > WAKE_LISTEN_SEC:
                        logger.info("[STT] 명령 대기 타임아웃 → IDLE")
                        self._d("DBG-WAKE",
                                f"TIMEOUT | mode={self._wake_mode} | "
                                f"listen_elapsed={elapsed:.2f}s → IDLE")
                        self._state   = STATE_IDLE
                        float_buf     = []
                        silence_start = None
                        speech_start  = None
                        listen_start  = None
                        if self.on_timeout:
                            asyncio.create_task(self.on_timeout())
                continue

            now       = time.time()
            qsize     = self._audio_queue.qsize()
            pcm_int16 = np.frombuffer(raw, dtype=np.int16)
            chunk_f32 = pcm_int16.astype(np.float32) / 32768.0
            energy    = float(np.sqrt(np.mean(chunk_f32 ** 2)))

            # ── 큐 백로그 경고 (디버그) ─────────────────────────────
            if self.debug_mode and qsize > 5:
                self._d("DBG-QUEUE",
                        f"⚠️  백로그 qsize={qsize} | state={self._state} "
                        f"→ 처리 지연 발생 중", level="warning")

            # ════════════════════════
            # IDLE: Porcupine 감지
            # ════════════════════════
            if self._state == STATE_IDLE:

                # 노이즈 프로파일 수집
                self._idle_chunk_cnt += 1
                if self._idle_chunk_cnt % 16 == 0:
                    self._collect_noise_profile(chunk_f32)

                # 주기적 에너지 로그 (IDLE 중 배경음 레벨 확인용)
                _vad_log_cnt += 1
                if self.debug_mode and _vad_log_cnt % _vad_log_every == 0:
                    self._d("DBG-VAD",
                            f"IDLE  energy={energy:.5f} | "
                            f"thresh={self.energy_threshold} | "
                            f"qsize={qsize}")

                if self._detect_porcupine(pcm_int16):
                    self._state     = STATE_LISTENING
                    self._wake_mode = "porcupine"
                    self._t_wake    = now
                    listen_start    = now
                    float_buf       = []
                    self._noise_frames = []
                    self._stats.wake_mode_counts["porcupine"] += 1

                    logger.info(
                        f"[WAKE] ✅ Porcupine 감지! → LISTENING ({WAKE_LISTEN_SEC}초 대기)"
                    )
                    self._d("DBG-WAKE",
                            f"✅ DETECTED | mode=porcupine | t={now:.3f} | "
                            f"IDLE→LISTENING | noise_profile={'있음' if self._noise_profile is not None else '없음'}")

                    if self.on_wake:
                        asyncio.create_task(self.on_wake())

            # ════════════════════════
            # LISTENING: VAD 발화 수집
            # ════════════════════════
            elif self._state == STATE_LISTENING:
                is_speech = energy > self.energy_threshold
                listen_elapsed = now - listen_start if listen_start else 0

                # VAD 상세 로그
                if self.debug_mode:
                    status_tag = "SPEECH" if is_speech else "SILENT"
                    sil_elapsed = (now - silence_start) if silence_start else 0
                    self._d("DBG-VAD",
                            f"LISTEN [{status_tag}] energy={energy:.5f} | "
                            f"thresh={self.energy_threshold} | "
                            f"buf={len(float_buf)}chunks | "
                            f"listen_elapsed={listen_elapsed:.2f}s | "
                            f"sil_elapsed={sil_elapsed:.2f}s | "
                            f"qsize={qsize}")

                if is_speech:
                    float_buf.append(chunk_f32)
                    silence_start = None
                    if speech_start is None:
                        speech_start       = now
                        self._t_speech_start = now
                        self._stats.attempts += 1
                        logger.info(f"[VAD] 발화 시작 (energy={energy:.4f})")
                        self._d("DBG-VAD",
                                f"🎙️  발화 시작 | energy={energy:.5f} | "
                                f"wake_to_speech={(now - self._t_wake):.3f}s")
                else:
                    if speech_start is not None:
                        float_buf.append(chunk_f32)
                        if silence_start is None:
                            silence_start = now

                        sil_elapsed = now - silence_start
                        duration    = now - speech_start

                        if sil_elapsed >= VAD_SILENCE_SEC:
                            if duration * 1000 >= VAD_MIN_SPEECH_MS:
                                audio = np.concatenate(float_buf)
                                logger.info(f"[VAD] 발화 종료: {duration:.1f}s")
                                self._d("DBG-VAD",
                                        f"🔇 발화 종료 | duration={duration:.2f}s | "
                                        f"audio_samples={len(audio)} | "
                                        f"sil_elapsed={sil_elapsed:.2f}s")
                                await self._transcribe(audio, speech_start)
                            else:
                                logger.debug(
                                    f"[VAD] 발화 너무 짧음 ({duration*1000:.0f}ms) → 재대기"
                                )
                                self._d("DBG-VAD",
                                        f"⚡ 발화 너무 짧음 | duration={duration*1000:.0f}ms | "
                                        f"min={VAD_MIN_SPEECH_MS}ms → filtered=too_short",
                                        level="warning")
                                self._stats.filtered_short += 1

                            self._state   = STATE_IDLE
                            float_buf     = []
                            silence_start = None
                            speech_start  = None
                            listen_start  = None

                    elif listen_start and listen_elapsed > WAKE_LISTEN_SEC:
                        logger.info("[STT] 발화 없음 타임아웃 → IDLE")
                        self._d("DBG-WAKE",
                                f"TIMEOUT(no speech) | listen_elapsed={listen_elapsed:.2f}s → IDLE")
                        self._state  = STATE_IDLE
                        listen_start = None
                        float_buf    = []
                        if self.on_timeout:
                            asyncio.create_task(self.on_timeout())

            # 최대 발화 길이 초과
            if (self._state == STATE_LISTENING
                    and speech_start
                    and now - speech_start >= VAD_MAX_SPEECH_SEC):
                audio = np.concatenate(float_buf)
                logger.info("[VAD] 최대 발화 길이 초과: 강제 처리")
                self._d("DBG-VAD",
                        f"⚠️  최대 발화 초과 | duration={now-speech_start:.1f}s → 강제 처리",
                        level="warning")
                await self._transcribe(audio, speech_start)
                self._state   = STATE_IDLE
                float_buf     = []
                silence_start = None
                speech_start  = None
                listen_start  = None

    # ════════════════════════════════════════════
    # Porcupine 감지
    # ════════════════════════════════════════════

    def _detect_porcupine(self, pcm_int16: np.ndarray) -> bool:
        if self._porcupine is None:
            return False
        try:
            result = self._porcupine.process(pcm_int16.tolist())
            return result >= 0
        except Exception as e:
            logger.warning(f"[WAKE] Porcupine 처리 오류: {e}")
            self._d("DBG-WAKE", f"Porcupine 오류: {e}", level="error")
            return False

    # ════════════════════════════════════════════
    # 노이즈 억제
    # ════════════════════════════════════════════

    def _check_noisereduce(self):
        try:
            import noisereduce  # noqa: F401
            self._nr_available = True
            logger.info("[STT] noisereduce 사용 가능 → 노이즈 억제 활성화")
        except ImportError:
            self._nr_available = False
            logger.warning("[STT] noisereduce 미설치 → 노이즈 억제 비활성화")

    def _collect_noise_profile(self, chunk: np.ndarray):
        if not (self._nr_available and self.noise_reduction):
            return
        self._noise_frames.append(chunk.copy())
        total = sum(len(f) for f in self._noise_frames)
        if total >= NOISE_PROFILE_SIZE:
            raw = np.concatenate(self._noise_frames)[:NOISE_PROFILE_SIZE]
            self._noise_profile   = raw.astype(np.float32)
            self._noise_collected = True
            self._noise_frames    = []
            if self._noise_refresh_cnt == 0:
                logger.info("[NR] 배경음 프로파일 초기 수집 완료 (0.5초)")
                self._d("DBG-NR",
                        f"프로파일 초기 수집 완료 | "
                        f"rms={float(np.sqrt(np.mean(self._noise_profile**2))):.5f}")
            else:
                self._d("DBG-NR",
                        f"프로파일 갱신 #{self._noise_refresh_cnt} | "
                        f"rms={float(np.sqrt(np.mean(self._noise_profile**2))):.5f}")
            self._noise_refresh_cnt += 1

    def _apply_noise_reduction(self, audio: np.ndarray) -> tuple[np.ndarray, float]:
        """노이즈 억제 적용. 반환: (처리된 오디오, 처리 시간ms)"""
        if not (self._nr_available and self.noise_reduction):
            return audio, 0.0
        t0 = time.time()
        try:
            import noisereduce as nr
            rms_before = float(np.sqrt(np.mean(audio ** 2)))
            if self._noise_profile is not None:
                reduced = nr.reduce_noise(
                    y=audio, sr=SAMPLE_RATE,
                    y_noise=self._noise_profile,
                    prop_decrease=self.noise_prop_decrease,
                    stationary=False, n_fft=512,
                )
            else:
                self._d("DBG-NR", "프로파일 없음 → stationary 폴백")
                reduced = nr.reduce_noise(
                    y=audio, sr=SAMPLE_RATE,
                    prop_decrease=0.6, stationary=True, n_fft=512,
                )
            rms_after = float(np.sqrt(np.mean(reduced ** 2)))
            nr_ms     = (time.time() - t0) * 1000
            self._d("DBG-NR",
                    f"rms_before={rms_before:.5f} | rms_after={rms_after:.5f} | "
                    f"감쇄={(1 - rms_after/(rms_before+1e-10))*100:.1f}% | "
                    f"nr_ms={nr_ms:.1f}ms")
            return reduced.astype(np.float32), nr_ms
        except Exception as e:
            logger.warning(f"[NR] 노이즈 억제 실패, 원본 사용: {e}")
            return audio, 0.0

    # ════════════════════════════════════════════
    # Whisper 추론
    # ════════════════════════════════════════════

    async def _transcribe(self, audio: np.ndarray, speech_start: Optional[float] = None):
        """오디오 → NR → Whisper → 정제 → 콜백 + 디버그 타임라인"""
        loop      = asyncio.get_event_loop()
        audio_sec = len(audio) / SAMPLE_RATE
        t_transcribe_start = time.time()

        logger.info(f"[TIMER] ▶ [1] Whisper 추론 시작 | 오디오: {audio_sec:.1f}s")

        # NR + Whisper 를 executor 에서 실행
        t0 = time.time()
        try:
            result = await loop.run_in_executor(
                None, self._run_whisper_with_debug, audio
            )
        except Exception as e:
            logger.error(f"[STT] 추론 오류: {e}")
            self._d("DBG-STT", f"추론 오류: {e}", level="error")
            return
        raw_text, nr_ms, whisper_ms = result
        t1 = time.time()

        logger.info(f"[TIMER] ✅ [1] Whisper 완료: {whisper_ms:.0f}ms | raw='{raw_text}'")
        self._d("DBG-STT",
                f"raw='{raw_text}' | "
                f"audio={audio_sec:.2f}s | nr_ms={nr_ms:.0f}ms | whisper_ms={whisper_ms:.0f}ms")

        self._stats.total_whisper_ms.append(whisper_ms)

        # 텍스트 정제
        clean_text, filter_reason = self._clean_text_debug(raw_text)

        if filter_reason:
            self._d("DBG-STT",
                    f"⚠️  filtered={filter_reason} | raw='{raw_text}'",
                    level="warning")
            if filter_reason == "hallucination":
                self._stats.filtered_halluc += 1
            elif filter_reason == "wake_residual":
                self._stats.filtered_wake += 1
            return

        if not clean_text:
            return

        self._d("DBG-STT", f"✅ 정제 결과='{clean_text}'")
        logger.info(f"[STT] 인식 결과: '{clean_text}'")

        # on_result 콜백
        logger.info(f"[TIMER] ▶ [2] on_result(LLM→ESP32) 시작")
        t2 = time.time()
        try:
            await self.on_result(clean_text)
        except Exception as e:
            logger.error(f"[STT] on_result 오류: {e}")
            return
        t3 = time.time()

        llm_ms      = (t3 - t2) * 1000
        total_ms    = (t3 - t_transcribe_start) * 1000
        wake_to_speech_ms = (
            (self._t_speech_start - self._t_wake) * 1000
            if self._t_wake and self._t_speech_start else 0
        )

        self._stats.success += 1
        self._stats.total_pipeline_ms.append(total_ms)

        # 파이프라인 타임라인
        timeline = (
            f"\n[TIMER] ═══════════════════════════════════════\n"
            f"[TIMER]  오디오 수집       : {audio_sec:.2f}s\n"
            f"[TIMER]  NR 처리           : {nr_ms:.0f}ms\n"
            f"[TIMER]  Whisper 추론      : {whisper_ms:.0f}ms\n"
            f"[TIMER]  LLM + ESP32       : {llm_ms:.0f}ms\n"
            f"[TIMER]  전체 처리         : {total_ms:.0f}ms\n"
            f"[TIMER] ═══════════════════════════════════════"
        )
        logger.info(timeline)

        self._d("DBG-PIPE",
                f"✅ 완료 | text='{clean_text}' | "
                f"wake_to_speech={wake_to_speech_ms:.0f}ms | "
                f"speech_dur={audio_sec*1000:.0f}ms | "
                f"nr_ms={nr_ms:.0f}ms | whisper_ms={whisper_ms:.0f}ms | "
                f"llm_ms={llm_ms:.0f}ms | total_ms={total_ms:.0f}ms | "
                f"mode={self._wake_mode}")

        # 10회마다 누적 통계 출력
        if self._stats.success % 10 == 0:
            self._d("DBG-STAT", self._stats.report(), level="info")

    def _run_whisper_with_debug(self, audio: np.ndarray) -> tuple[str, float, float]:
        """NR + Whisper 동기 실행. 반환: (raw_text, nr_ms, whisper_ms). OpenAI whisper 사용."""
        audio_nr, nr_ms = self._apply_noise_reduction(audio)
        # OpenAI whisper: float32, [-1, 1] 범위 기대
        if audio_nr.dtype != np.float32:
            audio_nr = audio_nr.astype(np.float32) / (np.iinfo(np.int16).max if audio_nr.dtype == np.int16 else 1.0)

        t0 = time.time()
        result = self._whisper.transcribe(
            audio_nr,
            language=self.language,
            fp16=(self.device != "cpu"),
        )
        raw_text = (result.get("text") or "").strip()
        whisper_ms = (time.time() - t0) * 1000
        return raw_text, nr_ms, whisper_ms

    # ════════════════════════════════════════════
    # 텍스트 정제 (필터 이유 반환)
    # ════════════════════════════════════════════

    def _clean_text_debug(self, text: str) -> tuple[str, Optional[str]]:
        """
        정제 결과와 필터 이유를 함께 반환
        filter_reason: None | "too_short" | "repeat_noise" | "hallucination" | "wake_residual"
        """
        original = text
        text = text.strip()
        if not text:
            return "", "empty"

        # 웨이크워드 잔류 제거
        wake_patterns = [
            r"(자비스야[,.]?\s*)+",
            r"(자비야[,.]?\s*)+",
            r"(자비스[,.]?\s*)+",
            r"(쟈비스야[,.]?\s*)+",
            r"(재비스야[,.]?\s*)+",
            r"([Jj]abis\s*ya[,.]?\s*)+",
            r"([Jj]arvis\s*ya[,.]?\s*)+",
            r"([Hh]ey\s*[Jj]arvis[,.]?\s*)+",
        ]
        for pat in wake_patterns:
            text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()
        text = text.lstrip(",.·· ").strip()

        if len(text) < 2:
            reason = "wake_residual" if original != text else "too_short"
            return "", reason

        # ── 한국어 IoT 오인식 교정 ──────────────────────────────────
        _KO_CORRECTIONS = {
            # 방 이름 오인식
            "칭실": "침실", "침신": "침실", "치실": "침실",
            "첨실": "침실", "침실실": "침실", "침숙": "침실",
            "욕시": "욕실", "요실": "욕실", "욕슬": "욕실",
            "현과": "현관", "현광": "현관", "현간": "현관",
            "차그": "차고",
            # 동사 오인식
            "켜죠": "켜줘", "켜주": "켜줘", "켜줘요": "켜줘",
            "켜져": "켜줘",
            "꺼죠": "꺼줘", "꺼주": "꺼줘", "꺼줘요": "꺼줘",
            "꺼져": "꺼줘",
            "열어죠": "열어줘", "열어줘요": "열어줘",
            "닫아죠": "닫아줘", "닫아줘요": "닫아줘",
            "알려죠": "알려줘", "알려줘요": "알려줘",
            # 기기명 오인식
            "잔든": "전등", "전든": "전등", "전댕": "전등",
        }
        words = text.split()
        corrected = " ".join(_KO_CORRECTIONS.get(w, w) for w in words)
        if corrected != text:
            logger.debug(f"[STT] 오인식 교정: '{text}' → '{corrected}'")
            text = corrected

        # 반복 노이즈
        if re.match(r"^(.{1,4})[,. ]+\1[,. ]+\1.*$", text):
            return "", "repeat_noise"

        # 환각 패턴
        hallucinations = [
            "MBC", "KBS", "SBS", "EBS", "YTN",
            "자막", "번역", "시청", "구독", "좋아요", "알림",
            "자막 제공", "번역 제공", "영상 제공",
            "감사합니다", "안녕하세요", "안녕히계세요",
            "Thank you", "Thank you for watching",
            "Subtitle", "Subtitles", "Subtitle by",
            "Please subscribe", "Like and subscribe",
            ".", "..", "...", "음", "어", "아",
            "음...", "어...", "네.", "네", "응", "응.", "음.", "어.",
        ]
        text_stripped = text.rstrip(".!?")
        if text in hallucinations or text_stripped in hallucinations:
            return "", "hallucination"

        return text, None

    async def run_with_retry(self):
        """start() 래퍼: 실패 시 재시도"""
        retry_delay = 5
        while True:
            try:
                await self.start()
                break
            except Exception as e:
                logger.error(f"[STT] 시작 실패: {e} → {retry_delay}초 후 재시도")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)