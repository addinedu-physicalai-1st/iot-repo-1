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
 * 공간별 핀 배정 (활성):
 *   욕실  (bathroom) : 4-Digit 7Seg (직접 핀) / DHT11 GPIO 33
 *   침실  (bedroom)  : Servo GPIO 2  (커튼)
 *   [LED 전체 비활성 — 주석처리됨]
 *
 * 의존 라이브러리 (Arduino Library Manager):
 *   - ArduinoJson     (6.x)
 *   - ESP32Servo
 *   (DHT11은 라이브러리 없이 직접 비트-뱅 구현 — 별도 설치 불필요)
 * 
 * 7세그먼트: 직접 핀 제어 (TM1637Display 라이브러리 미사용)
 *
 * 작성일: 2026-02-20
 * 수정일: 2026-02-22  단일 ESP32 통합 버전 (5개 공간 통합)
 * 수정일: 2026-02-22  PIR 센서 추가 (재실 감지 / 방범 모드)
 * 수정일: 2026-02-27  DHT11 온습도 센서 추가 (GPIO 33, 욕실)
 *
 * PIR 센서 (HC-SR501):
 *   GPIO 27  — PIR 신호 입력
 *   재실 모드: 일정 시간 움직임 없으면 서버에 이벤트 전송
 *   방범 모드: 움직임 감지 시 서버에 이벤트 전송 + 전체 조명 ON
 */

 #include <WiFi.h>
 #include <ArduinoJson.h>
 #include <ESP32Servo.h>
 #include "config.h"          // WiFi/서버 민감정보 (config.h.example 참조)


 // ================================================================
 // Config — 여기만 수정
 // ================================================================


 // WiFi/서버 설정은 config.h 에서 관리
 // ── TCP 서버 ──────────────────────────────────────────────────────
 #define SERVER_PORT    9000
 
// ── 디바이스 ID ───────────────────────────────────────────────────
#define DEVICE_ID      "esp32_home1"
#define CAPS_STR       "[\"servo\",\"seg7\",\"dht11\"]"  // led 비활성

// ── 타이밍 ────────────────────────────────────────────────────────
#define RECONNECT_DELAY_MS  3000
#define DHT11_INTERVAL_MS   5000   // DHT11 읽기 주기 (ms, 최소 2초 이상)
 
 
 // ================================================================
 // 핀 배정
 // ================================================================
 
 // ── LED (공간별) ──────────────────────────────────────────────────
//  #define PIN_LED_LIVING    15
//  #define PIN_LED_BATHROOM  32
//  #define PIN_LED_BEDROOM   35
//  #define PIN_LED_GARAGE    2
//  #define PIN_LED_ENTRANCE  34
 
 // ── 서보 (침실 커튼만 사용) ──────────────────────────────────────
 #define PIN_SERVO_BEDROOM   2     // 침실 커튼 서보
//  #define PIN_SERVO_GARAGE    15   // 차고문  (미사용)
//  #define PIN_SERVO_ENTRANCE  16   // 현관문  (미사용)
 
// ── 4-Digit 7세그먼트 (욕실) - 직접 핀 제어 ──────────────────────────
// 세그먼트 핀 (A-G)
#define PIN_A 4
#define PIN_B 5
#define PIN_C 18
#define PIN_D 19
#define PIN_E 21
#define PIN_F 22
#define PIN_G 23
#define   PIN_DP 25    // 소수점(DP) 핀 — 3번째 자리(SEG3)의 DP 애노드 연결

// 디지트 선택 핀 (공통 캐소드: LOW=선택, HIGH=OFF)
const int SEG1 = 13;  // 첫 번째 자리 (왼쪽, 항상 OFF)
const int SEG2 = 12;  // 두 번째 자리 — 십의 자리
const int SEG3 = 14;  // 세 번째 자리 — 일의 자리 + DP
const int SEG4 = 26;  // 네 번째 자리 (오른쪽) — 소수 첫째 자리

// 세그먼트 핀 배열 (A, B, C, D, E, F, G)
const int segPins[7] = {PIN_A, PIN_B, PIN_C, PIN_D, PIN_E, PIN_F, PIN_G};
// 디지트 핀 배열
const int digitPins[4] = {SEG1, SEG2, SEG3, SEG4};

