import sqlite3
import json
import os
from datetime import datetime

DB_FILE = "news.db"

def create_database():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # ─────────────────────────────────────────
    # 테이블 1: keyword_groups
    # keywords.json의 내용을 DB로 관리
    # ─────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS keyword_groups (
            id              TEXT PRIMARY KEY,
            group_name      TEXT NOT NULL,
            description     TEXT,
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT,
            tags            TEXT,
            keywords_label  TEXT,
            query_kr        TEXT,
            query_en        TEXT,
            last_collected_at TEXT,
            updated_at      TEXT
        )
    """)

    # ─────────────────────────────────────────
    # 테이블 2: articles
    # 수집된 뉴스 기사 저장
    # ─────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword_group_id    TEXT NOT NULL,
            keyword_group_name  TEXT,
            title               TEXT NOT NULL,
            url                 TEXT UNIQUE,
            source              TEXT,
            language            TEXT,
            published_at        TEXT,
            collected_at        TEXT,
            summary             TEXT,
            score_relevance     INTEGER,
            score_importance    INTEGER,
            analyzed_at         TEXT,
            is_analyzed         INTEGER DEFAULT 0,
            retention           TEXT DEFAULT '1year'
        )
    """)

    # ─────────────────────────────────────────
    # 테이블 3: article_feedback
    # 사용자 피드백 저장 (Phase 4)
    # ─────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS article_feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id  INTEGER NOT NULL,
            feedback    TEXT,
            memo        TEXT,
            created_at  TEXT
        )
    """)

    # ─────────────────────────────────────────
    # 테이블 4: monthly_stats
    # 월별 통계 (기사 삭제 후에도 트렌드 보존)
    # ─────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS monthly_stats (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            year_month        TEXT,
            keyword_group_id  TEXT,
            keyword_group_name TEXT,
            article_count     INTEGER DEFAULT 0,
            avg_relevance     REAL,
            avg_importance    REAL,
            top_sources       TEXT,
            created_at        TEXT
        )
    """)

    # ─────────────────────────────────────────
    # 인덱스 생성 (조회 속도 최적화)
    # ─────────────────────────────────────────
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_group    ON articles(keyword_group_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_pub      ON articles(published_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_analyzed ON articles(is_analyzed)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_collect  ON articles(collected_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_score    ON articles(score_importance, score_relevance)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_articles_lang     ON articles(language)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stats_month       ON monthly_stats(year_month, keyword_group_id)")

    conn.commit()
    return conn, cursor


def load_keywords_json():
    if not os.path.exists("keywords.json"):
        print("⚠️  keywords.json 파일이 없습니다. keyword_groups 테이블은 비어있습니다.")
        return None
    with open("keywords.json", "r", encoding="utf-8") as f:
        return json.load(f)


def sync_keyword_groups(cursor, data):
    """keywords.json의 내용을 keyword_groups 테이블에 동기화"""
    groups = data.get("keyword_groups", [])
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    for g in groups:
        cursor.execute("""
            INSERT OR REPLACE INTO keyword_groups
            (id, group_name, description, is_active, created_at,
             tags, keywords_label, query_kr, query_en,
             last_collected_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            g.get("id"),
            g.get("group_name"),
            g.get("description"),
            1 if g.get("is_active", True) else 0,
            g.get("created_at"),
            json.dumps(g.get("tags", []),           ensure_ascii=False),
            json.dumps(g.get("keywords_label", []), ensure_ascii=False),
            json.dumps(g.get("query",    {}),       ensure_ascii=False),
            json.dumps(g.get("query_en", {}),       ensure_ascii=False),
            g.get("stats", {}).get("last_collected_at"),
            now
        ))

    return len(groups)


def verify_database(cursor):
    """생성된 테이블과 인덱스 확인"""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
    indexes = [row[0] for row in cursor.fetchall()]

    cursor.execute("SELECT COUNT(*) FROM keyword_groups")
    kg_count = cursor.fetchone()[0]

    return tables, indexes, kg_count


# ─────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  news.db 생성 시작")
    print("=" * 50)

    # 1. DB 및 테이블 생성
    conn, cursor = create_database()
    print("\n✅ 테이블 생성 완료")

    # 2. keywords.json → keyword_groups 테이블 동기화
    data = load_keywords_json()
    if data:
        count = sync_keyword_groups(cursor, data)
        conn.commit()
        print(f"✅ keyword_groups 동기화 완료: {count}개 그룹")

    # 3. 검증 및 결과 출력
    tables, indexes, kg_count = verify_database(cursor)

    print("\n📋 생성된 테이블:")
    for t in tables:
        print(f"   - {t}")

    print("\n⚡ 생성된 인덱스:")
    for i in indexes:
        print(f"   - {i}")

    print(f"\n🔑 keyword_groups 테이블: {kg_count}개 그룹 저장됨")

    conn.close()

    print(f"\n✅ {DB_FILE} 생성 완료!")
    print("=" * 50)
