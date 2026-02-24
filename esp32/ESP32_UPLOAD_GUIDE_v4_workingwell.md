# ESP32 업로드 가이드 — Voice IoT Controller
**단일 ESP32 통합 버전 · 2026-02-22 (v4)**

---

## 1. 사전 준비

### Arduino IDE 설정

```
1. Arduino IDE 2.x 설치
   https://www.arduino.cc/en/software

2. ESP32 보드 패키지 설치
   File → Preferences → Additional boards manager URLs:
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json

   Tools → Board → Boards Manager → "esp32" 검색 → 설치

3. 보드 선택
   Tools → Board → ESP32 Arduino → ESP32 Dev Module
```

### 라이브러리 설치 (Library Manager)

```
Tools → Manage Libraries → 아래 검색 후 설치:

  ArduinoJson       by Benoit Blanchon   (6.x)
  ESP32Servo        by Kevin Harrington
  TM1637Display     by Avishay Orpaz
```

---

## 2. 디바이스 구성 개요

> **ESP32 1개 (esp32_home)** 로 5개 공간 전체 제어  
> `room` 필드로 공간 구분 → 서버가 GPIO 핀 자동 매핑

```
device_id: "esp32_home"
caps: ["led", "servo", "seg7"]

명령 예시:
  {"cmd":"led",   "room":"bedroom",  "state":"on"}    → GPIO 5 HIGH
  {"cmd":"servo", "room":"garage",   "angle":90}      → GPIO 15 90도
  {"cmd":"seg7",  "pin_clk":22, "pin_dio":23, "mode":"num", "value":25.5}
```

---

## 3. 핀 배정 전체

### LED (공간별)

| 공간 | room 값 | GPIO | 비고 |
|------|---------|------|------|
| 거실 | living | **2** | 내장 LED 겸용 |
| 욕실 | bathroom | **4** | |
| 침실 | bedroom | **5** | |
| 차고 | garage | **12** | |
| 현관 | entrance | **13** | |

### 서보 (공간별)

| 공간 | room 값 | GPIO | 용도 | 열림 | 닫힘 |
|------|---------|------|------|------|------|
| 침실 | bedroom | **14** | 커튼 | 90° | 0° |
| 차고 | garage | **15** | 차고문 | 90° | 0° |
| 현관 | entrance | **16** | 현관문 | 90° | 0° |

> ⚠️ **서보 외부 전원 주의**  
> 대형 서보(차고문/현관문) → 외부 5V 공급 필수  
> 3.3V 직결 시 전압 부족으로 서보 떨림 발생  
> 외부 5V 사용 시 GND는 ESP32와 반드시 공통 연결

### TM1637 7세그먼트 (욕실)

| 핀 | GPIO | 비고 |
|----|------|------|
| CLK | **22** | |
| DIO | **23** | |
| VCC | **5V** | ⚠️ 반드시 5V (3.3V 불가) |
| GND | GND | |

> ✅ CLK/DIO 신호선은 3.3V GPIO 직결 가능 (VCC만 5V 필요)

---

## 4. 핀 충돌 검증

| GPIO | 용도 | 안전 여부 |
|------|------|-----------|
| 2 | LED 거실 | ✅ (부팅 중 일시 LOW — 정상) |
| 4 | LED 욕실 | ✅ |
| 5 | LED 침실 | ✅ |
| 12 | LED 차고 | ✅ |
| 13 | LED 현관 | ✅ |
| 14 | Servo 침실 (커튼) | ✅ |
| 15 | Servo 차고 (차고문) | ✅ |
| 16 | Servo 현관 (현관문) | ✅ |
| 22 | SEG7 CLK | ✅ |
| 23 | SEG7 DIO | ✅ |
| 6~11 | 내장 Flash 전용 | ❌ 사용 금지 |
| 34~39 | 입력 전용 | ❌ 출력 불가 |

---

## 5. 빵판 배선

### LED 공통 배선 (5개 동일)

```
[ESP32]
 GPIO_N ──── 저항(330Ω) ──── LED(+, 긴다리)
 GND   ──────────────────── LED(-, 짧은다리)
```

| 공간 | GPIO |
|------|------|
| 거실 | 2 |
| 욕실 | 4 |
| 침실 | 5 |
| 차고 | 12 |
| 현관 | 13 |

> ✅ 저항 방향 없음  
> ⚠️ LED 극성: 긴다리(+) → 저항 → GPIO, 짧은다리(-) → GND

### 서보 배선

```
[ESP32]                [서보]
 GPIO14 (침실) ──── Signal (주황)
 GPIO15 (차고) ──── Signal (주황)
 GPIO16 (현관) ──── Signal (주황)

 외부 5V ────────── VCC (빨강)   ← 대형 서보 필수
 GND     ────────── GND (갈색)   ← ESP32 GND와 공통
```

### TM1637 배선 (욕실)

```
[ESP32]          [TM1637 모듈]
 GPIO22 ──────── CLK  (황)
 GPIO23 ──────── DIO  (백)
 5V    ──────── VCC  (적)  ← 반드시 5V
 GND   ──────── GND  (흑)
```

---

## 6. Config 수정 방법

### Step 1 — Config 섹션 수정 (상단)

```cpp
// ── WiFi ──────────────────────────────────────────
#define WIFI_SSID      "공유기_이름"       // ← 수정
#define WIFI_PASSWORD  "공유기_비밀번호"   // ← 수정

// ── TCP 서버 ───────────────────────────────────────
#define SERVER_IP      "192.168.x.x"      // ← 서버 PC IP 수정
#define SERVER_PORT    9000

// ── 디바이스 ID (변경 불필요) ──────────────────────
#define DEVICE_ID      "esp32_home"
#define CAPS_STR       "[\"led\",\"servo\",\"seg7\"]"
```

