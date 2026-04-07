"""
collector.py — Google News RSS 뉴스 수집 엔진
Phase 2: keywords.json의 키워드로 RSS 수집 → news.db 저장

실행 방법:
    python collector.py              # 전체 그룹 수집
    python collector.py --group kg_001  # 특정 그룹만 수집
    python collector.py --test       # 실제 저장 없이 테스트

필요 라이브러리 설치:
    pip install feedparser requests
"""

import sqlite3
import json
import os
import time
import argparse
from datetime import datetime, timedelta
from urllib.parse import quote

# feedparser, requests는 실행 시 import
try:
    import feedparser
    import requests
    LIBRARIES_OK = True
except ImportError:
    LIBRARIES_OK = False

# ─────────────────────────────────────────
# 설정값
# ─────────────────────────────────────────
DB_FILE           = "news.db"
KEYWORDS_FILE     = "keywords.json"
REQUEST_DELAY     = 2          # 요청 간 대기 시간(초) — 구글 차단 방지
MAX_ENTRIES       = 100        # RSS 한 번에 최대 수집 건수
REQUEST_TIMEOUT   = 10         # HTTP 요청 타임아웃(초)
USER_AGENT        = "Mozilla/5.0 (compatible; NewsCollector/1.0)"

# Google News RSS 기본 URL
RSS_BASE_KO = "https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
RSS_BASE_EN = "https://news.google.com/rss/search?q={query}&hl=en&gl=US&ceid=US:en"


# ─────────────────────────────────────────
# 1. 키워드 → RSS 검색 쿼리 생성
# ─────────────────────────────────────────
def build_query(query_dict):
    """
    query_dict = {"must": ["GTT"], "any": ["수주","계약"], "exclude": ["주가"]}
    → 'GTT (수주 OR 계약) -주가'
    """
    parts = []

    must = query_dict.get("must", [])
    any_ = query_dict.get("any",  [])
    excl = query_dict.get("exclude", [])

    # 필수 키워드 (AND)
    for kw in must:
        parts.append(kw)

    # 선택 키워드 (OR)
    if any_:
        if len(any_) == 1:
            parts.append(any_[0])
        else:
            parts.append(f"({' OR '.join(any_)})")

    # 제외 키워드 (NOT)
    for kw in excl:
        parts.append(f"-{kw}")

    return " ".join(parts)


def build_rss_url(query_str, language="ko"):
    """검색 쿼리 → RSS URL 생성"""
    encoded = quote(query_str)
    if language == "ko":
        return RSS_BASE_KO.format(query=encoded)
    else:
        return RSS_BASE_EN.format(query=encoded)


# ─────────────────────────────────────────
# 2. RSS 파싱
# ─────────────────────────────────────────
def parse_rss(url, language="ko"):
    """RSS URL → 기사 목록 반환"""
    articles = []

    try:
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        feed = feedparser.parse(response.content)

        for entry in feed.entries[:MAX_ENTRIES]:
            # 발행일 파싱
            published_at = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published_at = datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%dT%H:%M:%S")

            # 언론사명 추출
            source = ""
            if hasattr(entry, "source") and hasattr(entry.source, "title"):
                source = entry.source.title
            elif hasattr(entry, "tags") and entry.tags:
                source = entry.tags[0].get("term", "")

            # URL 정리 (Google 리다이렉트 URL 처리)
            url_clean = entry.get("link", "")

            articles.append({
                "title":        entry.get("title", "").strip(),
                "url":          url_clean,
                "source":       source,
                "language":     language,
                "published_at": published_at,
            })

    except requests.exceptions.RequestException as e:
        print(f"    ⚠️  요청 실패: {e}")
    except Exception as e:
        print(f"    ⚠️  파싱 오류: {e}")

    return articles


# ─────────────────────────────────────────
# 3. DB 저장
# ─────────────────────────────────────────
def save_articles(conn, articles, group_id, group_name):
    """기사 목록을 DB에 저장. 중복 URL은 자동 무시."""
    cursor = conn.cursor()
    saved  = 0
    duplicates = 0
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    for a in articles:
        if not a.get("title") or not a.get("url"):
            continue
        try:
            cursor.execute("""
                INSERT INTO articles
                (keyword_group_id, keyword_group_name, title, url,
                 source, language, published_at, collected_at,
                 is_analyzed, retention)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '1year')
            """, (
                group_id,
                group_name,
                a["title"],
                a["url"],
                a.get("source", ""),
                a.get("language", "ko"),
                a.get("published_at"),
                now
            ))
            saved += 1
        except sqlite3.IntegrityError:
            # URL UNIQUE 제약 위반 = 중복 기사 → 조용히 무시
            duplicates += 1

    conn.commit()
    return saved, duplicates


# ─────────────────────────────────────────
# 4. stats 업데이트
# ─────────────────────────────────────────
def update_stats(conn, group_id):
    """DB에서 집계해서 실제 통계 반환"""
    cursor = conn.cursor()
    now    = datetime.now()
    d7     = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    d30    = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

    cursor.execute("SELECT COUNT(*) FROM articles WHERE keyword_group_id=?", (group_id,))
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM articles WHERE keyword_group_id=? AND collected_at>=?", (group_id, d7))
    count_7d = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM articles WHERE keyword_group_id=? AND collected_at>=?", (group_id, d30))
    count_30d = cursor.fetchone()[0]

    return {
        "last_collected_at":   now.strftime("%Y-%m-%dT%H:%M:%S"),
        "article_count_7d":    count_7d,
        "article_count_30d":   count_30d,
        "total_article_count": total
    }


