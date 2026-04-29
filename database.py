"""
database.py — 중앙 DB 연결 모듈

DATABASE_URL 환경변수 유무에 따라 자동 전환:
  설정됨  → PostgreSQL (psycopg2 드라이버)
  미설정  → SQLite  (news.db, 로컬 개발용 폴백)

사용법:
    from database import get_conn, IS_POSTGRES, column_exists, db_available
    with get_conn() as conn:
        rows = conn.execute(text("SELECT 1")).fetchall()
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

# ─────────────────────────────────────────
# 엔진 초기화
# ─────────────────────────────────────────
_raw_url = os.environ.get("DATABASE_URL", "").strip()

if _raw_url:
    # Heroku / Railway 는 "postgres://" 접두사를 쓰므로 SQLAlchemy 호환 형식으로 변환
    if _raw_url.startswith("postgres://"):
        _raw_url = _raw_url.replace("postgres://", "postgresql://", 1)
    engine = create_engine(_raw_url, pool_pre_ping=True)
    IS_POSTGRES = True
else:
    engine = create_engine(
        "sqlite:///news.db",
        connect_args={"check_same_thread": False},
    )
    IS_POSTGRES = False


# ─────────────────────────────────────────
# 공개 인터페이스
# ─────────────────────────────────────────
def get_conn():
    """새 SQLAlchemy 커넥션을 반환한다. 호출자가 commit/close 책임."""
    return engine.connect()


def db_available() -> bool:
    """DB에 접근 가능한지 확인."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def column_exists(conn, table: str, column: str) -> bool:
    """테이블에 컬럼이 존재하는지 확인."""
    if IS_POSTGRES:
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).fetchone()
        return row is not None
    else:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(r[1] == column for r in rows)


def insert_ignore_sql(table: str, columns: list[str]) -> str:
    """중복 무시 INSERT SQL을 방언에 맞게 반환."""
    cols   = ", ".join(columns)
    params = ", ".join(f":{c}" for c in columns)
    if IS_POSTGRES:
        return (
            f"INSERT INTO {table} ({cols}) VALUES ({params}) "
            f"ON CONFLICT DO NOTHING"
        )
    else:
        return f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({params})"


def upsert_sql(table: str, columns: list[str], conflict_col: str) -> str:
    """UPSERT SQL을 방언에 맞게 반환 (충돌 시 모든 컬럼 업데이트)."""
    cols    = ", ".join(columns)
    params  = ", ".join(f":{c}" for c in columns)
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in columns if c != conflict_col
    )
    if IS_POSTGRES:
        return (
            f"INSERT INTO {table} ({cols}) VALUES ({params}) "
            f"ON CONFLICT ({conflict_col}) DO UPDATE SET {updates}"
        )
    else:
        return f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({params})"
