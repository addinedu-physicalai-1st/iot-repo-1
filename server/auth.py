"""
server/auth.py — FastAPI JWT 인증 모듈
Voice IoT Controller · iot-repo-1
보안 등급: HIGH | NIST SP 800-213 §4.3 | OWASP IoT OT2
"""

import os
import time
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from passlib.context import CryptContext

# ── 환경변수 필수 설정 ──────────────────────────────────────────
#   export JWT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
SECRET_KEY: str = os.environ.get("JWT_SECRET", "")
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_SEC: int = 3600  # 1시간

if not SECRET_KEY:
    raise RuntimeError("[AUTH] JWT_SECRET 환경변수가 설정되지 않았습니다.")

# ── 패스워드 해시 (대시보드 로그인용) ───────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


def create_access_token(subject: str) -> str:
    """JWT 토큰 발급"""
    payload = {
        "sub": subject,
        "iat": int(time.time()),
        "exp": int(time.time()) + ACCESS_TOKEN_EXPIRE_SEC,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """FastAPI Depends로 주입하는 JWT 검증 함수"""
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="유효하지 않거나 만료된 토큰입니다.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM]
        )
        if payload.get("sub") is None:
            raise exc
        return payload
    except JWTError:
        raise exc


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)
