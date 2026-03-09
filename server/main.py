"""
server/main.py
==============
Voice IoT Controller - 진입점 v0.9

v0.9 변경사항:
  - SmartGate 2FA 서브패키지 통합 (server/smartgate/)
  - SmartGateManager lifespan 등록 (startup / shutdown)
  - camera_stream.set_smartgate_manager() 연동 → 프레임 공급
  - DISABLE_SMARTGATE=1 환경 변수로 독립 비활성화
  - 신규 엔드포인트 (api_routes.py):
      GET  /smartgate/status         SmartGate 상태 조회
      POST /smartgate/reload-faces   얼굴 DB 재임베딩
  - _print_banner에 GATE 항목 추가

v0.8 변경사항:
  - ESP32-CAM UDP 기반 현관 보안 카메라 시스템 통합
  - camera_stream.py  : UDP 수신 스레드 + MJPEG WebSocket 스트리밍
  - frame_analyzer.py : InsightFace 얼굴인식 + YOLOv8 객체감지
  - face_db.py        : 등록 얼굴 DB REST API 라우터
  - 신규 엔드포인트:
      GET  /camera/entrance/stream    MJPEG HTTP 스트림 (오버레이 포함)
      GET  /camera/entrance/raw       MJPEG Raw (지연 최소, 재인코딩 없음)
      GET  /camera/entrance/snapshot  스냅샷 JPEG 1장
      GET  /face-db/list              등록 인물 목록
      POST /face-db/register          얼굴 등록 (multipart)
      DEL  /face-db/{name}            인물 삭제
      POST /face-db/rebuild           DB 재빌드
  - 신규 WS 메시지 타입:
      cam_alert  : 미등록 인물 / 택배 감지 알람
      cam_notify : 등록 얼굴 귀가 알림
  - lifespan에 cam_start / FrameAnalyzer 초기화 / analysis_loop task 추가
  - app.state에 camera_stream / frame_analyzer 바인딩

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
  - CameraStream (ESP32-CAM UDP) 생성 → FrameAnalyzer + WS 알람 연결
  - FastAPI lifespan 으로 TCP 서버 + STT 엔진 동시 구동
  - uvicorn 으로 HTTP/WebSocket 서버 실행

전체 파이프라인:
  마이크 → STTEngine("자비스야") → LLMEngine(Ollama)
                                        ↓
  WebSocket/REST → CommandRouter → TCPServer → ESP32
                                        ↓
                                   TTSEngine → 스피커

  ESP32-CAM → UDP → CameraStream → FrameAnalyzer
                                        ↓
                              cam_alert / cam_notify
                                        ↓
                               WS → 웹앱 알람 + 영상

실행:
  cd ~/dev_ws/voice_iot_controller
  uvicorn server.main:app --host 0.0.0.0 --port 8000

  STT 없이 실행 (서버만):
  DISABLE_STT=1 uvicorn server.main:app --host 0.0.0.0 --port 8000

  카메라 없이 실행:
  DISABLE_CAM=1 uvicorn server.main:app --host 0.0.0.0 --port 8000

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
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from server.tcp_server     import TCPServer
from server.websocket_hub  import WebSocketHub
from server.command_router import CommandRouter
from server.llm_engine     import LLMEngine
from server.stt_engine     import STTEngine
from server.tts_engine     import TTSEngine
from server.db_logger      import DBLogger
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

# ── [DEBUG] command_router 진단용 DEBUG 레벨 활성화 ──────────────────
# dnd_mode TTS 미억제 이슈(ISSUE_dnd모드_TTS알람미억제_20260307) 추적용
# 원인 확인 후 아래 두 줄 제거
logging.getLogger("server.command_router").setLevel(logging.DEBUG)
logging.getLogger("server.camera_stream").setLevel(logging.DEBUG)


# ─────────────────────────────────────────────
# 설정 로드
# ─────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
ENV_PATH    = Path(__file__).parent.parent / ".env"

def load_settings() -> dict:
    # .env 파일 로드 (없으면 무시)
    load_dotenv(ENV_PATH)

    if not CONFIG_PATH.exists():
        logger.error(f"설정 파일 없음: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    logger.info(f"설정 로드 완료: {CONFIG_PATH}")

    # ── .env 환경변수로 민감정보 오버라이드 ──────────────────
    if os.getenv("MIC_DEVICE"):
        cfg.setdefault("stt", {})["mic_device"] = int(os.environ["MIC_DEVICE"])

    if os.getenv("PORCUPINE_ACCESS_KEY"):
        cfg.setdefault("stt", {})["porcupine_access_key"] = os.environ["PORCUPINE_ACCESS_KEY"]

    if os.getenv("DB_HOST"):
        cfg.setdefault("database", {})["host"] = os.environ["DB_HOST"]
    if os.getenv("DB_USER"):
        cfg.setdefault("database", {})["user"] = os.environ["DB_USER"]
    if os.getenv("DB_PASSWORD"):
        cfg.setdefault("database", {})["password"] = os.environ["DB_PASSWORD"]

    if os.getenv("OLLAMA_HOST"):
        cfg.setdefault("ollama", {})["host"] = os.environ["OLLAMA_HOST"]
    if os.getenv("LLM_MODEL"):
        cfg.setdefault("ollama", {})["model"] = os.environ["LLM_MODEL"]

    # ── SmartGate 제스처 시퀀스 (.env SMARTGATE_SEQUENCE="1,0,3") ──
    if os.getenv("SMARTGATE_SEQUENCE"):
        try:
            seq = [int(x.strip()) for x in os.environ["SMARTGATE_SEQUENCE"].split(",")]
            cfg.setdefault("smartgate", {}).setdefault("gesture_auth", {})["sequence"] = seq
        except ValueError:
            logger.warning("[Config] SMARTGATE_SEQUENCE 파싱 실패 — settings.yaml 값 사용")

    return cfg


# ─────────────────────────────────────────────
# 앱 팩토리
# ─────────────────────────────────────────────

def create_app() -> FastAPI:
    cfg = load_settings()

    # ── 환경 변수 플래그 ─────────────────────────────────────────────
    disable_stt      = os.getenv("DISABLE_STT",      "0") == "1"
    disable_llm      = os.getenv("DISABLE_LLM",      "0") == "1"
    disable_tts      = os.getenv("DISABLE_TTS",      "0") == "1"
    disable_db       = os.getenv("DISABLE_DB",       "0") == "1"
    disable_cam      = os.getenv("DISABLE_CAM",      "0") == "1"   # v0.8 신규
    disable_smartgate= os.getenv("DISABLE_SMARTGATE","0") == "1"   # v0.9 신규

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

    # ── DB 로거 (SR-3.1 / SR-3.2) ────────────────────────────────────
    db_logger: DBLogger | None = None
    if not disable_db and cfg.get("database", {}).get("enabled", False):
        db_logger = DBLogger(cfg["database"])
        logger.info(f"DBLogger 생성: {cfg['database']['host']}:{cfg['database']['port']}/{cfg['database']['db']}")
    else:
        logger.warning("DB 비활성화 → 이벤트 로그 저장 안 함")

    # ── 명령 라우터 (LLM + DB 주입) ──────────────────────────────────
    command_router = CommandRouter(
        tcp_server=tcp_server,
        settings=cfg,
        llm_engine=llm_engine,
        db_logger=db_logger,
    )

    # ── STT 엔진 ────────────────────────────────────────────────────
    stt_engine: STTEngine | None = None
    if not disable_stt:
        _stt = cfg.get("stt", {})
        stt_engine = STTEngine(
            on_result              = _make_stt_callback(command_router, ws_hub, tts_engine, db_logger),
            on_wake                = _make_wake_callback(ws_hub, tts_engine),
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

    # ── v0.8: 카메라 스트림 + FrameAnalyzer ──────────────────────────
    frame_analyzer = None
    if not disable_cam:
        try:
            from server.frame_analyzer import FrameAnalyzer
            frame_analyzer = FrameAnalyzer()
            logger.info("FrameAnalyzer 생성 완료")
        except ImportError as e:
            logger.warning(f"[CAM] FrameAnalyzer import 실패 (패키지 미설치): {e}")
            logger.warning("[CAM] pip install opencv-python-headless ultralytics insightface onnxruntime")
            disable_cam = True
    else:
        logger.warning("카메라 비활성화 (DISABLE_CAM=1)")

    # ── v0.9: SmartGate 2FA Manager ──────────────────────────────────
    smartgate_manager = None
    if not disable_smartgate:
        try:
            from server.smartgate import SmartGateManager
            _sg_cfg = cfg.get("smartgate", {})
            if _sg_cfg.get("enabled", True):
                smartgate_manager = SmartGateManager(
                    settings=cfg,
                    tcp_server=tcp_server,
                    ws_broadcast_fn=ws_hub.broadcast,
                    tts_fn=tts_engine.speak if tts_engine else None,
                    db_logger=db_logger,
                )
                logger.info("[SmartGate] SmartGateManager 생성 완료")
            else:
                logger.warning("[SmartGate] settings.yaml smartgate.enabled=false — 비활성화")
        except ImportError as e:
            logger.warning(f"[SmartGate] import 실패 (서브패키지 미설치): {e}")
            disable_smartgate = True
        except Exception as e:
            logger.warning(f"[SmartGate] 초기화 실패: {e}")
            disable_smartgate = True
    else:
        logger.warning("SmartGate 비활성화 (DISABLE_SMARTGATE=1)")

    # ════════════════════════════════════════════
    # 2. 상호 의존성 연결
    # ════════════════════════════════════════════

    tcp_server.ws_broadcast = ws_hub.broadcast
    tcp_server.db_logger = db_logger
    ws_hub._on_message = command_router.handle
    ws_hub.db_logger = db_logger

    # ════════════════════════════════════════════
    # 3. lifespan
    # ════════════════════════════════════════════

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _print_banner(cfg, stt_engine, llm_engine, tts_engine, db_logger,
                      disable_cam, disable_smartgate, smartgate_manager)

        # TCP 서버 시작
        await tcp_server.start()

        # DB 초기화 (SR-3.1)
        if db_logger:
            db_ok = await db_logger.initialize()
            if db_ok:
                db_logger.log("server_event", "main", "서버 시작",
                              detail={"action": "startup", "version": "0.7"})
            else:
                logger.warning("[DB] 초기화 실패 — 이벤트 로그 비활성화")

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

        # ── v0.8: 카메라 스트림 시작 ────────────────────────────────
        analysis_task: asyncio.Task | None = None
        if not disable_cam and frame_analyzer is not None:
            import server.camera_stream as cam_mod

            # ESP-CAM 설정 (settings.yaml camera 블록 있으면 사용)
            _cam_cfg    = cfg.get("camera", {})
            _udp_port   = _cam_cfg.get("udp_port",   5005)
            # 항상 멀티파트 모드 사용 (헤더+조각 조립)
            _multipart  = _cam_cfg.get("multipart",  True)
            _analyze_n  = _cam_cfg.get("analyze_every", 10)

            cam_mod.UDP_PORT       = _udp_port
            cam_mod.ANALYZE_EVERY  = _analyze_n

            # ── v1.0: UDP IP 화이트리스트 주입 (MEDIUM-5) ──────────────
            _allowed_ips = cam_mod.load_allowed_cam_ips(_cam_cfg)
            cam_mod.set_allowed_cam_ips(_allowed_ips)

            # UDP 수신 스레드 시작
            cam_mod.start(multipart=_multipart)
            logger.info(
                f"[CAM] UDP 수신 시작 — port={_udp_port} "
                f"multipart={_multipart} analyze_every={_analyze_n}"
            )

            # FrameAnalyzer 모델 로드
            await frame_analyzer.load()
            logger.info("[CAM] FrameAnalyzer 모델 로드 완료")

            # face_db 라우터에 analyzer 주입
            try:
                from server import face_db as face_db_mod
                face_db_mod.set_analyzer(frame_analyzer)
                logger.info("[CAM] face_db analyzer 주입 완료")
            except ImportError:
                logger.warning("[CAM] face_db 모듈 없음 — 얼굴 DB API 비활성화")

            # TTS speak 래퍼 (tts_engine 없으면 None)
            async def _tts_speak(text: str):
                if tts_engine:
                    await tts_engine.speak(text)

            # ── v1.0: 보안모드 콜백을 analysis_loop 시작 전에 등록 ──────
            # 수정 이유: 기존 코드는 SmartGate 이후에 콜백 등록 → analysis_loop가
            # 콜백 없이 먼저 시작되는 순서 버그. analysis_loop 직전으로 이동.
            def _get_security_mode() -> str:
                """현재 PIR 보안모드 반환 (camera_stream 콜백용)"""
                pir = command_router._current_pir_mode
                # "dnd_mode" → "dnd", "away_mode" → "away", None → "off"
                if pir is None:
                    return "off"
                mode = pir.replace("_mode", "")
                logger.debug(f"[SecurityMode] _get_security_mode() → {mode} (raw={pir})")
                return mode

            cam_mod.set_security_mode_fn(_get_security_mode)
            logger.info("[CAM] 보안모드 콜백 연동 완료 (command_router → camera_stream)")

            # 분석 루프 asyncio task
            analysis_task = asyncio.create_task(
                cam_mod.analysis_loop(
                    ws_broadcast_fn=ws_hub.broadcast,
                    tts_fn=_tts_speak,
                )
            )
            logger.info("[CAM] analysis_loop task 시작")

        # ── v0.9: SmartGate 시작 ─────────────────────────────────────
        if smartgate_manager is not None:
            import server.camera_stream as _cam_ref
            _cam_ref.set_smartgate_manager(smartgate_manager)
            await smartgate_manager.start()
            logger.info("[SmartGate] SmartGateManager 시작 완료")

            # v1.1: frame_analyzer에 face_auth 주입 (얼굴 DB 통합)
            # → encodings.pkl 포맷 충돌 해소, 인식 결과 일관성 보장
            if frame_analyzer is not None:
                frame_analyzer.set_face_auth(smartgate_manager.face_auth)
                logger.info("[CAM] frame_analyzer ← SmartGate face_auth 연동 완료")

        logger.info("=" * 50)
        logger.info("서버 준비 완료 - 요청 대기 중")
        logger.info("=" * 50)

        yield  # ── 앱 실행 중 ──

        # ── 종료 처리 ───────────────────────────────────────────────
        logger.info("종료 신호 수신 - 정리 중...")

        if db_logger and db_logger.enabled:
            db_logger.log("server_event", "main", "서버 종료",
                          detail={"action": "shutdown"})
            await asyncio.sleep(0.3)  # fire-and-forget INSERT 완료 대기

        if stt_engine:
            await stt_engine.stop()
        if stt_task:
            stt_task.cancel()
        if poll_task:
            poll_task.cancel()

        # v0.9: SmartGate 종료
        if smartgate_manager is not None:
            await smartgate_manager.stop()
            logger.info("[SmartGate] SmartGateManager 종료 완료")

        # v0.8: 카메라 종료
        if not disable_cam and analysis_task:
            analysis_task.cancel()
            import server.camera_stream as cam_mod
            cam_mod.stop()
            logger.info("[CAM] 카메라 스트림 종료")

        if llm_engine:
            await llm_engine.close()

        await tcp_server.stop()

        if db_logger:
            await db_logger.close()

        logger.info("Voice IoT Controller 종료 완료")

    # ════════════════════════════════════════════
    # 4. FastAPI 앱 생성
    # ════════════════════════════════════════════

    app = FastAPI(
        title="Voice IoT Controller",
        description="음성 명령 기반 ESP32 IoT 디바이스 제어 시스템",
        version="0.8.0",
        lifespan=lifespan,
    )

    # 정적 파일 마운트
    static_dir = Path(__file__).parent.parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 기존 라우터 등록
    api_router = create_router(
        tcp_server=tcp_server,
        ws_hub=ws_hub,
        command_router=command_router,
        db_logger=db_logger,
        smartgate_manager=smartgate_manager,   # v0.9 추가
    )
    app.include_router(api_router)

    # ── v0.8: face_db 라우터 등록 ───────────────────────────────────
    if not disable_cam:
        try:
            from server.face_db import router as face_db_router
            app.include_router(face_db_router)
            logger.info("[CAM] face_db 라우터 등록 완료 (/face-db/*)")
        except ImportError:
            logger.warning("[CAM] face_db 라우터 등록 실패 — 모듈 없음")

    # ── PIR 이벤트 엔드포인트 (ESP32 → 서버) ────────────────────────
    @app.post("/pir-event")
    async def pir_event(request: Request):
        """
        현관 PIR 이벤트 수신 — 환영 전용 (v1.1)

        SmartGate 2FA 인증 성공 후 쿨다운(120초) 내 PIR 감지 시:
          → 현관 조명 ON + TTS 환영 인사 트리거
        쿨다운 외 또는 환영 완료 시:
          → 단순 로그 기록 (보안 경고 없음)

        ESP32: POST /pir-event {"type":"pir_event","event":"...","detail":"..."}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "msg": "invalid JSON"}, status_code=400)

        event   = body.get("event", "")
        detail  = body.get("detail", "")

        # ── SmartGate 환영 트리거 (인증 후 PIR 감지 시) ──
        if smartgate_manager is not None and smartgate_manager.welcome_pending:
            welcomed = await smartgate_manager.trigger_welcome()
            if welcomed:
                logger.info("[PIR] SmartGate 환영 동작 트리거 완료")
                return JSONResponse({
                    "status": "ok",
                    "msg": "🏠 환영 모드 — 조명 ON + TTS 인사",
                    "welcome": True,
                })

        # ── 환영 대기가 아닌 일반 PIR 감지 — 로그만 기록 ──
        logger.info(f"[PIR] 현관 감지 | event={event} detail={detail}")

        return JSONResponse({"status": "ok", "msg": "현관 PIR 감지 (정상)"})

    # ── v0.8: 카메라 엔드포인트 ─────────────────────────────────────
    @app.get("/camera/entrance/stream")
    async def camera_entrance_stream():
        """
        현관 ESP32-CAM MJPEG HTTP 스트림 (오버레이 포함, 분석용)
        웹앱: <img src="/camera/entrance/stream">
        """
        if disable_cam:
            return JSONResponse(
                {"status": "error", "msg": "카메라 비활성화 (DISABLE_CAM=1)"},
                status_code=503,
            )
        try:
            from server.camera_stream import mjpeg_generator
        except ImportError:
            return JSONResponse(
                {"status": "error", "msg": "camera_stream 모듈 없음"},
                status_code=503,
            )
        return StreamingResponse(
            mjpeg_generator(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control":    "no-cache, no-store, must-revalidate",
                "Pragma":           "no-cache",
                "X-Accel-Buffering":"no",   # nginx reverse proxy 버퍼링 비활성화
            },
        )

    @app.get("/camera/entrance/raw")
    async def camera_entrance_raw():
        """
        현관 ESP32-CAM Raw MJPEG 스트림 (지연 최소)
        디코드/오버레이/재인코딩 없이 원본 JPEG 전송
        """
        if disable_cam:
            return JSONResponse(
                {"status": "error", "msg": "카메라 비활성화 (DISABLE_CAM=1)"},
                status_code=503,
            )
        try:
            from server.camera_stream import mjpeg_raw_generator
        except ImportError:
            return JSONResponse(
                {"status": "error", "msg": "camera_stream 모듈 없음"},
                status_code=503,
            )
        return StreamingResponse(
            mjpeg_raw_generator(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control":    "no-cache, no-store, must-revalidate",
                "Pragma":           "no-cache",
                "X-Accel-Buffering":"no",
            },
        )

    @app.get("/camera/entrance/snapshot")
    async def camera_entrance_snapshot():
        """
        현관 ESP32-CAM 현재 프레임 스냅샷 1장 반환 (JPEG)
        알람 모달 팝업 시 사용
        """
        if disable_cam:
            return JSONResponse(
                {"status": "error", "msg": "카메라 비활성화"},
                status_code=503,
            )
        try:
            from server.camera_stream import get_latest_jpeg
        except ImportError:
            return JSONResponse(
                {"status": "error", "msg": "camera_stream 모듈 없음"},
                status_code=503,
            )
        jpeg = get_latest_jpeg()
        if jpeg is None:
            return JSONResponse(
                {"status": "error", "msg": "프레임 없음 — ESP32-CAM 연결 확인"},
                status_code=503,
            )
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/camera/entrance/status")
    async def camera_entrance_status():
        """카메라 + 분석기 현재 상태 조회"""
        if disable_cam:
            return JSONResponse({"active": False, "reason": "DISABLE_CAM=1"})
        try:
            from server.camera_stream import get_latest_jpeg, _last_verdict
            has_frame = get_latest_jpeg() is not None
            return JSONResponse({
                "active":    has_frame,
                "verdict":   _last_verdict.get("label", "clear"),
                "name":      _last_verdict.get("name"),
                "confidence":_last_verdict.get("confidence", 0.0),
                "timestamp": _last_verdict.get("timestamp", 0.0),
            })
        except ImportError:
            return JSONResponse({"active": False, "reason": "모듈 없음"})

    # ── 인스턴스 바인딩 (디버그 / 테스트용) ──────────────────────────
    app.state.tcp_server       = tcp_server
    app.state.ws_hub           = ws_hub
    app.state.command_router   = command_router
    app.state.llm_engine       = llm_engine
    app.state.stt_engine       = stt_engine
    app.state.tts_engine       = tts_engine
    app.state.frame_analyzer   = frame_analyzer      # v0.8 신규
    app.state.smartgate_manager= smartgate_manager   # v0.9 신규
    app.state.db_logger        = db_logger
    app.state.settings         = cfg

    return app


