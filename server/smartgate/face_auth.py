"""
SmartGate Face Authentication Module
InsightFace (buffalo_sc) 기반 얼굴 등록/매칭

face_db 디렉토리 구조:
    face_db/
    ├── encodings.enc        ← Fernet 암호화 캐시 (HIGH-3, 자동 생성/갱신)
    ├── encodings.pkl        ← 평문 캐시 레거시 (마이그레이션 후 자동 삭제)
    └── known/
        ├── stephen/
        │   ├── 001.jpg
        │   └── 002.jpg
        └── hong/
            └── 001.jpg

InsightFace 특징:
  - ArcFace 512d 임베딩 → face_recognition보다 정확도 높음
  - buffalo_sc (경량/빠름) / buffalo_l (고정밀) 선택 가능
  - onnxruntime CPU 추론 지원

변경 이력:
  v1.0: 최초 작성 (평문 pkl 저장)
  v1.1: HIGH-3 — face_store.save/load_embeddings() 연동 (Fernet 암호화)
        - 캐시 저장: pickle.dump → face_store.save_embeddings()
        - 캐시 로드: pickle.load → face_store.load_embeddings()
        - 마이그레이션: 평문 .pkl 존재 + .enc 없으면 자동 1회 변환
        - reload_faces(): enc + pkl 둘 다 삭제
        - 암호화 실패 시 평문 pkl fallback (서비스 중단 방지)
"""

import pickle
import numpy as np
from pathlib import Path
from typing import Optional, Tuple

try:
    import insightface
    from insightface.app import FaceAnalysis
    INSIGHTFACE_AVAILABLE = True
except ImportError:
    INSIGHTFACE_AVAILABLE = False
    print("[WARNING] insightface 미설치.")
    print("          pip install insightface onnxruntime")

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
        print("[WARNING] face_store 모듈 없음 — 평문 pkl fallback 사용")


