"""
tests/test_tts_debug.py
=======================
TTS 엔진 독립 디버그 스크립트

단계별로 문제 위치를 파악합니다:
  Step 1. 패키지 설치 확인
  Step 2. 모델 파일 존재 확인
  Step 3. TTSEngine 초기화 확인
  Step 4. 실제 발화 테스트
  Step 5. main.py tts_engine 연동 확인 (서버 실행 중일 때)

실행:
  cd ~/dev_ws/voice_iot_controller
  source venv/bin/activate
  python3 tests/test_tts_debug.py
"""

import asyncio
import sys
import os

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────
# Step 1. 패키지 설치 확인
# ─────────────────────────────────────────────

def check_packages():
    print("\n" + "="*50)
    print("Step 1. 패키지 설치 확인")
    print("="*50)

    packages = {
        "kokoro_onnx":  "kokoro-onnx",
        "sounddevice":  "sounddevice",
        "soundfile":    "soundfile",
        "elevenlabs":   "elevenlabs (선택)",
    }

    all_ok = True
    for module, pkg in packages.items():
        try:
            __import__(module)
            print(f"  ✅ {pkg}")
        except ImportError:
            if "선택" in pkg:
                print(f"  ⚠️  {pkg} — 미설치 (ElevenLabs 사용 시 필요)")
            else:
                print(f"  ❌ {pkg} — pip install {pkg.split()[0]}")
                all_ok = False

    return all_ok


# ─────────────────────────────────────────────
# Step 2. 모델 파일 존재 확인
# ─────────────────────────────────────────────

def check_model_files():
    print("\n" + "="*50)
    print("Step 2. Kokoro 모델 파일 확인")
    print("="*50)

    files = {
        "models/kokoro-v0_19.onnx": "Kokoro ONNX 모델 (~300MB)",
        "models/voices.bin":        "Kokoro 목소리 파일",
    }

    all_ok = True
    for path, desc in files.items():
        size = ""
        if os.path.exists(path):
            mb = os.path.getsize(path) / (1024 * 1024)
            size = f" ({mb:.1f} MB)"
            print(f"  ✅ {path}{size} — {desc}")
        else:
            print(f"  ❌ {path} — 없음")
            print(f"     → wget https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/{os.path.basename(path)}")
            all_ok = False

    return all_ok


# ─────────────────────────────────────────────
# Step 3. TTSEngine import 확인
# ─────────────────────────────────────────────

def check_tts_engine_import():
    print("\n" + "="*50)
    print("Step 3. TTSEngine import 확인")
    print("="*50)

    try:
        from server.tts_engine import TTSEngine
        print("  ✅ server/tts_engine.py import 성공")
        return True
    except ImportError as e:
        print(f"  ❌ import 실패: {e}")
        print("     → server/tts_engine.py 파일이 존재하는지 확인하세요")
        return False
    except Exception as e:
        print(f"  ❌ 예외 발생: {e}")
        return False


# ─────────────────────────────────────────────
# Step 4. TTSEngine 초기화 테스트
# ─────────────────────────────────────────────

async def check_tts_init():
    print("\n" + "="*50)
    print("Step 4. TTSEngine 초기화 테스트")
    print("="*50)

    try:
        from server.tts_engine import TTSEngine

        engine = TTSEngine(
            provider    = "kokoro",
            model_path  = "models/kokoro-v0_19.onnx",
            voices_path = "models/voices.bin",
            voice       = "af_heart",
            speed       = 1.0,
            lang        = "ko",
        )
        print("  TTSEngine 인스턴스 생성... ✅")

        print("  초기화 중 (모델 로드)...")
        ok = await engine.initialize()

        if ok:
            print(f"  ✅ 초기화 성공 | provider={engine.get_provider()}")
        else:
            print("  ❌ 초기화 실패 — 위 에러 메시지 확인")

        return ok, engine

    except Exception as e:
        print(f"  ❌ 예외 발생: {e}")
        import traceback
        traceback.print_exc()
        return False, None


# ─────────────────────────────────────────────
# Step 5. 실제 발화 테스트
# ─────────────────────────────────────────────

