#!/usr/bin/env python3
"""
YouTube -> MP3 Relay Server
============================
ESP32가 이 서버에 YouTube URL을 보내면, yt-dlp + ffmpeg로 MP3 스트림을 생성하여 전달합니다.

사용법:
    pip install flask yt-dlp
    # ffmpeg 설치 필요: sudo apt install ffmpeg
    python relay_server.py

설정은 프로젝트 루트의 .env 파일에서 읽습니다.
"""

import subprocess
import threading
import os
import shutil
from urllib.parse import unquote
from flask import Flask, request, Response, jsonify

app = Flask(__name__)

# ===== 경로 설정 =====
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
ENV_FILE = os.path.join(PROJECT_DIR, ".env")


# ===== .env 파일 로드 =====
def load_env(env_path):
    """프로젝트 루트의 .env 파일에서 환경변수 로드"""
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not os.environ.get(key):
                os.environ[key] = value


load_env(ENV_FILE)

# ===== 환경변수에서 설정 읽기 =====
RELAY_PORT = int(os.environ.get("RELAY_PORT", "8080"))
RELAY_ALLOWED_IPS = os.environ.get("RELAY_ALLOWED_IPS", "")

# ===== 허용 IP =====
def parse_allowed_ips(raw):
    ips = {"127.0.0.1"}
    if raw:
        for ip in raw.split(","):
            ip = ip.strip()
            if ip:
                ips.add(ip)
    return ips

ALLOWED_IPS = parse_allowed_ips(RELAY_ALLOWED_IPS)

# ===== yt-dlp 경로 탐색 =====
VENV_YT_DLP = os.path.join(PROJECT_DIR, ".relay-venv", "bin", "yt-dlp")

if os.path.isfile(VENV_YT_DLP):
    YT_DLP = VENV_YT_DLP
elif shutil.which("yt-dlp"):
    YT_DLP = shutil.which("yt-dlp")
else:
    YT_DLP = "yt-dlp"

active_process = None
process_lock = threading.Lock()


@app.before_request
def check_ip():
    """허용된 IP만 접근 가능"""
    if request.remote_addr not in ALLOWED_IPS:
        return jsonify({"error": "forbidden"}), 403


def kill_active():
    """기존 스트리밍 프로세스 종료"""
    global active_process
    with process_lock:
        if active_process and active_process.poll() is None:
            active_process.kill()
            active_process.wait()
            active_process = None


def get_audio_url(youtube_url):
    """yt-dlp로 오디오 스트림 URL 추출"""
    try:
        result = subprocess.run(
            [YT_DLP, "-f", "bestaudio", "-g", "--no-warnings", "--no-playlist", youtube_url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return result.stdout.strip()
        print(f"[yt-dlp error] {result.stderr}")
        return None
    except Exception as e:
        print(f"[yt-dlp exception] {e}")
        return None


def get_video_title(youtube_url):
    """yt-dlp로 영상 제목 추출"""
    try:
        result = subprocess.run(
            [YT_DLP, "--get-title", "--no-warnings", youtube_url],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return "Unknown"
    except Exception:
        return "Unknown"


@app.route("/")
def index():
    return jsonify({"status": "relay server running"})


@app.route("/stream")
def stream():
    """YouTube URL을 받아 MP3 스트림으로 변환하여 전달"""
    global active_process

    url = request.args.get("url")
    if not url:
        return jsonify({"error": "no url parameter"}), 400

    url = unquote(url)
    print(f"[Stream] Requested: {url}")

    audio_url = get_audio_url(url)
    if not audio_url:
        return jsonify({"error": "failed to get audio URL"}), 500

    print("[Stream] Audio URL obtained, starting ffmpeg...")

    kill_active()

    def generate():
        global active_process
        try:
            proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5",
                    "-i", audio_url,
                    "-vn",
                    "-acodec", "libmp3lame",
                    "-b:a", "128k",
                    "-ar", "44100",
                    "-ac", "2",
                    "-flush_packets", "1",
                    "-f", "mp3",
                    "-"
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=16384
            )

            with process_lock:
                active_process = proc

            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk

            proc.wait()
        except GeneratorExit:
            proc.kill()
            proc.wait()
        except Exception as e:
            print(f"[Stream error] {e}")

    return Response(generate(), mimetype="audio/mpeg")


@app.route("/info")
def info():
    """YouTube 영상 정보 조회"""
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "no url"}), 400

    title = get_video_title(url)
    return jsonify({"title": title})


@app.route("/stop", methods=["POST"])
def stop():
    """현재 스트리밍 중지"""
    kill_active()
    return jsonify({"status": "stopped"})


if __name__ == "__main__":
    print("=" * 50)
    print("YouTube -> MP3 Relay Server")
    print(f"  yt-dlp : {YT_DLP}")
    print(f"  port   : {RELAY_PORT}")
    print(f"  allowed: {ALLOWED_IPS}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=RELAY_PORT, threaded=True)
