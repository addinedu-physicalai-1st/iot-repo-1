"""
server/db_logger.py
===================
비동기 MySQL 이벤트 로그 서비스  v1.0

SR-3.1: DB 기반 장기 로그 관리
SR-3.2: DB 검색/조회 기능

역할:
  - aiomysql 커넥션 풀 관리 (초기화/종료)
  - 이벤트 로그 비동기 INSERT (fire-and-forget)
  - 이벤트 검색/조회 API 지원

사용:
  from server.db_logger import DBLogger
  db = DBLogger(cfg["database"])
  await db.initialize()

  # 로그 기록 (fire-and-forget, 호출자 블로킹 없음)
  db.log("device_control", "command_router",
         "침실 LED ON", device_id="esp32_home", room="bedroom",
         detail={"cmd":"led","state":"on","pin":5})

  # 검색 (SR-3.2)
  results = await db.search(category="sensor_data",
                            date_from="2026-02-01", limit=50)
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class DBLogger:
    """
    비동기 MySQL 이벤트 로그 서비스

    Parameters
    ----------
    config : dict  (settings.yaml database 블록)
        host, port, user, password, db, pool_size, pool_recycle
    """

    def __init__(self, config: dict):
        self._config = config
        self._pool = None
        self._enabled: bool = config.get("enabled", True)

    # ── 라이프사이클 ──────────────────────────────────────

    async def initialize(self) -> bool:
        """
        커넥션 풀 생성 + 테이블 존재 확인
        main.py lifespan 에서 호출
        Returns: 초기화 성공 여부
        """
        if not self._enabled:
            logger.info("[DB] 비활성화 상태 (enabled=false)")
            return False

        try:
            import aiomysql
            self._pool = await aiomysql.create_pool(
                host=self._config.get("host", "localhost"),
                port=self._config.get("port", 3306),
                user=self._config.get("user", ""),
                password=self._config.get("password", ""),
                db=self._config.get("db", "iot_smart_home"),
                minsize=1,
                maxsize=self._config.get("pool_size", 5),
                pool_recycle=self._config.get("pool_recycle", 3600),
                charset="utf8mb4",
                autocommit=True,
            )

            # 테이블 존재 확인
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SHOW TABLES LIKE 'event_logs'")
                    if not await cur.fetchone():
                        logger.error("[DB] event_logs 테이블 없음 — scripts/init_db.sql 실행 필요")
                        self._enabled = False
                        return False

            logger.info(
                f"[DB] 초기화 완료: {self._config['host']}:{self._config['port']}"
                f"/{self._config['db']} (pool_size={self._config.get('pool_size', 5)})"
            )
            return True

        except Exception as e:
            logger.warning(f"[DB] 초기화 실패 — 이벤트 로그 비활성화: {e}")
            self._enabled = False
            return False

    async def close(self):
        """커넥션 풀 종료. main.py lifespan 종료 시 호출"""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            logger.info("[DB] 커넥션 풀 종료")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 로그 기록 (SR-3.1) ────────────────────────────────

    def log(
        self,
        event_category: str,
        source_module: str,
        summary: str,
        *,
        device_id: str | None = None,
        room: str | None = None,
        detail: dict | None = None,
        level: str = "INFO",
    ) -> None:
        """
        이벤트 로그 비동기 기록 (fire-and-forget)

        asyncio.create_task() 로 실행하므로 호출자는 블로킹되지 않음.
        DB 연결 실패 시에도 기존 동작에 영향 없음.
        """
        if not self._enabled or not self._pool:
            return
        asyncio.create_task(
            self._insert_log(event_category, source_module, summary,
                             device_id, room, detail, level)
        )

    async def _insert_log(
        self,
        event_category: str,
        source_module: str,
        summary: str,
        device_id: str | None,
        room: str | None,
        detail: dict | None,
        level: str,
    ) -> None:
        """실제 INSERT 실행 (내부)"""
        try:
            detail_json = _json.dumps(detail, ensure_ascii=False) if detail else None
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO event_logs "
                        "(event_category, level, source_module, device_id, room, summary, detail) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (event_category, level, source_module,
                         device_id, room, summary, detail_json),
                    )
        except Exception as e:
            logger.warning(f"[DB] INSERT 실패: {e}")

    # ── 보안 미디어 기록 ───────────────────────────────────

    async def log_security_media(
        self,
        event_log_id: int,
        media_type: str,
        file_path: str,
        file_size_bytes: int | None = None,
        duration_sec: float | None = None,
        description: str | None = None,
    ) -> int | None:
        """보안 미디어 메타데이터 INSERT. Returns: media_id 또는 None"""
        if not self._enabled or not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO security_media "
                        "(event_log_id, media_type, file_path, file_size_bytes, "
                        " duration_sec, description) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (event_log_id, media_type, file_path,
                         file_size_bytes, duration_sec, description),
                    )
                    return cur.lastrowid
        except Exception as e:
            logger.warning(f"[DB] security_media INSERT 실패: {e}")
            return None

    # ── 검색/조회 (SR-3.2) ─────────────────────────────────

    async def search(
        self,
        *,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        device_id: str | None = None,
        room: str | None = None,
        level: str | None = None,
        keyword: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        이벤트 로그 검색 (SR-3.2: 날짜/이벤트 종류 별 검색)

        Parameters
        ----------
        category  : event_category 필터 (예: "sensor_data")
        date_from : 시작일 "YYYY-MM-DD" 또는 "YYYY-MM-DD HH:MM:SS"
        date_to   : 종료일
        device_id : 디바이스 ID 필터
        room      : 공간 필터
        level     : 로그 레벨 필터 (INFO/WARN/ERROR)
        keyword   : summary LIKE 검색
        limit     : 최대 반환 건수 (기본 100, 최대 500)
        offset    : 페이지네이션 오프셋

        Returns: list of event_log dicts
        """
        if not self._enabled or not self._pool:
            return []

        where, params = self._build_where(
            category, date_from, date_to, device_id, room, level, keyword
        )

        sql = (
            "SELECT id, created_at, event_category, level, source_module, "
            "       device_id, room, summary, detail "
            f"FROM event_logs {where} "
            "ORDER BY created_at DESC "
            "LIMIT %s OFFSET %s"
        )
        params.extend([min(limit, 500), offset])

        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    return [self._row_to_dict(row) for row in rows]
        except Exception as e:
            logger.warning(f"[DB] search 실패: {e}")
            return []

    async def count(
        self,
        *,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        device_id: str | None = None,
        room: str | None = None,
        level: str | None = None,
        keyword: str | None = None,
    ) -> int:
        """검색 조건에 맞는 총 레코드 수 반환 (페이지네이션용)"""
        if not self._enabled or not self._pool:
            return 0

        where, params = self._build_where(
            category, date_from, date_to, device_id, room, level, keyword
        )

        sql = f"SELECT COUNT(*) FROM event_logs {where}"

        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    row = await cur.fetchone()
                    return row[0] if row else 0
        except Exception as e:
            logger.warning(f"[DB] count 실패: {e}")
            return 0

    async def get_categories(self) -> list[str]:
        """사용된 event_category 목록 반환 (필터 UI용)"""
        if not self._enabled or not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT DISTINCT event_category FROM event_logs "
                        "ORDER BY event_category"
                    )
                    rows = await cur.fetchall()
                    return [r[0] for r in rows]
        except Exception as e:
            logger.warning(f"[DB] get_categories 실패: {e}")
            return []

    async def get_by_id(self, log_id: int) -> dict | None:
        """특정 로그 상세 조회"""
        if not self._enabled or not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, created_at, event_category, level, source_module, "
                        "       device_id, room, summary, detail "
                        "FROM event_logs WHERE id = %s",
                        (log_id,),
                    )
                    row = await cur.fetchone()
                    return self._row_to_dict(row) if row else None
        except Exception as e:
            logger.warning(f"[DB] get_by_id 실패: {e}")
            return None

    async def get_security_media(self, event_log_id: int) -> list[dict]:
        """특정 보안 이벤트의 미디어 목록 조회"""
        if not self._enabled or not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, event_log_id, created_at, media_type, "
                        "       file_path, file_size_bytes, duration_sec, description "
                        "FROM security_media WHERE event_log_id = %s "
                        "ORDER BY created_at",
                        (event_log_id,),
                    )
                    rows = await cur.fetchall()
                    return [
                        {
                            "id": r[0],
                            "event_log_id": r[1],
                            "created_at": r[2].isoformat() if isinstance(r[2], datetime) else str(r[2]),
                            "media_type": r[3],
                            "file_path": r[4],
                            "file_size_bytes": r[5],
                            "duration_sec": r[6],
                            "description": r[7],
                        }
                        for r in rows
                    ]
        except Exception as e:
            logger.warning(f"[DB] get_security_media 실패: {e}")
            return []

    async def get_stats(self) -> dict:
        """
        로그 통계 요약 (대시보드용)
        카테고리별 건수 + 최근 24시간 건수
        """
        if not self._enabled or not self._pool:
            return {"total": 0, "last_24h": 0, "by_category": {}}
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # 전체 건수
                    await cur.execute("SELECT COUNT(*) FROM event_logs")
                    total = (await cur.fetchone())[0]

                    # 최근 24시간
                    await cur.execute(
                        "SELECT COUNT(*) FROM event_logs "
                        "WHERE created_at > NOW() - INTERVAL 24 HOUR"
                    )
                    last_24h = (await cur.fetchone())[0]

                    # 카테고리별 건수
                    await cur.execute(
                        "SELECT event_category, COUNT(*) FROM event_logs "
                        "GROUP BY event_category ORDER BY COUNT(*) DESC"
                    )
                    by_category = {r[0]: r[1] for r in await cur.fetchall()}

                    return {
                        "total": total,
                        "last_24h": last_24h,
                        "by_category": by_category,
                    }
        except Exception as e:
            logger.warning(f"[DB] get_stats 실패: {e}")
            return {"total": 0, "last_24h": 0, "by_category": {}}

    # ── 패턴 분석 (SR-3.3) ─────────────────────────────────

    async def get_hourly_distribution(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
        device_id: str | None = None,
        day_type: str | None = None,
    ) -> list[dict]:
        """시간대별 활동 분포 (24개 항목 보장)"""
        if not self._enabled or not self._pool:
            return []

        conditions: list[str] = []
        params: list = []

        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            if len(date_to) == 10:  # "YYYY-MM-DD" → 하루 끝까지 포함
                params.append(date_to + " 23:59:59")
            else:
                params.append(date_to)
        if category:
            conditions.append("event_category = %s")
            params.append(category)
        if device_id:
            conditions.append("device_id = %s")
            params.append(device_id)
        if day_type == "weekday":
            conditions.append("DAYOFWEEK(created_at) BETWEEN 2 AND 6")
        elif day_type == "weekend":
            conditions.append("DAYOFWEEK(created_at) IN (1, 7)")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = (
            "SELECT HOUR(created_at) AS h, COUNT(*) AS cnt "
            f"FROM event_logs {where} "
            "GROUP BY h ORDER BY h"
        )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    by_hour = {r[0]: r[1] for r in rows}
                    return [{"hour": h, "count": by_hour.get(h, 0)} for h in range(24)]
        except Exception as e:
            logger.warning(f"[DB] get_hourly_distribution 실패: {e}")
            return []

    async def get_daily_timeline(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
        device_id: str | None = None,
    ) -> list[dict]:
        """일별 이벤트 건수 타임라인"""
        if not self._enabled or not self._pool:
            return []

        conditions: list[str] = []
        params: list = []

        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            if len(date_to) == 10:  # "YYYY-MM-DD" → 하루 끝까지 포함
                params.append(date_to + " 23:59:59")
            else:
                params.append(date_to)
        if category:
            conditions.append("event_category = %s")
            params.append(category)
        if device_id:
            conditions.append("device_id = %s")
            params.append(device_id)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = (
            "SELECT DATE(created_at) AS d, COUNT(*) AS cnt "
            f"FROM event_logs {where} "
            "GROUP BY d ORDER BY d"
        )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    return [
                        {"date": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                         "count": r[1]}
                        for r in rows
                    ]
        except Exception as e:
            logger.warning(f"[DB] get_daily_timeline 실패: {e}")
            return []

    async def get_category_distribution(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        device_id: str | None = None,
    ) -> list[dict]:
        """카테고리별 이벤트 분포"""
        if not self._enabled or not self._pool:
            return []

        conditions: list[str] = []
        params: list = []

        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            if len(date_to) == 10:  # "YYYY-MM-DD" → 하루 끝까지 포함
                params.append(date_to + " 23:59:59")
            else:
                params.append(date_to)
        if device_id:
            conditions.append("device_id = %s")
            params.append(device_id)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = (
            "SELECT event_category, COUNT(*) AS cnt "
            f"FROM event_logs {where} "
            "GROUP BY event_category ORDER BY cnt DESC"
        )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    return [{"category": r[0], "count": r[1]} for r in rows]
        except Exception as e:
            logger.warning(f"[DB] get_category_distribution 실패: {e}")
            return []

    async def get_device_activity(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
    ) -> list[dict]:
        """디바이스별 활동량"""
        if not self._enabled or not self._pool:
            return []

        conditions: list[str] = ["device_id IS NOT NULL"]
        params: list = []

        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            if len(date_to) == 10:  # "YYYY-MM-DD" → 하루 끝까지 포함
                params.append(date_to + " 23:59:59")
            else:
                params.append(date_to)
        if category:
            conditions.append("event_category = %s")
            params.append(category)

        where = "WHERE " + " AND ".join(conditions)
        sql = (
            "SELECT device_id, room, COUNT(*) AS cnt "
            f"FROM event_logs {where} "
            "GROUP BY device_id, room ORDER BY cnt DESC"
        )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    return [{"device_id": r[0], "room": r[1], "count": r[2]} for r in rows]
        except Exception as e:
            logger.warning(f"[DB] get_device_activity 실패: {e}")
            return []

    async def get_anomalies(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        threshold: float = 2.0,
    ) -> dict:
        """이상 패턴 탐지 (시간대별 평균 대비 spike/drop)"""
        if not self._enabled or not self._pool:
            return {"avg_by_hour": [], "anomalies": []}

        conditions: list[str] = []
        params: list = []

        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            if len(date_to) == 10:  # "YYYY-MM-DD" → 하루 끝까지 포함
                params.append(date_to + " 23:59:59")
            else:
                params.append(date_to)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = (
            "SELECT DATE(created_at) AS d, HOUR(created_at) AS h, COUNT(*) AS cnt "
            f"FROM event_logs {where} "
            "GROUP BY d, h ORDER BY d, h"
        )
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()

            from collections import defaultdict
            hour_counts: dict[int, list[tuple]] = defaultdict(list)
            for d, h, cnt in rows:
                date_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
                hour_counts[h].append((date_str, cnt))

            avg_by_hour = []
            anomalies = []

            for h in range(24):
                entries = hour_counts.get(h, [])
                if not entries:
                    avg_by_hour.append({"hour": h, "avg": 0})
                    continue
                avg = sum(c for _, c in entries) / len(entries)
                avg_by_hour.append({"hour": h, "avg": round(avg, 1)})
                if avg < 1:
                    continue
                for date_str, cnt in entries:
                    ratio = cnt / avg
                    if ratio >= threshold:
                        anomalies.append({
                            "date": date_str, "hour": h,
                            "count": cnt, "avg": round(avg, 1),
                            "ratio": round(ratio, 1), "type": "spike",
                        })
                    elif ratio <= (1 / threshold) and cnt > 0:
                        anomalies.append({
                            "date": date_str, "hour": h,
                            "count": cnt, "avg": round(avg, 1),
                            "ratio": round(ratio, 1), "type": "drop",
                        })

            anomalies.sort(key=lambda x: x["ratio"], reverse=True)
            return {"avg_by_hour": avg_by_hour, "anomalies": anomalies[:50]}

        except Exception as e:
            logger.warning(f"[DB] get_anomalies 실패: {e}")
            return {"avg_by_hour": [], "anomalies": []}

    # ── 내부 유틸 ──────────────────────────────────────────

    @staticmethod
    def _build_where(
        category: str | None,
        date_from: str | None,
        date_to: str | None,
        device_id: str | None,
        room: str | None,
        level: str | None,
        keyword: str | None,
    ) -> tuple[str, list]:
        """WHERE 절 동적 생성 (파라미터화된 쿼리)"""
        conditions: list[str] = []
        params: list = []

        if category:
            conditions.append("event_category = %s")
            params.append(category)
        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            if len(date_to) == 10:  # "YYYY-MM-DD" → 하루 끝까지 포함
                params.append(date_to + " 23:59:59")
            else:
                params.append(date_to)
        if device_id:
            conditions.append("device_id = %s")
            params.append(device_id)
        if room:
            conditions.append("room = %s")
            params.append(room)
        if level:
            conditions.append("level = %s")
            params.append(level)
        if keyword:
            conditions.append("summary LIKE %s")
            params.append(f"%{keyword}%")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        return where, params

    @staticmethod
    def _row_to_dict(row: tuple) -> dict:
        """SELECT 결과 row → dict 변환"""
        detail_raw = row[8]
        if isinstance(detail_raw, str):
            try:
                detail = _json.loads(detail_raw)
            except (ValueError, TypeError):
                detail = detail_raw
        else:
            detail = detail_raw

        created_at = row[1]
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()
        else:
            created_at = str(created_at)

        return {
            "id": row[0],
            "created_at": created_at,
            "event_category": row[2],
            "level": row[3],
            "source_module": row[4],
            "device_id": row[5],
            "room": row[6],
            "summary": row[7],
            "detail": detail,
        }
