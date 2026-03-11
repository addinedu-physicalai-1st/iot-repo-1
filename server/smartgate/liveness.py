"""
modules/liveness.py  v3.1
=========================
MediaPipe Face Mesh 기반 생존 인증 (Liveness Detection)
2D 사진 / 태블릿 스푸핑 방어

챌린지 종류 (config.yaml liveness.challenges_pool)
  blink       : 눈 깜빡임       (EAR — Eye Aspect Ratio)
  yaw         : 고개 좌우 회전  (코끝-귀 비율, 방향 지시 랜덤)
  nod         : 고개 위아래 끄덕임 (코끝 Pitch 변화)
  mouth_open  : 입 벌리기       (MAR — Mouth Aspect Ratio)

v2 변경사항
-----------
  - nod / mouth_open 챌린지 추가
  - challenges_pool + num_challenges: 매번 풀에서 랜덤 선택
  - yaw 방향 지시 랜덤화 (왼쪽 먼저 / 오른쪽 먼저)

v3.1 변경사항 (ESP-CAM Liveness 감지율 개선)
---------------------------------------------
  - 디버그 로그 추가: 매 프레임 EAR/yaw/pitch/MAR 값 출력 (5프레임마다)
    → threshold 튜닝의 근거 데이터 확보
  - ESP-CAM 프로파일(espcam) threshold 완화:
    blink_ear_thresh: 0.22→0.25 (저해상도 EAR 노이즈 보상)
    blink_consec_frames: 2→1 (저FPS에서 감긴 프레임 놓침 방지)
    yaw_threshold: 0.15→0.10 (작은 움직임도 감지)
    nod_threshold: 0.08→0.06
    mouth_mar_thresh: 0.45→0.35
    mouth_consec_frames: 2→1
    timeout_sec: 8→12 (저FPS 반응 지연 보상)
  - Face Mesh confidence 하향: 0.6→0.4 (저해상도 얼굴 검출률 향상)
  - 프레임 업스케일 옵션: upscale_to 설정 시 Face Mesh 입력 전 리사이즈
    → 320×240 프레임을 640×480으로 업스케일하면 랜드마크 정밀도 향상

config.yaml 예시
----------------
  liveness:
    enabled: true
    active_profile: espcam          # ← ESP-CAM 배포 시
    challenges_pool: ["blink", "yaw"]
    num_challenges: 2
    random_order: true

    profiles:
      laptop:
        timeout_sec: 8.0
        blink_ear_thresh: 0.22
        blink_consec_frames: 2
        yaw_threshold: 0.15
        nod_threshold: 0.08
        mouth_mar_thresh: 0.45
        mouth_consec_frames: 2
      espcam:
        timeout_sec: 12.0
        blink_ear_thresh: 0.25
        blink_consec_frames: 1
        yaw_threshold: 0.10
        nod_threshold: 0.06
        mouth_mar_thresh: 0.35
        mouth_consec_frames: 1
        challenges_pool: ["blink", "yaw"]
        num_challenges: 2
        upscale_to: 640              # 짧은 변 기준 업스케일 (0=비활성)
        debug_log: true              # 감지값 로그 출력
        mesh_detect_conf: 0.4        # Face Mesh detection confidence
        mesh_track_conf: 0.4         # Face Mesh tracking confidence
"""

import time
import math
import random
import logging
from typing import List, Tuple, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

try:
    import mediapipe as mp
    _mp_face_mesh = mp.solutions.face_mesh
    MEDIAPIPE_OK  = True
except ImportError:
    MEDIAPIPE_OK  = False
    print("[WARNING] mediapipe 미설치 — Liveness 비활성화")


# ──────────────────────────────────────────────────────────────
# Face Mesh 랜드마크 인덱스 (MediaPipe 468점)
# ──────────────────────────────────────────────────────────────

# 눈 EAR (각 눈 6점)
LEFT_EYE  = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33,  160, 158, 133, 153, 144]

# Yaw (좌우 회전): 코끝 + 양 귀 기준점
NOSE_TIP     = 1
LEFT_TEMPLE  = 234
RIGHT_TEMPLE = 454