async def check_tts_speak(engine):
    print("\n" + "="*50)
    print("Step 5. 실제 발화 테스트")
    print("="*50)

    test_texts = [
        "안녕하세요.",
        "침실 전등을 켰어요.",
        "오늘 날씨는 잘 모르지만, 창문으로 확인해보세요!",
    ]

    import time
    for text in test_texts:
        print(f"\n  발화: '{text}'")
        t0 = time.time()
        try:
            await engine.speak(text)
            elapsed = (time.time() - t0) * 1000
            print(f"  ✅ 완료 ({elapsed:.0f}ms)")
        except Exception as e:
            print(f"  ❌ 실패: {e}")
            import traceback
            traceback.print_exc()


# ─────────────────────────────────────────────
# Step 6. settings.yaml tts 블록 확인
# ─────────────────────────────────────────────

def check_settings():
    print("\n" + "="*50)
    print("Step 6. settings.yaml tts 블록 확인")
    print("="*50)

    try:
        import yaml
        with open("config/settings.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        tts = cfg.get("tts")
        if not tts:
            print("  ❌ settings.yaml에 'tts' 블록 없음")
            print("     → settings.yaml v0.8로 교체 필요")
            return False

        provider = tts.get("provider", "")
        print(f"  ✅ tts 블록 존재 | provider={provider}")

        tts_cfg = tts.get(provider, {})
        if provider == "kokoro":
            mp = tts_cfg.get("model_path", "")
            vp = tts_cfg.get("voices_path", "")
            print(f"     model_path:  {mp} {'✅' if os.path.exists(mp) else '❌ 파일 없음'}")
            print(f"     voices_path: {vp} {'✅' if os.path.exists(vp) else '❌ 파일 없음'}")
        elif provider == "elevenlabs":
            ak = tts_cfg.get("api_key", "")
            vi = tts_cfg.get("voice_id", "")
            print(f"     api_key:  {'✅ 설정됨' if ak else '❌ 비어있음'}")
            print(f"     voice_id: {'✅ 설정됨' if vi else '❌ 비어있음'}")

        return True

    except FileNotFoundError:
        print("  ❌ config/settings.yaml 없음")
        return False
    except Exception as e:
        print(f"  ❌ 예외: {e}")
        return False


# ─────────────────────────────────────────────
# Step 7. main.py TTS 연동 확인
# ─────────────────────────────────────────────

def check_main_py():
    print("\n" + "="*50)
    print("Step 7. main.py TTS 연동 확인")
    print("="*50)

    try:
        with open("server/main.py", encoding="utf-8") as f:
            content = f.read()

        checks = {
            "from server.tts_engine import TTSEngine": "TTSEngine import",
            "tts_engine":                              "tts_engine 인스턴스",
            "tts_engine.initialize()":                 "TTS 초기화 호출",
            "tts_engine.speak":                        "speak() 호출",
            "DISABLE_TTS":                             "DISABLE_TTS 환경변수",
            "v0.5":                                    "main.py 버전 v0.5",
        }

        all_ok = True
        for keyword, desc in checks.items():
            if keyword in content:
                print(f"  ✅ {desc}")
            else:
                print(f"  ❌ {desc} — 없음 (main.py v0.5로 교체 필요)")
                all_ok = False

        return all_ok

    except FileNotFoundError:
        print("  ❌ server/main.py 없음")
        return False


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

async def main():
    print("\n" + "="*50)
    print("🔊 TTS 디버그 스크립트")
    print("="*50)

    # Step 1~3: 동기 확인
    pkg_ok     = check_packages()
    model_ok   = check_model_files()
    import_ok  = check_tts_engine_import()
    settings_ok = check_settings()
    main_ok    = check_main_py()

    print("\n" + "="*50)
    print("중간 점검")
    print("="*50)
    print(f"  패키지:    {'✅' if pkg_ok     else '❌'}")
    print(f"  모델파일:  {'✅' if model_ok   else '❌'}")
    print(f"  import:    {'✅' if import_ok  else '❌'}")
    print(f"  settings:  {'✅' if settings_ok else '❌'}")
    print(f"  main.py:   {'✅' if main_ok    else '❌'}")

    if not (pkg_ok and model_ok and import_ok):
        print("\n❌ 위 항목을 먼저 해결 후 재실행하세요.")
        return

    # Step 4~5: 비동기 초기화 + 발화
    init_ok, engine = await check_tts_init()
    if init_ok and engine:
        await check_tts_speak(engine)

    print("\n" + "="*50)
    print("디버그 완료")
    print("="*50)


if __name__ == "__main__":
    asyncio.run(main())
