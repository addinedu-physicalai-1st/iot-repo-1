"""
server/api_routes.py
====================
FastAPI REST 엔드포인트 + WebSocket 라우터

엔드포인트:
  GET  /            - Web App index.html 서빙
  GET  /devices     - 연결된 ESP32 목록
  POST /command     - 수동 명령 직접 전송
  POST /voice       - STT 텍스트 → LLM → 명령 실행
  GET  /ws          - WebSocket 연결 (브라우저 실시간)

의존성 주입:
  TCPServer, WebSocketHub, CommandRouter 인스턴스를
  main.py 에서 생성 후 setup_routes() 로 주입
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, Query, Request, WebSocket, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, Response
from pydantic import BaseModel

from protocol.schema import validate_command, ws_cmd_result

logger = logging.getLogger(__name__)

# Web App HTML 경로 (프로젝트 루트 기준)
WEB_DIR = Path(__file__).parent.parent / "web"


# ─────────────────────────────────────────────
# Request / Response 모델
# ─────────────────────────────────────────────

class CommandRequest(BaseModel):
    """POST /command 요청 바디"""
    device_id: str
    cmd: str                          # led / servo / query / seg7
    pin: Optional[int] = None
    state: Optional[str] = None       # led: on / off
    angle: Optional[int] = None       # servo: 0~180
    sensor: Optional[str] = None      # query: dht22
    pin_clk: Optional[int] = None     # seg7
    pin_dio: Optional[int] = None     # seg7
    mode: Optional[str] = None        # seg7: temp / humidity / number / off
    value: Optional[float] = None     # seg7


class VoiceRequest(BaseModel):
    """POST /voice 요청 바디"""
    text: str                         # STT 변환된 텍스트


class MicDeviceRequest(BaseModel):
    """POST /stt/mic-device 요청 바디"""
    index: int


class TranscribeAudioRequest(BaseModel):
    """POST /stt/transcribe-audio 요청 바디 (브라우저 마이크용)"""
    audio: str          # base64 인코딩된 PCM int16 (mono)
    sample_rate: int = 16000


class CommandResponse(BaseModel):
    """공통 응답"""
    status: str                       # ok / fail / unknown
    msg: str


# ─────────────────────────────────────────────
# 라우터 팩토리
# ─────────────────────────────────────────────

def create_router(tcp_server, ws_hub, command_router, db_logger=None, smartgate_manager=None) -> APIRouter:
    """
    APIRouter 생성 및 엔드포인트 등록

    Parameters
    ----------
    tcp_server          : TCPServer 인스턴스
    ws_hub              : WebSocketHub 인스턴스
    command_router      : CommandRouter 인스턴스
    db_logger           : DBLogger 인스턴스 (선택)
    smartgate_manager   : SmartGateManager 인스턴스 (선택, v1.5)
    """
    router = APIRouter()

    # ── GET /favicon.ico ─────────────────────────────────────────────
    @router.get("/favicon.ico")
    async def favicon():
        """favicon 없음 — 404 방지"""
        return Response(status_code=204)

    # ── GET / ───────────────────────────────────────────────────────
    @router.get("/", response_class=HTMLResponse)
    async def serve_index():
        """첫 페이지: web/index_dashboard.html 서빙"""
        index_path = WEB_DIR / "index_dashboard.html"
        if not index_path.exists():
            return HTMLResponse(
                content="<h2>대시보드 준비 중입니다. (web/index_dashboard.html 없음)</h2>",
                status_code=200,
            )
        return FileResponse(str(index_path))

    # ── GET /devices ─────────────────────────────────────────────────
    @router.get("/devices")
    async def get_devices():
        """연결된 ESP32 디바이스 목록 반환"""
        devices = tcp_server.get_device_list()
        return {
            "count": len(devices),
            "devices": devices,
            "ws_clients": ws_hub.connected_count,
        }

    # ── POST /command ────────────────────────────────────────────────
    @router.post("/command", response_model=CommandResponse)
    async def post_command(req: CommandRequest):
        """
        수동 명령 직접 전송
        예: {"device_id": "esp32_bedroom", "cmd": "led", "pin": 2, "state": "on"}
        """
        data = req.model_dump(exclude_none=True)
        logger.info(f"[API] POST /command: {data}")

        # 유효성 검사
        ok, err = validate_command(data)
        if not ok:
            raise HTTPException(status_code=422, detail=err)

        result_json = await command_router.execute(data)

        import json
        result = json.loads(result_json)
        return CommandResponse(status=result["status"], msg=result["msg"])

    # ── POST /voice ──────────────────────────────────────────────────
    @router.post("/voice", response_model=CommandResponse)
    async def post_voice(req: VoiceRequest):
        """
        STT 텍스트 → LLM 파싱 → ESP32 명령 실행
        예: {"text": "침실 불 켜줘"}
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="빈 텍스트")

        logger.info(f"[API] POST /voice: '{text}'")

        result_json = await command_router.handle(
            client_id="rest_api",
            data={"type": "voice_text", "text": text},
        )

        import json
        result = json.loads(result_json)
        return CommandResponse(status=result["status"], msg=result["msg"])

    # ── GET /ws ──────────────────────────────────────────────────────
    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """브라우저 WebSocket 연결 진입점"""
        await ws_hub.connect(websocket)

    # ── GET /status ──────────────────────────────────────────────────
    @router.get("/status")
    async def get_status():
        """서버 상태 요약"""
        return {
            "server": "running",
            "tcp_clients": tcp_server.connected_count,
            "ws_clients":  ws_hub.connected_count,
            "devices":     [d["device_id"] for d in tcp_server.get_device_list()],
            "stt_state":   _get_stt_state(),
        }

    # ── POST /stt/activate ───────────────────────────────────────────
    @router.post("/stt/activate", response_model=CommandResponse)
    async def stt_activate():
        """
        버튼 모드 트리거 - STTEngine 을 LISTENING 상태로 즉시 전환.
        Web App 마이크 버튼 / PyQt6 버튼 / 외부 REST 호출 모두 지원.

        웨이크 워드 모드와 공존:
          버튼 클릭 → 즉시 LISTENING
          "헤이 IoT" 발화 → 자동 LISTENING
        """
        import sys
        stt = None
        for mod in sys.modules.values():
            app = getattr(mod, 'app', None)
            if app and hasattr(getattr(app, 'state', None), 'stt_engine'):
                stt = app.state.stt_engine
                break

        if stt is None:
            return CommandResponse(
                status="warn",
                msg="STTEngine 비활성화 상태 (DISABLE_STT=1)"
            )
        if stt.state != "IDLE":
            return CommandResponse(
                status="ok",
                msg=f"STT 이미 활성화 중: {stt.state}"
            )

        stt.activate()
        await ws_hub.broadcast(
            '{"type":"wake_detected","msg":"버튼 트리거 → 명령을 말씀하세요"}'
        )
        return CommandResponse(status="ok", msg="STT LISTENING 활성화")

    def _get_stt_state():
        import sys
        for mod in sys.modules.values():
            app = getattr(mod, 'app', None)
            if app and hasattr(getattr(app, 'state', None), 'stt_engine'):
                stt = app.state.stt_engine
                return getattr(stt, 'state', 'N/A') if stt else 'DISABLED'
        return 'N/A'

    def _get_stt_engine():
        import sys
        for mod in sys.modules.values():
            app = getattr(mod, 'app', None)
            if app and hasattr(getattr(app, 'state', None), 'stt_engine'):
                return app.state.stt_engine
        return None

    # ── GET /stt/devices ─────────────────────────────────────────
    @router.get("/stt/devices")
    async def stt_devices(request: Request):
        """서버에서 인식 가능한 마이크(입력 장치) 목록 반환"""
        import sounddevice as sd

        # 마이크 목록 조회 (STT 엔진과 무관하게 항상 시도)
        try:
            devices = sd.query_devices()
        except Exception as e:
            return {"devices": [], "current": None, "error": f"sounddevice: {e}"}

        def _val(d, key, default=0):
            try:
                return d.get(key, default) if hasattr(d, "get") else getattr(d, key, default)
            except Exception:
                return default

        mic_list = []
        for i, d in enumerate(devices):
            max_in = _val(d, "max_input_channels", 0)
            if max_in <= 0:
                continue
            name = _val(d, "name", f"Device {i}")
            sr = _val(d, "default_samplerate", 0)
            mic_list.append({"index": i, "name": str(name), "channels": int(max_in), "sample_rate": int(sr)})

        # 현재 STT 마이크 인덱스 (실패해도 devices는 반환)
        current_idx = None
        try:
            stt = getattr(request.app.state, "stt_engine", None)
            if stt is not None:
                current_idx = getattr(stt, "mic_device", None)
        except Exception:
            pass

        return {"devices": mic_list, "current": current_idx}

    # ── POST /stt/mic-device ──────────────────────────────────────
    # @router.post("/stt/mic-device", response_model=CommandResponse)
    # async def stt_set_mic_device(req: MicDeviceRequest):
    #     """마이크 장치 변경 (런타임 교체)"""
    #     stt = _get_stt_engine()
    #     if stt is None:
    #         return CommandResponse(status="warn", msg="STTEngine 비활성화 상태 (DISABLE_STT=1)")

    #     ok = await stt.set_mic_device(req.index)
    #     if ok:
    #         logger.info(f"[API] 마이크 변경 완료: device={req.index}")
    #         return CommandResponse(status="ok", msg=f"마이크 변경 완료: device={req.index}")
    #     else:
    #         return CommandResponse(status="fail", msg=f"마이크 변경 실패: device={req.index}")

    @router.post("/stt/mic-device", response_model=CommandResponse)
    async def stt_set_mic_device(request: Request, req: MicDeviceRequest):
        """마이크 장치 변경 (런타임 교체)"""
        stt = getattr(request.app.state, "stt_engine", None)
        if stt is None:
            return CommandResponse(status="warn", msg="STTEngine 비활성화 상태 (DISABLE_STT=1)")

        ok = await stt.set_mic_device(req.index)
        if ok:
            # 아직 STT가 안 돌아가고 있다면 여기서 시작
            if not getattr(stt, "_running", False):
                import asyncio
                asyncio.create_task(stt.run_with_retry())
            logger.info(f"[API] 마이크 변경 완료: device={req.index}")
            return CommandResponse(status="ok", msg=f"마이크 변경 완료: device={req.index}")
        else:
            return CommandResponse(status="fail", msg=f"마이크 변경 실패: device={req.index}")

    # ── POST /stt/transcribe-audio (브라우저 마이크 → Whisper) ─────
    @router.post("/stt/transcribe-audio")
    async def stt_transcribe_audio(request: Request, req: TranscribeAudioRequest):
        """브라우저에서 캡처한 오디오 → Whisper 전사 (원격/모바일 접속 시)"""
        stt = getattr(request.app.state, "stt_engine", None)
        if stt is None:
            return {"text": "", "status": "warn", "msg": "STTEngine 비활성화 (DISABLE_STT=1)"}
        try:
            raw = base64.b64decode(req.audio)
            audio = np.frombuffer(raw, dtype=np.int16)
            text = await stt.transcribe_audio(audio, sample_rate=req.sample_rate)
            return {"text": text, "status": "ok"}
        except Exception as e:
            logger.warning(f"[API] transcribe-audio 오류: {e}")
            return {"text": "", "status": "fail", "msg": str(e)}

    # ── SR-3.2: 이벤트 로그 검색/조회 API ─────────────────────────

    @router.get("/logs/search")
    async def logs_search(
        category:  Optional[str] = Query(None, description="이벤트 카테고리"),
        date_from: Optional[str] = Query(None, description="시작일 (YYYY-MM-DD)"),
        date_to:   Optional[str] = Query(None, description="종료일 (YYYY-MM-DD)"),
        device_id: Optional[str] = Query(None, description="디바이스 ID"),
        room:      Optional[str] = Query(None, description="공간"),
        level:     Optional[str] = Query(None, description="로그 레벨"),
        keyword:   Optional[str] = Query(None, description="summary 키워드"),
        limit:     int = Query(100, ge=1, le=500, description="최대 반환 건수"),
        offset:    int = Query(0, ge=0, description="오프셋"),
    ):
        """이벤트 로그 검색 (날짜/카테고리/디바이스 등 필터)"""
        if not db_logger or not db_logger.enabled:
            return {"items": [], "total": 0, "msg": "DB 비활성화 상태"}

        items = await db_logger.search(
            category=category, date_from=date_from, date_to=date_to,
            device_id=device_id, room=room, level=level,
            keyword=keyword, limit=limit, offset=offset,
        )
        total = await db_logger.count(
            category=category, date_from=date_from, date_to=date_to,
            device_id=device_id, room=room, level=level, keyword=keyword,
        )
        return {"items": items, "total": total}

    @router.get("/logs/categories")
    async def logs_categories():
        """사용된 이벤트 카테고리 목록"""
        if not db_logger or not db_logger.enabled:
            return {"categories": []}
        categories = await db_logger.get_categories()
        return {"categories": categories}

    @router.get("/logs/stats")
    async def logs_stats():
        """로그 통계 요약 (대시보드용)"""
        if not db_logger or not db_logger.enabled:
            return {"total": 0, "last_24h": 0, "by_category": {}}
        stats = await db_logger.get_stats()
        return stats

    # ── SR-3.3: 패턴 분석 API ──────────────────────────────

    @router.get("/logs/pattern/hourly")
    async def logs_pattern_hourly(
        date_from: Optional[str] = Query(None),
        date_to:   Optional[str] = Query(None),
        category:  Optional[str] = Query(None),
        device_id: Optional[str] = Query(None),
        day_type:  Optional[str] = Query(None, description="weekday|weekend"),
    ):
        """시간대별 활동 분포 (SR-3.3)"""
        if not db_logger or not db_logger.enabled:
            return {"items": []}
        items = await db_logger.get_hourly_distribution(
            date_from=date_from, date_to=date_to,
            category=category, device_id=device_id, day_type=day_type,
        )
        return {"items": items}

    @router.get("/logs/pattern/daily")
    async def logs_pattern_daily(
        date_from: Optional[str] = Query(None),
        date_to:   Optional[str] = Query(None),
        category:  Optional[str] = Query(None),
        device_id: Optional[str] = Query(None),
    ):
        """일별 이벤트 타임라인 (SR-3.3)"""
        if not db_logger or not db_logger.enabled:
            return {"items": []}
        items = await db_logger.get_daily_timeline(
            date_from=date_from, date_to=date_to,
            category=category, device_id=device_id,
        )
        return {"items": items}

    @router.get("/logs/pattern/categories")
    async def logs_pattern_categories(
        date_from: Optional[str] = Query(None),
        date_to:   Optional[str] = Query(None),
        device_id: Optional[str] = Query(None),
    ):
        """카테고리별 분포 (SR-3.3)"""
        if not db_logger or not db_logger.enabled:
            return {"items": []}
        items = await db_logger.get_category_distribution(
            date_from=date_from, date_to=date_to, device_id=device_id,
        )
        return {"items": items}

    @router.get("/logs/pattern/devices")
    async def logs_pattern_devices(
        date_from: Optional[str] = Query(None),
        date_to:   Optional[str] = Query(None),
        category:  Optional[str] = Query(None),
    ):
        """디바이스별 활동량 (SR-3.3)"""
        if not db_logger or not db_logger.enabled:
            return {"items": []}
        items = await db_logger.get_device_activity(
            date_from=date_from, date_to=date_to, category=category,
        )
        return {"items": items}

    @router.get("/logs/pattern/anomalies")
    async def logs_pattern_anomalies(
        date_from: Optional[str] = Query(None),
        date_to:   Optional[str] = Query(None),
        threshold: float = Query(2.0, ge=1.5, le=5.0),
    ):
        """이상 패턴 탐지 (SR-3.3)"""
        if not db_logger or not db_logger.enabled:
            return {"avg_by_hour": [], "anomalies": []}
        result = await db_logger.get_anomalies(
            date_from=date_from, date_to=date_to, threshold=threshold,
        )
        return result

    @router.get("/logs/{log_id}")
    async def logs_detail(log_id: int):
        """특정 이벤트 로그 상세 조회"""
        if not db_logger or not db_logger.enabled:
            raise HTTPException(status_code=503, detail="DB 비활성화 상태")
        item = await db_logger.get_by_id(log_id)
        if not item:
            raise HTTPException(status_code=404, detail="로그를 찾을 수 없음")

        # 보안 이벤트인 경우 미디어 첨부
        if item.get("event_category") == "security_alert":
            media = await db_logger.get_security_media(log_id)
            item["media"] = media

        return item

    # ── v1.5: SmartGate 엔드포인트 ──────────────────────────────────

    @router.get("/smartgate/status")
    async def smartgate_status():
        """SmartGate 2FA 현재 상태 조회"""
        if smartgate_manager is None:
            return {"enabled": False, "msg": "SmartGate 비활성화 (DISABLE_SMARTGATE=1 또는 미초기화)"}
        return smartgate_manager.status

    @router.post("/smartgate/reload-faces", response_model=CommandResponse)
    async def smartgate_reload_faces():
        """등록 얼굴 DB 재임베딩 트리거"""
        if smartgate_manager is None:
            return CommandResponse(status="warn", msg="SmartGate 비활성화 상태")
        try:
            smartgate_manager.face_auth.reload_faces()
            return CommandResponse(status="ok", msg="얼굴 DB 재임베딩 완료")
        except Exception as e:
            logger.error(f"[SmartGate] reload-faces 오류: {e}")
            return CommandResponse(status="fail", msg=str(e))

    @router.post("/smartgate/arm")
    async def smartgate_arm():
        """SmartGate 인증 시작 (IDLE → ARMED)"""
        if smartgate_manager is None:
            return {"status": "fail", "msg": "SmartGate 비활성화 상태"}
        return await smartgate_manager.arm()

    @router.post("/smartgate/disarm")
    async def smartgate_disarm():
        """SmartGate 인증 취소 (ARMED → IDLE)"""
        if smartgate_manager is None:
            return {"status": "fail", "msg": "SmartGate 비활성화 상태"}
        return await smartgate_manager.disarm()

    # ── v1.9: 얼굴 등록 엔드포인트 ──────────────────────────────────

    @router.post("/smartgate/register-face")
    async def smartgate_register_face(request: Request):
        """현재 카메라 프레임에서 얼굴을 캡처하여 face_db에 저장"""
        if smartgate_manager is None:
            return {"status": "fail", "msg": "SmartGate 비활성화 상태"}

        try:
            body = await request.json()
        except Exception:
            return {"status": "fail", "msg": "invalid JSON"}

        name = (body.get("name") or "").strip().lower()
        if not name:
            return {"status": "fail", "msg": "사용자 이름을 입력하세요"}

        import re
        if not re.match(r'^[\w가-힣]+$', name):
            return {"status": "fail", "msg": "이름은 영문/한글/숫자만 가능합니다"}

        try:
            from server.camera_stream import get_latest_jpeg
            jpeg_bytes = get_latest_jpeg()
        except ImportError:
            return {"status": "fail", "msg": "camera_stream 모듈 없음"}

        if jpeg_bytes is None:
            return {"status": "fail", "msg": "카메라 프레임 없음 — 스트림이 활성 상태인지 확인하세요"}

        import numpy as np
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return {"status": "fail", "msg": "프레임 디코드 실패"}

        faces = smartgate_manager.face_auth._app.get(frame)
        if len(faces) == 0:
            return {"status": "fail", "msg": "얼굴이 감지되지 않았습니다. 카메라를 바라보고 다시 시도하세요"}
        if len(faces) > 1:
            return {"status": "fail", "msg": f"얼굴이 {len(faces)}개 감지됨 — 1명만 촬영하세요"}

        face_db_dir = Path(smartgate_manager.face_auth.face_db_dir)
        user_dir = face_db_dir / "known" / name
        user_dir.mkdir(parents=True, exist_ok=True)

        existing = list(user_dir.glob("*.jpg")) + list(user_dir.glob("*.png"))
        next_idx = len(existing) + 1
        save_path = user_dir / f"{next_idx:03d}.jpg"

        cv2.imwrite(str(save_path), frame)
        logger.info(f"[SmartGate] 📸 얼굴 등록: {name} → {save_path} (총 {next_idx}장)")

        cache_path = face_db_dir / "encodings.pkl"
        if cache_path.exists():
            cache_path.unlink()
            logger.info("[SmartGate] encodings.pkl 초기화 → 다음 reload 시 재임베딩")

        return {
            "status": "ok",
            "msg": f"✅ {name} 얼굴 등록 완료 ({next_idx}장)",
            "name": name,
            "count": next_idx,
            "path": str(save_path),
        }

    @router.get("/smartgate/registered-faces")
    async def smartgate_registered_faces():
        """등록된 얼굴 사용자 목록 + 이미지 수 조회"""
        if smartgate_manager is None:
            return {"status": "fail", "msg": "SmartGate 비활성화 상태"}

        face_db_dir = Path(smartgate_manager.face_auth.face_db_dir)
        known_dir = face_db_dir / "known"

        if not known_dir.exists():
            return {"users": [], "total": 0}

        users = []
        for user_dir in sorted(known_dir.iterdir()):
            if user_dir.is_dir():
                imgs = list(user_dir.glob("*.jpg")) + list(user_dir.glob("*.png"))
                users.append({"name": user_dir.name, "count": len(imgs)})

        return {"users": users, "total": len(users)}

    @router.delete("/smartgate/registered-faces/{name}")
    async def smartgate_delete_face(name: str):
        """특정 사용자의 등록 얼굴 전체 삭제"""
        if smartgate_manager is None:
            return {"status": "fail", "msg": "SmartGate 비활성화 상태"}

        import shutil
        face_db_dir = Path(smartgate_manager.face_auth.face_db_dir)
        user_dir = face_db_dir / "known" / name

        if not user_dir.exists():
            return {"status": "fail", "msg": f"'{name}' 사용자 없음"}

        count = len(list(user_dir.glob("*.jpg")) + list(user_dir.glob("*.png")))
        shutil.rmtree(str(user_dir))
        logger.info(f"[SmartGate] 🗑️ 얼굴 삭제: {name} ({count}장)")

        cache_path = face_db_dir / "encodings.pkl"
        if cache_path.exists():
            cache_path.unlink()

        return {"status": "ok", "msg": f"'{name}' 삭제 완료 ({count}장)", "name": name}

    return router