# ─────────────────────────────────────────────
# STT 콜백 팩토리
# ─────────────────────────────────────────────

def _make_stt_callback(
    command_router: CommandRouter,
    ws_hub: WebSocketHub,
    tts_engine: TTSEngine | None,
    db_logger: DBLogger | None = None,
):
    """
    STTEngine.on_result 콜백 생성
    음성 인식 텍스트 → CommandRouter → ESP32
    LLM 응답의 tts_response → TTSEngine.speak() (비동기, 논블로킹)
    """
    async def on_stt_result(text: str):
        import time

        # TTS 재생 중 마이크 수음 방지 — 루프 차단
        if tts_engine and tts_engine.is_speaking:
            logger.info(f"[STT] TTS 재생 중 — 입력 무시: '{text[:30]}'")
            return

        logger.info(f"[Pipeline] STT → '{text}'")

        # DB 로그: 음성 인식 결과 (SR-3.1)
        if db_logger and db_logger.enabled:
            db_logger.log("voice_input", "stt_engine", f"STT 인식: '{text}'",
                          detail={"text": text, "source": "stt_engine"})

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


def _make_wake_callback(ws_hub: WebSocketHub, tts_engine: TTSEngine | None = None):
    """웨이크 워드 감지 콜백 — TTS 재생 중이면 즉시 중지"""
    async def on_wake():
        if tts_engine and tts_engine.is_speaking:
            tts_engine.stop()
            logger.info("[Pipeline] 웨이크 워드 감지 → TTS 재생 중지")
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

