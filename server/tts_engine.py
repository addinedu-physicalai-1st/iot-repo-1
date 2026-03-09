"""
server/tts_engine.py
====================
TTS (Text-to-Speech) 엔진  v1.1

역할:
  - Kokoro(로컬 ONNX) / ElevenLabs(API) / edge-tts(Microsoft) 공통 인터페이스
  - LLM 음성 답변 및 리액션 텍스트를 스피커로 출력
  - asyncio 기반 비동기 발화 (메인 파이프라인 블로킹 없음)
  - 동시 발화 방지 (Lock)

v1.1 변경사항:
  - edge-tts provider 추가 (Microsoft Edge TTS, 무료, 한국어 지원)
  - TTSProvider enum에 EDGE 추가
  - _init_edge() / _speak_edge() 구현
  - 한국어 기본 목소리: ko-KR-SunHiNeural (여성)
  - 설치: pip install edge-tts

v1.0 신규:
  - TTSEngine 클래스: provider 전환 가능 구조
  - Kokoro ONNX 로컬 TTS 지원
  - ElevenLabs API TTS 지원
  - CPU 블로킹 작업 → run_in_executor 처리

사용:
  from server.tts_engine import TTSEngine

  # edge-tts (한국어 권장)
  engine = TTSEngine(provider="edge", voice="ko-KR-SunHiNeural")
  await engine.initialize()
  await engine.speak("침실 전등을 켰어요.")

설치:
  pip install edge-tts sounddevice soundfile   # edge-tts
  pip install kokoro-onnx sounddevice          # Kokoro
  pip install elevenlabs soundfile             # ElevenLabs

한국어 목소리 (edge-tts):
  ko-KR-SunHiNeural   ← 여성, 자연스러운 한국어 (기본값)
  ko-KR-InJoonNeural  ← 남성
"""

from __future__ import annotations

import asyncio
import io
import logging
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Provider 열거형
# ─────────────────────────────────────────────

class TTSProvider(str, Enum):
    KOKORO      = "kokoro"
    ELEVENLABS  = "elevenlabs"
    EDGE        = "edge"


# ─────────────────────────────────────────────
# TTS 엔진
# ─────────────────────────────────────────────

