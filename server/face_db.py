"""
face_db.py — 등록 얼굴 DB 관리 REST API 라우터
v1.0 | Voice IoT Controller

엔드포인트:
  GET    /face-db/list              등록 인물 목록
  POST   /face-db/register          새 얼굴 사진 등록 (multipart)
  DELETE /face-db/{name}            인물 삭제
  POST   /face-db/rebuild           DB 재빌드 (인코딩 캐시 갱신)
"""

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/face-db", tags=["FaceDB"])

FACE_DB_DIR     = Path("face_db/known")
ENCODINGS_CACHE = Path("face_db/encodings.pkl")
ALLOWED_EXT     = {".jpg", ".jpeg", ".png"}

# FrameAnalyzer 싱글턴 참조 (main.py에서 주입)
_analyzer = None

def set_analyzer(analyzer):
    global _analyzer
    _analyzer = analyzer


# ──────────────────────────────────────────
# 등록 인물 목록
# ──────────────────────────────────────────
@router.get("/list")
async def list_faces():
    """
    등록된 인물 목록 + 각 인물의 사진 수 반환
    """
    if not FACE_DB_DIR.exists():
        return JSONResponse({"persons": [], "total": 0})

    persons = []
    for d in sorted(FACE_DB_DIR.iterdir()):
        if not d.is_dir():
            continue
        photos = [p for p in d.iterdir() if p.suffix.lower() in ALLOWED_EXT]
        persons.append({"name": d.name, "photo_count": len(photos)})

    return JSONResponse({"persons": persons, "total": len(persons)})


# ──────────────────────────────────────────
# 새 얼굴 등록
# ──────────────────────────────────────────
@router.post("/register")
async def register_face(
    name:  str        = Form(..., description="등록할 인물 이름 (영문/한글)"),
    files: list[UploadFile] = File(..., description="얼굴 사진 (jpg/png, 복수 가능)"),
):
    """
    인물 이름 + 사진 파일(들)을 받아 face_db/known/{name}/ 에 저장
    저장 후 자동으로 DB 재빌드
    """
    if not name.strip():
        raise HTTPException(status_code=400, detail="이름을 입력해주세요.")
    if not files:
        raise HTTPException(status_code=400, detail="사진 파일이 없습니다.")

    person_dir = FACE_DB_DIR / name
    person_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for upload in files:
        ext = Path(upload.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            continue
        # 기존 파일 수 기반 순번 부여
        existing = list(person_dir.glob("*"))
        idx      = len(existing) + 1
        save_path = person_dir / f"{name}_{idx:03d}{ext}"
        content   = await upload.read()
        save_path.write_bytes(content)
        saved.append(save_path.name)

    if not saved:
        raise HTTPException(status_code=400, detail="유효한 이미지 파일이 없습니다.")

    # DB 재빌드
    if _analyzer:
        _analyzer.rebuild_face_db()
        rebuild_msg = "DB 재빌드 완료"
    else:
        rebuild_msg = "analyzer 미연결 — 수동 재빌드 필요"

    logger.info(f"[FaceDB] 등록: {name} ({len(saved)}장) | {rebuild_msg}")
    return JSONResponse({
        "success": True,
        "name":    name,
        "saved":   saved,
        "rebuild": rebuild_msg,
    })


# ──────────────────────────────────────────
# 인물 삭제
# ──────────────────────────────────────────
@router.delete("/{name}")
async def delete_face(name: str):
    """
    등록된 인물 폴더 전체 삭제 + DB 재빌드
    """
    person_dir = FACE_DB_DIR / name
    if not person_dir.exists():
        raise HTTPException(status_code=404, detail=f"'{name}' 등록 정보 없음")

    shutil.rmtree(person_dir)

    if _analyzer:
        _analyzer.rebuild_face_db()

    logger.info(f"[FaceDB] 삭제: {name}")
    return JSONResponse({"success": True, "deleted": name})


# ──────────────────────────────────────────
# DB 수동 재빌드
# ──────────────────────────────────────────
@router.post("/rebuild")
async def rebuild_db():
    """
    face_db/known/ 폴더 기반 인코딩 캐시 강제 재생성
    """
    if _analyzer is None:
        raise HTTPException(status_code=503, detail="FrameAnalyzer 미초기화")

    _analyzer.rebuild_face_db()
    count = len(_analyzer._known_db)
    logger.info(f"[FaceDB] 수동 재빌드 완료: {count}명")
    return JSONResponse({"success": True, "registered_count": count})
