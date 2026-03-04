-- ================================================================
-- Voice IoT Controller - 이벤트 로그 DB 스키마
-- SR-3.1: DB 기반 장기 로그 관리
-- SR-3.2: DB 검색/조회 기능
--
-- 사용법:
--   mysql -u root -p < scripts/init_db.sql
-- ================================================================

CREATE DATABASE IF NOT EXISTS iot_smart_home
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE iot_smart_home;

-- ────────────────────────────────────────────
-- 사용자 생성 (이미 존재하면 스킵)
-- ────────────────────────────────────────────
CREATE USER IF NOT EXISTS 'gjkong'@'localhost' IDENTIFIED BY '1111';
GRANT ALL PRIVILEGES ON iot_smart_home.* TO 'gjkong'@'localhost';
FLUSH PRIVILEGES;

-- ────────────────────────────────────────────
-- 1. 이벤트 로그 테이블 (통합)
-- ────────────────────────────────────────────
-- event_category 값:
--   device_control   : LED/서보 제어 명령
--   device_ack       : ESP32 ACK 응답
--   sensor_data      : 온도/습도 센서
--   security_alert   : PIR 보안 알림
--   voice_input      : STT 음성 인식 결과
--   llm_parse        : LLM 명령 파싱 결과
--   device_connect   : ESP32 연결
--   device_disconnect: ESP32 연결 해제
--   ws_connect       : 웹 클라이언트 연결
--   ws_disconnect    : 웹 클라이언트 해제
--   music_control    : 음악 제어
--   server_event     : 서버 시작/종료
--   pir_mode         : PIR 모드 변경
--   bathroom_temp    : 욕실 온도 설정/조회
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS event_logs (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    created_at      DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    event_category  VARCHAR(30)     NOT NULL,
    level           VARCHAR(10)     NOT NULL DEFAULT 'INFO',
    source_module   VARCHAR(30)     NOT NULL,
    device_id       VARCHAR(50)     NULL,
    room            VARCHAR(20)     NULL,
    summary         VARCHAR(200)    NOT NULL,
    detail          JSON            NULL,

    INDEX idx_created_at    (created_at),
    INDEX idx_category      (event_category),
    INDEX idx_device_room   (device_id, room),
    INDEX idx_category_date (event_category, created_at),
    INDEX idx_level         (level)

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='IoT 스마트홈 통합 이벤트 로그 (SR-3.1)';


-- ────────────────────────────────────────────
-- 2. 보안 미디어 테이블 (영상/이미지)
-- ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS security_media (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    event_log_id    BIGINT UNSIGNED NOT NULL,
    created_at      DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    media_type      VARCHAR(20)     NOT NULL COMMENT 'image/jpeg, video/mp4 등',
    file_path       VARCHAR(500)    NOT NULL COMMENT '미디어 파일 저장 경로',
    file_size_bytes INT UNSIGNED    NULL,
    duration_sec    FLOAT           NULL     COMMENT '영상 길이 (초)',
    description     VARCHAR(200)    NULL,

    INDEX idx_event_log_id (event_log_id),
    INDEX idx_created_at   (created_at),

    CONSTRAINT fk_security_media_event
        FOREIGN KEY (event_log_id) REFERENCES event_logs(id)
        ON DELETE CASCADE

) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='보안 이벤트 영상/이미지 미디어 (SR-3.1)';