def sync_stats_to_json(stats_map):
    """수집 통계를 keywords.json에 반영"""
    if not os.path.exists(KEYWORDS_FILE):
        return

    with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    for g in data.get("keyword_groups", []):
        gid = g.get("id")
        if gid in stats_map:
            g["stats"] = stats_map[gid]

    data["metadata"]["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sync_stats_to_db(conn, group_id, stats):
    """수집 통계를 DB keyword_groups 테이블에도 반영"""
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE keyword_groups
        SET last_collected_at = ?, updated_at = ?
        WHERE id = ?
    """, (stats["last_collected_at"], stats["last_collected_at"], group_id))
    conn.commit()


# ─────────────────────────────────────────
# 5. 메인 수집 로직
# ─────────────────────────────────────────
def collect_group(conn, group, test_mode=False):
    """그룹 1개 수집 실행"""
    gid        = group["id"]
    gname      = group["group_name"]
    query_kr   = group.get("query",    {})
    query_en   = group.get("query_en", {})

    total_saved = 0
    total_dupl  = 0

    # ── 한국어 RSS 수집 ──
    if query_kr.get("must") or query_kr.get("any"):
        q_str = build_query(query_kr)
        url   = build_rss_url(q_str, "ko")
        print(f"    🇰🇷 한국어 검색: {q_str}")
        print(f"       URL: {url}")

        if not test_mode:
            articles = parse_rss(url, "ko")
            saved, dupl = save_articles(conn, articles, gid, gname)
            total_saved += saved
            total_dupl  += dupl
            print(f"       → 수집 {len(articles)}건 / 저장 {saved}건 / 중복 {dupl}건")
            time.sleep(REQUEST_DELAY)
        else:
            print(f"       → [테스트 모드] 실제 저장 생략")

    # ── 영어 RSS 수집 ──
    if query_en.get("must") or query_en.get("any"):
        q_str = build_query(query_en)
        url   = build_rss_url(q_str, "en")
        print(f"    🇺🇸 영어 검색: {q_str}")
        print(f"       URL: {url}")

        if not test_mode:
            articles = parse_rss(url, "en")
            saved, dupl = save_articles(conn, articles, gid, gname)
            total_saved += saved
            total_dupl  += dupl
            print(f"       → 수집 {len(articles)}건 / 저장 {saved}건 / 중복 {dupl}건")
            time.sleep(REQUEST_DELAY)
        else:
            print(f"       → [테스트 모드] 실제 저장 생략")

    return total_saved, total_dupl


def run_collector(target_group_id=None, test_mode=False):
    """전체 수집 실행"""

    # ── 라이브러리 체크 ──
    if not LIBRARIES_OK:
        print("❌ 필요한 라이브러리가 없습니다.")
        print("   pip install feedparser requests")
        return

    # ── DB 연결 ──
    if not os.path.exists(DB_FILE):
        print(f"❌ {DB_FILE} 파일이 없습니다. 먼저 db_setup.py를 실행해주세요.")
        return

    conn = sqlite3.connect(DB_FILE)

    # ── keywords.json 읽기 ──
    if not os.path.exists(KEYWORDS_FILE):
        print(f"❌ {KEYWORDS_FILE} 파일이 없습니다.")
        conn.close()
        return

    with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    groups = data.get("keyword_groups", [])

    # 활성 그룹만 필터
    active_groups = [g for g in groups if g.get("is_active", True)]

    # 특정 그룹 지정 시 필터
    if target_group_id:
        active_groups = [g for g in active_groups if g["id"] == target_group_id]
        if not active_groups:
            print(f"❌ 그룹 ID '{target_group_id}'를 찾을 수 없습니다.")
            conn.close()
            return

    # ── 수집 시작 ──
    start_time = datetime.now()
    print("=" * 55)
    print(f"  📡 뉴스 수집 시작  {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    if test_mode:
        print("  ⚠️  테스트 모드 — 실제 DB 저장 없음")
    print("=" * 55)

    grand_saved = 0
    grand_dupl  = 0
    stats_map   = {}

    for i, group in enumerate(active_groups, 1):
        print(f"\n[{i}/{len(active_groups)}] {group['group_name']} ({group['id']})")

        saved, dupl = collect_group(conn, group, test_mode)
        grand_saved += saved
        grand_dupl  += dupl

        if not test_mode:
            # stats 업데이트
            stats = update_stats(conn, group["id"])
            stats_map[group["id"]] = stats
            sync_stats_to_db(conn, group["id"], stats)
            print(f"    📊 누적 통계: 7일 {stats['article_count_7d']}건 / "
                  f"30일 {stats['article_count_30d']}건 / "
                  f"전체 {stats['total_article_count']}건")

    # ── keywords.json 통계 동기화 ──
    if not test_mode and stats_map:
        sync_stats_to_json(stats_map)
        print(f"\n✅ keywords.json 통계 업데이트 완료")

    # ── 완료 요약 ──
    elapsed = (datetime.now() - start_time).seconds
    print("\n" + "=" * 55)
    print(f"  ✅ 수집 완료")
    print(f"  그룹 수:    {len(active_groups)}개")
    print(f"  신규 저장:  {grand_saved}건")
    print(f"  중복 제외:  {grand_dupl}건")
    print(f"  소요 시간:  {elapsed}초")
    print("=" * 55)

    conn.close()


# ─────────────────────────────────────────
# 실행 진입점
# ─────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google News RSS 수집기")
    parser.add_argument("--group", type=str, help="특정 그룹 ID만 수집 (예: kg_001)")
    parser.add_argument("--test",  action="store_true", help="테스트 모드 (저장 안 함)")
    args = parser.parse_args()

    run_collector(
        target_group_id=args.group,
        test_mode=args.test
    )
