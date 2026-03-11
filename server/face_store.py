"""
server/face_store.py — 얼굴 임베딩 Fernet 암호화 저장/로드
HIGH-3 | NIST SP 800-213 §4.4 | OWASP IoT OT6

보안 목표:
  - 512d 얼굴 임베딩 벡터(생체정보)를 AES-128-CBC(Fernet)로 암호화 저장
  - FACE_DB_KEY(.env) 없이는 복호화 불가
  - 기존 평문 encodings.pkl → encodings.enc 1회 자동 마이그레이션

공개 API:
  save_embeddings(db, path)          dict/list → 암호화 → .enc 저장
  load_embeddings(path)              .enc → 복호화 → dict/list 반환
  migrate_plaintext_db(pkl, enc)     평문 .pkl → .enc 변환 후 .pkl 삭제

환경변수:
  FACE_DB_KEY  Fernet 키 (base64, 44자)
               생성: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
               .env에 저장 후 chmod 600 .env
"""

import logging
import os
import pickle
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# ── Fernet 임포트 ──────────────────────────────────────────────
try:
    from cryptography.fernet import Fernet, InvalidToken
    FERNET_AVAILABLE = True
except ImportError:
    FERNET_AVAILABLE = False
    logger.warning("[FaceStore] cryptography 미설치 — 평문 fallback 사용")
    logger.warning("            pip install cryptography")


# ── 키 로드 ────────────────────────────────────────────────────
def _get_fernet() -> "Fernet | None":
    """
    환경변수 FACE_DB_KEY에서 Fernet 인스턴스 반환.
    키 없거나 유효하지 않으면 None 반환 (평문 fallback).
    """
    if not FERNET_AVAILABLE:
        return None

    key_str = os.environ.get("FACE_DB_KEY", "").strip()
    if not key_str:
        logger.warning(
            "[FaceStore] ⚠️ FACE_DB_KEY 미설정 — 평문 fallback 사용 (보안 위험)\n"
            "  키 생성: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "  .env에 FACE_DB_KEY=<위 출력값> 추가"
        )
        return None

    try:
        return Fernet(key_str.encode())
    except Exception as e:
        logger.error(f"[FaceStore] FACE_DB_KEY 유효하지 않음: {e}")
        return None


# ── 공개 API ───────────────────────────────────────────────────

def save_embeddings(db: Union[dict, list], path: str) -> bool:
    """
    얼굴 임베딩 DB를 Fernet 암호화하여 .enc 파일로 저장.

    Args:
        db   : {"embeddings": [...], "names": [...]} 또는 list 포맷
        path : 저장 경로 (예: "face_db/encodings.enc")

    Returns:
        True  → 암호화 저장 성공
        False → 실패 (로그 확인)
    """
    enc_path = Path(path)
    enc_path.parent.mkdir(parents=True, exist_ok=True)

    raw = pickle.dumps(db)
    fernet = _get_fernet()

    if fernet is not None:
        try:
            encrypted = fernet.encrypt(raw)
            enc_path.write_bytes(encrypted)
            logger.info(f"[FaceStore] ✅ 암호화 저장 완료: {enc_path}")
            return True
        except Exception as e:
            logger.error(f"[FaceStore] 암호화 저장 실패: {e}")
            return False
    else:
        # 평문 fallback (FACE_DB_KEY 미설정 시)
        try:
            enc_path.write_bytes(raw)
            logger.warning(f"[FaceStore] ⚠️ 평문 저장 (암호화 비활성): {enc_path}")
            return True
        except Exception as e:
            logger.error(f"[FaceStore] 평문 저장 실패: {e}")
            return False


def load_embeddings(path: str) -> Union[dict, list, None]:
    """
    .enc 파일을 Fernet 복호화하여 임베딩 DB 반환.

    Args:
        path : 읽을 경로 (예: "face_db/encodings.enc")

    Returns:
        dict 또는 list → 성공
        None           → 파일 없음 / 복호화 실패
    """
    enc_path = Path(path)
    if not enc_path.exists():
        logger.debug(f"[FaceStore] 파일 없음: {enc_path}")
        return None

    raw_bytes = enc_path.read_bytes()
    fernet = _get_fernet()

    if fernet is not None:
        try:
            decrypted = fernet.decrypt(raw_bytes)
            db = pickle.loads(decrypted)
            logger.info(f"[FaceStore] ✅ 복호화 로드 완료: {enc_path}")
            return db
        except InvalidToken:
            logger.error(
                f"[FaceStore] ❌ 복호화 실패 (키 불일치 또는 파일 손상): {enc_path}\n"
                "  → FACE_DB_KEY가 변경되었거나 파일이 손상되었습니다.\n"
                "  → face_db/known/ 이미지로 재임베딩이 필요합니다."
            )
            return None
        except Exception as e:
            logger.error(f"[FaceStore] 로드 오류: {e}")
            return None
    else:
        # 평문 fallback — 암호화되지 않은 파일 시도
        try:
            db = pickle.loads(raw_bytes)
            logger.warning(f"[FaceStore] ⚠️ 평문 로드 (암호화 비활성): {enc_path}")
            return db
        except Exception as e:
            logger.error(f"[FaceStore] 평문 로드 실패: {e}")
            return None


def migrate_plaintext_db(pkl_path: str, enc_path: str) -> bool:
    """
    기존 평문 encodings.pkl → encodings.enc 1회 변환.
    성공 시 원본 .pkl 삭제.

    Args:
        pkl_path : 기존 평문 캐시 경로 (예: "face_db/encodings.pkl")
        enc_path : 변환 후 암호화 저장 경로 (예: "face_db/encodings.enc")

    Returns:
        True  → 마이그레이션 성공
        False → 실패 (원본 유지)
    """
    pkl = Path(pkl_path)
    enc = Path(enc_path)

    if not pkl.exists():
        logger.debug(f"[FaceStore] 마이그레이션 대상 없음: {pkl}")
        return False

    if enc.exists():
        logger.info(f"[FaceStore] 이미 암호화 파일 존재 — 마이그레이션 스킵: {enc}")
        return True

    try:
        with open(pkl, "rb") as f:
            db = pickle.load(f)
    except Exception as e:
        logger.error(f"[FaceStore] 평문 pkl 읽기 실패: {e}")
        return False

    success = save_embeddings(db, str(enc))
    if success:
        try:
            pkl.unlink()
            logger.info(f"[FaceStore] ✅ 마이그레이션 완료 | {pkl} → {enc} (원본 삭제)")
        except Exception as e:
            logger.warning(f"[FaceStore] 원본 .pkl 삭제 실패 (수동 삭제 권장): {e}")
        return True
    else:
        logger.error("[FaceStore] 마이그레이션 실패 — 원본 .pkl 유지")
        return False