### Step 2 — 서버 PC IP 확인

```bash
# 서버 PC (Ubuntu)
ip addr show | grep "inet " | grep -v 127.0.0.1
# 예: inet 192.168.0.15/24 → SERVER_IP = "192.168.0.15"
```

### Step 3 — 업로드

```
1. USB 케이블로 ESP32 연결
2. Tools → Port → /dev/ttyUSB0 (Linux) 또는 COM3 (Windows)
3. Tools → Upload Speed → 115200
4. 업로드 버튼 (→) 클릭
5. "Connecting..." 표시 시 ESP32 BOOT 버튼 1초 누름 (보드에 따라)
```

---

## 7. 시리얼 모니터 정상 로그

```
Tools → Serial Monitor → Baud Rate: 115200
```

### 정상 부팅 로그

```
[Boot] esp32_home — 통합 5개 공간
[LED] 5개 핀 초기화 완료
[SERVO] 3개 초기화 완료 (침실/차고/현관)
[SEG7] 초기화 완료 (욕실)
[WiFi] 연결 중: MyWifi
[WiFi] 연결 성공: 192.168.0.21
[TCP] 서버 연결: 192.168.0.15:9000
[TCP] 연결 성공
[TCP] 등록: {"type":"register","device_id":"esp32_home","caps":["led","servo","seg7"]}
```

### 명령 수신 로그

```
[CMD] 수신: {"cmd":"led","room":"bedroom","state":"on"}
[LED] bedroom GPIO5 → ON
[ACK] cmd=led status=ok

[CMD] 수신: {"cmd":"servo","room":"garage","angle":90}
[SERVO] garage → 90도
[ACK] cmd=servo status=ok

[CMD] 수신: {"cmd":"seg7","pin_clk":22,"pin_dio":23,"mode":"num","value":25.5}
[SEG7] mode=num value=25.5
[ACK] cmd=seg7 status=ok
```

---

## 8. 업로드 체크리스트

```
□ WiFi SSID / PASSWORD 수정 완료
□ SERVER_IP 수정 완료 (ip addr 로 확인)
□ 업로드 완료
□ 시리얼 모니터 열기 (115200)
□ [Boot] esp32_home 로그 확인
□ [WiFi] 연결 성공 로그 확인
□ [TCP] 등록 로그 확인 (caps:["led","servo","seg7"])
□ 대시보드 esp32_home 카드 → ONLINE

LED 동작 테스트 (5개):
  □ 거실  LED ON/OFF (GPIO 2)
  □ 욕실  LED ON/OFF (GPIO 4)
  □ 침실  LED ON/OFF (GPIO 5)
  □ 차고  LED ON/OFF (GPIO 12)
  □ 현관  LED ON/OFF (GPIO 13)

서보 동작 테스트 (3개):
  □ 침실 커튼  0°(닫힘) / 90°(열림)  (GPIO 14)
  □ 차고 차고문 0°(닫힘) / 90°(열림) (GPIO 15)
  □ 현관 현관문 0°(닫힘) / 90°(열림) (GPIO 16)
  □ 서보 외부 5V 전원 공급 확인 (대형 서보)

TM1637 동작 테스트:
  □ 욕실 세그먼트 숫자 표시 확인
  □ TM1637 VCC = 5V 확인
```

---

## 9. 수동 테스트 명령 (curl)

```bash
SERVER="http://localhost:8000"

# 거실 LED ON
curl -X POST $SERVER/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd":"led","device_id":"esp32_home","room":"living","state":"on"}'

# 침실 LED OFF
curl -X POST $SERVER/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd":"led","device_id":"esp32_home","room":"bedroom","state":"off"}'

# 전체 불 끄기
curl -X POST $SERVER/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd":"led","device_id":"all","state":"off"}'

# 차고문 열기
curl -X POST $SERVER/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd":"servo","device_id":"esp32_home","room":"garage","angle":90}'

# 현관문 닫기
curl -X POST $SERVER/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd":"servo","device_id":"esp32_home","room":"entrance","angle":0}'

# 욕실 세그먼트 표시
curl -X POST $SERVER/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd":"seg7","device_id":"esp32_home","pin_clk":22,"pin_dio":23,"mode":"num","value":25.5}'

# 전체 상태 조회
curl -X POST $SERVER/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd":"status","device_id":"esp32_home","room":"all","target":"all"}'

# 연결 디바이스 목록
curl $SERVER/api/devices
```

---

## 10. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `Connecting...` 반복 | BOOT 버튼 필요 | "Connecting..." 표시 시 BOOT 1초 누름 |
| WiFi 연결 실패 | SSID/PW 오타 또는 5GHz | SSID 재확인 (ESP32는 2.4GHz만 지원) |
| TCP 연결 실패 | SERVER_IP 오타 / 서버 미실행 | `ip addr`로 IP 확인, 서버 먼저 실행 |
| 특정 LED 안 켜짐 | room 값 오타 | curl 테스트로 room 값 확인 |
| 7세그먼트 미표시 | VCC 3.3V | TM1637 VCC를 5V로 변경 |
| 서보 떨림 | 전원 부족 | 외부 5V 별도 공급 + GND 공통 연결 |
| caps 미수신 | 펌웨어 CAPS_STR 오타 | `"[\"led\",\"servo\",\"seg7\"]"` 정확히 확인 |
| room 명령 무시 | 서버 room 매핑 오류 | ROOM_LED_PIN / ROOM_SERVO_PIN 확인 |
| 대시보드 OFFLINE | register 미수신 | 시리얼 로그 확인 후 재업로드 |

---

*Voice IoT Controller · ESP32_UPLOAD_GUIDE · 2026-02-22 (v4 — 단일 esp32_home 통합, room 기반 제어)*
