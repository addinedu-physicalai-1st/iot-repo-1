"""
server/main.py
==============
Voice IoT Controller - 진입점 v0.7

v0.7 변경사항:
  - PIR 이벤트 수신 엔드포인트 추가: POST /pir-event
    · ESP32 → 서버로 guard_alert / presence_alert 이벤트 수신
    · context(away/sleep/home) 별 로그 + WS 브로드캐스트
    · telegram_bot 연동 준비 (모듈 존재 시 자동 호출)

v0.6 변경사항:
  - TTSEngine 생성: edge-tts 파라미터 추가 (edge_rate, edge_volume)
  - settings.yaml tts.edge 블록 읽기 지원

v0.5 변경사항:
  - TTSEngine 추가: 서버 시작 시 초기화 (settings.yaml tts 블록)
  - _make_stt_callback: LLM 파싱 결과의 tts_response → TTSEngine.speak() 비동기 호출
  - _print_banner: TTS 상태 출력 추가
  - lifespan: TTS 엔진 초기화 및 종료 처리 추가

v0.4 변경사항:
  - LLM 워밍업 추가: 서버 시작 시 더미 호출로 qwen2.5:7b 콜드 스타트 제거

역할:
  - FastAPI 앱 생성 및 설정 로드
  - TCPServer / WebSocketHub / CommandRouter 인스턴스 생성 및 연결
  - LLMEngine (Ollama) 생성 → CommandRouter 주입
  - STTEngine (Whisper + 웨이크 워드) 생성 → 음성 파이프라인 연결
  - TTSEngine (Kokoro / ElevenLabs / edge-tts) 생성 → LLM 응답 음성 출력
  - FastAPI lifespan 으로 TCP 서버 + STT 엔진 동시 구동
  - uvicorn 으로 HTTP/WebSocket 서버 실행

전체 파이프라인:
  마이크 → STTEngine("자비스야") → LLMEngine(Ollama)
                                        ↓
  WebSocket/REST → CommandRouter → TCPServer → ESP32
                                        ↓
                                   TTSEngine → 스피커

실행:
  cd ~/dev_ws/voice_iot_controller
  uvicorn server.main:app --host 0.0.0.0 --port 8000

  STT 없이 실행 (서버만):
  DISABLE_STT=1 uvicorn server.main:app --host 0.0.0.0 --port 8000

  TTS 없이 실행:
  DISABLE_TTS=1 uvicorn server.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from server.tcp_server     import TCPServer
from server.websocket_hub  import WebSocketHub
from server.command_router import CommandRouter
from server.llm_engine     import LLMEngine
from server.stt_engine     import STTEngine
from server.tts_engine     import TTSEngine
from server.api_routes     import create_router

# ─────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 설정 로드
# ─────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"

def load_settings() -> dict:
    if not CONFIG_PATH.exists():
        logger.error(f"설정 파일 없음: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    logger.info(f"설정 로드 완료: {CONFIG_PATH}")
    return cfg


# ─────────────────────────────────────────────
# 앱 팩토리
# ─────────────────────────────────────────────

def create_app() -> FastAPI:
    cfg = load_settings()

    # ── 환경 변수 플래그 ─────────────────────────────────────────────
    disable_stt = os.getenv("DISABLE_STT", "0") == "1"
    disable_llm = os.getenv("DISABLE_LLM", "0") == "1"
    disable_tts = os.getenv("DISABLE_TTS", "0") == "1"

    # ════════════════════════════════════════════
    # 1. 핵심 인스턴스 생성
    # ════════════════════════════════════════════
    tcp_server = TCPServer(
        host=cfg["server"]["host"],
        port=cfg["server"]["tcp_port"],
    )

    ws_hub = WebSocketHub()

    # ── 브라우저 접속 시 현재 상태 즉시 전송 ────────────────────────
    async def _on_ws_connect(client_id: str, send_fn):
        """새 브라우저 접속 → device_list + 각 device_update 즉시 전송"""
        from protocol.schema import ws_device_list, ws_device_update
        devices = tcp_server.get_device_list()
        await send_fn(ws_device_list(devices))
        for d in devices:
            await send_fn(ws_device_update(d["device_id"], d["state"]))
        logger.info(f"[WS] 접속 {client_id} → device_list {len(devices)}개 + state 전송")

    ws_hub._on_connect = _on_ws_connect

    # ── LLM 엔진 ────────────────────────────────────────────────────
    llm_engine: LLMEngine | None = None
    if not disable_llm:
        llm_engine = LLMEngine(
            model=cfg["ollama"]["model"],
            host=cfg["ollama"]["host"],
            timeout=cfg["ollama"].get("timeout", 10),
        )
        logger.info(
            f"LLMEngine 생성: model={cfg['ollama']['model']} "
            f"host={cfg['ollama']['host']}"
        )
    else:
        logger.warning("LLM 비활성화 (DISABLE_LLM=1) → 키워드 fallback 사용")

    # ── TTS 엔진 ────────────────────────────────────────────────────
    tts_engine: TTSEngine | None = None
    if not disable_tts:
        _tts = cfg.get("tts", {})
        _provider = _tts.get("provider", "kokoro")
        _tts_cfg  = _tts.get(_provider, {})
        tts_engine = TTSEngine(
            provider     = _provider,
            # Kokoro
            model_path   = _tts_cfg.get("model_path",  "models/kokoro-v0_19.onnx"),
            voices_path  = _tts_cfg.get("voices_path", "models/voices.bin"),
            voice        = _tts_cfg.get("voice",       "ko-KR-SunHiNeural"),
            speed        = _tts_cfg.get("speed",       1.0),
            lang         = _tts_cfg.get("lang",        "en-us"),
            # ElevenLabs
            api_key      = _tts_cfg.get("api_key",     ""),
            voice_id     = _tts_cfg.get("voice_id",    ""),
            model_id     = _tts_cfg.get("model_id",    "eleven_multilingual_v2"),
            # edge-tts
            edge_rate    = _tts_cfg.get("edge_rate",   "+0%"),
            edge_volume  = _tts_cfg.get("edge_volume", "+0%"),
        )
        logger.info(f"TTSEngine 생성: provider={_provider} voice={_tts_cfg.get('voice','ko-KR-SunHiNeural')}")
    else:
        logger.warning("TTS 비활성화 (DISABLE_TTS=1)")

    # ── 명령 라우터 (LLM 주입) ───────────────────────────────────────
    command_router = CommandRouter(
        tcp_server=tcp_server,
        settings=cfg,
        llm_engine=llm_engine,
    )

    # ── STT 엔진 ────────────────────────────────────────────────────
    stt_engine: STTEngine | None = None
    if not disable_stt:
        _stt = cfg.get("stt", {})
        stt_engine = STTEngine(
            on_result              = _make_stt_callback(command_router, ws_hub, tts_engine),
            on_wake                = _make_wake_callback(ws_hub),
            on_timeout             = _make_timeout_callback(ws_hub),
            model_size             = _stt.get("model_size", "base"),
            language               = _stt.get("language", "ko"),
            device                 = _stt.get("device", "cpu"),
            wake_word              = _stt.get("wake_word", "자비스야"),
            porcupine_access_key   = _stt.get("porcupine_access_key", ""),
            porcupine_model_path   = _stt.get("porcupine_model_path", ""),
            porcupine_params_path  = _stt.get("porcupine_params_path", ""),
            mic_device             = _stt.get("mic_device"),
            energy_threshold       = _stt.get("vad_energy_threshold", 0.02),
            noise_reduction        = _stt.get("noise_reduction", True),
            noise_prop_decrease    = _stt.get("noise_prop_decrease", 0.85),
            debug_mode             = _stt.get("debug_mode", False),
        )
        logger.info(
            f"STTEngine 생성: model={_stt.get('model_size', 'base')} "
            f"wake_word={_stt.get('wake_word', '자비스야')} "
            f"noise_reduction={_stt.get('noise_reduction', True)} "
            f"prop_decrease={_stt.get('noise_prop_decrease', 0.85)}"
        )
    else:
        logger.warning("STT 비활성화 (DISABLE_STT=1) → 수동 입력만 사용")

    # ════════════════════════════════════════════
    # 2. 상호 의존성 연결
    # ════════════════════════════════════════════

    tcp_server.ws_broadcast = ws_hub.broadcast
    ws_hub._on_message = command_router.handle

    # ════════════════════════════════════════════
    # 3. lifespan
    # ════════════════════════════════════════════

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _print_banner(cfg, stt_engine, llm_engine, tts_engine)

        # TCP 서버 시작
        await tcp_server.start()

        # LLM 연결 확인 + 워밍업
        if llm_engine:
            available = await llm_engine.is_available()
            if available:
                models = await llm_engine.list_models()
                logger.info(f"Ollama 연결 성공 | 모델: {models}")
                await _warmup_llm(llm_engine)
            else:
                logger.warning(
                    "Ollama 연결 실패 → 키워드 fallback 으로 동작 "
                    f"(host={cfg['ollama']['host']})"
                )

        # TTS 엔진 초기화
        if tts_engine:
            tts_ok = await tts_engine.initialize()
            if tts_ok:
                logger.info(f"[TTS] 초기화 완료: provider={tts_engine.get_provider()}")
            else:
                logger.warning("[TTS] 초기화 실패 — 음성 답변 비활성화")

        # STT 엔진 시작 (비동기 태스크)
        stt_task: asyncio.Task | None = None
        if stt_engine:
            stt_task = asyncio.create_task(_start_stt(stt_engine))

        # 상태 폴링 태스크 시작 (30초 주기)
        poll_interval = cfg.get("state_polling", {}).get("interval", 30)
        poll_task = asyncio.create_task(tcp_server.start_polling(interval=poll_interval))
        logger.info(f"[State] 주기적 상태 폴링 시작 (interval={poll_interval}s)")

        logger.info("=" * 50)
        logger.info("서버 준비 완료 - 요청 대기 중")
        logger.info("=" * 50)

        yield  # ── 앱 실행 중 ──

        # ── 종료 처리 ───────────────────────────────────────────────
        logger.info("종료 신호 수신 - 정리 중...")

        if stt_engine:
            await stt_engine.stop()
        if stt_task:
            stt_task.cancel()
        if poll_task:
            poll_task.cancel()

        if llm_engine:
            await llm_engine.close()

        await tcp_server.stop()
        logger.info("Voice IoT Controller 종료 완료")

    # ════════════════════════════════════════════
    # 4. FastAPI 앱 생성
    # ════════════════════════════════════════════

    app = FastAPI(
        title="Voice IoT Controller",
        description="음성 명령 기반 ESP32 IoT 디바이스 제어 시스템",
        version="0.6.0",
        lifespan=lifespan,
    )

    # 정적 파일 마운트
    static_dir = Path(__file__).parent.parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 라우터 등록
    api_router = create_router(
        tcp_server=tcp_server,
        ws_hub=ws_hub,
        command_router=command_router,
    )
    app.include_router(api_router)

    # ── PIR 이벤트 엔드포인트 (ESP32 → 서버) ────────────────────────
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.post("/pir-event")
    async def pir_event(request: Request):
        """
        ESP32 PIR 이벤트 수신 (v0.7)
        ESP32: POST /pir-event {"type":"pir_event","event":"guard_alert","detail":"...","context":"away"}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "msg": "invalid JSON"}, status_code=400)

        event   = body.get("event", "")
        detail  = body.get("detail", "")
        context = body.get("context", "unknown")

        # 이벤트별 메시지 생성
        msg_map = {
            ("guard_alert",    "away"):  "🚨 외출 중 침입 감지!",
            ("guard_alert",    "sleep"): "🚨 취침 중 거실 침입 감지!",
            ("presence_alert", "home"):  "⚠️ 장시간 움직임 없음 — 괜찮으신가요?",
        }
        alert_msg = msg_map.get((event, context), f"PIR 이벤트: {event} ({context})")

        logger.warning(f"[PIR] {alert_msg} | detail={detail}")

        # WS 브로드캐스트 → 프론트엔드 알림
        await ws_hub.broadcast(
            f'{{"type":"pir_alert","msg":"{alert_msg}","event":"{event}","context":"{context}"}}'
        )

        # Telegram 알림 (telegram_bot 모듈 존재 시)
        try:
            from server.telegram_bot import send_alert
            await send_alert(alert_msg)
            logger.info("[PIR] Telegram 알림 전송 완료")
        except ImportError:
            logger.info("[PIR] telegram_bot 미설치 — Telegram 알림 스킵")
        except Exception as e:
            logger.warning(f"[PIR] Telegram 알림 실패: {e}")

        return JSONResponse({"status": "ok", "msg": alert_msg})

    # 인스턴스 바인딩 (디버그 / 테스트용)
    app.state.tcp_server     = tcp_server
    app.state.ws_hub         = ws_hub
    app.state.command_router = command_router
    app.state.llm_engine     = llm_engine
    app.state.stt_engine     = stt_engine
    app.state.tts_engine     = tts_engine
    app.state.settings       = cfg

    return app


