/*
 * esp32_client.ino
 * ================
 * Voice IoT Controller - ESP32 단일 통합 클라이언트
 *
 * 역할:
 *   - WiFi 연결 후 TCP 서버에 접속
 *   - ESP32 1개로 5개 공간 전체 제어
 *   - device_id = "esp32_home" 으로 통합 등록
 *   - JSON 명령의 "room" 필드로 공간 구분 → 해당 핀 제어
 *
 * 공간별 핀 배정:
 *   거실  (living)   : LED GPIO 2
 *   욕실  (bathroom) : LED GPIO 4  / TM1637 CLK GPIO 22 / DIO GPIO 23
 *   침실  (bedroom)  : LED GPIO 5  / Servo GPIO 14
 *   차고  (garage)   : LED GPIO 12 / Servo GPIO 156
 *   현관  (entrance) : LED GPIO 13 / Servo GPIO 16
 *
 * 의존 라이브러리 (Arduino Library Manager):
 *   - ArduinoJson     (6.x)
 *   - ESP32Servo
 * 
 * 7세그먼트: 직접 핀 제어 (TM1637Display 라이브러리 미사용)
 *
 * 작성일: 2026-02-20
 * 수정일: 2026-02-22  단일 ESP32 통합 버전 (5개 공간 통합)
 * 수정일: 2026-02-22  PIR 센서 추가 (재실 감지 / 방범 모드)
 *
 * PIR 센서 (HC-SR501):
 *   GPIO 27  — PIR 신호 입력
 *   재실 모드: 일정 시간 움직임 없으면 서버에 이벤트 전송
 *   방범 모드: 움직임 감지 시 서버에 이벤트 전송 + 전체 조명 ON
 */

#include <WiFi.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>


// ================================================================
// Config — 여기만 수정
// ================================================================



#define WIFI_SSID      "addinedu_201class_2-2.4G"
#define WIFI_PASSWORD  "201class2!"

// ── TCP 서버 ──────────────────────────────────────────────────────
#define SERVER_IP      "192.168.0.154"  // 서버 PC IP
#define SERVER_PORT    9000

// ── 디바이스 ID ───────────────────────────────────────────────────
#define DEVICE_ID      "esp32_home"
#define CAPS_STR       "[\"led\",\"servo\",\"seg7\"]"

// ── 타이밍 ────────────────────────────────────────────────────────
#define RECONNECT_DELAY_MS  3000


// ================================================================
// 핀 배정
// ================================================================

// ── LED (공간별) ──────────────────────────────────────────────────
#define PIN_LED_LIVING    2 // *
//#define PIN_LED_BATHROOM  4
//#define PIN_LED_BEDROOM   5
//#define PIN_LED_GARAGE    12
//#define PIN_LED_ENTRANCE  13

// ── 서보 (공간별) ─────────────────────────────────────────────────
//#define PIN_SERVO_BEDROOM   14   // 커튼 
#define PIN_SERVO_GARAGE    15   // 차고문  // *
//#define PIN_SERVO_ENTRANCE  16   // 현관문

// ── 4-Digit 7세그먼트 (욕실) - 직접 핀 제어 ──────────────────────────
// ⚠️ 주의: PIN_A=4는 PIN_LED_BATHROOM=4와 겹칩니다. 필요시 핀 번호 변경하세요.
// 세그먼트 핀 (A-G)
const int PIN_A = 4;   // ⚠️ PIN_LED_BATHROOM과 충돌 가능
const int PIN_B = 5;   // ⚠️ PIN_LED_BEDROOM과 충돌 가능
const int PIN_C = 18;
const int PIN_D = 19;
const int PIN_E = 21;
const int PIN_F = 22;
const int PIN_G = 23;

// 디지트 선택 핀 (공통 캐소드: LOW=선택, HIGH=OFF)
const int SEG1 = 13;  // 첫 번째 자리 (왼쪽)
const int SEG2 = 12;  // 두 번째 자리
const int SEG3 = 14;  // 세 번째 자리
const int SEG4 = 26;  // 네 번째 자리 (오른쪽)

// 세그먼트 핀 배열 (A, B, C, D, E, F, G)
const int segPins[7] = {PIN_A, PIN_B, PIN_C, PIN_D, PIN_E, PIN_F, PIN_G};
// 디지트 핀 배열
const int digitPins[4] = {SEG1, SEG2, SEG3, SEG4};