class FaceAuthenticator:
    """
    InsightFace 기반 얼굴 인증기

    - face_db_dir  : face_db 루트 경로
    - known_dir    : face_db/known/ (사용자별 서브디렉토리)
    - cache_path   : face_db/encodings.pkl  (레거시 평문 — 마이그레이션 후 삭제)
    - enc_path     : face_db/encodings.enc  (HIGH-3 Fernet 암호화 캐시)
    - tolerance    : 코사인 유사도 임계값 (높을수록 엄격, 권장 0.35~0.50)
    - model_name   : "buffalo_sc" (경량/빠름) | "buffalo_l" (고정밀)
    """

    def __init__(
        self,
        face_db_dir: str = "face_db",
        tolerance: float = 0.40,
        min_face_size: int = 80,
        model_name: str = "buffalo_sc",
    ):
        self.face_db_dir   = Path(face_db_dir)
        self.known_dir     = self.face_db_dir / "known"
        self.cache_path    = self.face_db_dir / "encodings.pkl"   # 레거시
        self.enc_path      = self.face_db_dir / "encodings.enc"   # HIGH-3
        self.tolerance     = tolerance
        self.min_face_size = min_face_size
        self.model_name    = model_name

        self.known_embeddings: list = []   # List[np.ndarray] shape (512,)
        self.known_names: list      = []   # List[str]
        self._app = None

        if INSIGHTFACE_AVAILABLE:
            self._init_model()
            self._load_faces()

    # ──────────────────────────────────────────
    # 모델 초기화
    # ──────────────────────────────────────────
    def _init_model(self):
        """InsightFace FaceAnalysis 모델 로드"""
        print(f"[FaceAuth] InsightFace 모델 로드 중: {self.model_name} ...")
        try:
            self._app = FaceAnalysis(
                name=self.model_name,
                providers=["CPUExecutionProvider"],  # GPU: "CUDAExecutionProvider"
            )
            self._app.prepare(ctx_id=0, det_size=(640, 640))
            print(f"[FaceAuth] ✅ 모델 로드 완료: {self.model_name}")
        except Exception as e:
            print(f"[FaceAuth] ❌ 모델 로드 실패: {e}")
            self._app = None

    # ──────────────────────────────────────────
    # 얼굴 등록 로드
    # ──────────────────────────────────────────
    def _collect_image_files(self) -> list:
        """known/ 하위 모든 사용자 디렉토리 이미지 수집"""
        files = []
        if not self.known_dir.exists():
            return files
        for user_dir in sorted(self.known_dir.iterdir()):
            if user_dir.is_dir():
                for ext in ("*.jpg", "*.jpeg", "*.png"):
                    files.extend(sorted(user_dir.glob(ext)))
        return files

    def _load_faces(self):
        """
        face_db/known/<n>/ 구조에서 얼굴 임베딩 로드 (암호화 캐시 지원)

        우선순위:
          1) encodings.enc (Fernet 암호화) — HIGH-3
          2) encodings.pkl 존재 시 → encodings.enc로 마이그레이션 후 사용
          3) 캐시 없음 → 새로 임베딩 → encodings.enc로 암호화 저장
        """
        self.face_db_dir.mkdir(parents=True, exist_ok=True)
        self.known_dir.mkdir(parents=True, exist_ok=True)

        image_files = self._collect_image_files()
        if not image_files:
            print("[FaceAuth] ⚠️  등록된 얼굴 없음.")
            print(f"           경로: {self.known_dir}/<사용자명>/001.jpg ...")
            return

        # ── HIGH-3: 평문 .pkl 존재 + .enc 없으면 자동 마이그레이션 ──
        if self.cache_path.exists() and not self.enc_path.exists():
            if FACE_STORE_AVAILABLE:
                print("[FaceAuth] 평문 캐시 감지 → Fernet 마이그레이션 실행")
                ok = face_store.migrate_plaintext_db(
                    str(self.cache_path), str(self.enc_path)
                )
                if ok:
                    print("[FaceAuth] ✅ 마이그레이션 완료 → encodings.enc")
                else:
                    print("[FaceAuth] ⚠️ 마이그레이션 실패 — 평문 pkl 유지")
            else:
                print("[FaceAuth] face_store 없음 — 마이그레이션 스킵 (평문 pkl 유지)")

        # ── 유효한 캐시 탐색: enc 우선, 없으면 pkl fallback ──
        cache_file = None
        if self.enc_path.exists():
            cache_file = self.enc_path
        elif self.cache_path.exists():
            cache_file = self.cache_path

        if cache_file is not None:
            cache_mtime  = cache_file.stat().st_mtime
            images_mtime = max(f.stat().st_mtime for f in image_files)

            if cache_mtime > images_mtime:
                cache = self._load_cache(cache_file)
                if cache is not None:
                    loaded = self._parse_cache(cache)
                    if loaded:
                        unique = list(dict.fromkeys(self.known_names))
                        print(f"[FaceAuth] 캐시 로드 완료 | 사용자: {unique} | {len(self.known_embeddings)}장")
                        return
                # 캐시 파싱 실패 → 삭제 후 재임베딩
                cache_file.unlink(missing_ok=True)

        # ── 새로 임베딩 ───────────────────────────
        if self._app is None:
            print("[FaceAuth] ❌ 모델 미초기화 상태로 인코딩 불가")
            return

        print("[FaceAuth] 얼굴 임베딩 중...")
        import cv2
        self.known_embeddings.clear()
        self.known_names.clear()

        for img_path in image_files:
            name  = img_path.parent.name
            img   = cv2.imread(str(img_path))
            if img is None:
                print(f"  ❌ {img_path.name}: 이미지 읽기 실패")
                continue
            faces = self._app.get(img)
            if faces:
                emb = faces[0].normed_embedding  # 정규화된 512d 벡터
                self.known_embeddings.append(emb)
                self.known_names.append(name)
                print(f"  ✅ {name} ← {img_path.name}")
            else:
                print(f"  ❌ {img_path.name}: 얼굴 감지 실패")

        # ── HIGH-3: 암호화 저장 ───────────────────
        self._save_cache()

        unique = list(dict.fromkeys(self.known_names))
        print(f"[FaceAuth] 임베딩 완료 | 사용자: {unique} | 총 {len(self.known_embeddings)}장")

    # ──────────────────────────────────────────
    # 캐시 저장/로드 헬퍼
    # ──────────────────────────────────────────
    def _save_cache(self):
        """
        HIGH-3: face_store.save_embeddings()로 암호화 저장.
        실패 시 평문 pkl fallback (서비스 중단 방지).
        """
        payload = {"embeddings": self.known_embeddings, "names": self.known_names}

        if FACE_STORE_AVAILABLE:
            ok = face_store.save_embeddings(payload, str(self.enc_path))
            if ok:
                # 암호화 성공 시 레거시 평문 pkl 잔존하면 삭제
                if self.cache_path.exists():
                    self.cache_path.unlink(missing_ok=True)
                    print("[FaceAuth] 레거시 encodings.pkl 삭제 완료")
                return
            print("[FaceAuth] ⚠️ 암호화 저장 실패 → 평문 pkl fallback")

        # fallback: 평문 pkl (v1.0 동작 유지)
        try:
            with open(self.cache_path, "wb") as f:
                pickle.dump(payload, f)
            print(f"[FaceAuth] ⚠️ 평문 저장 (fallback): {self.cache_path}")
        except Exception as e:
            print(f"[FaceAuth] ❌ 캐시 저장 실패: {e}")

    def _load_cache(self, cache_file: Path):
        """
        캐시 파일 로드.
        .enc → face_store.load_embeddings()
        .pkl → pickle.load() (레거시 fallback)
        """
        if cache_file.suffix == ".enc" and FACE_STORE_AVAILABLE:
            return face_store.load_embeddings(str(cache_file))
        else:
            try:
                with open(cache_file, "rb") as f:
                    return pickle.load(f)
            except Exception as e:
                print(f"[FaceAuth] 평문 캐시 로드 실패: {e}")
                return None

    def _parse_cache(self, cache) -> bool:
        """
        캐시 포맷 감지 및 파싱.
        face_auth dict 포맷 / frame_analyzer list 포맷 모두 지원.
        Returns True if successfully loaded.
        """
        if isinstance(cache, dict):
            # face_auth 포맷: {"embeddings": [...], "names": [...]}
            self.known_embeddings = cache.get("embeddings", cache.get("encodings", []))
            self.known_names      = cache.get("names", [])
        elif isinstance(cache, list):
            # frame_analyzer 포맷: [{"name": str, "embedding": np.array}, ...]
            self.known_embeddings = []
            self.known_names      = []
            for entry in cache:
                if isinstance(entry, dict) and "embedding" in entry:
                    self.known_embeddings.append(entry["embedding"])
                    self.known_names.append(entry["name"])
            print("[FaceAuth] ⚠️ frame_analyzer 포맷 캐시 변환 완료")
        else:
            print("[FaceAuth] ⚠️ 알 수 없는 캐시 포맷 → 재임베딩")
            return False

        if self.known_embeddings:
            return True

        print("[FaceAuth] ⚠️ 캐시에 임베딩 없음 → 재임베딩")
        return False

    def reload_faces(self):
        """
        캐시 삭제 후 재임베딩.
        HIGH-3: enc + pkl 둘 다 삭제.
        """
        self.enc_path.unlink(missing_ok=True)
        self.cache_path.unlink(missing_ok=True)
        print("[FaceAuth] 캐시 삭제 (enc + pkl) → 재임베딩 시작")
        self._load_faces()

    # ──────────────────────────────────────────
    # 프레임 인증
    # ──────────────────────────────────────────
    def authenticate(self, frame_bgr: np.ndarray) -> Tuple[bool, Optional[str], float]:
        """
        프레임에서 얼굴 인증 (BGR 입력 - OpenCV 기본 포맷)

        Returns:
            (success, name, similarity)
            - success    : 인증 성공 여부
            - name       : 인식된 사용자 이름 (실패 시 None)
            - similarity : 코사인 유사도 (0~1, 높을수록 유사)
        """
        if not INSIGHTFACE_AVAILABLE or self._app is None:
            return False, None, 0.0
        if not self.known_embeddings:
            return False, None, 0.0

        faces = self._app.get(frame_bgr)
        if not faces:
            return False, None, 0.0

        # 최소 크기 필터 + 가장 큰 얼굴 선택
        valid = [f for f in faces if (f.bbox[3] - f.bbox[1]) >= self.min_face_size]
        if not valid:
            return False, None, 0.0
        face = max(valid, key=lambda f: (f.bbox[3] - f.bbox[1]) * (f.bbox[2] - f.bbox[0]))

        emb = face.normed_embedding
        known_mat = np.array(self.known_embeddings)        # (N, 512)
        sims = np.dot(known_mat, emb)                      # 코사인 유사도 (정규화 완료)

        best_idx  = int(np.argmax(sims))
        best_sim  = float(sims[best_idx])

        if best_sim >= self.tolerance:
            return True, self.known_names[best_idx], best_sim

        return False, None, best_sim

    def get_face_boxes(self, frame_bgr: np.ndarray) -> list:
        """얼굴 bounding box 반환 (시각화용) → [(x1,y1,x2,y2), ...]"""
        if not INSIGHTFACE_AVAILABLE or self._app is None:
            return []
        faces = self._app.get(frame_bgr)
        return [tuple(map(int, f.bbox)) for f in faces]

    @property
    def is_ready(self) -> bool:
        return INSIGHTFACE_AVAILABLE and self._app is not None and len(self.known_embeddings) > 0
