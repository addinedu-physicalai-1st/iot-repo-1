"""
frame_analyzer.py — InsightFace 얼굴인식 + YOLOv8 객체 감지 통합 분석기
v1.3 | Voice IoT Controller

v1.3 변경 (HIGH-3):
  - ENCODINGS_CACHE: encodings.pkl → encodings.enc (Fernet 암호화)
  - _load_face_db(): pickle.load → face_store.load_embeddings()
  - _build_face_db(): pickle.dump → face_store.save_embeddings()
  - rebuild_face_db(): enc + pkl 둘 다 삭제

v1.2 변경:
  - bbox 표시 통합: InsightFace 얼굴 bbox 1개만 표시 (YOLO person bbox 제거)
    → 얼굴 미검출 시에만 YOLO person bbox fallback 표시
  - 라벨 형식 간소화: "FACE:stephen 69%" → "stephen 69%", "FACE:UNKNOWN" → "UNKNOWN"

v1.1 변경:
  - set_face_auth() 외부 임베딩 참조, _match_face() 통합

판정 우선순위:
  1. 등록 얼굴 매칭  → known     (조용히 로그)
  2. 사람 + 패키지   → delivery  (약한 알람)
  3. 사람 + 미등록   → intruder  (강한 알람 🚨)
  4. 사람 없음       → clear
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ── HIGH-3: face_store 임포트 ──────────────────────────────────
try:
    from server import face_store
    FACE_STORE_AVAILABLE = True
except ImportError:
    try:
        import face_store
        FACE_STORE_AVAILABLE = True
    except ImportError:
        FACE_STORE_AVAILABLE = False
        logger.warning("[FrameAnalyzer] face_store 모듈 없음 — 평문 pkl fallback 사용")

# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────
FACE_DB_DIR        = Path("face_db/known")          # 등록 얼굴 이미지 폴더
ENCODINGS_CACHE    = Path("face_db/encodings.enc")  # HIGH-3: Fernet 암호화 캐시
ENCODINGS_LEGACY   = Path("face_db/encodings.pkl")  # HIGH-3: 레거시 평문 (삭제 대상)
FACE_THRESHOLD     = 0.45   # InsightFace cosine distance (낮을수록 엄격)
YOLO_CONF          = 0.50   # YOLO 신뢰도 임계값
YOLO_MODEL         = "yolov8n.pt"   # nano 모델 (경량, CPU 충분)

# YOLO COCO 클래스 중 택배 관련 label
DELIVERY_CLASSES   = {"backpack", "handbag", "suitcase", "box"}
# (YOLOv8 기본 모델에 'box'는 없으나 custom 학습 시 추가 가능)
# 실용적 대안: suitcase / handbag 포함

PERSON_CLASS       = "person"


# ──────────────────────────────────────────
# FrameAnalyzer
# ──────────────────────────────────────────
class FrameAnalyzer:
    def __init__(self):
        self._face_app   = None   # InsightFace ArcFaceONNX
        self._yolo       = None   # Ultralytics YOLO
        self._known_db: list[dict] = []   # [{name, embedding}, ...]
        self._loaded     = False
        self._external_face_auth = None   # v1.1: 외부 FaceAuthenticator 참조

    def set_face_auth(self, face_auth):
        """SmartGate FaceAuthenticator 인스턴스 주입 (main.py에서 호출)
        → 자체 얼굴 DB 대신 face_auth의 임베딩을 직접 사용
        → encodings.pkl 포맷 충돌 완전 해소
        """
        self._external_face_auth = face_auth
        logger.info(f"[FrameAnalyzer] 외부 FaceAuth 연동 | "
                    f"사용자: {list(dict.fromkeys(face_auth.known_names))} | "
                    f"{len(face_auth.known_embeddings)}장")

    # ── 초기화 ──────────────────────────────
    async def load(self):
        """InsightFace + YOLO 모델 로드 (비동기 래퍼 — 실제는 동기)"""
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_sync)

    def _load_sync(self):
        """동기 모델 로드"""
        self._load_insightface()
        self._load_yolo()
        self._load_face_db()
        self._loaded = True
        logger.info("[FrameAnalyzer] 모든 모델 로드 완료")

    def _load_insightface(self):
        """InsightFace buffalo_sc 모델 로드"""
        try:
            from insightface.app import FaceAnalysis
            self._face_app = FaceAnalysis(
                name="buffalo_sc",          # 경량 모델 (buffalo_l: 고정확도)
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
            )
            self._face_app.prepare(ctx_id=0, det_size=(640, 640))
            logger.info("[FrameAnalyzer] InsightFace 로드 완료")
        except Exception as e:
            logger.error(f"[FrameAnalyzer] InsightFace 로드 실패: {e}")
            self._face_app = None

    def _load_yolo(self):
        """YOLOv8 모델 로드"""
        try:
            from ultralytics import YOLO
            self._yolo = YOLO(YOLO_MODEL)
            logger.info(f"[FrameAnalyzer] YOLOv8 로드 완료: {YOLO_MODEL}")
        except Exception as e:
            logger.error(f"[FrameAnalyzer] YOLOv8 로드 실패: {e}")
            self._yolo = None

    # ── 얼굴 DB 관리 ─────────────────────────
    def _load_face_db(self):
        """등록 얼굴 인코딩 로드 (암호화 캐시 우선, face_auth.py 포맷 호환)
        HIGH-3: encodings.enc (Fernet) 우선, 없으면 encodings.pkl fallback
        """
        # enc 우선, 없으면 pkl fallback
        cache_file = None
        if ENCODINGS_CACHE.exists():
            cache_file = ENCODINGS_CACHE
        elif ENCODINGS_LEGACY.exists():
            cache_file = ENCODINGS_LEGACY
            logger.warning("[FrameAnalyzer] 평문 pkl 캐시 감지 — face_auth 재시작 시 자동 마이그레이션됩니다")

        if cache_file is not None:
            try:
                # HIGH-3: enc → face_store 복호화, pkl → 재빌드로 전환
                if cache_file.suffix == ".enc" and FACE_STORE_AVAILABLE:
                    raw = face_store.load_embeddings(str(cache_file))
                elif cache_file.suffix == ".pkl":
                    logger.warning("[FrameAnalyzer] 레거시 pkl 캐시 — pickle 직접 로드 차단, 재빌드")
                    self._build_face_db()
                    return
                else:
                    raw = None

                if raw is None:
                    logger.warning("[FrameAnalyzer] 캐시 복호화 실패, 재생성")
                    self._build_face_db()
                    return

                # ── 포맷 감지 & 변환 ──
                # face_auth 포맷: {"embeddings": [...], "names": [...]}
                # frame_analyzer 포맷: [{"name": str, "embedding": np.array}, ...]
                if isinstance(raw, dict) and "embeddings" in raw:
                    embeddings = raw.get("embeddings", raw.get("encodings", []))
                    names = raw.get("names", [])
                    user_embs: dict[str, list] = {}
                    for emb, name in zip(embeddings, names):
                        user_embs.setdefault(name, []).append(emb)
                    self._known_db = []
                    for name, emb_list in user_embs.items():
                        if not emb_list:
                            continue
                        mean_emb = np.mean(emb_list, axis=0)
                        norm = np.linalg.norm(mean_emb)
                        if norm == 0:
                            continue
                        mean_emb = mean_emb / norm
                        self._known_db.append({"name": name, "embedding": mean_emb})
                    unique = [e["name"] for e in self._known_db]
                    logger.info(
                        f"[FrameAnalyzer] 얼굴 DB 캐시 로드 (face_auth 포맷 변환): "
                        f"{unique} | 총 {len(embeddings)}장"
                    )
                elif isinstance(raw, list):
                    self._known_db = raw
                    logger.info(f"[FrameAnalyzer] 얼굴 DB 캐시 로드: {len(self._known_db)}명")
                else:
                    logger.warning("[FrameAnalyzer] 캐시 포맷 인식 불가, 재생성")
                    self._build_face_db()
                return
            except Exception as e:
                logger.warning(f"[FrameAnalyzer] 캐시 로드 실패, 재생성: {e}")

        self._build_face_db()

    def _build_face_db(self):
        """face_db/known/ 폴더에서 얼굴 인코딩 생성 및 캐시 저장"""
        if not FACE_DB_DIR.exists() or self._face_app is None:
            logger.warning("[FrameAnalyzer] 얼굴 DB 폴더 없거나 InsightFace 미로드")
            return

        db = []
        for person_dir in FACE_DB_DIR.iterdir():
            if not person_dir.is_dir():
                continue
            name = person_dir.name
            embeddings = []

            for img_path in person_dir.glob("*.jpg"):
                img = cv2.imread(str(img_path))
                if img is None:
                    continue
                faces = self._face_app.get(img)
                if faces:
                    embeddings.append(faces[0].normed_embedding)

            for img_path in person_dir.glob("*.png"):
                img = cv2.imread(str(img_path))
                if img is None:
                    continue
                faces = self._face_app.get(img)
                if faces:
                    embeddings.append(faces[0].normed_embedding)

            if embeddings:
                # 평균 임베딩 (여러 사진의 대표값)
                mean_emb = np.mean(embeddings, axis=0)
                mean_emb = mean_emb / np.linalg.norm(mean_emb)
                db.append({"name": name, "embedding": mean_emb})
                logger.info(f"[FrameAnalyzer] 등록: {name} ({len(embeddings)}장)")

        self._known_db = db
        ENCODINGS_CACHE.parent.mkdir(parents=True, exist_ok=True)

        # HIGH-3: 암호화 저장 (face_store)
        if FACE_STORE_AVAILABLE:
            face_store.save_embeddings(db, str(ENCODINGS_CACHE))
        else:
            logger.warning("[FrameAnalyzer] face_store 없음 — 캐시 저장 스킵 (매 시작 시 재빌드)")
        logger.info(f"[FrameAnalyzer] 얼굴 DB 생성 완료: {len(db)}명")

    def rebuild_face_db(self):
        """외부 호출용 — 새 얼굴 등록 후 DB 재빌드
        HIGH-3: enc + pkl 둘 다 삭제 후 재빌드
        """
        ENCODINGS_CACHE.unlink(missing_ok=True)
        ENCODINGS_LEGACY.unlink(missing_ok=True)
        self._build_face_db()
        logger.info("[FrameAnalyzer] 얼굴 DB 재빌드 완료")

    # ── 얼굴 매칭 ────────────────────────────
    def _match_face(self, embedding: np.ndarray) -> tuple[str, float]:
        """
        등록 얼굴 DB와 cosine similarity 비교
        v1.1: 외부 face_auth가 주입되면 해당 임베딩을 우선 사용
        Returns: (name or 'unknown', confidence 0~1)
        """
        # ── 외부 face_auth 임베딩 우선 사용 ──
        if self._external_face_auth is not None:
            fa = self._external_face_auth
            if fa.known_embeddings:
                if len(fa.known_embeddings) != len(fa.known_names):
                    logger.error("[FrameAnalyzer] embeddings/names 길이 불일치")
                    return "unknown", 0.0
                known_mat = np.array(fa.known_embeddings)  # (N, 512)
                sims = np.dot(known_mat, embedding)
                best_idx = int(np.argmax(sims))
                best_sim = float(sims[best_idx])
                logger.debug(
                    f"[FrameAnalyzer] 얼굴 매칭: best={fa.known_names[best_idx]} "
                    f"sim={best_sim:.3f} / threshold={fa.tolerance}"
                )
                if best_sim >= fa.tolerance:
                    confidence = best_sim  # 0~1 범위 코사인 유사도
                    return fa.known_names[best_idx], round(confidence, 3)
                return "unknown", 0.0

        # ── 자체 DB fallback ──
        if not self._known_db:
            return "unknown", 0.0

        best_name  = "unknown"
        best_score = -1.0

        for entry in self._known_db:
            score = float(np.dot(embedding, entry["embedding"]))
            if score > best_score:
                best_score = score
                best_name  = entry["name"]

        distance = 1.0 - best_score
        if distance < FACE_THRESHOLD:
            confidence = 1.0 - (distance / FACE_THRESHOLD)
            return best_name, round(confidence, 3)
        return "unknown", 0.0

    # ── 가장 큰 사람 bbox 선택 ──────────────
    @staticmethod
    def _get_largest_person(person_boxes: list[dict]) -> dict | None:
        """
        YOLO person 박스 중 하단 y좌표(y + h)가 가장 큰 1명 반환
        고정 앵글 카메라 기준: 프레임 하단에 가까울수록 카메라와 가까운 인물
        (면적 기준은 하체만 찍힌 가까운 사람이 불리할 수 있어 y+h 기준 사용)
        """
        if not person_boxes:
            return None
        return max(person_boxes, key=lambda d: d["y"] + d["h"])

    # ── YOLO 감지 ────────────────────────────
    def _detect_objects(self, frame_bgr: np.ndarray) -> list[dict]:
        """
        YOLOv8로 객체 감지
        Returns: [{label, confidence, x, y, w, h}, ...]
        """
        if self._yolo is None:
            return []

        results = self._yolo(frame_bgr, conf=YOLO_CONF, verbose=False)
        detections = []

        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                label  = self._yolo.names[cls_id]
                conf   = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                detections.append({
                    "label":      label,
                    "confidence": round(conf, 3),
                    "x": x1, "y": y1,
                    "w": x2 - x1,
                    "h": y2 - y1,
                })

        return detections

    # ── 메인 분석 ────────────────────────────
    def analyze(self, jpeg_bytes: bytes) -> dict:
        """
        JPEG bytes를 받아 verdict dict 반환 (동기, executor에서 실행)

        Returns:
        {
            "label":      "clear" | "known" | "delivery" | "intruder",
            "name":       str | None,
            "confidence": float,
            "bbox":       [{x,y,w,h,label}, ...],
            "detections": [{label,confidence,x,y,w,h}, ...],
        }
        """
        t0 = time.perf_counter()

        # JPEG 디코딩 + 좌우 반전 (camera_stream._decode_frame과 동일)
        arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return self._verdict("clear")
        frame = cv2.flip(frame, 1)  # v1.2: 좌우 반전 (ESP-CAM 미러링 보정)

        bbox_list = []

        # ── STEP 1: YOLO 객체 감지 ──────────
        detections   = self._detect_objects(frame)
        person_boxes = [d for d in detections if d["label"] == PERSON_CLASS]
        pkg_boxes    = [d for d in detections if d["label"] in DELIVERY_CLASSES]

        # 사람 없음 → clear (person 외 객체만 bbox에 표시)
        if not person_boxes:
            for d in detections:
                bbox_list.append({
                    "x": d["x"], "y": d["y"],
                    "w": d["w"], "h": d["h"],
                    "label": f"{d['label']} {d['confidence']:.0%}",
                })
            return self._verdict("clear", bbox=bbox_list, detections=detections)

        # ── 가장 큰 사람 1명만 선택 ─────────
        primary_person = self._get_largest_person(person_boxes)
        px, py, pw, ph = (primary_person["x"], primary_person["y"],
                          primary_person["w"], primary_person["h"])

        # non-person 객체(택배 등)만 bbox에 추가
        for d in detections:
            if d["label"] != PERSON_CLASS:
                bbox_list.append({
                    "x": d["x"], "y": d["y"],
                    "w": d["w"], "h": d["h"],
                    "label": f"{d['label']} {d['confidence']:.0%}",
                })

        # ── STEP 2: InsightFace 얼굴인식 ────
        # 전체 프레임에서 얼굴 감지 (저해상도 ESP-CAM 크롭 시 감지 실패 방지)
        matched_name = None
        best_conf    = 0.0
        face_unknown = False
        face_detected = False    # v1.2: 얼굴 bbox 표시 여부 추적

        if self._face_app is not None:
            faces = self._face_app.get(frame)
            logger.debug(f"[FrameAnalyzer] InsightFace 감지: {len(faces)}개 얼굴 | frame={frame.shape[1]}×{frame.shape[0]}")
            # 가장 큰 얼굴 우선 매칭 (ESP-CAM 저해상도 — 보통 1명)
            faces_sorted = sorted(faces, key=lambda f: (f.bbox[3]-f.bbox[1])*(f.bbox[2]-f.bbox[0]), reverse=True)
            for face in faces_sorted:
                bb = face.bbox.astype(int)
                fx1, fy1, fx2, fy2 = bb[0], bb[1], bb[2], bb[3]

                emb  = face.normed_embedding
                name, conf = self._match_face(emb)

                bbox_list.append({
                    "x": fx1, "y": fy1,
                    "w": fx2 - fx1, "h": fy2 - fy1,
                    "label": f"{name} {conf:.0%}" if name != "unknown"
                             else "UNKNOWN",
                })
                face_detected = True

                if name != "unknown" and conf > best_conf:
                    best_conf    = conf
                    matched_name = name
                elif name == "unknown":
                    face_unknown = True

        # v1.2: 얼굴 미검출 시에만 YOLO person bbox를 fallback으로 표시
        if not face_detected:
            bbox_list.append({
                "x": px, "y": py, "w": pw, "h": ph,
                "label": f"person {primary_person['confidence']:.0%}",
            })

        elapsed = time.perf_counter() - t0
        logger.debug(f"[FrameAnalyzer] 분석 {elapsed*1000:.0f}ms | "
                     f"persons={len(person_boxes)} primary=({pw}×{ph}) "
                     f"faces_matched={matched_name}")

        # ── STEP 3: 판정 ───────────────────
        # 우선순위: known > delivery > intruder
        if matched_name:
            return self._verdict(
                "known",
                name=matched_name,
                confidence=best_conf,
                bbox=bbox_list,
                detections=detections,
            )

        if pkg_boxes and not face_unknown:
            # 패키지 있고 사람 얼굴 미감지 → 택배
            return self._verdict(
                "delivery",
                confidence=max(d["confidence"] for d in pkg_boxes),
                bbox=bbox_list,
                detections=detections,
            )

        if face_unknown or (primary_person and not matched_name):
            # 미등록 인물 (primary person 기준)
            return self._verdict(
                "intruder",
                confidence=primary_person["confidence"],
                bbox=bbox_list,
                detections=detections,
            )

        return self._verdict("clear", bbox=bbox_list, detections=detections)

    @staticmethod
    def _verdict(
        label: str,
        name: Optional[str]  = None,
        confidence: float    = 0.0,
        bbox: list           = None,
        detections: list     = None,
    ) -> dict:
        return {
            "label":      label,
            "name":       name,
            "confidence": confidence,
            "bbox":       bbox or [],
            "detections": detections or [],
        }