class TTSEngine:
    """
    Kokoro / ElevenLabs / edge-tts 공통 TTS 인터페이스

    Parameters
    ----------
    provider     : "kokoro" | "elevenlabs" | "edge"

    [Kokoro 전용]
    model_path   : Kokoro ONNX 모델 경로
    voices_path  : Kokoro voices.bin 경로
    voice        : Kokoro 목소리 ID (기본: af_sarah)
    speed        : 발화 속도 (기본: 1.0)
    lang         : 언어 코드 (기본: en-us)

    [ElevenLabs 전용]
    api_key      : ElevenLabs API 키
    voice_id     : ElevenLabs 목소리 ID
    model_id     : ElevenLabs 모델 ID

    [edge-tts 전용]
    voice        : edge-tts 목소리 (기본: ko-KR-SunHiNeural)
    edge_rate    : 발화 속도 "+10%" 빠르게 / "-10%" 느리게 (기본: "+0%")
    edge_volume  : 볼륨 조절 (기본: "+0%")
    """

    def __init__(
        self,
        provider: str       = "edge",
        # Kokoro 설정
        model_path: str     = "models/kokoro-v0_19.onnx",
        voices_path: str    = "models/voices.bin",
        voice: str          = "ko-KR-SunHiNeural",
        speed: float        = 1.0,
        lang: str           = "en-us",
        # ElevenLabs 설정
        api_key: str        = "",
        voice_id: str       = "",
        model_id: str       = "eleven_multilingual_v2",
        # edge-tts 전용
        edge_rate: str      = "+0%",
        edge_volume: str    = "+0%",
    ):
        self.provider     = TTSProvider(provider)
        self.model_path   = model_path
        self.voices_path  = voices_path
        self.voice        = voice
        self.speed        = speed
        self.lang         = lang
        self.api_key      = api_key
        self.voice_id     = voice_id
        self.model_id     = model_id
        self.edge_rate    = edge_rate
        self.edge_volume  = edge_volume

        self._kokoro      = None
        self._el_client   = None
        self._lock        = asyncio.Lock()   # 동시 발화 방지
        self._available   = False
        self.is_speaking  = False            # TTS 재생 중 STT 뮤트용 플래그

    # ── 초기화 ──────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        """TTS 엔진 초기화. 성공 여부 반환."""
        try:
            if self.provider == TTSProvider.KOKORO:
                return await self._init_kokoro()
            elif self.provider == TTSProvider.ELEVENLABS:
                return await self._init_elevenlabs()
            elif self.provider == TTSProvider.EDGE:
                return await self._init_edge()
        except Exception as e:
            logger.error(f"[TTS] 초기화 실패: {e}")
            self._available = False
            return False

    async def _init_kokoro(self) -> bool:
        try:
            from kokoro_onnx import Kokoro
        except ImportError:
            logger.error("[TTS] kokoro-onnx 미설치. pip install kokoro-onnx sounddevice")
            return False

        if not Path(self.model_path).exists():
            logger.error(f"[TTS] Kokoro 모델 파일 없음: {self.model_path}")
            return False
        if not Path(self.voices_path).exists():
            logger.error(f"[TTS] Kokoro voices 파일 없음: {self.voices_path}")
            return False

        loop = asyncio.get_event_loop()
        self._kokoro = await loop.run_in_executor(
            None,
            lambda: Kokoro(self.model_path, self.voices_path)
        )
        self._available = True
        logger.info(f"[TTS] Kokoro 초기화 완료 | voice={self.voice} speed={self.speed} lang={self.lang}")
        return True

    async def _init_elevenlabs(self) -> bool:
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError:
            logger.error("[TTS] elevenlabs 미설치. pip install elevenlabs soundfile")
            return False

        if not self.api_key:
            logger.error("[TTS] ElevenLabs API 키 없음 (settings.yaml tts.elevenlabs.api_key)")
            return False
        if not self.voice_id:
            logger.error("[TTS] ElevenLabs voice_id 없음 (settings.yaml tts.elevenlabs.voice_id)")
            return False

        self._el_client = ElevenLabs(api_key=self.api_key)
        self._available = True
        logger.info(f"[TTS] ElevenLabs 초기화 완료 | voice_id={self.voice_id} model={self.model_id}")
        return True

    async def _init_edge(self) -> bool:
        """edge-tts 초기화 — 패키지 확인 + 목소리 연결 테스트"""
        try:
            import edge_tts
        except ImportError:
            logger.error("[TTS] edge-tts 미설치. pip install edge-tts")
            return False

        try:
            import sounddevice
            import soundfile
        except ImportError:
            logger.error("[TTS] sounddevice/soundfile 미설치. pip install sounddevice soundfile")
            return False

        # 목소리 연결 테스트 (첫 청크만 확인)
        try:
            tts = edge_tts.Communicate("테스트", voice=self.voice, rate=self.edge_rate)
            async for chunk in tts.stream():
                if chunk["type"] == "audio":
                    break
            self._available = True
            logger.info(f"[TTS] edge-tts 초기화 완료 | voice={self.voice} rate={self.edge_rate}")
            return True
        except Exception as e:
            logger.error(f"[TTS] edge-tts 목소리 테스트 실패: {e}")
            return False

    # ── 발화 ────────────────────────────────────────────────────────

    async def speak(self, text: str) -> None:
        """
        텍스트를 음성으로 재생.
        - 빈 문자열은 무시
        - Lock 으로 동시 발화 방지 (이전 발화 완료 후 다음 발화)
        """
        if not text or not text.strip():
            return
        if not self._available:
            logger.warning(f"[TTS] 엔진 미초기화 상태 — 발화 건너뜀: '{text[:20]}'")
            return

        async with self._lock:
            self.is_speaking = True
            try:
                logger.info(
                    f"[TTS] 발화 시작: '{text[:30]}...'" if len(text) > 30
                    else f"[TTS] 발화 시작: '{text}'"
                )
                if self.provider == TTSProvider.KOKORO:
                    await self._speak_kokoro(text)
                elif self.provider == TTSProvider.ELEVENLABS:
                    await self._speak_elevenlabs(text)
                elif self.provider == TTSProvider.EDGE:
                    await self._speak_edge(text)
                logger.info("[TTS] 발화 완료")
            except Exception as e:
                logger.error(f"[TTS] 발화 실패: {e}")
            finally:
                self.is_speaking = False  # 예외 발생해도 반드시 해제

    async def _speak_kokoro(self, text: str) -> None:
        import sounddevice as sd

        loop = asyncio.get_event_loop()
        samples, sr = await loop.run_in_executor(
            None,
            lambda: self._kokoro.create(
                text,
                voice=self.voice,
                speed=self.speed,
                lang=self.lang,
            )
        )
        await loop.run_in_executor(
            None,
            lambda: (sd.play(samples, sr), sd.wait())
        )

    async def _speak_elevenlabs(self, text: str) -> None:
        import sounddevice as sd
        import soundfile as sf

        loop = asyncio.get_event_loop()
        audio_gen = await loop.run_in_executor(
            None,
            lambda: self._el_client.text_to_speech.convert(
                voice_id=self.voice_id,
                text=text,
                model_id=self.model_id,
            )
        )
        audio_bytes = b"".join(audio_gen)
        buf = io.BytesIO(audio_bytes)
        data, sr = sf.read(buf, dtype="float32")
        await loop.run_in_executor(
            None,
            lambda: (sd.play(data, sr), sd.wait())
        )

    async def _speak_edge(self, text: str) -> None:
        """
        edge-tts → MP3 스트림 수집 → soundfile/sounddevice 재생
        완전 비동기: 네트워크 IO는 edge-tts 코루틴, 오디오 재생은 executor
        """
        import edge_tts
        import sounddevice as sd
        import soundfile as sf

        # MP3 스트림 수집
        buf = io.BytesIO()
        tts = edge_tts.Communicate(
            text,
            voice=self.voice,
            rate=self.edge_rate,
            volume=self.edge_volume,
        )
        async for chunk in tts.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])

        if buf.tell() == 0:
            logger.warning("[TTS] edge-tts 오디오 데이터 없음")
            return

        buf.seek(0)
        loop = asyncio.get_event_loop()

        def _play():
            data, sr = sf.read(buf, dtype="float32")
            sd.play(data, sr)
            sd.wait()

        await loop.run_in_executor(None, _play)

    # ── 상태 확인 ────────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """엔진 초기화 여부 확인"""
        return self._available

    def get_provider(self) -> str:
        """현재 provider 반환"""
        return self.provider.value
