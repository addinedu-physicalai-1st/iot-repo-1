"""
SmartGate Gesture Authentication Module v2.0
mediapipe==0.10.14 (mp.solutions API) 기반

[모드 1] number  - 손가락 수 시퀀스     예: [1, 0, 3]  ☝→✊→3개
[모드 2] shape   - 공중 도형 드로잉     예: ["circle", "triangle", "square"]

도형 드로잉 규칙:
  - 검지(1개)만 펴면 궤적 수집 시작
  - 주먹(0)으로 도형 완성 신호
  - 지원 도형: circle / triangle / square

도형 분류 알고리즘:
  cv2.approxPolyDP() 꼭짓점 수 + 원형도(circularity)
  - circle   : circularity > 0.65
  - triangle : vertices == 3
  - square   : vertices == 4 + 종횡비 0.5~2.0
"""

import time
import math
from collections import deque
from typing import List, Optional, Tuple, Union

import cv2
import numpy as np

try:
    import mediapipe as mp
    MEDIAPIPE_AVAILABLE = True
except ImportError:
    MEDIAPIPE_AVAILABLE = False
    print("[WARNING] mediapipe 미설치. pip install mediapipe==0.10.14")


# ──────────────────────────────────────────────────────────────
# 도형 분류기
# ──────────────────────────────────────────────────────────────
class ShapeClassifier:
    """2D 점 궤적 → 도형 분류"""

    SHAPE_NAMES = {
        "circle":   "원 ○",
        "triangle": "삼각형 △",
        "square":   "사각형 □",
        "unknown":  "미인식 ?",
    }

    @staticmethod
    def classify(points: List[Tuple[int, int]]) -> str:
        if len(points) < 10:
            return "unknown"

        pts = np.array(points, dtype=np.float32)
        pts = ShapeClassifier._resample(pts, n=64)

        x, y, w, h = cv2.boundingRect(pts.astype(np.int32))
        if w < 20 or h < 20:
            return "unknown"

        contour   = pts.reshape(-1, 1, 2).astype(np.int32)
        area      = cv2.contourArea(contour)
        perimeter = cv2.arcLength(contour, closed=True)
        circularity = (4 * math.pi * area / (perimeter ** 2)) if perimeter > 0 else 0

        epsilon  = 0.04 * perimeter
        approx   = cv2.approxPolyDP(contour, epsilon, closed=True)
        vertices = len(approx)
        aspect   = w / h if h > 0 else 1.0

        if circularity > 0.65:
            return "circle"
        elif vertices >= 5 and circularity > 0.45:
            return "circle"
        elif vertices == 3:
            return "triangle"
        elif vertices == 4 and 0.5 < aspect < 2.0:
            return "square"
        else:
            return "unknown"

    @staticmethod
    def _resample(pts: np.ndarray, n: int = 64) -> np.ndarray:
        dists  = np.sqrt(np.sum(np.diff(pts, axis=0) ** 2, axis=1))
        total  = np.sum(dists)
        if total < 1:
            return pts
        interval   = total / (n - 1)
        resampled  = [pts[0]]
        d_acc, i   = 0.0, 1
        while len(resampled) < n and i < len(pts):
            d = np.linalg.norm(pts[i] - pts[i - 1])
            if d_acc + d >= interval:
                t      = (interval - d_acc) / d
                new_pt = pts[i - 1] + t * (pts[i] - pts[i - 1])
                resampled.append(new_pt)
                pts    = np.vstack([pts[:i], new_pt, pts[i:]])
                d_acc  = 0.0
            else:
                d_acc += d
            i += 1
        while len(resampled) < n:
            resampled.append(pts[-1])
        return np.array(resampled, dtype=np.float32)


