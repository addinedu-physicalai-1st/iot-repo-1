"""
gui/dashboard.py
================
Voice IoT Controller - PyQt6 데스크탑 대시보드

역할:
  - FastAPI REST API 폴링 + WebSocket 실시간 수신
  - pyqtgraph 센서 실시간 그래프 (온도/습도)
  - 디바이스 상태 패널 (LED / 서보 / 센서)
  - 음성 명령 (로컬 Whisper STT)
  - 명령 히스토리 로그

실행:
  cd ~/dev_ws/voice_iot_controller
  python -m gui.dashboard
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from datetime import datetime
from typing import Optional

import requests
from PyQt6.QtCore import (
    Qt, QThread, QTimer, pyqtSignal, QObject
)
from PyQt6.QtGui import QColor, QFont, QPalette, QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit,
    QTextEdit, QFrame, QSizePolicy,
    QSlider, QGroupBox, QSplitter,
    QScrollArea,
)
import pyqtgraph as pg
import websockets
import asyncio

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────

SERVER_HOST  = "localhost"
SERVER_PORT  = 8000
BASE_URL     = f"http://{SERVER_HOST}:{SERVER_PORT}"
WS_URL       = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
POLL_INTERVAL_MS = 5000     # REST 폴링 주기
MAX_GRAPH_POINTS = 60       # 그래프 최대 포인트 수
MAX_LOG_LINES    = 200      # 로그 최대 라인 수

# ── 색상 팔레트 ──────────────────────────────────────────────────────
C = {
    "bg0":    "#050810",
    "bg1":    "#090d18",
    "bg2":    "#0d1220",
    "bg3":    "#111828",
    "cyan":   "#00C8FF",
    "green":  "#00FF88",
    "yellow": "#FFB400",
    "red":    "#FF4060",
    "purple": "#A060FF",
    "text":   "#C8E0F0",
    "text2":  "#5A7090",
    "border": "#1A2840",
}


# ════════════════════════════════════════════════════════════════════
# WebSocket 수신 스레드
# ════════════════════════════════════════════════════════════════════

class WSWorker(QObject):
    """WebSocket 메시지를 Qt 시그널로 전달"""
    message  = pyqtSignal(dict)
    connected    = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self._running = False

    def start(self):
        self._running = True
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self._run)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_loop())
        loop.close()

    async def _ws_loop(self):
        while self._running:
            try:
                async with websockets.connect(self.url) as ws:
                    self.connected.emit()
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            data = json.loads(raw)
                            self.message.emit(data)
                        except asyncio.TimeoutError:
                            continue
            except Exception:
                self.disconnected.emit()
                await asyncio.sleep(3)


# ════════════════════════════════════════════════════════════════════
# REST 폴링 타이머
# ════════════════════════════════════════════════════════════════════

class RestPoller(QObject):
    devices_updated = pyqtSignal(list)

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._poll()
        self._timer.start(POLL_INTERVAL_MS)

    def stop(self):
        self._timer.stop()

    def _poll(self):
        try:
            r = requests.get(f"{self.base_url}/devices", timeout=2)
            if r.status_code == 200:
                self.devices_updated.emit(r.json().get("devices", []))
        except Exception:
            pass

    def send_command(self, data: dict) -> Optional[dict]:
        try:
            r = requests.post(
                f"{self.base_url}/command", json=data, timeout=3
            )
            return r.json()
        except Exception as e:
            return {"status": "fail", "msg": str(e)}

    def send_voice(self, text: str) -> Optional[dict]:
        try:
            r = requests.post(
                f"{self.base_url}/voice", json={"text": text}, timeout=10
            )
            return r.json()
        except Exception as e:
            return {"status": "fail", "msg": str(e)}

    def activate_stt(self) -> Optional[dict]:
        """
        POST /stt/activate - 서버 STTEngine LISTENING 전환
        Returns: 응답 dict 또는 None (서버 미연결)
        """
        try:
            r = requests.post(
                f"{self.base_url}/stt/activate", timeout=3
            )
            return r.json()
        except requests.exceptions.ConnectionError:
            return None   # 서버 미연결
        except Exception as e:
            return {"status": "fail", "msg": str(e)}


# ════════════════════════════════════════════════════════════════════
# 스타일 헬퍼
# ════════════════════════════════════════════════════════════════════

def card_style(accent=C["cyan"]) -> str:
    return f"""
        QGroupBox {{
            background: {C['bg1']};
            border: 1px solid {C['border']};
            border-top: 2px solid {accent};
            border-radius: 10px;
            margin-top: 8px;
            padding: 12px;
            color: {C['text']};
            font-family: 'Rajdhani';
            font-size: 13px;
            font-weight: bold;
            letter-spacing: 1px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 4px;
            color: {accent};
            font-size: 11px;
            letter-spacing: 2px;
        }}
    """

def btn_style(color=C["cyan"], size=11) -> str:
    return f"""
        QPushButton {{
            background: {C['bg2']};
            border: 1px solid {C['border']};
            border-radius: 6px;
            color: {C['text2']};
            font-family: 'Rajdhani';
            font-size: {size}px;
            font-weight: 600;
            padding: 5px 10px;
            letter-spacing: 1px;
        }}
        QPushButton:hover {{
            border-color: {color};
            color: {color};
        }}
        QPushButton:pressed {{
            background: rgba(0,200,255,0.1);
        }}
    """

def label_val_style(color=C["cyan"], size=22) -> str:
    return f"""
        QLabel {{
            color: {color};
            font-family: 'JetBrains Mono', monospace;
            font-size: {size}px;
            font-weight: bold;
        }}
    """


# ════════════════════════════════════════════════════════════════════
# 디바이스 카드 위젯
# ════════════════════════════════════════════════════════════════════

class DeviceCard(QGroupBox):
    command_requested = pyqtSignal(dict)

    DEVICE_CONFIG = {
        "esp32_garage":   {"label": "🚗 차고",  "accent": C["yellow"], "caps": ["led","servo"]},
        "esp32_bathroom": {"label": "🚿 욕실",  "accent": C["cyan"],   "caps": ["led","temp"]},
        "esp32_bedroom":  {"label": "🛏️ 침실",  "accent": C["purple"], "caps": ["led","dht22","servo","seg7"]},
        "esp32_entrance": {"label": "🚪 현관",  "accent": C["green"],  "caps": ["led","servo"]},
    }

    def __init__(self, device_id: str, parent=None):
        cfg = self.DEVICE_CONFIG.get(device_id, {})
        super().__init__(cfg.get("label", device_id).upper(), parent)
        self.device_id = device_id
        self.accent    = cfg.get("accent", C["cyan"])
        self.caps      = cfg.get("caps", [])
        self.online    = False

        self.setStyleSheet(card_style(self.accent))
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # 온라인 상태 뱃지
        self.badge = QLabel("● OFFLINE")
        self.badge.setStyleSheet(f"color:{C['red']}; font-size:10px; font-family:'JetBrains Mono'; letter-spacing:1px;")
        layout.addWidget(self.badge)

        # ── LED 제어 ───────────────────────────────────────────────
        led_row = QHBoxLayout()
        btn_on  = QPushButton("💡 ON")
        btn_off = QPushButton("💡 OFF")
        for b in (btn_on, btn_off):
            b.setStyleSheet(btn_style(self.accent))
            b.setFixedHeight(30)
        btn_on.clicked.connect(lambda: self.command_requested.emit(
            {"cmd":"led","pin":2,"state":"on","device_id":self.device_id}))
        btn_off.clicked.connect(lambda: self.command_requested.emit(
            {"cmd":"led","pin":2,"state":"off","device_id":self.device_id}))
        led_row.addWidget(btn_on)
        led_row.addWidget(btn_off)
        layout.addLayout(led_row)

        # ── 서보 제어 ──────────────────────────────────────────────
        if "servo" in self.caps:
            servo_row = QHBoxLayout()
            btn_open  = QPushButton("🔓 열기")
            btn_close = QPushButton("🔒 닫기")
            for b in (btn_open, btn_close):
                b.setStyleSheet(btn_style(self.accent))
                b.setFixedHeight(30)
            btn_open.clicked.connect(lambda: self.command_requested.emit(
                {"cmd":"servo","pin":18,"angle":90,"device_id":self.device_id}))
            btn_close.clicked.connect(lambda: self.command_requested.emit(
                {"cmd":"servo","pin":18,"angle":0,"device_id":self.device_id}))
            servo_row.addWidget(btn_open)
            servo_row.addWidget(btn_close)
            layout.addLayout(servo_row)

            # 슬라이더
            slider_row = QHBoxLayout()
            self.servo_label = QLabel("0°")
            self.servo_label.setStyleSheet(
                f"color:{C['yellow']}; font-family:'JetBrains Mono'; font-size:11px;")
            self.servo_label.setFixedWidth(32)
            self.servo_slider = QSlider(Qt.Orientation.Horizontal)
            self.servo_slider.setRange(0, 180)
            self.servo_slider.setValue(0)
            self.servo_slider.setStyleSheet(f"""
                QSlider::groove:horizontal {{
                    background:{C['bg2']}; height:4px; border-radius:2px;
                }}
                QSlider::handle:horizontal {{
                    background:{C['yellow']}; width:14px; height:14px;
                    border-radius:7px; margin:-5px 0;
                }}
                QSlider::sub-page:horizontal {{
                    background:{self.accent}; border-radius:2px;
                }}
            """)
            self.servo_slider.valueChanged.connect(
                lambda v: self.servo_label.setText(f"{v}°"))
            self.servo_slider.sliderReleased.connect(lambda: self.command_requested.emit(
                {"cmd":"servo","pin":18,"angle":self.servo_slider.value(),"device_id":self.device_id}))
            slider_row.addWidget(QLabel("각도"))
            slider_row.addWidget(self.servo_slider)
            slider_row.addWidget(self.servo_label)
            layout.addLayout(slider_row)

        # ── 센서 표시 ──────────────────────────────────────────────
        if any(c in self.caps for c in ["dht22","temp"]):
            sensor_row = QHBoxLayout()

            self.temp_val = QLabel("--.-")
            self.temp_val.setStyleSheet(label_val_style(C["cyan"], 20))
            self.temp_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            temp_box = QVBoxLayout()
            temp_box.addWidget(self.temp_val)
            lbl_t = QLabel("°C")
            lbl_t.setStyleSheet(f"color:{C['text2']}; font-size:10px;")
            lbl_t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            temp_box.addWidget(lbl_t)
            sensor_row.addLayout(temp_box)

            if "dht22" in self.caps:
                self.humi_val = QLabel("--.-")
                self.humi_val.setStyleSheet(label_val_style(C["purple"], 20))
                self.humi_val.setAlignment(Qt.AlignmentFlag.AlignCenter)
                humi_box = QVBoxLayout()
                humi_box.addWidget(self.humi_val)
                lbl_h = QLabel("%")
                lbl_h.setStyleSheet(f"color:{C['text2']}; font-size:10px;")
                lbl_h.setAlignment(Qt.AlignmentFlag.AlignCenter)
                humi_box.addWidget(lbl_h)
                sensor_row.addLayout(humi_box)

            btn_query = QPushButton("📊 조회")
            btn_query.setStyleSheet(btn_style(self.accent))
            btn_query.setFixedHeight(30)
            sensor_qtype = "dht22" if "dht22" in self.caps else "ds18b20"
            btn_query.clicked.connect(lambda: self.command_requested.emit(
                {"cmd":"query","sensor":sensor_qtype,"device_id":self.device_id}))
            sensor_row.addWidget(btn_query)
            layout.addLayout(sensor_row)

        # ── 7세그먼트 (침실만) ─────────────────────────────────────
        if "seg7" in self.caps:
            self.seg7_display = QLabel("- - . -")
            self.seg7_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.seg7_display.setStyleSheet(f"""
                QLabel {{
                    background: #000;
                    border: 1px solid rgba(255,180,0,0.3);
                    border-radius: 6px;
                    padding: 6px;
                    color: {C['yellow']};
                    font-family: 'JetBrains Mono';
                    font-size: 20px;
                    font-weight: bold;
                    letter-spacing: 4px;
                }}
            """)
            layout.addWidget(self.seg7_display)

            seg7_row = QHBoxLayout()
            for mode, label in [("temp","온도"), ("humidity","습도"), ("off","OFF")]:
                b = QPushButton(label)
                b.setStyleSheet(btn_style(C["yellow"], 10))
                b.setFixedHeight(24)
                b.clicked.connect(lambda _, m=mode: self._send_seg7(m))
                seg7_row.addWidget(b)
            layout.addLayout(seg7_row)

        layout.addStretch()

    def _send_seg7(self, mode: str):
        val = 0.0
        if hasattr(self, 'temp_val') and mode == "temp":
            try: val = float(self.temp_val.text())
            except: val = 0.0
        elif hasattr(self, 'humi_val') and mode == "humidity":
            try: val = float(self.humi_val.text())
            except: val = 0.0
        self.command_requested.emit({
            "cmd":"seg7","pin_clk":22,"pin_dio":23,
            "mode":mode,"value":val,"device_id":self.device_id
        })

    def set_online(self, online: bool):
        self.online = online
        if online:
            self.badge.setText("● ONLINE")
            self.badge.setStyleSheet(
                f"color:{C['green']}; font-size:10px; font-family:'JetBrains Mono'; letter-spacing:1px;")
        else:
            self.badge.setText("● OFFLINE")
            self.badge.setStyleSheet(
                f"color:{C['red']}; font-size:10px; font-family:'JetBrains Mono'; letter-spacing:1px;")

    def update_sensor(self, temp=None, humidity=None):
        if temp is not None and hasattr(self, 'temp_val'):
            self.temp_val.setText(f"{temp:.1f}")
        if humidity is not None and hasattr(self, 'humi_val'):
            self.humi_val.setText(f"{humidity:.1f}")
        if hasattr(self, 'seg7_display') and temp is not None:
            self.seg7_display.setText(f"{temp:.1f}°C")

    def update_servo(self, angle: int):
        if hasattr(self, 'servo_slider'):
            self.servo_slider.setValue(angle)


# ════════════════════════════════════════════════════════════════════
# 메인 윈도우
# ════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Voice IoT Controller  |  Dashboard v0.2")
        self.resize(1280, 800)

        self._setup_theme()
        self._build_ui()
        self._setup_graph()
        self._setup_workers()

    # ── 테마 ────────────────────────────────────────────────────────

    def _setup_theme(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background: {C['bg0']};
                color: {C['text']};
                font-family: 'Rajdhani';
            }}
            QSplitter::handle {{ background: {C['border']}; }}
            QScrollBar:vertical {{
                background:{C['bg1']}; width:6px; border-radius:3px;
            }}
            QScrollBar::handle:vertical {{
                background:{C['border']}; border-radius:3px;
            }}
            QLineEdit {{
                background:{C['bg2']}; border:1px solid {C['border']};
                border-radius:6px; color:{C['text']};
                padding:6px 10px; font-size:13px;
                font-family:'Rajdhani';
            }}
            QLineEdit:focus {{ border-color:{C['cyan']}; }}
        """)

        pg.setConfigOptions(
            background=C["bg1"],
            foreground=C["text2"],
            antialias=True,
        )

    # ── UI 구성 ─────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(12, 10, 12, 10)

        # ── 헤더 ────────────────────────────────────────────────────
        header = self._make_header()
        root.addWidget(header)

        # ── 음성 입력 패널 ───────────────────────────────────────────
        voice = self._make_voice_panel()
        root.addWidget(voice)

        # ── 메인 스플리터 ────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        # 좌측: 디바이스 카드
        left = self._make_device_panel()
        splitter.addWidget(left)

        # 우측: 그래프 + 로그
        right = self._make_right_panel()
        splitter.addWidget(right)

        splitter.setSizes([820, 440])
        root.addWidget(splitter, 1)

        # ── 상태바 ───────────────────────────────────────────────────
        self._make_status_bar()

    def _make_header(self) -> QWidget:
        w = QFrame()
        w.setStyleSheet(f"""
            QFrame {{
                background:{C['bg1']}; border:1px solid {C['border']};
                border-radius:10px; padding:4px 0;
            }}
        """)
        h = QHBoxLayout(w)
        h.setContentsMargins(16, 6, 16, 6)

        title = QLabel("⚡ Voice IoT Controller")
        title.setStyleSheet(f"""
            color:#fff; font-size:18px; font-weight:700;
            letter-spacing:2px; font-family:'Rajdhani';
        """)
        h.addWidget(title)

        ver = QLabel("v0.2")
        ver.setStyleSheet(f"color:{C['text2']}; font-size:11px; font-family:'JetBrains Mono';")
        h.addWidget(ver)
        h.addStretch()

        self.ws_label = QLabel("● WS 연결 중")
        self.ws_label.setStyleSheet(f"color:{C['yellow']}; font-size:11px; font-family:'JetBrains Mono';")
        h.addWidget(self.ws_label)

        return w

    def _make_voice_panel(self) -> QWidget:
        # ── 외부 래퍼: cyan accent 상단 바 역할 ─────────────────────
        outer = QFrame()
        outer.setStyleSheet(f"""
            QFrame#voiceOuter {{
                background:{C['cyan']};
                border-radius:10px;
                padding:0px;
            }}
        """)
        outer.setObjectName("voiceOuter")
        outer.setFixedHeight(90)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 2, 0, 0)  # 상단 2px = cyan 라인
        outer_layout.setSpacing(0)

        # ── 내부 패널 ────────────────────────────────────────────────
        w = QFrame()
        w.setObjectName("voiceInner")
        w.setStyleSheet(f"""
            QFrame#voiceInner {{
                background:{C['bg1']};
                border:none;
                border-bottom-left-radius:10px;
                border-bottom-right-radius:10px;
                border-top-left-radius:0px;
                border-top-right-radius:0px;
            }}
        """)
        outer_layout.addWidget(w)

        h = QHBoxLayout(w)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(16)

        # 마이크 버튼
        self.mic_btn = QPushButton("🎙️")
        self.mic_btn.setFixedSize(56, 56)
        self.mic_btn.setStyleSheet(f"""
            QPushButton {{
                background:{C['bg2']}; border:2px solid {C['cyan']};
                border-radius:28px; font-size:22px;
            }}
            QPushButton:hover {{
                background:rgba(0,200,255,0.12);
                border-color:{C['cyan']};
            }}
        """)
        self.mic_btn.clicked.connect(self._on_mic_click)
        h.addWidget(self.mic_btn)

        # 음성 텍스트
        voice_col = QVBoxLayout()
        voice_col.setSpacing(2)

        lbl = QLabel("VOICE COMMAND")
        lbl.setStyleSheet(f"color:{C['text2']}; font-size:10px; letter-spacing:2px; border:none; background:transparent;")

        self.voice_text = QLabel('버튼을 누르거나 "자비스야"라고 말하세요')
        self.voice_text.setStyleSheet(f"color:{C['text2']}; font-size:13px; border:none; background:transparent;")

        self.voice_result = QLabel("")
        self.voice_result.setStyleSheet(
            f"color:{C['cyan']}; font-family:'JetBrains Mono'; font-size:11px; border:none; background:transparent;")

        # 웨이크 워드 / 버튼 모드 상태 뱃지
        self.voice_mode_label = QLabel("⬤ IDLE")
        self.voice_mode_label.setStyleSheet(
            f"color:{C['text2']}; font-family:'JetBrains Mono'; font-size:9px; border:none; background:transparent;")

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(lbl)
        top_row.addWidget(self.voice_mode_label)
        top_row.addStretch()

        voice_col.addLayout(top_row)
        voice_col.addWidget(self.voice_text)
        voice_col.addWidget(self.voice_result)
        h.addLayout(voice_col, 1)

        # 구분선
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"background:{C['border']}; border:none; max-width:1px;")
        h.addWidget(sep)

        # 수동 입력
        self.manual_input = QLineEdit()
        self.manual_input.setPlaceholderText("수동 명령 입력 (예: 침실 불 켜줘)")
        self.manual_input.setFixedWidth(260)
        self.manual_input.returnPressed.connect(self._on_send)

        send_btn = QPushButton("전송 ▶")
        send_btn.setStyleSheet(f"""
            QPushButton {{
                background:{C['cyan']}; color:#000;
                border:none; border-radius:6px;
                font-family:'Rajdhani'; font-weight:700;
                font-size:13px; padding:8px 16px;
            }}
            QPushButton:hover {{ background:#00E5FF; }}
        """)
        send_btn.clicked.connect(self._on_send)

        h.addWidget(self.manual_input)
        h.addWidget(send_btn)
        return outer

    def _make_device_panel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(8)

        lbl = QLabel("ESP32 DEVICES")
        lbl.setStyleSheet(f"color:{C['text2']}; font-size:10px; letter-spacing:3px;")
        layout.addWidget(lbl)

        grid = QGridLayout()
        grid.setSpacing(10)

        self.device_cards: dict[str, DeviceCard] = {}
        positions = [
            ("esp32_garage",   0, 0),
            ("esp32_bathroom", 0, 1),
            ("esp32_bedroom",  1, 0),
            ("esp32_entrance", 1, 1),
        ]
        for did, row, col in positions:
            card = DeviceCard(did)
            card.command_requested.connect(self._on_command)
            self.device_cards[did] = card
            grid.addWidget(card, row, col)

        layout.addLayout(grid)
        return w

    def _make_right_panel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(10)

        # 그래프
        graph_group = QGroupBox("SENSOR HISTORY")
        graph_group.setStyleSheet(card_style(C["cyan"]))
        graph_layout = QVBoxLayout(graph_group)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setMinimumHeight(200)
        self.plot_widget.setBackground(C["bg2"])
        self.plot_widget.showGrid(x=True, y=True, alpha=0.15)
        self.plot_widget.getAxis('left').setTextPen(C["text2"])
        self.plot_widget.getAxis('bottom').setTextPen(C["text2"])
        graph_layout.addWidget(self.plot_widget)
        layout.addWidget(graph_group)

        # 로그
        log_group = QGroupBox("COMMAND LOG")
        log_group.setStyleSheet(card_style(C["green"]))
        log_layout = QVBoxLayout(log_group)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(200)
        self.log_text.setStyleSheet(f"""
            QTextEdit {{
                background:{C['bg2']}; border:none;
                color:{C['text']}; font-family:'JetBrains Mono';
                font-size:11px;
            }}
        """)
        log_layout.addWidget(self.log_text)

        clear_btn = QPushButton("CLEAR LOG")
        clear_btn.setStyleSheet(btn_style(C["red"], 10))
        clear_btn.setFixedHeight(24)
        clear_btn.clicked.connect(self.log_text.clear)
        log_layout.addWidget(clear_btn)
        layout.addWidget(log_group, 1)

        return w

    def _make_status_bar(self):
        self.statusBar().setStyleSheet(f"""
            QStatusBar {{
                background:{C['bg1']}; border-top:1px solid {C['border']};
                color:{C['text2']}; font-family:'JetBrains Mono'; font-size:10px;
            }}
        """)
        self.statusBar().showMessage(
            f"  SERVER: {SERVER_HOST}:{SERVER_PORT}  |  TCP: 0 devices  |  WS: 연결 중"
        )

    # ── 그래프 초기화 ────────────────────────────────────────────────

    def _setup_graph(self):
        self._graph_x  = list(range(MAX_GRAPH_POINTS))
        self._temp_bed = deque([None] * MAX_GRAPH_POINTS, maxlen=MAX_GRAPH_POINTS)
        self._humi_bed = deque([None] * MAX_GRAPH_POINTS, maxlen=MAX_GRAPH_POINTS)
        self._temp_bat = deque([None] * MAX_GRAPH_POINTS, maxlen=MAX_GRAPH_POINTS)

        pen_temp = pg.mkPen(color=C["purple"], width=2)
        pen_humi = pg.mkPen(color=C["cyan"],   width=2)
        pen_bath = pg.mkPen(color=C["green"],  width=2)

        self._curve_temp = self.plot_widget.plot(
            pen=pen_temp, name="침실 온도(°C)", symbol=None)
        self._curve_humi = self.plot_widget.plot(
            pen=pen_humi, name="침실 습도(%)", symbol=None)
        self._curve_bath = self.plot_widget.plot(
            pen=pen_bath, name="욕실 온도(°C)", symbol=None)

        legend = self.plot_widget.addLegend(
            offset=(10, 10),
            labelTextColor=C["text2"],
        )

    def _update_graph(self):
        def clean(dq):
            vals = [v if v is not None else float('nan') for v in dq]
            return vals

        self._curve_temp.setData(self._graph_x, clean(self._temp_bed))
        self._curve_humi.setData(self._graph_x, clean(self._humi_bed))
        self._curve_bath.setData(self._graph_x, clean(self._temp_bat))

    # ── 워커 초기화 ─────────────────────────────────────────────────

    def _setup_workers(self):
        # WS 워커
        self.ws_worker = WSWorker(WS_URL)
        self.ws_worker.message.connect(self._on_ws_message)
        self.ws_worker.connected.connect(
            lambda: self.ws_label.setText(f"● WS 연결됨")
            or self.ws_label.setStyleSheet(
                f"color:{C['green']}; font-size:11px; font-family:'JetBrains Mono';")
        )
        self.ws_worker.disconnected.connect(
            lambda: self.ws_label.setText(f"● WS 끊김")
            or self.ws_label.setStyleSheet(
                f"color:{C['red']}; font-size:11px; font-family:'JetBrains Mono';")
        )
        self.ws_worker.start()

        # REST 폴러
        self.rest_poller = RestPoller(BASE_URL)
        self.rest_poller.devices_updated.connect(self._on_devices_updated)
        self.rest_poller.start()

        self._log("Voice IoT Dashboard 시작", "info")

    # ── 이벤트 핸들러 ────────────────────────────────────────────────

    def _on_ws_message(self, data: dict):
        t = data.get("type")

        if t == "device_list":
            self._on_devices_updated(data.get("devices", []))

        elif t == "sensor_data":
            did      = data.get("device_id")
            temp     = data.get("temp")
            humidity = data.get("humidity")
            card = self.device_cards.get(did)
            if card:
                card.update_sensor(temp, humidity)

            if did == "esp32_bedroom":
                if temp     is not None: self._temp_bed.append(temp)
                if humidity is not None: self._humi_bed.append(humidity)
                if temp     is None:     self._temp_bed.append(None)
                if humidity is None:     self._humi_bed.append(None)
            elif did == "esp32_bathroom":
                if temp is not None: self._temp_bat.append(temp)
                else:                self._temp_bat.append(None)
            self._update_graph()

        elif t == "device_update":
            did   = data.get("device_id")
            state = data.get("state", {})
            card  = self.device_cards.get(did)
            if card and "servo_18" in state:
                card.update_servo(state["servo_18"])

        elif t == "wake_detected":
            self.voice_mode_label.setText("🎙 LISTENING")
            self.voice_mode_label.setStyleSheet(
                f"color:{C['green']}; font-family:'JetBrains Mono'; font-size:9px; border:none; background:transparent;")
            self.voice_text.setText("명령을 말씀하세요...")
            self.voice_text.setStyleSheet(f"color:{C['text']}; font-size:13px; border:none; background:transparent;")
            self.mic_btn.setStyleSheet(self.mic_btn.styleSheet().replace(
                f"border:2px solid {C['cyan']}", f"border:2px solid {C['green']}"))
            self._log("웨이크 워드 감지 → 명령 대기", "info")

        elif t == "wake_timeout":
            self._set_voice_idle()
            self._log("명령 대기 시간 초과 → IDLE", "warn")

        elif t == "stt_result":
            text = data.get("text", "")
            # STT 인식 결과 표시 (처리 중 상태)
            self.voice_text.setText(f"🔄 {text}")
            self.voice_text.setStyleSheet(f"color:{C['cyan']}; font-size:13px; border:none; background:transparent;")
            self._set_voice_idle()
            self._log(f'🎙️ STT: "{text}"', "info")
            # 3초 후 자동 클리어
            QTimer.singleShot(3000, self._clear_voice_text)

        elif t == "cmd_result":
            status = data.get("status", "")
            msg    = data.get("msg",    "")
            color  = C["green"] if status == "ok" else C["red"] if status == "fail" else C["text2"]
            self.voice_result.setText(f"{'✅' if status=='ok' else '❌' if status=='fail' else '❓'} {msg}")
            self.voice_result.setStyleSheet(f"color:{color}; font-size:12px; border:none; background:transparent;")
            # 결과 표시 후 4초 뒤 클리어
            QTimer.singleShot(4000, self._clear_voice_result)

    def _on_devices_updated(self, devices: list):
        online_ids = {d["device_id"] for d in devices}
        for did, card in self.device_cards.items():
            card.set_online(did in online_ids)

        cnt = len(devices)
        self.statusBar().showMessage(
            f"  SERVER: {SERVER_HOST}:{SERVER_PORT}  |  "
            f"TCP: {cnt} device{'s' if cnt!=1 else ''}  |  "
            f"WS: {'연결됨' if self.ws_worker._running else '끊김'}"
        )

    def _on_command(self, data: dict):
        result = self.rest_poller.send_command(data)
        if result:
            status = result.get("status","?")
            msg    = result.get("msg","")
            self._log(
                f"→ {data['device_id']} {data['cmd']} | {msg}",
                "ok" if status == "ok" else "fail"
            )

    def _on_send(self):
        text = self.manual_input.text().strip()
        if not text: return
        self.manual_input.clear()
        self.voice_text.setText(text)
        self.voice_text.setStyleSheet(f"color:{C['text']}; font-size:14px;")
        result = self.rest_poller.send_voice(text)
        if result:
            self._log(f'🗣 "{text}" → {result.get("msg","")}',
                      "ok" if result.get("status") == "ok" else "fail")

    def _on_mic_click(self):
        """
        마이크 버튼 클릭 핸들러
        - 서버 연결 시: REST POST /stt/activate → STTEngine LISTENING 전환
        - 서버 미연결:  안내 메시지
        """
        if self.voice_mode_label.text() == "🎙 LISTENING":
            self._log("🎙️ 이미 LISTENING 상태입니다", "warn")
            return

        result = self.rest_poller.activate_stt()

        if result is None:
            self._log("🎙️ 서버 연결 없음 - 수동 입력을 사용하세요", "warn")
            return

        status = result.get("status", "fail")
        msg    = result.get("msg", "")

        if status == "ok":
            self._log(f"🎙️ {msg}", "info")
            # WS wake_detected 수신 전 UI 즉시 선반영
            self.voice_mode_label.setText("🎙 LISTENING")
            self.voice_mode_label.setStyleSheet(
                f"color:{C['green']}; font-family:'JetBrains Mono'; "
                f"font-size:9px; border:none; background:transparent;")
            self.voice_text.setText("명령을 말씀하세요...")
            self.voice_text.setStyleSheet(
                f"color:{C['text']}; font-size:13px; border:none; background:transparent;")
        elif status == "warn":
            # STT 비활성화 상태 → 수동 입력 안내
            self._log(f"⚠️ STT 비활성화 - 수동 입력 사용", "warn")
            self.voice_text.setText("⚠️ STT 비활성화 - 수동 입력 사용")
            self.voice_text.setStyleSheet(
                f"color:{C['yellow']}; font-size:13px; border:none; background:transparent;")
            QTimer.singleShot(3000, self._clear_voice_text)
        else:
            self._log(f"🎙️ 활성화 실패: {msg}", "fail")

    # ── 로그 ────────────────────────────────────────────────────────

    COLOR_MAP = {
        "ok":   C["green"],
        "fail": C["red"],
        "info": C["cyan"],
        "warn": C["yellow"],
    }

    def _log(self, msg: str, level: str = "info"):
        t   = datetime.now().strftime("%H:%M:%S")
        col = self.COLOR_MAP.get(level, C["text"])
        self.log_text.append(
            f'<span style="color:{C["text2"]};">[{t}]</span> '
            f'<span style="color:{col};">{msg}</span>'
        )
        # 최대 라인 수 제한
        doc = self.log_text.document()
        while doc.blockCount() > MAX_LOG_LINES:
            cursor = self.log_text.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    def _set_voice_idle(self):
        """음성 패널을 IDLE 상태로 초기화"""
        self.voice_mode_label.setText("⬤ IDLE")
        self.voice_mode_label.setStyleSheet(
            f"color:{C['text2']}; font-family:'JetBrains Mono'; font-size:9px; border:none; background:transparent;")
        self.mic_btn.setStyleSheet(f"""
            QPushButton {{
                background:{C['bg2']}; border:2px solid {C['cyan']};
                border-radius:28px; font-size:22px;
            }}
            QPushButton:hover {{
                background:rgba(0,200,255,0.12);
                border-color:{C['cyan']};
            }}
        """)

    def _clear_voice_text(self):
        """STT 텍스트 클리어 → 힌트 문구 복원"""
        self.voice_text.setText('버튼을 누르거나 "자비스야"라고 말하세요')
        self.voice_text.setStyleSheet(
            f"color:{C['text2']}; font-size:13px; border:none; background:transparent;")

    def _clear_voice_result(self):
        """명령 결과 텍스트 클리어"""
        self.voice_result.setText("")

    def closeEvent(self, event):
        self.ws_worker.stop()
        self.rest_poller.stop()
        super().closeEvent(event)


# ════════════════════════════════════════════════════════════════════
# 진입점
# ════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Voice IoT Controller")

    # 기본 다크 팔레트
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,       QColor(C["bg0"]))
    palette.setColor(QPalette.ColorRole.WindowText,   QColor(C["text"]))
    palette.setColor(QPalette.ColorRole.Base,         QColor(C["bg1"]))
    palette.setColor(QPalette.ColorRole.AlternateBase,QColor(C["bg2"]))
    palette.setColor(QPalette.ColorRole.Text,         QColor(C["text"]))
    palette.setColor(QPalette.ColorRole.Button,       QColor(C["bg2"]))
    palette.setColor(QPalette.ColorRole.ButtonText,   QColor(C["text"]))
    palette.setColor(QPalette.ColorRole.Highlight,    QColor(C["cyan"]))
    app.setPalette(palette)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
