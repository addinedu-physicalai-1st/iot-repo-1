"""
Voice IoT Controller — Pipeline Performance Monitor
=====================================================
웨이크워드 → STT → LLM → TTS 각 단계 소요 시간을 실시간 모니터링.

사용법:
  1) Voice IoT Controller 서버 로그를 파싱하는 모드:
     python pipeline_monitor.py --log ~/dev_ws/voice_iot_controller/logs/server.log

  2) WebSocket으로 서버에 직접 연결하는 모드:
     python pipeline_monitor.py --ws ws://localhost:8000/ws

  3) 수동 기록 모드 (테스트용):
     python pipeline_monitor.py --manual

출력:
  - 터미널 실시간 대시보드
  - CSV 기록 (pipeline_metrics.csv)
  - 통계 요약 (평균, 중위값, p95, 최소, 최대)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════
#  데이터 구조
# ═══════════════════════════════════════════════

@dataclass
class PipelineRecord:
    """한 번의 음성 명령 파이프라인 기록"""
    timestamp: str = ""
    command_text: str = ""
    wake_ms: float = 0.0      # 웨이크워드 감지 시간
    stt_ms: float = 0.0       # STT 추론 시간
    llm_ms: float = 0.0       # LLM 파싱 시간
    tts_ms: float = 0.0       # TTS 생성 시간
    total_ms: float = 0.0     # 전체 파이프라인
    stt_model: str = ""       # whisper 모델 (tiny/base/small/medium)
    llm_model: str = ""       # ollama 모델
    success: bool = True

    def calc_total(self):
        self.total_ms = self.wake_ms + self.stt_ms + self.llm_ms + self.tts_ms


# ═══════════════════════════════════════════════
#  통계 계산
# ═══════════════════════════════════════════════

def calc_stats(values: list) -> dict:
    """평균, 중위값, p95, 최소, 최대 계산"""
    if not values:
        return {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0}
    
    sorted_v = sorted(values)
    n = len(sorted_v)
    p95_idx = min(int(n * 0.95), n - 1)
    
    return {
        "avg": sum(sorted_v) / n,
        "median": sorted_v[n // 2],
        "p95": sorted_v[p95_idx],
        "min": sorted_v[0],
        "max": sorted_v[-1],
        "count": n,
    }


# ═══════════════════════════════════════════════
#  터미널 UI
# ═══════════════════════════════════════════════

CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
WHITE = "\033[97m"

def bar(value_ms: float, max_ms: float = 5000, width: int = 30) -> str:
    """수평 바 차트"""
    ratio = min(value_ms / max_ms, 1.0) if max_ms > 0 else 0
    filled = int(ratio * width)
    
    if value_ms < 500:
        color = GREEN
    elif value_ms < 1500:
        color = YELLOW
    else:
        color = RED
    
    return f"{color}{'█' * filled}{'░' * (width - filled)}{RESET}"


def format_ms(ms: float) -> str:
    """밀리초를 보기 좋게 포맷"""
    if ms < 1:
        return f"{DIM}  —  {RESET}"
    elif ms < 1000:
        return f"{GREEN}{ms:>7.0f}ms{RESET}"
    elif ms < 2000:
        return f"{YELLOW}{ms:>7.0f}ms{RESET}"
    else:
        return f"{RED}{ms:>7.0f}ms{RESET}"


def render_dashboard(records: deque, stats_window: int = 50):
    """터미널 대시보드 렌더링"""
    print(CLEAR, end="")
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║  🎙️  Voice IoT Pipeline Monitor                  {DIM}{now}{RESET}  {CYAN}║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════════════╝{RESET}")
    print()
    
    if not records:
        print(f"  {DIM}아직 기록이 없습니다. 음성 명령을 실행하세요...{RESET}")
        return
    
    # ── 최근 기록 (최대 10개) ──
    recent = list(records)[-10:]
    
    print(f"  {BOLD}{WHITE}[ 최근 파이프라인 기록 ]{RESET}")
    print(f"  {DIM}{'#':>3}  {'명령':^20}  {'Wake':>8}  {'STT':>8}  {'LLM':>8}  {'TTS':>8}  {'Total':>8}  {'Bar'}{RESET}")
    print(f"  {DIM}{'─' * 95}{RESET}")
    
    for i, r in enumerate(recent, 1):
        cmd = (r.command_text[:18] + "..") if len(r.command_text) > 20 else r.command_text
        status = f"{GREEN}✓{RESET}" if r.success else f"{RED}✗{RESET}"
        print(
            f"  {status}{i:>2}  {cmd:<20}  "
            f"{format_ms(r.wake_ms)}  {format_ms(r.stt_ms)}  "
            f"{format_ms(r.llm_ms)}  {format_ms(r.tts_ms)}  "
            f"{format_ms(r.total_ms)}  {bar(r.total_ms)}"
        )
    
    print()
    
    # ── 통계 (최근 N개 기준) ──
    window = list(records)[-stats_window:]
    stages = {
        "Wake Word": [r.wake_ms for r in window if r.wake_ms > 0],
        "STT":       [r.stt_ms for r in window if r.stt_ms > 0],
        "LLM":       [r.llm_ms for r in window if r.llm_ms > 0],
        "TTS":       [r.tts_ms for r in window if r.tts_ms > 0],
        "Total":     [r.total_ms for r in window if r.total_ms > 0],
    }
    
    colors = {
        "Wake Word": MAGENTA,
        "STT": CYAN,
        "LLM": YELLOW,
        "TTS": GREEN,
        "Total": WHITE,
    }
    
    print(f"  {BOLD}{WHITE}[ 통계 — 최근 {len(window)}건 ]{RESET}")
    print(f"  {DIM}{'Stage':>10}  {'Avg':>8}  {'Median':>8}  {'P95':>8}  {'Min':>8}  {'Max':>8}  {'Count':>6}{RESET}")
    print(f"  {DIM}{'─' * 70}{RESET}")
    
    for stage, values in stages.items():
        s = calc_stats(values)
        c = colors.get(stage, WHITE)
        if s["count"] == 0:
            print(f"  {c}{stage:>10}{RESET}  {DIM}{'데이터 없음':^50}{RESET}")
        else:
            print(
                f"  {c}{BOLD}{stage:>10}{RESET}  "
                f"{s['avg']:>7.0f}ms  {s['median']:>7.0f}ms  "
                f"{s['p95']:>7.0f}ms  {s['min']:>7.0f}ms  "
                f"{s['max']:>7.0f}ms  {s['count']:>5}건"
            )
    
    print()
    
    # ── 성능 등급 ──
    total_stats = calc_stats(stages.get("Total", []))
    avg = total_stats["avg"]
    if avg > 0:
        if avg < 1500:
            grade = f"{GREEN}⚡ EXCELLENT (<1.5s){RESET}"
        elif avg < 2500:
            grade = f"{YELLOW}✓  GOOD (1.5~2.5s){RESET}"
        elif avg < 4000:
            grade = f"{YELLOW}△  FAIR (2.5~4.0s){RESET}"
        else:
            grade = f"{RED}✗  SLOW (>4.0s){RESET}"
        print(f"  {BOLD}Performance Grade: {grade}")
    
    print()
    print(f"  {DIM}[q] 종료  [c] CSV 내보내기  [r] 초기화  [Enter] 수동 입력{RESET}")


# ═══════════════════════════════════════════════
#  CSV 저장/로드
# ═══════════════════════════════════════════════

CSV_FIELDS = [
    "timestamp", "command_text", "wake_ms", "stt_ms",
    "llm_ms", "tts_ms", "total_ms", "stt_model", "llm_model", "success"
]

def save_csv(records: deque, filepath: str):
    """CSV 파일로 저장"""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in records:
            writer.writerow(asdict(r))
    print(f"\n  {GREEN}✓ {len(records)}건 저장됨: {filepath}{RESET}")


def load_csv(filepath: str) -> deque:
    """CSV 파일에서 로드"""
    records = deque(maxlen=1000)
    if not os.path.exists(filepath):
        return records
    
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            r = PipelineRecord(
                timestamp=row.get("timestamp", ""),
                command_text=row.get("command_text", ""),
                wake_ms=float(row.get("wake_ms", 0)),
                stt_ms=float(row.get("stt_ms", 0)),
                llm_ms=float(row.get("llm_ms", 0)),
                tts_ms=float(row.get("tts_ms", 0)),
                total_ms=float(row.get("total_ms", 0)),
                stt_model=row.get("stt_model", ""),
                llm_model=row.get("llm_model", ""),
                success=row.get("success", "True").lower() == "true",
            )
            records.append(r)
    return records


# ═══════════════════════════════════════════════
#  로그 파싱 모드
# ═══════════════════════════════════════════════

# 서버 로그 패턴 (stt_engine.py, llm_engine.py 등에서 출력하는 형식)
LOG_PATTERNS = {
    "wake": re.compile(r"wake.*?detected.*?(\d+\.?\d*)ms", re.IGNORECASE),
    "stt": re.compile(r"stt.*?(?:inference|transcri).*?(\d+\.?\d*)ms", re.IGNORECASE),
    "llm": re.compile(r"llm.*?(?:parse|infer).*?(\d+\.?\d*)ms", re.IGNORECASE),
    "tts": re.compile(r"tts.*?(?:generat|speak|synthes).*?(\d+\.?\d*)ms", re.IGNORECASE),
    "text": re.compile(r"stt.*?result.*?[\"'](.+?)[\"']", re.IGNORECASE),
    "total": re.compile(r"pipeline.*?total.*?(\d+\.?\d*)ms", re.IGNORECASE),
}


def parse_log_line(line: str, current: PipelineRecord) -> Optional[PipelineRecord]:
    """서버 로그 한 줄을 파싱하여 PipelineRecord 업데이트"""
    
    for key, pattern in LOG_PATTERNS.items():
        match = pattern.search(line)
        if not match:
            continue
        
        if key == "wake":
            current.wake_ms = float(match.group(1))
        elif key == "stt":
            current.stt_ms = float(match.group(1))
        elif key == "llm":
            current.llm_ms = float(match.group(1))
        elif key == "tts":
            current.tts_ms = float(match.group(1))
        elif key == "text":
            current.command_text = match.group(1)
        elif key == "total":
            current.total_ms = float(match.group(1))
            current.timestamp = datetime.now().isoformat()
            current.calc_total()
            completed = current
            return completed
    
    # STT + LLM 까지 완료되면 하나의 레코드로 간주
    if current.stt_ms > 0 and current.llm_ms > 0 and "cmd_result" in line.lower():
        current.timestamp = datetime.now().isoformat()
        current.calc_total()
        return current
    
    return None


def follow_log(filepath: str, records: deque, csv_path: str):
    """로그 파일을 tail -f 방식으로 추적"""
    print(f"  {CYAN}📄 로그 파일 모니터링: {filepath}{RESET}")
    print(f"  {DIM}Ctrl+C로 종료{RESET}\n")
    
    current = PipelineRecord()
    
    with open(filepath, "r") as f:
        f.seek(0, 2)  # 파일 끝으로 이동
        
        while True:
            line = f.readline()
            if not line:
                render_dashboard(records)
                time.sleep(0.5)
                continue
            
            completed = parse_log_line(line.strip(), current)
            if completed:
                records.append(completed)
                current = PipelineRecord()
                
                # 자동 저장 (10건마다)
                if len(records) % 10 == 0:
                    save_csv(records, csv_path)


# ═══════════════════════════════════════════════
#  WebSocket 모드
# ═══════════════════════════════════════════════

def ws_monitor(url: str, records: deque, csv_path: str):
    """WebSocket으로 서버 이벤트 수신"""
    try:
        import websocket
    except ImportError:
        print(f"  {RED}websocket-client 필요: pip install websocket-client{RESET}")
        sys.exit(1)
    
    print(f"  {CYAN}🌐 WebSocket 연결: {url}{RESET}")
    
    current = PipelineRecord()
    
    def on_message(ws, message):
        nonlocal current
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")
            
            if msg_type == "wake_detected":
                current = PipelineRecord()
                current.wake_ms = data.get("latency_ms", 0)
            
            elif msg_type == "stt_result":
                current.stt_ms = data.get("latency_ms", 0)
                current.command_text = data.get("text", "")
                current.stt_model = data.get("model", "")
            
            elif msg_type == "llm_result":
                current.llm_ms = data.get("latency_ms", 0)
                current.llm_model = data.get("model", "")
            
            elif msg_type == "tts_done":
                current.tts_ms = data.get("latency_ms", 0)
            
            elif msg_type == "cmd_result":
                current.timestamp = datetime.now().isoformat()
                current.success = data.get("status") == "ok"
                if data.get("pipeline_ms"):
                    current.total_ms = float(data["pipeline_ms"])
                else:
                    current.calc_total()
                records.append(current)
                current = PipelineRecord()
                
                if len(records) % 10 == 0:
                    save_csv(records, csv_path)
            
            # 서버가 timing 정보를 한번에 보내는 경우
            elif msg_type == "pipeline_timing":
                r = PipelineRecord(
                    timestamp=datetime.now().isoformat(),
                    command_text=data.get("text", ""),
                    wake_ms=float(data.get("wake_ms", 0)),
                    stt_ms=float(data.get("stt_ms", 0)),
                    llm_ms=float(data.get("llm_ms", 0)),
                    tts_ms=float(data.get("tts_ms", 0)),
                    total_ms=float(data.get("total_ms", 0)),
                    stt_model=data.get("stt_model", ""),
                    llm_model=data.get("llm_model", ""),
                    success=data.get("success", True),
                )
                if r.total_ms == 0:
                    r.calc_total()
                records.append(r)
        
        except (json.JSONDecodeError, KeyError):
            pass
        
        render_dashboard(records)
    
    def on_error(ws, error):
        print(f"  {RED}WebSocket 에러: {error}{RESET}")
    
    def on_close(ws, code, msg):
        print(f"\n  {YELLOW}WebSocket 연결 종료{RESET}")
        save_csv(records, csv_path)
    
    def on_open(ws):
        print(f"  {GREEN}✓ WebSocket 연결 성공{RESET}")
    
    ws = websocket.WebSocketApp(
        url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open,
    )
    ws.run_forever()


# ═══════════════════════════════════════════════
#  수동 입력 모드
# ═══════════════════════════════════════════════

def manual_mode(records: deque, csv_path: str):
    """수동으로 측정값 입력"""
    print(f"  {CYAN}✏️  수동 입력 모드{RESET}")
    print(f"  {DIM}각 단계의 소요 시간(ms)을 입력하세요. Enter=건너뛰기, q=종료{RESET}\n")
    
    render_dashboard(records)
    
    while True:
        try:
            cmd = input(f"\n  {BOLD}명령어 입력 (q=종료, c=CSV저장, r=초기화): {RESET}").strip()
            
            if cmd.lower() == "q":
                save_csv(records, csv_path)
                print(f"  {GREEN}종료합니다.{RESET}")
                break
            elif cmd.lower() == "c":
                save_csv(records, csv_path)
                continue
            elif cmd.lower() == "r":
                records.clear()
                render_dashboard(records)
                continue
            
            r = PipelineRecord()
            r.timestamp = datetime.now().isoformat()
            r.command_text = input(f"  {DIM}음성 명령 텍스트: {RESET}").strip() or "테스트"
            
            wake = input(f"  {MAGENTA}Wake Word (ms): {RESET}").strip()
            r.wake_ms = float(wake) if wake else 0
            
            stt = input(f"  {CYAN}STT (ms): {RESET}").strip()
            r.stt_ms = float(stt) if stt else 0
            
            llm = input(f"  {YELLOW}LLM (ms): {RESET}").strip()
            r.llm_ms = float(llm) if llm else 0
            
            tts = input(f"  {GREEN}TTS (ms): {RESET}").strip()
            r.tts_ms = float(tts) if tts else 0
            
            r.stt_model = input(f"  {DIM}STT 모델 (기본: small): {RESET}").strip() or "small"
            r.llm_model = input(f"  {DIM}LLM 모델 (기본: qwen2.5:7b): {RESET}").strip() or "qwen2.5:7b"
            
            success = input(f"  {DIM}성공? (Y/n): {RESET}").strip().lower()
            r.success = success != "n"
            
            r.calc_total()
            records.append(r)
            
            render_dashboard(records)
        
        except KeyboardInterrupt:
            print()
            save_csv(records, csv_path)
            break
        except ValueError as e:
            print(f"  {RED}입력 오류: {e}{RESET}")


# ═══════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Voice IoT Pipeline Performance Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
사용 예시:
  python pipeline_monitor.py --manual
  python pipeline_monitor.py --log ~/dev_ws/voice_iot_controller/logs/server.log
  python pipeline_monitor.py --ws ws://localhost:8000/ws
  python pipeline_monitor.py --csv pipeline_metrics.csv --stats
        """
    )
    
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--log", metavar="FILE", help="서버 로그 파일 모니터링 (tail -f)")
    mode.add_argument("--ws", metavar="URL", help="WebSocket 서버에 연결")
    mode.add_argument("--manual", action="store_true", help="수동 입력 모드")
    mode.add_argument("--stats", action="store_true", help="기존 CSV에서 통계만 출력")
    
    parser.add_argument("--csv", default="pipeline_metrics.csv", help="CSV 파일 경로 (기본: pipeline_metrics.csv)")
    parser.add_argument("--window", type=int, default=50, help="통계 윈도우 크기 (기본: 50)")
    
    args = parser.parse_args()
    
    # 기존 CSV 로드
    records = load_csv(args.csv)
    if records:
        print(f"  {GREEN}✓ 기존 기록 {len(records)}건 로드됨{RESET}")
    
    try:
        if args.stats:
            render_dashboard(records, args.window)
        elif args.manual:
            manual_mode(records, args.csv)
        elif args.log:
            follow_log(args.log, records, args.csv)
        elif args.ws:
            ws_monitor(args.ws, records, args.csv)
    except KeyboardInterrupt:
        print(f"\n  {YELLOW}종료합니다...{RESET}")
        save_csv(records, args.csv)


if __name__ == "__main__":
    main()