def _print_banner(cfg: dict, stt_engine, llm_engine, tts_engine, db_logger=None,
                  disable_cam: bool = False, disable_smartgate: bool = False,
                  smartgate_manager=None):
    _stt = cfg.get("stt", {})
    _tts = cfg.get("tts", {})
    _db  = cfg.get("database", {})
    _cam = cfg.get("camera", {})
    w = 52
    logger.info("=" * w)
    logger.info(" Voice IoT Controller  v0.8")
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
    logger.info(f"  DB   : {'✅ MySQL ' + _db.get('host','') + '/' + _db.get('db','') if db_logger else '⛔ 비활성화'}")
    # v0.8 카메라 배너
    if not disable_cam:
        _udp_port = _cam.get("udp_port", 5005)
        logger.info(f"  CAM  : ✅ ESP32-CAM UDP:{_udp_port} (InsightFace + YOLOv8)")
    else:
        logger.info(f"  CAM  : ⛔ 비활성화 (DISABLE_CAM=1)")
    # v0.9 SmartGate 배너
    if not disable_smartgate and smartgate_manager is not None:
        _sg     = cfg.get("smartgate", {})
        _gc     = _sg.get("gesture_auth", {})
        _mode   = _gc.get("mode", "number")
        _seq    = _gc.get("sequence", [])
        _lv_cfg = _sg.get("liveness", {})
        _profile= _lv_cfg.get("active_profile", "laptop")
        logger.info(f"  GATE : ✅ SmartGate 2FA | {_mode} {_seq}")
        logger.info(f"         Liveness profile: [{_profile}]")
    else:
        logger.info(f"  GATE : ⛔ SmartGate 비활성화")
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
