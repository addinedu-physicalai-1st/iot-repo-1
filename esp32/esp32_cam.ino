/**
 * esp_cam_entrance.ino
 * ESP32-CAM 현관 보안 카메라 — UDP JPEG 스트리밍
 * Voice IoT Controller v1.1
 *
 * v1.1 수정:
 *   - cam_dma_config failed (0xffffffff) 버그 수정
 *   - PSRAM 강제 감지 후 해상도 단계적 fallback
 *   - fb_count 1 고정 (DMA 메모리 부족 방지)
 *   - jpeg_quality 낮춰 메모리 사용량 감소
 *   - grab_mode JPEG 명시
 *
 * 보드: AI Thinker ESP32-CAM (OV2640)
 */

#include "esp_camera.h"
#include "esp_heap_caps.h"   // PSRAM 확인용
#include <WiFi.h>
#include <WiFiUdp.h>
#include "config.h"          // WiFi/서버 민감정보 (config.h.example 참조)

// ──────────────────────────────────────────
// 서버 설정 (SERVER_IP는 config.h에서 정의)
// ──────────────────────────────────────────
const uint16_t SERVER_PORT = 5005;             // camera_stream.py UDP 포트
const uint16_t LOCAL_PORT  = 5006;             // ESP-CAM 로컬 UDP 포트

// ──────────────────────────────────────────
// 카메라 핀 (AI Thinker ESP32-CAM 고정)
// ──────────────────────────────────────────
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// ──────────────────────────────────────────
// 파라미터
// ──────────────────────────────────────────
#define FRAME_INTERVAL_MS  100    // 캡처 간격 (~10fps)
#define UDP_MTU            1400
#define FLASH_LED_PIN        4

struct __attribute__((packed)) FrameHeader {
  uint8_t  magic[4];
  uint32_t frame_id;
  uint32_t total_len;
  uint16_t part_idx;
  uint16_t total_parts;
};

// ──────────────────────────────────────────
// 전역
// ──────────────────────────────────────────
WiFiUDP  udp;
uint32_t frameId    = 0;
bool     camReady   = false;

// ──────────────────────────────────────────
// 카메라 초기화 (단계적 fallback)
// ──────────────────────────────────────────
bool initCamera() {
  bool hasPsram = psramFound();
  Serial.printf("[CAM] PSRAM 감지: %s\n", hasPsram ? "✅ 있음" : "❌ 없음");
  if (hasPsram) {
    Serial.printf("[CAM] PSRAM 크기: %d bytes\n", ESP.getPsramSize());
    Serial.printf("[CAM] PSRAM 여유: %d bytes\n", ESP.getFreePsram());
  }
  Serial.printf("[CAM] Heap 여유: %d bytes\n", ESP.getFreeHeap());

  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;

  if (hasPsram) {
    config.frame_size   = FRAMESIZE_SVGA;
    config.jpeg_quality = 15;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    Serial.println("[CAM] 모드: PSRAM → SVGA(800x600) q=15");
  } else {
    config.frame_size   = FRAMESIZE_QVGA;
    config.jpeg_quality = 12;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    Serial.println("[CAM] 모드: DRAM → QVGA(320x240) q=12");
  }

  esp_err_t err = esp_camera_init(&config);

  if (err != ESP_OK) {
    Serial.printf("[CAM] 1차 초기화 실패(0x%x) → 해상도 낮춰 재시도\n", err);
    esp_camera_deinit();
    delay(200);

    if (hasPsram) {
      config.frame_size   = FRAMESIZE_QVGA;
      config.jpeg_quality = 15;
      config.fb_location  = CAMERA_FB_IN_PSRAM;
      Serial.println("[CAM] 재시도: QVGA(320x240)");
    } else {
      config.frame_size   = FRAMESIZE_96X96;
      config.jpeg_quality = 25;
      Serial.println("[CAM] 재시도: 96x96 (최소)");
    }
    err = esp_camera_init(&config);
  }

  if (err != ESP_OK) {
    Serial.printf("[CAM] ❌ 최종 초기화 실패: 0x%x\n", err);
    return false;
  }

  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_brightness(s, 0);
    s->set_contrast(s, 1);
    s->set_saturation(s, 0);
    s->set_gainceiling(s, (gainceiling_t)4);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_exposure_ctrl(s, 1);
    s->set_aec2(s, 1);
    s->set_ae_level(s, 1);
    s->set_aec_value(s, 400);
    s->set_gain_ctrl(s, 1);
    s->set_agc_gain(s, 0);
    s->set_bpc(s, 1);
    s->set_wpc(s, 1);
    s->set_raw_gma(s, 1);
    s->set_lenc(s, 1);
    s->set_hmirror(s, 0);
    s->set_vflip(s, 1);
  }

  Serial.printf("[CAM] ✅ 초기화 완료 — Heap여유: %d bytes\n", ESP.getFreeHeap());
  return true;
}

