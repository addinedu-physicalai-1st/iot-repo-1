/*
 * main.cpp  (converted from esp32_client_last0225.ino)
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
 *   차고  (garage)   : LED GPIO 12 / Servo GPIO 15
 *   현관  (entrance) : LED GPIO 13 / Servo GPIO 16
 *
 * 의존 라이브러리 (PlatformIO lib_deps):
 *   - ArduinoJson     (6.x)
 *   - ESP32Servo
 *   - TM1637Display
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

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <TM1637Display.h>


// ================================================================
// Config — 여기만 수정
// ================================================================



#define WIFI_SSID      ""
#define WIFI_PASSWORD  ""

// ── TCP 서버 ──────────────────────────────────────────────────────
#define SERVER_IP      ""  // 서버 PC IP
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
#define PIN_LED_LIVING    2
#define PIN_LED_BATHROOM  4
#define PIN_LED_BEDROOM   5
#define PIN_LED_GARAGE    12
#define PIN_LED_ENTRANCE  13

// ── 서보 (공간별) ─────────────────────────────────────────────────
#define PIN_SERVO_BEDROOM   14   // 커튼
#define PIN_SERVO_GARAGE    15   // 차고문
#define PIN_SERVO_ENTRANCE  16   // 현관문

// ── TM1637 7세그먼트 (욕실) ───────────────────────────────────────
#define PIN_SEG7_CLK  22
#define PIN_SEG7_DIO  23

// ── PIR 센서 ─────────────────────────────────────────────────────
#define PIN_PIR       27   // HC-SR501 OUT 핀

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

TM1637Display seg7(PIN_SEG7_CLK, PIN_SEG7_DIO);

String rxBuffer = "";

// ── PIR 상태 변수 ─────────────────────────────────────────────────
int           pirMode          = PIR_MODE_OFF;
unsigned long lastMotionTime   = 0;   // 마지막 움직임 감지 시각
unsigned long lastAlertTime    = 0;   // 마지막 알림 전송 시각
bool          pirAlertSent     = false;


// ================================================================
// 함수 프로토타입
// ================================================================

void connectWiFi();
void connectServer();
void sendRegister();
void processCommand(String raw);
int resolveLedPin(const char* room);
Servo* resolveServo(const char* room);
void handlePir();
void cmdPirMode(const char* mode);
void allLightsOn();
void sendPirEvent(const char* eventType, const char* detail);
void cmdLed(int pin, bool on, const char* room);
void cmdServo(Servo* sv, int angle, const char* room);
void cmdSeg7(const char* mode, float value);
void sendAck(const char* cmd, const char* status);
void sendError(const char* errMsg);


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

  // ── TM1637 초기화 ────────────────────────────────────────────
  seg7.setBrightness(5);
  seg7.showNumberDec(0);
  Serial.println("[SEG7] 초기화 완료 (욕실)");

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
        Serial.println("[PIR] 방범모드 - 움직임 감지!");
        allLightsOn();
        sendPirEvent("guard_alert", "motion_detected");
        lastAlertTime = now;
      }
    }

  } else {
    // 재실 모드: 4시간 이상 정적 → 이상 감지
    if (pirMode == PIR_MODE_PRESENCE && !pirAlertSent) {
      if (now - lastMotionTime > PIR_STATIC_TIMEOUT_MS) {
        Serial.println("[PIR] 재실모드 - 장시간 정적 감지!");
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
    seg7.clear();
    Serial.println("[SEG7] OFF");
    return;
  }
  // 소수점 1자리 표시: 23.5 → 235 + 소수점
  int display_val = (int)(value * 10);
  seg7.showNumberDecEx(display_val, 0b01000000, false, 4, 0);
  Serial.printf("[SEG7] mode=%s value=%.1f\n", mode, value);
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