// 0-9 숫자 패턴 (HIGH=켜짐, LOW=꺼짐) - 공통 캐소드
// A, B, C, D, E, F, G 순서
const byte digitPatterns[10][7] = {
  {HIGH, HIGH, HIGH, HIGH, HIGH, HIGH, LOW },  // 0
  {LOW,  HIGH, HIGH, LOW,  LOW,  LOW,  LOW },  // 1
  {HIGH, HIGH, LOW,  HIGH, HIGH, LOW,  HIGH},  // 2
  {HIGH, HIGH, HIGH, HIGH, LOW,  LOW,  HIGH},  // 3
  {LOW,  HIGH, HIGH, LOW,  LOW,  HIGH, HIGH},  // 4
  {HIGH, LOW,  HIGH, HIGH, LOW,  HIGH, HIGH},  // 5
  {HIGH, LOW,  HIGH, HIGH, HIGH, HIGH, HIGH},  // 6
  {HIGH, HIGH, HIGH, LOW,  LOW,  LOW,  LOW },  // 7
  {HIGH, HIGH, HIGH, HIGH, HIGH, HIGH, HIGH},  // 8
  {HIGH, HIGH, HIGH, HIGH, LOW,  HIGH, HIGH}   // 9
};

// 현재 표시할 값 (4자리)
int seg7Digits[4] = {0, 0, 0, 0};
bool seg7Enabled = false;
int currentDigit = 0;  // 현재 표시 중인 디지트 (0-3)
unsigned long lastSeg7Update = 0;
#define SEG7_REFRESH_INTERVAL 5  // 각 디지트 표시 간격 (ms)

// ── PIR 센서 ─────────────────────────────────────────────────────
#define PIN_PIR       27   // HC-SR501 OUT 핀 // *

// ── PIR 모드 정의 ─────────────────────────────────────────────────
#define PIR_MODE_OFF      0   // 비활성
#define PIR_MODE_PRESENCE 1   // 재실 감지 (정적 이상 감지)
#define PIR_MODE_GUARD    2   // 방범 모드 (외출 시 침입 감지)

// ── PIR 타이밍 설정 ───────────────────────────────────────────────
#define PIR_STATIC_TIMEOUT_MS   (4UL * 60 * 60 * 1000)  // 재실: 4시간 무움직임 → 이상
#define PIR_ALERT_COOLDOWN_MS   (30UL * 1000)            // 알림 재전송 방지 쿨다운 30초


// ================================================================
// 전역 객체
// ================================================================

WiFiClient tcpClient;

Servo servoBedroom;
Servo servoGarage;
Servo servoEntrance;

String rxBuffer = "";

// ── PIR 상태 변수 ─────────────────────────────────────────────────
int           pirMode          = PIR_MODE_OFF;
unsigned long lastMotionTime   = 0;   // 마지막 움직임 감지 시각
unsigned long lastAlertTime    = 0;   // 마지막 알림 전송 시각
bool          pirAlertSent     = false;


// ================================================================
// setup
// ================================================================

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n[Boot] " DEVICE_ID " — 통합 5개 공간");

  // ── LED 핀 초기화 ────────────────────────────────────────────
  int ledPins[] = {
    PIN_LED_LIVING, PIN_LED_BATHROOM, PIN_LED_BEDROOM,
    PIN_LED_GARAGE, PIN_LED_ENTRANCE
  };
  for (int i = 0; i < 5; i++) {
    pinMode(ledPins[i], OUTPUT);
    digitalWrite(ledPins[i], LOW);
  }
  Serial.println("[LED] 5개 핀 초기화 완료");

  // ── 서보 초기화 ──────────────────────────────────────────────
  servoBedroom.attach(PIN_SERVO_BEDROOM);
  servoGarage.attach(PIN_SERVO_GARAGE);
  servoEntrance.attach(PIN_SERVO_ENTRANCE);
  servoBedroom.write(0);
  servoGarage.write(0);
  servoEntrance.write(0);
  Serial.println("[SERVO] 3개 초기화 완료 (침실/차고/현관)");

  // ── 7세그먼트 초기화 ────────────────────────────────────────────
  initSeg7();
  Serial.println("[SEG7] 초기화 완료 (욕실) - 직접 핀 제어");

  // ── PIR 초기화 ───────────────────────────────────────────────
  pinMode(PIN_PIR, INPUT);
  lastMotionTime = millis();
  Serial.println("[PIR] 초기화 완료 (GPIO27) - 캘리브레이션 대기 중...");
  delay(2000);  // 간단 캘리브레이션 대기
  Serial.println("[PIR] 준비 완료");

  connectWiFi();
  connectServer();
}


