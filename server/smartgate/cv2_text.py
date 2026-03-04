"""
cv2_text.py - PIL 기반 한글/유니코드 텍스트 렌더링 유틸
cv2.putText() 대체용

사용법:
    from modules.cv2_text import put_text, load_font
    font = load_font(size=24)
    put_text(frame, "안녕하세요", (20, 40), font, color=(255,255,255))
"""

import numpy as np
import cv2
from pathlib import Path
from typing import Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("[cv2_text] PIL 미설치. pip install Pillow")

# 한글 폰트 후보 경로 (우선순위 순)
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf",
]

_font_cache: dict = {}


def find_korean_font() -> str:
    """시스템에서 한글 지원 폰트 경로 반환"""
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path

    # fc-list로 동적 탐색
    import subprocess
    try:
        result = subprocess.run(
            ["fc-list", ":lang=ko", "--format=%{file}\n"],
            capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and Path(line).exists():
                return line
    except Exception:
        pass

    return None


def load_font(size: int = 22) -> "ImageFont":
    """PIL 폰트 로드 (캐시 지원)"""
    if not PIL_AVAILABLE:
        return None

    if size in _font_cache:
        return _font_cache[size]

    font_path = find_korean_font()
    if font_path:
        try:
            font = ImageFont.truetype(font_path, size)
            _font_cache[size] = font
            return font
        except Exception as e:
            print(f"[cv2_text] 폰트 로드 실패 ({font_path}): {e}")

    # 폴백: PIL 기본 폰트 (한글 미지원이지만 크래시 방지)
    font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def put_text(
    frame: np.ndarray,
    text: str,
    pos: Tuple[int, int],
    font,
    color: Tuple[int, int, int] = (255, 255, 255),
    shadow: bool = True,
) -> np.ndarray:
    """
    frame(BGR)에 한글 텍스트 렌더링 후 반환
    
    Args:
        frame : OpenCV BGR 프레임
        text  : 출력 텍스트 (한글 포함 가능)
        pos   : (x, y) 좌상단 기준
        font  : load_font()로 로드한 PIL 폰트
        color : BGR 색상 튜플
        shadow: 그림자 효과 여부
    """
    if not PIL_AVAILABLE or font is None:
        # PIL 없으면 cv2.putText 폴백 (한글 깨짐 감수)
        cv2.putText(frame, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        return frame

    # BGR → RGB
    img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw    = ImageDraw.Draw(img_pil)

    x, y = pos
    rgb = (color[2], color[1], color[0])  # BGR → RGB

    # 그림자
    if shadow:
        draw.text((x+1, y+1), text, font=font, fill=(0, 0, 0))

    draw.text((x, y), text, font=font, fill=rgb)

    # RGB → BGR
    frame[:] = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    return frame


def put_text_centered(
    frame: np.ndarray,
    text: str,
    cy: int,
    font,
    color: Tuple[int, int, int] = (255, 255, 255),
    shadow: bool = True,
) -> np.ndarray:
    """프레임 가로 중앙 정렬 텍스트"""
    if not PIL_AVAILABLE or font is None:
        h, w = frame.shape[:2]
        cv2.putText(frame, text, (w//2 - len(text)*7, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        return frame

    img_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw    = ImageDraw.Draw(img_pil)
    w       = frame.shape[1]

    bbox = font.getbbox(text)
    tw   = bbox[2] - bbox[0]
    x    = (w - tw) // 2
    rgb  = (color[2], color[1], color[0])

    if shadow:
        draw.text((x+1, cy+1), text, font=font, fill=(0, 0, 0))
    draw.text((x, cy), text, font=font, fill=rgb)

    frame[:] = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    return frame