// ──────────────────────────────────────────
// Wi-Fi 연결
// ──────────────────────────────────────────
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[WiFi] 연결 중");
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 40) {
    delay(500);
    Serial.print(".");
    retry++;
  }
  if (WiFi.status() == WL_CONNECTED) {
    // ── v1.3 핵심 수정: WiFi 절전모드 OFF + 최대 출력 고정 ──
    WiFi.setSleep(false);                      // 절전모드 OFF (ping 지연 해결)
    WiFi.setTxPower(WIFI_POWER_19_5dBm);       // 최대 출력 (신호 안정화)
    Serial.printf("\n[WiFi] ✅ 연결 완료 — IP: %s\n",
                  WiFi.localIP().toString().c_str());
    Serial.println("[WiFi] 절전모드: OFF | TxPower: 19.5dBm");
  } else {
    Serial.println("\n[WiFi] ❌ 연결 실패 — 재시작");
    ESP.restart();
  }
}

// ──────────────────────────────────────────
// UDP 전송 (단순 모드)
// ──────────────────────────────────────────
void sendFrameSimple(camera_fb_t* fb) {
  if (fb->len > 65000) {
    sendFrameMultipart(fb);
    return;
  }
  udp.beginPacket(SERVER_IP, SERVER_PORT);
  udp.write(fb->buf, fb->len);
  udp.endPacket();
}

// ──────────────────────────────────────────
// UDP 전송 (멀티파트 모드)
// ──────────────────────────────────────────
void sendFrameMultipart(camera_fb_t* fb) {
  const uint8_t magic[4] = {0xAB, 0xCD, 0xEF, 0x01};
  uint32_t totalLen   = fb->len;
  uint16_t totalParts = (totalLen + UDP_MTU - 1) / UDP_MTU;

  for (uint16_t i = 0; i < totalParts; i++) {
    uint32_t offset   = (uint32_t)i * UDP_MTU;
    uint32_t chunkLen = min((uint32_t)UDP_MTU, totalLen - offset);

    uint8_t header[16];
    memcpy(header, magic, 4);
    uint32_t fid_be = htonl(frameId);
    memcpy(header + 4, &fid_be, 4);
    uint32_t tl_be = htonl(totalLen);
    memcpy(header + 8, &tl_be, 4);
    uint16_t pi_be = htons(i);
    memcpy(header + 12, &pi_be, 2);
    uint16_t tp_be = htons(totalParts);
    memcpy(header + 14, &tp_be, 2);

    udp.beginPacket(SERVER_IP, SERVER_PORT);
    udp.write(header, 16);
    udp.write(fb->buf + offset, chunkLen);
    udp.endPacket();
    delay(2);
  }
}

// ──────────────────────────────────────────
// 서버 명령 수신 (플래시 LED 제어)
// ──────────────────────────────────────────
void checkServerCommand() {
  int packetSize = udp.parsePacket();
  if (!packetSize) return;

  char buf[64] = {0};
  udp.read(buf, sizeof(buf) - 1);
  String cmd = String(buf);
  cmd.trim();

  if (cmd.indexOf("flash_on") >= 0) {
    digitalWrite(FLASH_LED_PIN, HIGH);
  } else if (cmd.indexOf("flash_off") >= 0) {
    digitalWrite(FLASH_LED_PIN, LOW);
  } else if (cmd.indexOf("flash_blink") >= 0) {
    for (int i = 0; i < 3; i++) {
      digitalWrite(FLASH_LED_PIN, HIGH); delay(200);
      digitalWrite(FLASH_LED_PIN, LOW);  delay(200);
    }
  }
}

// ──────────────────────────────────────────
// Setup
// ──────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Serial.println("\n[ESP32-CAM] 현관 보안 카메라 v1.3 시작");

  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);

  connectWiFi();

  int attempt = 0;
  while (!camReady && attempt < 3) {
    attempt++;
    Serial.printf("[CAM] 초기화 시도 %d/3\n", attempt);
    camReady = initCamera();
    if (!camReady) {
      Serial.println("[CAM] 실패 → 2초 후 재시도");
      delay(2000);
    }
  }

  if (!camReady) {
    Serial.println("[ERROR] 카메라 초기화 최종 실패 → 10초 후 재시작");
    for (int i = 0; i < 6; i++) {
      digitalWrite(FLASH_LED_PIN, HIGH); delay(300);
      digitalWrite(FLASH_LED_PIN, LOW);  delay(300);
    }
    delay(10000);
    ESP.restart();
  }

  udp.begin(LOCAL_PORT);
  Serial.printf("[UDP] 서버: %s:%d | 로컬포트: %d\n",
                SERVER_IP, SERVER_PORT, LOCAL_PORT);
  Serial.println("[ESP32-CAM] ✅ 준비 완료 — 스트리밍 시작");
}

// ──────────────────────────────────────────
// Loop
// ──────────────────────────────────────────
void loop() {
  if (!camReady) {
    delay(5000);
    ESP.restart();
    return;
  }

  // Wi-Fi 재연결
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] 연결 끊김 — 재연결");
    connectWiFi();
    return;
  }

  checkServerCommand();

  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[CAM] 프레임 캡처 실패");
    delay(500);
    return;
  }

  if (fb->format == PIXFORMAT_JPEG && fb->len > 0) {
    sendFrameSimple(fb);
    frameId++;

    if (frameId % 50 == 0) {
      Serial.printf("[CAM] 프레임 %u | 크기: %u bytes | Heap: %d\n",
                    frameId, fb->len, ESP.getFreeHeap());
    }
  }

  esp_camera_fb_return(fb);
  delay(FRAME_INTERVAL_MS);
}