# ─────────────────────────────────────────────
# STT 콜백 팩토리
# ─────────────────────────────────────────────

def _make_stt_callback(
    command_router: CommandRouter,
    ws_hub: WebSocketHub,
    tts_engine: TTSEngine | None,
):
    """
    STTEngine.on_result 콜백 생성
    음성 인식 텍스트 → CommandRouter → ESP32
    LLM 응답의 tts_response → TTSEngine.speak() (비동기, 논블로킹)
    """
    async def on_stt_result(text: str):
        import time
        logger.info(f"[Pipeline] STT → '{text}'")

        # WS 브로드캐스트: 인식된 텍스트 전달
        await ws_hub.broadcast(
            f'{{"type":"stt_result","text":"{text}"}}'
        )

        # CommandRouter → LLM 파싱 → ESP32 전송
        logger.info(f"[TIMER] ▶ [B] CommandRouter(LLM→ESP32) 시작")
        tB0 = time.time()
        result = await command_router.handle(
            client_id="stt_engine",
            data={"type": "voice_text", "text": text},
        )
        tB1 = time.time()
        logger.info(f"[TIMER] ✅ [B] CommandRouter 완료: {tB1-tB0:.2f}s")

        # TTS 음성 답변 (논블로킹)
        # command_router.handle() 이 반환한 result 에서 tts_response 추출
        tts_text = _extract_tts_response(result)
        if tts_text and tts_engine:
            asyncio.create_task(tts_engine.speak(tts_text))
            logger.info(f"[TTS] 발화 예약: '{tts_text[:30]}'")

        # 결과 브로드캐스트
        await ws_hub.broadcast(result)

    return on_stt_result