// ================================================================
// loop
// ================================================================

void loop() {
  if (!tcpClient.connected()) {
    Serial.println("[TCP] 연결 끊김 - 재연결 시도");
    delay(RECONNECT_DELAY_MS);
    connectServer();
    return;
  }

  while (tcpClient.available()) {
    char c = tcpClient.read();
    if (c == '\n') {
      processCommand(rxBuffer);
      rxBuffer = "";
    } else {
      rxBuffer += c;
    }
  }

  // PIR 감지 처리
  handlePir();
  
  // 7세그먼트 업데이트 (멀티플렉싱)
  updateSeg7();
}


// ================================================================
// WiFi 연결
// ================================================================

void connectWiFi() {
  Serial.printf("[WiFi] 연결 중: %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 20) {
    delay(500);
    Serial.print(".");
    retry++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WiFi] 연결 성공: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[WiFi] 연결 실패 - 재시작");
    ESP.restart();
  }
}


// ================================================================
// TCP 서버 연결 + 등록
// ================================================================

void connectServer() {
  Serial.printf("[TCP] 서버 연결: %s:%d\n", SERVER_IP, SERVER_PORT);

  if (!tcpClient.connect(SERVER_IP, SERVER_PORT)) {
    Serial.println("[TCP] 연결 실패");
    return;
  }

  Serial.println("[TCP] 연결 성공");
  sendRegister();
}

void sendRegister() {
  String msg = "{\"type\":\"register\",\"device_id\":\"";
  msg += DEVICE_ID;
  msg += "\",\"caps\":";
  msg += CAPS_STR;
  msg += "}\n";
  tcpClient.print(msg);
  Serial.printf("[TCP] 등록: %s", msg.c_str());
}


// ================================================================
// 명령 처리
// ================================================================

void processCommand(String raw) {
  raw.trim();
  if (raw.length() == 0) return;

  Serial.printf("[CMD] 수신: %s\n", raw.c_str());

  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, raw);

  if (err) {
    Serial.printf("[CMD] JSON 파싱 오류: %s\n", err.c_str());
    sendError("JSON parse error");
    return;
  }

  const char* cmd  = doc["cmd"];
  const char* room = doc["room"] | "";   // 공간 구분자: living/bathroom/bedroom/garage/entrance

  if (!cmd) {
    sendError("missing cmd field");
    return;
  }

  // ── LED ───────────────────────────────────────────────────────
  if (strcmp(cmd, "led") == 0) {
    int pin = resolveLedPin(room);
    if (pin < 0) { sendError("unknown room for led"); return; }
    const char* state = doc["state"] | "off";
    cmdLed(pin, strcmp(state, "on") == 0, room);
    sendAck("led", "ok");

  // ── SERVO ─────────────────────────────────────────────────────
  } else if (strcmp(cmd, "servo") == 0) {
    Servo* sv = resolveServo(room);
    if (!sv) { sendError("unknown room for servo"); return; }
    int angle = doc["angle"] | 0;
    angle = constrain(angle, 0, 180);
    cmdServo(sv, angle, room);
    sendAck("servo", "ok");

  // ── SEG7 ──────────────────────────────────────────────────────
  } else if (strcmp(cmd, "seg7") == 0) {
    const char* mode = doc["mode"]  | "num";
    float       val  = doc["value"] | 0.0f;
    cmdSeg7(mode, val);
    sendAck("seg7", "ok");

  // ── PIR 모드 설정 ─────────────────────────────────────────────
  } else if (strcmp(cmd, "pir_mode") == 0) {
    const char* mode = doc["mode"] | "off";
    cmdPirMode(mode);
    sendAck("pir_mode", "ok");

  } else {
    Serial.printf("[CMD] 알 수 없는 명령: %s\n", cmd);
    sendError("unknown cmd");
  }
}


