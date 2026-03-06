/*
 * config.h — ESP32 민감정보 설정
 * ================================
 * 이 파일을 config.h 로 복사한 뒤 실제 값을 입력하세요.
 *   cp config.h.example config.h
 *
 * config.h 는 .gitignore 처리되어 Git에 커밋되지 않습니다.
 */

#ifndef CONFIG_H
#define CONFIG_H

// ── Wi-Fi ────────────────────────────────────
#define WIFI_SSID      "addinedu_201class_2-2.4G"
#define WIFI_PASSWORD  "201class2!"

// ── 서버 IP (Python 서버) ────────────────────
#define SERVER_IP      "192.168.0.189"

#endif // CONFIG_H