def _extract_tts_response(result) -> str | None:
    """
    CommandRouter.handle() 반환값에서 tts_response 추출
    result 는 JSON 문자열 또는 dict 일 수 있음
    """
    if not result:
        return None
    try:
        import json as _json
        if isinstance(result, str):
            data = _json.loads(result)
        elif isinstance(result, dict):
            data = result
        else:
            return None
        return data.get("tts_response") or None
    except Exception:
        return None


def _make_wake_callback(ws_hub: WebSocketHub):
    """웨이크 워드 감지 콜백"""
    async def on_wake():
        logger.info("[Pipeline] 웨이크 워드 감지 → UI 알림")
        await ws_hub.broadcast(
            '{"type":"wake_detected","msg":"자비스야 감지됨 - 명령을 말씀하세요"}'
        )
    return on_wake


def _make_timeout_callback(ws_hub: WebSocketHub):
    """명령 대기 타임아웃 콜백"""
    async def on_timeout():
        logger.info("[Pipeline] 웨이크 워드 타임아웃 → IDLE 복귀")
        await ws_hub.broadcast(
            '{"type":"wake_timeout","msg":"명령 대기 시간 초과"}'
        )
    return on_timeout


# ─────────────────────────────────────────────
# LLM 워밍업 (콜드 스타트 제거)
# ─────────────────────────────────────────────