// ================================================================
// room → LED 핀 매핑
// ================================================================

int resolveLedPin(const char* room) {
  if (strcmp(room, "living")   == 0) return PIN_LED_LIVING;
  if (strcmp(room, "bathroom") == 0) return PIN_LED_BATHROOM;
  if (strcmp(room, "bedroom")  == 0) return PIN_LED_BEDROOM;
  if (strcmp(room, "garage")   == 0) return PIN_LED_GARAGE;
  if (strcmp(room, "entrance") == 0) return PIN_LED_ENTRANCE;
  return -1;
}


// ================================================================
// room → Servo 객체 매핑
// ================================================================

Servo* resolveServo(const char* room) {
  if (strcmp(room, "bedroom")  == 0) return &servoBedroom;
  if (strcmp(room, "garage")   == 0) return &servoGarage;
  if (strcmp(room, "entrance") == 0) return &servoEntrance;
  return nullptr;
}


// ================================================================
// PIR 센서 처리
// ================================================================

void handlePir() {
  if (pirMode == PIR_MODE_OFF) return;

  bool motionDetected = (digitalRead(PIN_PIR) == HIGH);
  unsigned long now   = millis();

  if (motionDetected) {
    lastMotionTime = now;
    pirAlertSent   = false;  // 움직임 있으면 알림 플래그 초기화

    // 방범 모드: 움직임 감지 → 즉시 알림
    if (pirMode == PIR_MODE_GUARD) {
      if (now - lastAlertTime > PIR_ALERT_COOLDOWN_MS) {
        Serial.println("[PIR] 🚨 방범모드 - 움직임 감지!");
        allLightsOn();
        sendPirEvent("guard_alert", "motion_detected");
        lastAlertTime = now;
      }
    }

  } else {
    // 재실 모드: 4시간 이상 정적 → 이상 감지
    if (pirMode == PIR_MODE_PRESENCE && !pirAlertSent) {
      if (now - lastMotionTime > PIR_STATIC_TIMEOUT_MS) {
        Serial.println("[PIR] ⚠️ 재실모드 - 장시간 정적 감지!");
        sendPirEvent("presence_alert", "static_too_long");
        pirAlertSent  = true;
        lastAlertTime = now;
      }
    }
  }
}

void cmdPirMode(const char* mode) {
  if (strcmp(mode, "presence") == 0) {
    pirMode        = PIR_MODE_PRESENCE;
    lastMotionTime = millis();
    pirAlertSent   = false;
    Serial.println("[PIR] 재실 감지 모드 ON");
  } else if (strcmp(mode, "guard") == 0) {
    pirMode        = PIR_MODE_GUARD;
    pirAlertSent   = false;
    Serial.println("[PIR] 방범 모드 ON");
  } else {
    pirMode = PIR_MODE_OFF;
    Serial.println("[PIR] 모드 OFF");
  }
}

void allLightsOn() {
  int ledPins[] = {
    PIN_LED_LIVING, PIN_LED_BATHROOM, PIN_LED_BEDROOM,
    PIN_LED_GARAGE, PIN_LED_ENTRANCE
  };
  for (int i = 0; i < 5; i++) {
    digitalWrite(ledPins[i], HIGH);
  }
  Serial.println("[PIR] 전체 조명 ON");
}

void sendPirEvent(const char* eventType, const char* detail) {
  if (!tcpClient.connected()) return;
  String msg = "{\"type\":\"pir_event\",\"event\":\"";
  msg += eventType;
  msg += "\",\"detail\":\"";
  msg += detail;
  msg += "\",\"device_id\":\"";
  msg += DEVICE_ID;
  msg += "\"}\n";
  tcpClient.print(msg);
  Serial.printf("[PIR] 이벤트 전송: %s / %s\n", eventType, detail);
}


// ================================================================
// 디바이스 제어 함수
// ================================================================

void cmdLed(int pin, bool on, const char* room) {
  digitalWrite(pin, on ? HIGH : LOW);
  Serial.printf("[LED] %s GPIO%d → %s\n", room, pin, on ? "ON" : "OFF");
}

