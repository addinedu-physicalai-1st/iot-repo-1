"""
SmartGate Face Authentication Module
InsightFace (buffalo_l) 기반 얼굴 등록/매칭

face_db 디렉토리 구조:
    face_db/
    ├── encodings.pkl        ← 인코딩 캐시 (자동 생성/갱신)
    └── known/
        ├── stephen/         ← 서브디렉토리명 = 사용자 이름
        │   ├── 001.jpg
        │   └── 002.jpg
        └── hong/
            └── 001.jpg

InsightFace 특징:
  - ArcFace 512d 임베딩 → face_recognition보다 정확도 높음
  - buffalo_sc (경량) / buffalo_l (고정밀) 선택 가능
  - onnxruntime CPU 추론 지원
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


class FaceAuthenticator:
    """
    InsightFace 기반 얼굴 인증기

    - face_db_dir  : face_db 루트 경로
    - known_dir    : face_db/known/ (사용자별 서브디렉토리)
    - cache_path   : face_db/encodings.pkl
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
        self.cache_path    = self.face_db_dir / "encodings.pkl"
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
        """face_db/known/<n>/ 구조에서 얼굴 임베딩 로드 (캐시 지원)"""
        self.face_db_dir.mkdir(parents=True, exist_ok=True)
        self.known_dir.mkdir(parents=True, exist_ok=True)

        image_files = self._collect_image_files()
        if not image_files:
            print("[FaceAuth] ⚠️  등록된 얼굴 없음.")
            print(f"           경로: {self.known_dir}/<사용자명>/001.jpg ...")
            return

        # ── 캐시 유효성 검사 ──────────────────────
        if self.cache_path.exists():
            cache_mtime  = self.cache_path.stat().st_mtime
            images_mtime = max(f.stat().st_mtime for f in image_files)
            if cache_mtime > images_mtime:
                with open(self.cache_path, "rb") as f:
                    cache = pickle.load(f)

                # ── 포맷 감지: face_auth dict 포맷 vs frame_analyzer list 포맷 ──
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
                    print(f"[FaceAuth] ⚠️ frame_analyzer 포맷 캐시 변환 완료")
                else:
                    print(f"[FaceAuth] ⚠️ 알 수 없는 캐시 포맷 → 재임베딩")
                    # 캐시 삭제 후 아래에서 새로 생성
                    self.cache_path.unlink(missing_ok=True)

                if self.known_embeddings:
                    unique = list(dict.fromkeys(self.known_names))
                    print(f"[FaceAuth] 캐시 로드 완료 | 사용자: {unique} | {len(self.known_embeddings)}장")
                    return
                else:
                    print(f"[FaceAuth] ⚠️ 캐시에 임베딩 없음 → 재임베딩")
                    self.cache_path.unlink(missing_ok=True)

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

        # ── 캐시 저장 ─────────────────────────────
        with open(self.cache_path, "wb") as f:
            pickle.dump({"embeddings": self.known_embeddings, "names": self.known_names}, f)

        unique = list(dict.fromkeys(self.known_names))
        print(f"[FaceAuth] 임베딩 완료 | 사용자: {unique} | 총 {len(self.known_embeddings)}장")

    def reload_faces(self):
        """캐시 삭제 후 재임베딩"""
        if self.cache_path.exists():
            self.cache_path.unlink()
            print("[FaceAuth] encodings.pkl 삭제 → 재임베딩 시작")
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