# Pitch (위아래): 코끝 + 이마 + 턱
FOREHEAD     = 10
CHIN         = 152

# Mouth MAR (6점)
UPPER_LIP    = 13
LOWER_LIP    = 14
MOUTH_LEFT   = 61
MOUTH_RIGHT  = 291
MOUTH_TOP    = 0
MOUTH_BOTTOM = 17


# ──────────────────────────────────────────────────────────────
# 계산 유틸
# ──────────────────────────────────────────────────────────────

def _ear(lm, eye_idx: List[int], w: int, h: int) -> float:
    """Eye Aspect Ratio: 낮을수록 눈이 감긴 상태"""
    pts = [(lm[i].x * w, lm[i].y * h) for i in eye_idx]
    p1, p2, p3, p4, p5, p6 = pts
    A = math.dist(p2, p6)
    B = math.dist(p3, p5)
    C = math.dist(p1, p4)
    return (A + B) / (2.0 * C) if C > 0 else 0.0


def _yaw(lm, w: int, h: int) -> float:
    """Yaw 정규화: 음수=왼쪽, 양수=오른쪽 (약 -0.5 ~ +0.5)"""
    nose  = lm[NOSE_TIP]
    left  = lm[LEFT_TEMPLE]
    right = lm[RIGHT_TEMPLE]
    mid_x = (left.x + right.x) / 2.0
    span  = abs(right.x - left.x)
    return (nose.x - mid_x) / span if span > 0.01 else 0.0


def _pitch(lm, w: int, h: int) -> float:
    """Pitch 정규화: 위로 끄덕=음수, 아래로 끄덕=양수"""
    nose     = lm[NOSE_TIP]
    forehead = lm[FOREHEAD]
    chin     = lm[CHIN]
    span     = abs(chin.y - forehead.y)
    if span < 0.01:
        return 0.0
    mid_y = (forehead.y + chin.y) / 2.0
    return (nose.y - mid_y) / span


def _mar(lm, w: int, h: int) -> float:
    """Mouth Aspect Ratio: 높을수록 입이 벌어짐"""
    ul = (lm[UPPER_LIP].x * w,    lm[UPPER_LIP].y * h)
    ll = (lm[LOWER_LIP].x * w,    lm[LOWER_LIP].y * h)
    ml = (lm[MOUTH_LEFT].x * w,   lm[MOUTH_LEFT].y * h)
    mr = (lm[MOUTH_RIGHT].x * w,  lm[MOUTH_RIGHT].y * h)
    mt = (lm[MOUTH_TOP].x * w,    lm[MOUTH_TOP].y * h)
    mb = (lm[MOUTH_BOTTOM].x * w, lm[MOUTH_BOTTOM].y * h)
    vertical   = (math.dist(ul, ll) + math.dist(mt, mb)) / 2.0
    horizontal = math.dist(ml, mr)
    return vertical / horizontal if horizontal > 0 else 0.0