void cmdServo(Servo* sv, int angle, const char* room) {
  sv->write(angle);
  Serial.printf("[SERVO] %s → %d도\n", room, angle);
}

void cmdSeg7(const char* mode, float value) {
  if (strcmp(mode, "off") == 0) {
    seg7Enabled = false;
    clearSeg7();
    Serial.println("[SEG7] OFF");
    return;
  }
  
  seg7Enabled = true;
  
  // 소수점 1자리 표시: 23.5 → 235 (소수점은 G 세그먼트로 표시 가능)
  int display_val = (int)(value * 10);
  
  // 4자리로 분리
  seg7Digits[0] = (display_val / 1000) % 10;  // 천의 자리
  seg7Digits[1] = (display_val / 100) % 10;    // 백의 자리
  seg7Digits[2] = (display_val / 10) % 10;     // 십의 자리
  seg7Digits[3] = display_val % 10;             // 일의 자리
  
  // 소수점 표시 (세 번째 자리에 소수점)
  // 이건 나중에 확장 가능
  
  Serial.printf("[SEG7] mode=%s value=%.1f → [%d][%d][%d][%d]\n", 
                mode, value, seg7Digits[0], seg7Digits[1], seg7Digits[2], seg7Digits[3]);
}


// ================================================================
// 7세그먼트 제어 함수
// ================================================================

void initSeg7() {
  // 세그먼트 핀 초기화 (출력)
  for (int i = 0; i < 7; i++) {
    pinMode(segPins[i], OUTPUT);
    digitalWrite(segPins[i], LOW);
  }
  
  // 디지트 핀 초기화 (출력, HIGH=OFF)
  for (int i = 0; i < 4; i++) {
    pinMode(digitPins[i], OUTPUT);
    digitalWrite(digitPins[i], HIGH);  // 모든 디지트 OFF
  }
  
  // 초기값 0 표시
  seg7Digits[0] = 0;
  seg7Digits[1] = 0;
  seg7Digits[2] = 0;
  seg7Digits[3] = 0;
  seg7Enabled = true;
}

void clearSeg7() {
  // 모든 디지트 OFF
  for (int i = 0; i < 4; i++) {
    digitalWrite(digitPins[i], HIGH);
  }
  // 모든 세그먼트 OFF
  for (int i = 0; i < 7; i++) {
    digitalWrite(segPins[i], LOW);
  }
}

void displayDigit(int digit, int number) {
  // 이전 디지트 OFF
  if (digit > 0) {
    digitalWrite(digitPins[digit - 1], HIGH);
  } else {
    digitalWrite(digitPins[3], HIGH);
  }
  
  // 세그먼트 모두 OFF
  for (int i = 0; i < 7; i++) {
    digitalWrite(segPins[i], LOW);
  }
  
  // 해당 디지트 선택 (LOW)
  digitalWrite(digitPins[digit], LOW);
  
  // 숫자 패턴 출력
  for (int i = 0; i < 7; i++) {
    digitalWrite(segPins[i], digitPatterns[number][i]);
  }
}

void updateSeg7() {
  if (!seg7Enabled) {
    clearSeg7();
    return;
  }
  
  // 타이머 기반 멀티플렉싱 (각 디지트를 순차적으로 표시)
  unsigned long now = millis();
  if (now - lastSeg7Update >= SEG7_REFRESH_INTERVAL) {
    displayDigit(currentDigit, seg7Digits[currentDigit]);
    currentDigit = (currentDigit + 1) % 4;  // 다음 디지트로
    lastSeg7Update = now;
  }
}

// ================================================================
// TCP 응답 전송 유틸
// ================================================================

void sendAck(const char* cmd, const char* status) {
  String msg = "{\"type\":\"ack\",\"cmd\":\"";
  msg += cmd;
  msg += "\",\"status\":\"";
  msg += status;
  msg += "\"}\n";
  tcpClient.print(msg);
  Serial.printf("[ACK] cmd=%s status=%s\n", cmd, status);
}

void sendError(const char* errMsg) {
  String msg = "{\"type\":\"error\",\"msg\":\"";
  msg += errMsg;
  msg += "\"}\n";
  tcpClient.print(msg);
  Serial.printf("[ERR] %s\n", errMsg);
}