// ── DHT11 온습도 센서 (디지털) ────────────────────────────────────
#define DHTPIN        33   // GPIO 33 — DHT11 DATA 핀 연결

 // ── PIR 센서 ─────────────────────────────────────────────────────
 #define PIN_PIR       27   // HC-SR501 OUT 핀 // *
 
 // ── PIR 모드 정의 ─────────────────────────────────────────────────
 #define PIR_MODE_OFF      0   // 비활성
 #define PIR_MODE_PRESENCE 1   // 재실 감지 (정적 이상 감지)
 #define PIR_MODE_GUARD    2   // 방범 모드 (외출 시 침입 감지)
 
 // ── PIR 타이밍 설정 ───────────────────────────────────────────────
 #define PIR_STATIC_TIMEOUT_MS   (4UL * 60 * 60 * 1000)  // 재실: 4시간 무움직임 → 이상
 #define PIR_ALERT_COOLDOWN_MS   (30UL * 1000)            // 알림 재전송 방지 쿨다운 30초

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

// 마이너스(-) 기호: 가운데 세그먼트(G)만 ON
#define SEG7_IDX_MINUS  10
const byte minusPattern[7] = {LOW, LOW, LOW, LOW, LOW, LOW, HIGH};

// ── 7세그먼트 상태 변수 ───────────────────────────────────────────────
int  seg7Digits[4]  = {0, 0, 0, 0};
bool seg7Enabled    = false;
int  currentDigit   = 0;
unsigned long lastSeg7Update = 0;
#define SEG7_REFRESH_INTERVAL 5   // 각 디지트 표시 간격 (ms)

// ── 온도 표시 전용 변수 ───────────────────────────────────────────────
float seg7CurrentTemp  = 20.0f;   // 현재 온도 (서버에서 수신, 항상 기본 표시)
float seg7TargetTemp   = 20.0f;   // 희망 온도
bool  seg7ShowTarget   = false;   // true: 희망온도 3초 표시 중
unsigned long seg7TargetEnd = 0;  // 희망온도 표시 종료 시각 (millis)
#define SEG7_TARGET_SHOW_MS 3000  // 희망온도 표시 유지 시간 (ms)
 
 
 // ================================================================
 // 전역 객체
 // ================================================================
 
 WiFiClient tcpClient;
 Servo servoBedroom;   // 침실 커튼
 String rxBuffer = "";
 