async def _warmup_llm(llm_engine: LLMEngine):
    """
    서버 시작 시 더미 호출로 qwen2.5:7b 를 메모리에 올려둠
    → 첫 번째 실제 명령의 콜드 스타트 지연(5~7초) 제거
    """
    import time
    logger.info("[LLM] 워밍업 시작 — 모델 메모리 적재 중...")
    t0 = time.time()
    try:
        await llm_engine.parse("테스트")
    except Exception as e:
        logger.warning(f"[LLM] 워밍업 중 예외 (무시): {e}")
    elapsed = (time.time() - t0) * 1000
    logger.info(f"[LLM] 워밍업 완료 — {elapsed:.0f}ms (이후 첫 명령부터 빠르게 응답)")


# ─────────────────────────────────────────────
# STT 시작 래퍼 (오류 복구 포함)
# ─────────────────────────────────────────────

async def _start_stt(stt_engine: STTEngine):
    """STT 엔진 시작 + 예외 발생 시 재시작 로직"""
    retry_delay = 5
    while True:
        try:
            await stt_engine.start()
            break
        except Exception as e:
            logger.error(f"[STT] 시작 실패: {e} → {retry_delay}초 후 재시도")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


# ─────────────────────────────────────────────
# 시작 배너
# ─────────────────────────────────────────────