# ──────────────────────────────────────────────────────────────
# 메인 인증기
# ──────────────────────────────────────────────────────────────
class GestureAuthenticator:
    """
    숫자 수신호 + 도형 드로잉 듀얼 모드 인증기

    mode="number" : sequence=[1, 0, 3]
    mode="shape"  : sequence=["circle", "triangle", "square"]
    """

    FINGER_TIPS = [4, 8, 12, 16, 20]
    FINGER_PIPS = [3, 6, 10, 14, 18]
    FINGER_MCPS = [2, 5, 9, 13, 17]

    DRAW_FINGER_COUNT = 1    # 궤적 수집: 검지만
    SHAPE_SEPARATOR   = 0    # 도형 완성: 주먹
    MIN_TRAIL_POINTS  = 15   # 최소 궤적 점 수

    def __init__(
        self,
        mode: str = "number",
        sequence: List[Union[int, str]] = [1, 0, 3],
        timeout_sec: float = 7.0,
        hold_frames: int = 8,
        cooldown_sec: float = 1.5,
    ):
        self.mode            = mode
        self.target_sequence = sequence
        self.timeout_sec     = timeout_sec
        self.hold_frames     = hold_frames
        self.cooldown_sec    = cooldown_sec

        # 공통 상태
        self.current_sequence: List        = []
        self.sequence_start_time: Optional[float] = None
        self.last_gesture_time: float      = 0.0
        self.last_gesture_val              = None
        self._frame_buffer: deque          = deque(maxlen=hold_frames)

        # 도형 모드 전용
        self._trail: List[Tuple[int, int]] = []
        self._drawing: bool                = False
        self._last_fist_time: float        = 0.0
        self._classified_shape: Optional[str] = None

        # MediaPipe (mp.solutions API - mediapipe==0.10.14)
        self._hands      = None
        self._mp_hands   = None
        self._mp_draw    = None
        self._mp_styles  = None

        if MEDIAPIPE_AVAILABLE:
            self._init_mediapipe()

        print(f"[GestureAuth] 모드: [{mode.upper()}] {sequence}")

    # ──────────────────────────────────────────
    # MediaPipe 초기화
    # ──────────────────────────────────────────
    def _init_mediapipe(self):
        try:
            self._mp_hands  = mp.solutions.hands
            self._hands     = self._mp_hands.Hands(
                static_image_mode=False,
                max_num_hands=1,
                min_detection_confidence=0.7,
                min_tracking_confidence=0.6,
            )
            self._mp_draw   = mp.solutions.drawing_utils
            self._mp_styles = mp.solutions.drawing_styles
            print("[GestureAuth] ✅ MediaPipe Hands 초기화 완료")
        except Exception as e:
            print(f"[GestureAuth] ❌ 초기화 실패: {e}")
            self._hands = None

    # ──────────────────────────────────────────
    # 손가락 수 카운팅
    # ──────────────────────────────────────────
    def count_fingers(self, hand_landmarks) -> int:
        """mp.solutions.hands 랜드마크에서 펴진 손가락 수 반환 (0~5)"""
        lm          = hand_landmarks.landmark
        count       = 0
        wrist_x     = lm[0].x
        index_mcp_x = lm[5].x
        thumb_tip_x = lm[self.FINGER_TIPS[0]].x
        thumb_pip_x = lm[self.FINGER_PIPS[0]].x

        if index_mcp_x > wrist_x:   # 오른손
            if thumb_tip_x > thumb_pip_x: count += 1
        else:                        # 왼손
            if thumb_tip_x < thumb_pip_x: count += 1

        for i in range(1, 5):
            if lm[self.FINGER_TIPS[i]].y < lm[self.FINGER_PIPS[i]].y:
                count += 1
        return count

    # ──────────────────────────────────────────
    # 프레임 처리 (공통 진입점)
    # ──────────────────────────────────────────
    def process_frame(self, frame_rgb: np.ndarray):
        """
        Returns:
            (auth_success, current_sequence, finger_count, hand_results)
        """
        if not MEDIAPIPE_AVAILABLE or self._hands is None:
            return False, [], None, None

        now = time.time()
        if self.sequence_start_time and (now - self.sequence_start_time) > self.timeout_sec:
            self._reset_sequence()

        results = self._hands.process(frame_rgb)

        if self.mode == "number":
            return self._process_number(results, now)
        else:
            h, w = frame_rgb.shape[:2]
            return self._process_shape(results, now, w, h)

    # ──────────────────────────────────────────
    # 숫자 모드
    # ──────────────────────────────────────────
    def _process_number(self, results, now: float):
        finger_count = None
        if results.multi_hand_landmarks:
            hand_lm   = results.multi_hand_landmarks[0]
            raw_count = self.count_fingers(hand_lm)
            self._frame_buffer.append(raw_count)

            if len(self._frame_buffer) == self.hold_frames:
                if len(set(self._frame_buffer)) == 1:
                    stable       = self._frame_buffer[0]
                    finger_count = stable
                    if stable != self.last_gesture_val:
                        self._record(stable, now)
                else:
                    finger_count = raw_count
        else:
            self._frame_buffer.clear()

        if self.current_sequence == list(self.target_sequence):
            self._reset_sequence()
            return True, list(self.target_sequence), finger_count, results

        return False, list(self.current_sequence), finger_count, results

    # ──────────────────────────────────────────
    # 도형 모드
    # ──────────────────────────────────────────
    def _process_shape(self, results, now: float, w: int, h: int):
        finger_count           = None
        self._classified_shape = None

        if results.multi_hand_landmarks:
            hand_lm      = results.multi_hand_landmarks[0]
            finger_count = self.count_fingers(hand_lm)

            tip    = hand_lm.landmark[8]   # 검지 TIP
            tip_px = (int(tip.x * w), int(tip.y * h))

            if finger_count == self.DRAW_FINGER_COUNT:
                if not self._drawing:
                    self._drawing = True
                    self._trail.clear()
                self._trail.append(tip_px)

            elif finger_count == self.SHAPE_SEPARATOR:
                if (self._drawing and
                        len(self._trail) >= self.MIN_TRAIL_POINTS and
                        now - self._last_fist_time > 1.0):

                    shape                  = ShapeClassifier.classify(self._trail)
                    self._classified_shape = shape
                    self._drawing          = False
                    self._last_fist_time   = now

                    if shape != "unknown":
                        self._record(shape, now)
                else:
                    self._drawing = False
            else:
                self._drawing = False
        else:
            self._frame_buffer.clear()
            self._drawing = False

        if self.current_sequence == list(self.target_sequence):
            self._reset_sequence()
            return True, list(self.target_sequence), finger_count, results

        return False, list(self.current_sequence), finger_count, results

    # ──────────────────────────────────────────
    # 공통 헬퍼
    # ──────────────────────────────────────────
    def _record(self, val, now: float):
        if not self.current_sequence:
            self.sequence_start_time = now
        self.current_sequence.append(val)
        self.last_gesture_time = now
        self.last_gesture_val  = val
        max_len = len(self.target_sequence)
        if len(self.current_sequence) > max_len:
            self.current_sequence = self.current_sequence[-max_len:]

    def _reset_sequence(self):
        self.current_sequence.clear()
        self.sequence_start_time = None
        self.last_gesture_val    = None
        self._frame_buffer.clear()
        self._trail.clear()
        self._drawing          = False
        self._classified_shape = None

    # ──────────────────────────────────────────
    # 시각화
    # ──────────────────────────────────────────
    def draw_landmarks(self, frame_bgr: np.ndarray, results) -> np.ndarray:
        """손 랜드마크 시각화 (mp.solutions.drawing_utils)"""
        if (not MEDIAPIPE_AVAILABLE or results is None or
                not results.multi_hand_landmarks):
            return frame_bgr
        for hand_lm in results.multi_hand_landmarks:
            self._mp_draw.draw_landmarks(
                frame_bgr,
                hand_lm,
                self._mp_hands.HAND_CONNECTIONS,
                self._mp_styles.get_default_hand_landmarks_style(),
                self._mp_styles.get_default_hand_connections_style(),
            )
        return frame_bgr

    def draw_trail(self, frame_bgr: np.ndarray) -> np.ndarray:
        """도형 모드: 검지 궤적 + 분류 결과 시각화"""
        if self.mode != "shape" or len(self._trail) < 2:
            return frame_bgr

        for i in range(1, len(self._trail)):
            alpha = i / len(self._trail)
            c     = int(200 * alpha)
            cv2.line(frame_bgr, self._trail[i-1], self._trail[i], (0, c, 255), 3)

        cv2.circle(frame_bgr, self._trail[0],  8, (0, 255, 0),   -1)
        cv2.circle(frame_bgr, self._trail[-1], 8, (0, 0, 255),   -1)

        if self._classified_shape:
            name  = ShapeClassifier.SHAPE_NAMES.get(self._classified_shape, "?")
            color = (0, 255, 100) if self._classified_shape != "unknown" else (0, 80, 255)
            cv2.putText(frame_bgr, name,
                        (self._trail[-1][0] + 10, self._trail[-1][1]),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
        return frame_bgr

    def get_remaining_time(self) -> Optional[float]:
        if self.sequence_start_time is None:
            return None
        return max(0.0, self.timeout_sec - (time.time() - self.sequence_start_time))

    @property
    def is_drawing(self) -> bool:
        return self._drawing

    @property
    def trail_count(self) -> int:
        return len(self._trail)

    @property
    def is_ready(self) -> bool:
        return MEDIAPIPE_AVAILABLE and self._hands is not None