// ── DHT11 크로스태스크 공유 변수 (Core0 → Core1) ─────────────────
volatile float g_dhtTemp  = NAN;   // 마지막으로 검증된 온도값
volatile bool  g_dhtReady = false; // 새 값이 준비됐을 때 true

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
 
  /* ── LED 핀 초기화 (비활성) ──────────────────────────────────────
   int ledPins[] = {
     PIN_LED_LIVING, PIN_LED_BATHROOM, PIN_LED_BEDROOM,
     PIN_LED_GARAGE, PIN_LED_ENTRANCE
   };
   for (int i = 0; i < 5; i++) {
     pinMode(ledPins[i], OUTPUT);
     digitalWrite(ledPins[i], LOW);
   }
   Serial.println("[LED] 5개 핀 초기화 완료");
  ── LED 끝 ─────────────────────────────────────────────────────── */

   // ── 서보 초기화 (침실 커튼) ──────────────────────────────────
   servoBedroom.attach(PIN_SERVO_BEDROOM);
   servoBedroom.write(0);
   Serial.println("[SERVO] 침실 서보 초기화 완료 (GPIO" + String(PIN_SERVO_BEDROOM) + ")");
 
   // ── 7세그먼트 초기화 ────────────────────────────────────────────
   initSeg7();
   Serial.println("[SEG7] 초기화 완료 (욕실) - 직접 핀 제어");
 
  // ── DHT11 FreeRTOS 태스크 (Core 1) ──────────────────────────
  // 내부 풀업 활성화 (외부 4.7kΩ 풀업 저항 없을 때 보조)
  pinMode(DHTPIN, INPUT_PULLUP);
  // Core 1 사용: Core 0는 WiFi/TCP 스택 전용으로 비워둠
  // (Core 0에서 noInterrupts() 실행 시 WiFi keepalive 누락 → TCP 끊김 방지)
  xTaskCreatePinnedToCore(
    taskDHT11,   // 함수
    "DHT11",     // 이름
    4096,        // 스택 크기 (bytes) — 여유 있게 4KB
    NULL,        // 파라미터
    1,           // 우선순위
    NULL,        // 핸들 (불필요)
    1            // Core 1 (WiFi 스택 간섭 방지)
  );
  Serial.println("[DHT11] 태스크 생성 완료 (Core 1, GPIO33, 직접 비트-뱅)");

  // ── PIR 초기화 ───────────────────────────────────────────────
  pinMode(PIN_PIR, INPUT);
  lastMotionTime = millis();
  Serial.println("[PIR] 초기화 완료 (GPIO27) - 캘리브레이션 대기 중...");
  delay(2000);  // 간단 캘리브레이션 대기
  // 첫 감지 즉시 발동 보장: 쿨다운이 이미 지난 것으로 설정
  lastAlertTime = millis() - PIR_ALERT_COOLDOWN_MS;
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

  // DHT11 새 온도값 수신 (taskDHT11이 Core 0에서 비동기로 채움)
  if (g_dhtReady) {
    g_dhtReady = false;
    float t = g_dhtTemp;
    seg7CurrentTemp = t;
    if (!seg7ShowTarget) {
      setTempDisplay(t);
      seg7Enabled = true;
    }
    sendSensorData(t, "bathroom");
  }

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
 
  /* ── LED (비활성) ────────────────────────────────────────────────
   if (strcmp(cmd, "led") == 0) {
     int pin = resolveLedPin(room);
     if (pin < 0) { sendError("unknown room for led"); return; }
     const char* state = doc["state"] | "off";
     cmdLed(pin, strcmp(state, "on") == 0, room);
     sendAck("led", "ok");
   } else
  ── LED 끝 ─────────────────────────────────────────────────────── */

   // ── SERVO ─────────────────────────────────────────────────────
   if (strcmp(cmd, "servo") == 0) {
     Servo* sv = resolveServo(room);
     if (!sv) { sendError("unknown room for servo"); return; }
     int angle = doc["angle"] | 0;
     angle = constrain(angle, 0, 180);
     cmdServo(sv, angle, room);
     sendAckServo("ok", angle, resolveServoPin(room));
 
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
 
 
/* ================================================================
// room → LED 핀 매핑 (비활성)
// ================================================================
int resolveLedPin(const char* room) {
  if (strcmp(room, "living")   == 0) return PIN_LED_LIVING;
  if (strcmp(room, "bathroom") == 0) return PIN_LED_BATHROOM;
  if (strcmp(room, "bedroom")  == 0) return PIN_LED_BEDROOM;
  if (strcmp(room, "garage")   == 0) return PIN_LED_GARAGE;
  if (strcmp(room, "entrance") == 0) return PIN_LED_ENTRANCE;
  return -1;
}
================================================================ */
 
 
// ================================================================
// room → Servo 객체 매핑 (침실만 활성)
// ================================================================

Servo* resolveServo(const char* room) {
  if (strcmp(room, "bedroom") == 0) return &servoBedroom;
  return nullptr;  // garage, entrance 미사용
}

int resolveServoPin(const char* room) {
  if (strcmp(room, "bedroom") == 0) return PIN_SERVO_BEDROOM;
  return -1;
}
 
 
 // ================================================================
 // PIR 센서 처리
 // ================================================================
 
void handlePir() {
  bool motionDetected = (digitalRead(PIN_PIR) == HIGH);
  unsigned long now   = millis();

  // 상태 전환(없음 → 감지) 시에만 출력 + TCP 전송 (스팸 방지)
  static bool prevMotion = false;
  if (motionDetected && !prevMotion) {
    Serial.println("[PIR] 감지됨!");
    sendPirEvent("motion_detected", "living_room");
  }
  prevMotion = motionDetected;

  if (pirMode == PIR_MODE_OFF) return;

  if (motionDetected) {
    lastMotionTime = now;
    pirAlertSent   = false;  // 움직임 있으면 알림 플래그 초기화
 
     // 방범 모드: 움직임 감지 → 즉시 알림
     if (pirMode == PIR_MODE_GUARD) {
       if (now - lastAlertTime > PIR_ALERT_COOLDOWN_MS) {
         Serial.println("[PIR] 🚨 방범모드 - 움직임 감지!");
         // allLightsOn();  // LED 비활성
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
 
/* ── allLightsOn (비활성) ────────────────────────────────────────
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
─────────────────────────────────────────────────────────────── */
 
void sendPirEvent(const char* eventType, const char* detail) {
  if (!tcpClient.connected()) return;
  String msg = "{\"type\":\"pir_event\",\"location\":\"pir_living_room\",\"event\":\"";
  msg += eventType;
  msg += "\",\"detail\":\"";
  msg += detail;
  msg += "\",\"device_id\":\"";
  msg += DEVICE_ID;
  msg += "\"}\n";
  tcpClient.print(msg);
  Serial.printf("[PIR] 이벤트 전송: %s / %s (pir_living_room)\n", eventType, detail);
}
 
 
// ================================================================
// DHT11 온도 센서 — 직접 비트-뱅 구현 (라이브러리 없음)
// ================================================================

/**
 * DHT11 직접 읽기 (ESP32 전용)
 * - 시작 신호: 20ms LOW → 30µs HIGH
 * - 비트 읽기 구간에서만 noInterrupts()로 타이밍 보호 (~5ms)
 * - 반환값: true=성공, false=타임아웃 또는 체크섬 오류
 */
bool dht11Read(uint8_t pin, int &temp) {
  uint8_t data[5] = {0, 0, 0, 0, 0};

  // ── 시작 신호: 20ms LOW 출력 ──────────────────────────────────
  pinMode(pin, OUTPUT);
  digitalWrite(pin, LOW);
  delayMicroseconds(20000);   // 20ms (DHT11 최소 18ms)
  digitalWrite(pin, HIGH);
  delayMicroseconds(30);
  pinMode(pin, INPUT_PULLUP);

  // ── 비트 읽기: 인터럽트 비활성화로 타이밍 보호 (~5ms) ──────────
  noInterrupts();

  uint32_t t;

  // DHT11 응답 ACK: 80µs LOW
  t = micros();
  while (digitalRead(pin) == HIGH) {
    if (micros() - t > 200) { interrupts(); return false; }
  }
  // DHT11 응답 ACK: 80µs HIGH
  t = micros();
  while (digitalRead(pin) == LOW) {
    if (micros() - t > 200) { interrupts(); return false; }
  }
  t = micros();
  while (digitalRead(pin) == HIGH) {
    if (micros() - t > 200) { interrupts(); return false; }
  }

  // 40비트 수신 (humidity 16bit + temp 16bit + checksum 8bit)
  for (int i = 0; i < 40; i++) {
    // 비트 시작 LOW (~50µs) 대기
    t = micros();
    while (digitalRead(pin) == LOW) {
      if (micros() - t > 100) { interrupts(); return false; }
    }
    // 40µs 후 샘플링: '0'=~26µs HIGH, '1'=~70µs HIGH
    delayMicroseconds(40);
    data[i / 8] <<= 1;
    if (digitalRead(pin) == HIGH) {
      data[i / 8] |= 1;
      // HIGH 종료 대기
      t = micros();
      while (digitalRead(pin) == HIGH) {
        if (micros() - t > 100) { interrupts(); return false; }
      }
    }
  }

  interrupts();

  // 체크섬 검증
  uint8_t sum = data[0] + data[1] + data[2] + data[3];
  if (data[4] != (sum & 0xFF)) {
    Serial.printf("[DHT11] 체크섬 오류: %02X != %02X\n", data[4], sum & 0xFF);
    return false;
  }

  temp = data[2];  // 온도 정수부 (DHT11 기준 data[2])
  return true;
}

/**
 * DHT11 읽기 태스크 — Core 0에서 실행
 * loop() (Core 1)의 seg7 멀티플렉싱을 블로킹하지 않음.
 */
void taskDHT11(void* pv) {
  vTaskDelay(pdMS_TO_TICKS(3000));  // DHT11 전원 안정화 3초 대기

  for (;;) {
    int temperature = 0;
    if (dht11Read(DHTPIN, temperature) && temperature >= 0 && temperature <= 80) {
      g_dhtTemp  = (float)temperature;
      g_dhtReady = true;
    } else {
      Serial.println("[DHT11] 읽기 실패 — 2초 후 재시도");
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }
    vTaskDelay(pdMS_TO_TICKS(DHT11_INTERVAL_MS));
  }
}

/**
 * 온도 데이터를 서버로 전송
 * 형식: {"type":"sensor","device":"dht11","room":"bathroom","temp":25.3}
 */
void sendSensorData(float temp, const char* room) {
  if (!tcpClient.connected()) return;
  String msg = "{\"type\":\"sensor\",\"device\":\"dht11\",\"room\":\"";
  msg += room;
  msg += "\",\"temp\":";
  msg += String(temp, 1);
  msg += "}\n";
  tcpClient.print(msg);
  Serial.printf("[DHT11] 전송: temp=%.1f°C\n", temp);
}


// ================================================================
// 디바이스 제어 함수
// ================================================================

/* ── cmdLed (비활성) ─────────────────────────────────────────────
/* ── cmdLed (비활성) ─────────────────────────────────────────────
void cmdLed(int pin, bool on, const char* room) {
  digitalWrite(pin, on ? HIGH : LOW);
  Serial.printf("[LED] %s GPIO%d → %s\n", room, pin, on ? "ON" : "OFF");
}
─────────────────────────────────────────────────────────────── */

void cmdServo(Servo* sv, int angle, const char* room) {
   sv->write(angle);
   Serial.printf("[SERVO] %s → %d도\n", room, angle);
 }
 
void cmdSeg7(const char* mode, float value) {

  // ── 끄기 ──────────────────────────────────────────────────────────
  if (strcmp(mode, "off") == 0) {
    seg7Enabled    = false;
    seg7ShowTarget = false;
    clearSeg7();
    Serial.println("[SEG7] OFF");
    return;
  }

  seg7Enabled = true;

  // ── 현재온도 수신 (서버가 DHT22 등에서 읽어 주기적으로 전송) ────────
  if (strcmp(mode, "current_temp") == 0) {
    seg7CurrentTemp = value;
    if (!seg7ShowTarget) {
      setTempDisplay(seg7CurrentTemp);
    }
    Serial.printf("[SEG7] 현재온도 수신: %.1f°C\n", value);

  // ── 희망온도 수신 (난방 ON 또는 +/- 버튼 시) ─────────────────────
  } else if (strcmp(mode, "target_temp") == 0) {
    seg7TargetTemp = value;
    seg7ShowTarget = true;
    seg7TargetEnd  = millis() + SEG7_TARGET_SHOW_MS;
    setTempDisplay(seg7TargetTemp);
    Serial.printf("[SEG7] 희망온도 표시 시작: %.1f°C (3초)\n", value);

  // ── 하위 호환: 직접 값 표시 ────────────────────────────────────────
  } else {
    setTempDisplay(value);
    Serial.printf("[SEG7] 직접 표시: %.1f°C\n", value);
  }

  Serial.printf("[SEG7] Digits → [OFF][%d][%d.][%d]\n",
                seg7Digits[1], seg7Digits[2], seg7Digits[3]);
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
  // DP 핀 초기화
  pinMode(PIN_DP, OUTPUT);
  digitalWrite(PIN_DP, LOW);

  // 디지트 핀 초기화 (출력, HIGH=OFF)
  for (int i = 0; i < 4; i++) {
    pinMode(digitPins[i], OUTPUT);
    digitalWrite(digitPins[i], HIGH);
  }

  // 초기 현재온도(20.0) 표시
  setTempDisplay(seg7CurrentTemp);
  seg7Enabled = true;
}

void clearSeg7() {
  for (int i = 0; i < 4; i++) {
    digitalWrite(digitPins[i], HIGH);
  }
  for (int i = 0; i < 7; i++) {
    digitalWrite(segPins[i], LOW);
  }
  digitalWrite(PIN_DP, LOW);
}
 
// ── 온도값 → seg7Digits 변환 ─────────────────────────────────────────
// 표시 배치: [-/OFF][십의자리][일의자리+DP][소수점첫째]
// 예) 23.5°C → [OFF][2][3.][5]
// 예) -5.3°C → [-  ][0][5.][3]  (첫째 자리에 '-' 표시)
void setTempDisplay(float temp) {
  bool isNeg   = (temp < 0.0f);
  float absTemp = isNeg ? -temp : temp;
  int v = (int)(absTemp * 10.0f + 0.5f);  // 소수점 제거: 5.3 → 53

  seg7Digits[0] = isNeg ? SEG7_IDX_MINUS : 0;  // 음수면 '-', 양수면 OFF
  seg7Digits[1] = (v / 100) % 10;  // 십의 자리
  seg7Digits[2] = (v / 10)  % 10;  // 일의 자리 (DP는 updateSeg7에서 처리)
  seg7Digits[3] =  v        % 10;  // 소수 첫째 자리
}

// ── 단일 디지트 출력 (dp=true면 소수점도 켬) ──────────────────────────
void displayDigit(int digit, int number, bool dp) {
  // 이전 디지트 OFF
  int prev = (digit > 0) ? digit - 1 : 3;
  digitalWrite(digitPins[prev], HIGH);

  // 세그먼트 + DP 전부 OFF
  for (int i = 0; i < 7; i++) digitalWrite(segPins[i], LOW);
  digitalWrite(PIN_DP, LOW);

  // 해당 디지트 선택 (LOW = 켜짐)
  digitalWrite(digitPins[digit], LOW);

  // 숫자 or 마이너스 패턴 출력
  if (number == SEG7_IDX_MINUS) {
    for (int i = 0; i < 7; i++) digitalWrite(segPins[i], minusPattern[i]);
  } else {
    for (int i = 0; i < 7; i++) digitalWrite(segPins[i], digitPatterns[number][i]);
  }

  // 소수점
  if (dp) digitalWrite(PIN_DP, HIGH);
}

// ── updateSeg7: 타임아웃 확인 + 멀티플렉싱 ────────────────────────────
void updateSeg7() {
  if (!seg7Enabled) {
    clearSeg7();
    return;
  }

  // 희망온도 3초 표시 타임아웃 확인
  if (seg7ShowTarget && (long)(millis() - seg7TargetEnd) >= 0) {
    seg7ShowTarget = false;
    setTempDisplay(seg7CurrentTemp);
    Serial.println("[SEG7] 희망온도 표시 종료 → 현재온도로 복귀");
  }

  // 멀티플렉싱 (5ms 간격)
  unsigned long now = millis();
  if (now - lastSeg7Update >= SEG7_REFRESH_INTERVAL) {

    if (currentDigit == 0 && seg7Digits[0] == 0) {
      // 첫째 자리 OFF (양수 온도): 이전(digit 3)만 끄고 아무것도 켜지 않음
      digitalWrite(digitPins[3], HIGH);
      for (int i = 0; i < 7; i++) digitalWrite(segPins[i], LOW);
      digitalWrite(PIN_DP, LOW);
    } else {
      // 세번째 자리(index 2)에는 소수점 ON / 첫째 자리는 음수 '-' 표시
      bool dp = (currentDigit == 2);
      displayDigit(currentDigit, seg7Digits[currentDigit], dp);
    }

    currentDigit = (currentDigit + 1) % 4;
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

void sendAckServo(const char* status, int angle, int pin) {
  String msg = "{\"type\":\"ack\",\"cmd\":\"servo\",\"status\":\"";
  msg += status;
  msg += "\",\"angle\":";
  msg += angle;
  msg += ",\"pin\":";
  msg += pin;
  msg += "}\n";
  tcpClient.print(msg);
  Serial.printf("[ACK] cmd=servo status=%s angle=%d pin=%d\n", status, angle, pin);
}
 
 void sendError(const char* errMsg) {
   String msg = "{\"type\":\"error\",\"msg\":\"";
   msg += errMsg;
   msg += "\"}\n";
   tcpClient.print(msg);
   Serial.printf("[ERR] %s\n", errMsg);
 }
 