def _print_banner(cfg: dict, stt_engine, llm_engine, tts_engine):
    _stt = cfg.get("stt", {})
    _tts = cfg.get("tts", {})
    w = 52
    logger.info("=" * w)
    logger.info(" Voice IoT Controller  v0.6")
    logger.info("=" * w)
    logger.info(f"  TCP  : {cfg['server']['host']}:{cfg['server']['tcp_port']}")
    logger.info(f"  HTTP : 0.0.0.0:{cfg['server']['ws_port']}")
    logger.info(f"  WS   : ws://0.0.0.0:{cfg['server']['ws_port']}/ws")
    logger.info("-" * w)
    logger.info(f"  LLM  : {'✅ ' + cfg['ollama']['model'] if llm_engine else '⛔ 비활성화'}")
    logger.info(f"  STT  : {'✅ ' + _stt.get('model_size','base') + ' (Whisper)' if stt_engine else '⛔ 비활성화'}")
    logger.info(f"  WAKE : {'✅ Porcupine / ' + _stt.get('wake_word','자비스야') if stt_engine else '⛔ 비활성화'}")
    _nr = _stt.get("noise_reduction", True)
    _pd = _stt.get("noise_prop_decrease", 0.85)
    logger.info(f"  NR   : {'✅ prop_decrease=' + str(_pd) if _nr else '⛔ 비활성화'}")
    _provider = _tts.get("provider", "edge") if tts_engine else None
    _voice    = _tts.get(_provider, {}).get("voice", "") if _provider else ""
    logger.info(f"  TTS  : {'✅ ' + _provider + ' / ' + _voice if tts_engine else '⛔ 비활성화'}")
    logger.info("=" * w)


# ─────────────────────────────────────────────
# 앱 인스턴스 (uvicorn 직접 참조용)
# ─────────────────────────────────────────────

app = create_app()


# ─────────────────────────────────────────────
# 직접 실행
# ─────────────────────────────────────────────

if __name__ == "__main__":
    cfg = load_settings()
    uvicorn.run(
        "server.main:app",
        host="0.0.0.0",
        port=cfg["server"]["ws_port"],
        reload=False,
        log_level="info",
    )
