# 대용량 모델 파일 (Git 미포함)

이 디렉터리의 대용량 바이너리(ONNX, Porcupine 등)는 **Git에 올리지 않습니다**.  
아래 스크립트나 수동 다운로드로 `models/` 폴더를 채운 뒤 서버를 실행하세요.

## 한 번에 받기

```bash
./scripts/download_models.sh
```

실행 전 `scripts/download_models.sh`에 필요한 URL/경로가 설정돼 있는지 확인하세요.

## 수동 준비

| 파일 | 용도 | 획득 방법 |
|------|------|-----------|
| `models/kokoro-v0_19.onnx` | TTS (Kokoro) | [아래 Kokoro](#kokoro-tts) 참고 |
| `models/voices.bin` | TTS 음성 데이터 | Kokoro 사용 시 필요 (프로젝트/공개 링크 확인) |
| `models/자비스야_ko_linux_v4_0_0.ppn` | 웨이크 워드 "자비스야" | [Picovoice Console](https://console.picovoice.ai/)에서 생성 후 다운로드 |
| `models/porcupine_params_ko.pv` | Porcupine 한국어 파라미터 | Picovoice SDK/문서에서 다운로드 |

### Kokoro TTS

- **kokoro-v0_19.onnx** (약 310MB):  
  - Hugging Face:  
    https://huggingface.co/thewh1teagle/Kokoro/resolve/main/kokoro-v0_19.onnx  
  - 저장 경로: `models/kokoro-v0_19.onnx`
- **voices.bin**:  
  - 현재 설정(`config/settings.yaml`)에서는 TTS provider가 `edge`이면 Kokoro/voices.bin은 사용하지 않을 수 있습니다.  
  - Kokoro를 쓸 경우 프로젝트에서 안내하는 voices.bin 경로를 확인하세요.

### Porcupine (웨이크 워드)

1. [Picovoice Console](https://console.picovoice.ai/) 가입/로그인
2. Wake Word 생성: 한국어, 커스텀 "자비스야", Linux 선택 후 `.ppn` 다운로드
3. `models/자비스야_ko_linux_v4_0_0.ppn` 등으로 저장
4. `porcupine_params_ko.pv`는 [Picovoice GitHub](https://github.com/Picovoice/porcupine) 또는 문서에서 한국어 리소스로 제공되는 파일을 `models/`에 둡니다.

## 의존성 (pip)

대용량 파일과 별도로, Python 패키지는 다음으로 설치합니다.

```bash
pip install -r requirements.txt
```

이 리포지토리에는 `requirements.txt`만 있고, 실제 패키지는 PyPI 등에서 받습니다.

---

## Git 푸시 오류 (Large files detected) 해결

이미 `models/` 안의 대용량 파일이 커밋된 상태에서 푸시하면 GitHub에서 거절됩니다.  
**히스토리에서 해당 경로를 제거한 뒤** 다시 푸시해야 합니다.

1. **git-filter-repo 사용 (권장)**  
   히스토리에서 `models/` 디렉터리를 완전히 제거합니다.

   ```bash
   pip install git-filter-repo
   git filter-repo --path models/ --invert-paths --force
   ```

   이후 원격을 다시 추가하고 푸시:

   ```bash
   git remote add origin https://github.com/addinedu-physicalai-1st/iot-repo-1.git
   git push origin feature/dev_base
   ```

2. **git filter-branch 사용**  
   `git-filter-repo`를 쓰지 않을 때:

   ```bash
   git filter-branch --force --index-filter \
     'git rm -r --cached --ignore-unmatch models/' --prune-empty HEAD
   git push origin feature/dev_base --force
   ```

3. **이미 .gitignore에 `models/`를 추가했으므로**, 앞으로는 `models/` 안의 파일이 커밋되지 않습니다.  
   모델은 위의 `download_models.sh` 또는 수동 다운로드로만 준비하면 됩니다.
