"""
db_setup.py — DB 테이블 생성 및 초기화

DATABASE_URL 환경변수가 있으면 PostgreSQL, 없으면 SQLite(news.db)에 생성.

실행:
    python db_setup.py
"""

import json
import os
from datetime import datetime

from sqlalchemy import (
    Column, Index, Integer, MetaData, SmallInteger,
    String, Table, Text, text,
)

from database import engine, get_conn, IS_POSTGRES, upsert_sql

KEYWORDS_FILE = "keywords.json"

# ─────────────────────────────────────────
# 테이블 정의 (SQLAlchemy MetaData)
# ─────────────────────────────────────────
metadata = MetaData()

keyword_groups = Table(
    "keyword_groups", metadata,
    Column("id",                String(20), primary_key=True),
    Column("group_name",        Text,       nullable=False),
    Column("description",       Text),
    Column("is_active",         SmallInteger, default=1),
    Column("created_at",        Text),
    Column("tags",              Text),
    Column("keywords_label",    Text),
    Column("query_kr",          Text),
    Column("query_en",          Text),
    Column("last_collected_at", Text),
    Column("updated_at",        Text),
)

articles = Table(
    "articles", metadata,
    Column("id",                Integer, primary_key=True, autoincrement=True),
    Column("keyword_group_id",  Text,    nullable=False),
    Column("keyword_group_name",Text),
    Column("title",             Text,    nullable=False),
    Column("url",               Text,    unique=True),
    Column("source",            Text),
    Column("language",          Text),
    Column("published_at",      Text),
    Column("collected_at",      Text),
    Column("summary",           Text),
    Column("score_relevance",   SmallInteger),
    Column("score_importance",  SmallInteger),
    Column("analyzed_at",       Text),
    Column("is_analyzed",       SmallInteger, default=0),
    Column("retention",         Text,   default="1year"),
    Column("keywords",          Text),
)

article_feedback = Table(
    "article_feedback", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("article_id", Integer, nullable=False),
    Column("feedback",   Text),
    Column("memo",       Text),
    Column("created_at", Text),
)

monthly_stats = Table(
    "monthly_stats", metadata,
    Column("id",                Integer, primary_key=True, autoincrement=True),
    Column("year_month",        Text),
    Column("keyword_group_id",  Text),
    Column("keyword_group_name",Text),
    Column("article_count",     Integer, default=0),
    Column("avg_relevance",     Text),
    Column("avg_importance",    Text),
    Column("top_sources",       Text),
    Column("created_at",        Text),
)

# ─────────────────────────────────────────
# 인덱스
# ─────────────────────────────────────────
Index("idx_articles_group",    articles.c.keyword_group_id)
Index("idx_articles_pub",      articles.c.published_at)
Index("idx_articles_analyzed", articles.c.is_analyzed)
Index("idx_articles_collect",  articles.c.collected_at)
Index("idx_articles_score",    articles.c.score_importance, articles.c.score_relevance)
Index("idx_articles_lang",     articles.c.language)
Index("idx_stats_month",       monthly_stats.c.year_month, monthly_stats.c.keyword_group_id)


# ─────────────────────────────────────────
# 테이블 생성
# ─────────────────────────────────────────
def create_database():
    """테이블과 인덱스를 생성(이미 존재하면 무시)."""
    metadata.create_all(engine, checkfirst=True)
    conn = get_conn()
    return conn


# ─────────────────────────────────────────
# keywords.json → keyword_groups 동기화
# ─────────────────────────────────────────
def load_keywords_json():
    if not os.path.exists(KEYWORDS_FILE):
        print(f"⚠️  {KEYWORDS_FILE} 파일이 없습니다.")
        return None
    with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def sync_keyword_groups(conn, data):
    """keywords.json 내용을 keyword_groups 테이블에 동기화."""
    groups = data.get("keyword_groups", [])
    now    = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    cols = [
        "id", "group_name", "description", "is_active", "created_at",
        "tags", "keywords_label", "query_kr", "query_en",
        "last_collected_at", "updated_at",
    ]
    sql = text(upsert_sql("keyword_groups", cols, "id"))

    for g in groups:
        conn.execute(sql, {
            "id":                g.get("id"),
            "group_name":        g.get("group_name"),
            "description":       g.get("description"),
            "is_active":         1 if g.get("is_active", True) else 0,
            "created_at":        g.get("created_at"),
            "tags":              json.dumps(g.get("tags",            []), ensure_ascii=False),
            "keywords_label":    json.dumps(g.get("keywords_label",  []), ensure_ascii=False),
            "query_kr":          json.dumps(g.get("query",           {}), ensure_ascii=False),
            "query_en":          json.dumps(g.get("query_en",        {}), ensure_ascii=False),
            "last_collected_at": g.get("stats", {}).get("last_collected_at"),
            "updated_at":        now,
        })

    conn.commit()
    return len(groups)


def verify_database(conn):
    """생성된 테이블·인덱스 목록과 keyword_groups 건수 반환."""
    if IS_POSTGRES:
        tables = [
            r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' ORDER BY table_name"
            )).fetchall()
        ]
        indexes = [
            r[0] for r in conn.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname='public' ORDER BY indexname"
            )).fetchall()
        ]
    else:
        tables  = [r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )).fetchall()]
        indexes = [r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        )).fetchall()]

    kg_count = conn.execute(text("SELECT COUNT(*) FROM keyword_groups")).fetchone()[0]
    return tables, indexes, kg_count


# ─────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────
if __name__ == "__main__":
    db_type = "PostgreSQL" if IS_POSTGRES else "SQLite (news.db)"
    print("=" * 50)
    print(f"  DB 생성 시작 [{db_type}]")
    print("=" * 50)

    conn = create_database()
    print("\n✅ 테이블 생성 완료")

    data = load_keywords_json()
    if data:
        count = sync_keyword_groups(conn, data)
        print(f"✅ keyword_groups 동기화 완료: {count}개 그룹")

    tables, indexes, kg_count = verify_database(conn)

    print("\n📋 생성된 테이블:")
    for t in tables:
        print(f"   - {t}")

    print("\n⚡ 생성된 인덱스:")
    for i in indexes:
        print(f"   - {i}")

    print(f"\n🔑 keyword_groups: {kg_count}개 그룹")
    conn.close()
    print(f"\n✅ [{db_type}] 초기화 완료!")
    print("=" * 50)