# ──────────────────────────────────────────────────────────────
# LivenessChecker v3.1
# ──────────────────────────────────────────────────────────────
class LivenessChecker:

    def __init__(self, cfg: dict):
        # ── 프로파일 병합 ──────────────────────────────────────────
        # active_profile 값으로 profiles 섹션 선택 후 cfg에 병합
        # 우선순위: profiles[active] > cfg 최상위
        active  = cfg.get("active_profile", "laptop")
        profile = cfg.get("profiles", {}).get(active, {})
        if profile:
            print(f"[Liveness] 프로파일 적용: '{active}'")
        else:
            print(f"[Liveness] 프로파일 '{active}' 없음 — 기본값 사용")
        # profile 값이 cfg 최상위보다 우선
        c = {**cfg, **profile}

        self.enabled         = c.get("enabled", True)
        self.active_profile  = active
        self._pool           = c.get("challenges_pool",
                                     ["blink", "yaw", "nod", "mouth_open"])
        self._num            = c.get("num_challenges", 2)
        self._random_order   = c.get("random_order", True)
        self.timeout_sec     = c.get("timeout_sec", 8.0)

        # 파라미터
        self._ear_thresh     = c.get("blink_ear_thresh", 0.22)
        self._blink_frames   = c.get("blink_consec_frames", 2)
        self._yaw_thresh     = c.get("yaw_threshold", 0.15)
        self._yaw_hold       = c.get("yaw_hold_frames", 2)
        self._nod_thresh     = c.get("nod_threshold", 0.08)
        self._nod_hold       = c.get("nod_hold_frames", 2)
        self._mar_thresh     = c.get("mouth_mar_thresh", 0.45)
        self._mouth_frames   = c.get("mouth_consec_frames", 2)

        # v3.1: ESP-CAM 최적화 파라미터
        self._upscale_to     = c.get("upscale_to", 0)          # 짧은변 기준 업스케일 (0=비활성)
        self._debug_log      = c.get("debug_log", False)       # 감지값 로그 출력
        self._mesh_det_conf  = c.get("mesh_detect_conf", 0.6)  # Face Mesh detection confidence
        self._mesh_trk_conf  = c.get("mesh_track_conf", 0.6)   # Face Mesh tracking confidence
        self._debug_counter  = 0                                 # 로그 출력 주기 카운터

        # 런타임 상태
        self._challenges: List[str] = []
        self._meta: dict            = {}
        self._current_idx           = 0
        self._start_time            = 0.0
        self._active                = False
        self._reset_state()

        # Face Mesh 초기화 (v3.1: confidence 프로파일별 설정)
        self._face_mesh = None
        if MEDIAPIPE_OK and self.enabled:
            self._face_mesh = _mp_face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=1,
                refine_landmarks=True,
                min_detection_confidence=self._mesh_det_conf,
                min_tracking_confidence=self._mesh_trk_conf,
            )
            print(f"[Liveness] ✅ Face Mesh 초기화 완료 (v3.1) | 프로파일: {self.active_profile}")
            print(f"[Liveness] 풀: {self._pool}  | 매회 {self._num}개 랜덤 선택")
            print(f"[Liveness] EAR={self._ear_thresh} | yaw={self._yaw_thresh} | "
                  f"nod={self._nod_thresh} | MAR={self._mar_thresh}")
            print(f"[Liveness] timeout={self.timeout_sec}s | upscale={self._upscale_to} | "
                  f"debug={self._debug_log}")
            print(f"[Liveness] mesh_conf: det={self._mesh_det_conf} trk={self._mesh_trk_conf}")

    # ──────────────────────────────────────────
    # v3.1: 프레임 업스케일 (저해상도 보상)
    # ──────────────────────────────────────────
    def _upscale_frame(self, frame_bgr: np.ndarray) -> np.ndarray:
        """짧은 변이 upscale_to보다 작으면 INTER_LINEAR으로 업스케일"""
        if self._upscale_to <= 0:
            return frame_bgr
        h, w = frame_bgr.shape[:2]
        short_side = min(h, w)
        if short_side >= self._upscale_to:
            return frame_bgr
        scale = self._upscale_to / short_side
        new_w = int(w * scale)
        new_h = int(h * scale)
        return cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    # ──────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────
    def start(self):
        """얼굴 인증 성공 직후 호출"""
        if not self.enabled or self._face_mesh is None:
            return

        n        = min(self._num, len(self._pool))
        selected = random.sample(self._pool, n)
        if self._random_order:
            random.shuffle(selected)

        self._challenges  = selected
        self._meta        = {}
        self._current_idx = 0
        self._start_time  = time.time()
        self._active      = True
        self._debug_counter = 0
        self._reset_state()

        # yaw 방향 지시 랜덤 메타 생성
        if "yaw" in self._challenges:
            self._meta["yaw_first"] = random.choice([-1, 1])

        print(f"[Liveness] 챌린지: {self._challenges}")

    def reset(self):
        self._active      = False
        self._current_idx = 0
        self._challenges  = []
        self._meta        = {}
        self._debug_counter = 0
        self._reset_state()

    def process(self, frame_bgr: np.ndarray) -> Tuple[bool, bool, str]:
        """
        Returns: (done, failed, message)
        """
        if not self.enabled or self._face_mesh is None:
            return True, False, ""
        if not self._active:
            return False, False, ""

        elapsed = time.time() - self._start_time
        if elapsed > self.timeout_sec:
            self.reset()
            return False, True, "⏰ 시간 초과 — 다시 시도하세요"

        remaining = self.timeout_sec - elapsed

        if self._current_idx >= len(self._challenges):
            self.reset()
            return True, False, "✅ 생존 인증 완료"

        current = self._challenges[self._current_idx]
        msg     = self._get_msg(current)

        # v3.1: 업스케일 적용
        proc_frame = self._upscale_frame(frame_bgr)

        frame_rgb = cv2.cvtColor(proc_frame, cv2.COLOR_BGR2RGB)
        results   = self._face_mesh.process(frame_rgb)

        if not results.multi_face_landmarks:
            # 얼굴 미검출 시 baseline 초기화 (재검출 시 새 기준점 사용)
            self._reset_state()
            # v3.1: 얼굴 미검출 디버그 로그
            self._debug_counter += 1
            if self._debug_log and self._debug_counter % 5 == 0:
                h, w = proc_frame.shape[:2]
                logger.info(f"[Liveness] ❌ 얼굴 미검출 | frame={w}x{h} | "
                            f"challenge={current} | {remaining:.1f}s")
            return False, False, f"{msg}  ({remaining:.1f}s)"

        lm   = results.multi_face_landmarks[0].landmark
        h, w = proc_frame.shape[:2]

        # v3.1: 디버그 로그 — 현재 감지값 출력 (5프레임마다)
        self._debug_counter += 1
        if self._debug_log and self._debug_counter % 5 == 0:
            avg_ear  = (_ear(lm, LEFT_EYE, w, h) + _ear(lm, RIGHT_EYE, w, h)) / 2.0
            cur_yaw  = _yaw(lm, w, h)
            cur_pitch = _pitch(lm, w, h)
            cur_mar  = _mar(lm, w, h)
            yaw_delta = (cur_yaw - self._yaw_baseline) if self._yaw_baseline is not None else 0.0
            nod_delta = (cur_pitch - self._nod_baseline) if self._nod_baseline is not None else 0.0
            logger.info(
                f"[Liveness] 📊 {current} | "
                f"EAR={avg_ear:.3f}(thr={self._ear_thresh}) | "
                f"yaw={cur_yaw:.3f}(Δ={yaw_delta:.3f},thr=±{self._yaw_thresh}) | "
                f"pitch={cur_pitch:.3f}(Δ={nod_delta:.3f},thr=±{self._nod_thresh}) | "
                f"MAR={cur_mar:.3f}(thr={self._mar_thresh}) | "
                f"frame={w}x{h} | {remaining:.1f}s"
            )

        # 챌린지 검사
        passed = False
        if current == "blink":
            passed = self._check_blink(lm, w, h)
        elif current == "yaw":
            passed = self._check_yaw(lm, w, h)
        elif current == "nod":
            passed = self._check_nod(lm, w, h)
        elif current == "mouth_open":
            passed = self._check_mouth(lm, w, h)

        if passed:
            print(f"[Liveness] ✅ {current} 통과")
            self._current_idx += 1
            self._reset_state()
            if self._current_idx >= len(self._challenges):
                self.reset()
                return True, False, "✅ 생존 인증 완료"
            next_msg = self._get_msg(self._challenges[self._current_idx])
            return False, False, next_msg

        step = f"({self._current_idx + 1}/{len(self._challenges)})"
        return False, False, f"{msg} {step}  {remaining:.1f}s"

    # ──────────────────────────────────────────
    # 챌린지 메시지
    # ──────────────────────────────────────────
    def _get_msg(self, ch: str) -> str:
        if ch == "blink":
            return "눈을 깜빡여 주세요 👁"
        elif ch == "yaw":
            first = self._meta.get("yaw_first", -1)
            return ("고개를 왼쪽 → 오른쪽으로 돌려 주세요 ↔"
                    if first == -1 else
                    "고개를 오른쪽 → 왼쪽으로 돌려 주세요 ↔")
        elif ch == "nod":
            return "고개를 위아래로 끄덕여 주세요 ↕"
        elif ch == "mouth_open":
            return "입을 크게 벌려 주세요 👄"
        return ch

    # ──────────────────────────────────────────
    # 챌린지 검사 메서드
    # ──────────────────────────────────────────
    def _check_blink(self, lm, w: int, h: int) -> bool:
        avg_ear = (_ear(lm, LEFT_EYE, w, h) + _ear(lm, RIGHT_EYE, w, h)) / 2.0
        if avg_ear < self._ear_thresh:
            self._blink_counter += 1
        else:
            if self._blink_counter >= self._blink_frames:
                self._blink_counter = 0
                return True
            self._blink_counter = 0
        return False

    def _check_yaw(self, lm, w: int, h: int) -> bool:
        yaw = _yaw(lm, w, h)
        if self._yaw_baseline is None:
            self._yaw_baseline = yaw
            return False
        delta = yaw - self._yaw_baseline
        first = self._meta.get("yaw_first", -1)

        if first == -1:   # 왼쪽 먼저
            if not self._yaw_first_done:
                if delta < -self._yaw_thresh:
                    self._yaw_first_counter += 1
                    if self._yaw_first_counter >= self._yaw_hold:
                        self._yaw_first_done = True
                else:
                    self._yaw_first_counter = 0
            elif not self._yaw_second_done:
                if delta > self._yaw_thresh:
                    self._yaw_second_counter += 1
                    if self._yaw_second_counter >= self._yaw_hold:
                        self._yaw_second_done = True
                else:
                    self._yaw_second_counter = 0
        else:              # 오른쪽 먼저
            if not self._yaw_first_done:
                if delta > self._yaw_thresh:
                    self._yaw_first_counter += 1
                    if self._yaw_first_counter >= self._yaw_hold:
                        self._yaw_first_done = True
                else:
                    self._yaw_first_counter = 0
            elif not self._yaw_second_done:
                if delta < -self._yaw_thresh:
                    self._yaw_second_counter += 1
                    if self._yaw_second_counter >= self._yaw_hold:
                        self._yaw_second_done = True
                else:
                    self._yaw_second_counter = 0

        return self._yaw_first_done and self._yaw_second_done

    def _check_nod(self, lm, w: int, h: int) -> bool:
        pitch = _pitch(lm, w, h)
        if self._nod_baseline is None:
            self._nod_baseline = pitch
            return False
        delta = pitch - self._nod_baseline

        if not self._nod_up_done:
            if delta < -self._nod_thresh:
                self._nod_up_counter += 1
                if self._nod_up_counter >= self._nod_hold:
                    self._nod_up_done = True
            else:
                self._nod_up_counter = 0
        elif not self._nod_down_done:
            if delta > self._nod_thresh:
                self._nod_down_counter += 1
                if self._nod_down_counter >= self._nod_hold:
                    self._nod_down_done = True
            else:
                self._nod_down_counter = 0

        return self._nod_up_done and self._nod_down_done

    def _check_mouth(self, lm, w: int, h: int) -> bool:
        mar = _mar(lm, w, h)
        if mar > self._mar_thresh:
            self._mouth_counter += 1
        else:
            if self._mouth_counter >= self._mouth_frames:
                self._mouth_counter = 0
                return True
            self._mouth_counter = 0
        return False

    # ──────────────────────────────────────────
    # 내부 상태 초기화
    # ──────────────────────────────────────────
    def _reset_state(self):
        self._blink_counter      = 0
        self._yaw_baseline       = None
        self._yaw_first_done     = False
        self._yaw_second_done    = False
        self._yaw_first_counter  = 0
        self._yaw_second_counter = 0
        self._nod_baseline       = None
        self._nod_up_done        = False
        self._nod_down_done      = False
        self._nod_up_counter     = 0
        self._nod_down_counter   = 0
        self._mouth_counter      = 0

    # ──────────────────────────────────────────
    # 상태 조회
    # ──────────────────────────────────────────
    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def current_challenge(self) -> Optional[str]:
        if not self._active or self._current_idx >= len(self._challenges):
            return None
        return self._challenges[self._current_idx]

    @property
    def is_ready(self) -> bool:
        return MEDIAPIPE_OK and self._face_mesh is not None
