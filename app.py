import streamlit as st
import json
import os
import re
import pandas as pd
from collections import Counter
from datetime import datetime, date, timedelta

from anthropic import Anthropic
from dotenv import load_dotenv
from sqlalchemy import text

from database import get_conn, db_available, IS_POSTGRES

load_dotenv()

# ─────────────────────────────────────────
# 페이지 설정
# ─────────────────────────────────────────
st.set_page_config(
    page_title="키워드 관리 시스템",
    page_icon="🔑",
    layout="wide"
)

KEYWORDS_FILE = "keywords.json"
DB_FILE       = "news.db"

# ─────────────────────────────────────────
# 태그 색상 매핑 (분류용 태그별 고정 색상)
# ─────────────────────────────────────────
TAG_COLORS = {
    "경쟁사":    "#e74c3c",
    "자사기술":  "#2980b9",
    "시장동향":  "#27ae60",
    "친환경선박":"#8e44ad",
    "규제·인증": "#e67e22",
    "기술동향":  "#16a085",
}
DEFAULT_TAG_COLOR  = "#7f8c8d"
LABEL_COLOR        = "#bdc3c7"   # keywords_label 뱃지 (회색)
DEFAULT_NEWS_DAYS  = 30
MAX_ARTICLES_SHOWN = 100

# ─────────────────────────────────────────
# DB 조회
# ─────────────────────────────────────────
def load_keyword_groups_from_db():
    """keyword_groups 테이블에서 활성 그룹 목록 반환."""
    if not db_available():
        return []
    conn = get_conn()
    rows = conn.execute(
        text("SELECT id, group_name FROM keyword_groups WHERE is_active=1 ORDER BY group_name")
    ).fetchall()
    conn.close()
    return rows


def load_articles(group_id=None, date_from=None, date_to=None, limit=MAX_ARTICLES_SHOWN):
    """최근 분석 완료 기사를 관련도 내림차순으로 반환."""
    if not db_available():
        return []
    conn  = get_conn()
    where = "WHERE a.is_analyzed = 1"
    params: dict = {}

    if group_id:
        where += " AND a.keyword_group_id = :gid"
        params["gid"] = group_id
    if date_from:
        where += " AND a.published_at >= :dfrom"
        params["dfrom"] = str(date_from)
    if date_to:
        where += " AND a.published_at <= :dto"
        params["dto"] = str(date_to) + "T23:59:59"

    sql = text(
        f"SELECT a.id, a.title, a.url, a.source, a.published_at, "
        f"       a.score_relevance, a.score_importance, "
        f"       a.summary, a.keywords, k.group_name "
        f"FROM   articles a "
        f"LEFT JOIN keyword_groups k ON a.keyword_group_id = k.id "
        f"{where} "
        f"ORDER BY a.score_relevance DESC, a.score_importance DESC, a.published_at DESC "
        f"LIMIT :limit"
    )
    params["limit"] = limit
    rows = conn.execute(sql, params).mappings().fetchall()
    conn.close()
    return rows


# ─────────────────────────────────────────
# 점수 뱃지
# ─────────────────────────────────────────
def score_badge_html(score, label):
    if score is None:
        color, val = "#95a5a6", "-"
    elif score >= 8:
        color, val = "#27ae60", str(score)
    elif score >= 5:
        color, val = "#e67e22", str(score)
    else:
        color, val = "#e74c3c", str(score)
    return (
        f'<span style="background:{color};color:white;padding:3px 11px;'
        f'border-radius:12px;font-size:13px;font-weight:bold;margin-right:4px;">'
        f'{label} {val}</span>'
    )


def keyword_tag_html(word):
    return (
        f'<span style="background:#2c3e50;color:#ecf0f1;padding:2px 9px;'
        f'border-radius:10px;font-size:12px;margin:2px;display:inline-block;">'
        f'{word}</span>'
    )


# ─────────────────────────────────────────
# 피드백 DB 함수
# ─────────────────────────────────────────
def save_feedback(article_id: int, feedback: str) -> None:
    """피드백을 article_feedback 테이블에 저장 (기존 값은 대체)."""
    conn = get_conn()
    conn.execute(
        text("DELETE FROM article_feedback WHERE article_id = :aid"),
        {"aid": article_id},
    )
    conn.execute(
        text(
            "INSERT INTO article_feedback (article_id, feedback, created_at) "
            "VALUES (:aid, :fb, :ts)"
        ),
        {"aid": article_id, "fb": feedback, "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")},
    )
    conn.commit()
    conn.close()


def load_feedback_map(article_ids: list) -> dict:
    """article_id → 최신 feedback 값 딕셔너리 반환."""
    if not article_ids:
        return {}
    conn = get_conn()
    # IN 절은 방언별로 처리
    placeholders = ",".join(str(i) for i in article_ids)
    rows = conn.execute(
        text(
            f"SELECT article_id, feedback FROM article_feedback "
            f"WHERE  article_id IN ({placeholders}) "
            f"ORDER  BY created_at DESC"
        )
    ).fetchall()
    conn.close()
    result = {}
    for aid, fb in rows:
        if aid not in result:
            result[aid] = fb
    return result


# ─────────────────────────────────────────
# JSON 읽기 / 쓰기
# ─────────────────────────────────────────
def load_keywords():
    if os.path.exists(KEYWORDS_FILE):
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"metadata": {}, "keyword_groups": []}

def save_keywords(data):
    data["metadata"]["last_updated"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    data["metadata"]["total_groups"]  = len(data["keyword_groups"])
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_id(groups):
    existing = {g["id"] for g in groups}
    for i in range(1, 999):
        nid = f"kg_{i:03d}"
        if nid not in existing:
            return nid

def parse_lines(text):
    return [l.strip() for l in text.strip().split("\n") if l.strip()]

# ─────────────────────────────────────────
# 피드백 통계 조회
# ─────────────────────────────────────────
def load_feedback_stats() -> dict:
    """피드백 집계 결과를 딕셔너리로 반환."""
    if not db_available():
        return {}
    conn = get_conn()

    row = conn.execute(text("""
        SELECT COUNT(*),
               SUM(CASE WHEN feedback='good' THEN 1 ELSE 0 END),
               SUM(CASE WHEN feedback='bad'  THEN 1 ELSE 0 END)
        FROM article_feedback
    """)).fetchone()
    if not row or row[0] == 0:
        conn.close()
        return {}
    total, good, bad = row

    by_group = conn.execute(text("""
        SELECT COALESCE(a.keyword_group_name, '(미분류)') AS grp,
               SUM(CASE WHEN f.feedback='good' THEN 1 ELSE 0 END) AS good,
               SUM(CASE WHEN f.feedback='bad'  THEN 1 ELSE 0 END) AS bad,
               COUNT(*) AS total
        FROM article_feedback f
        JOIN articles a ON f.article_id = a.id
        GROUP BY grp
        ORDER BY good DESC
    """)).fetchall()

    by_source = conn.execute(text("""
        SELECT COALESCE(a.source, '(출처없음)') AS src,
               SUM(CASE WHEN f.feedback='good' THEN 1 ELSE 0 END) AS good,
               SUM(CASE WHEN f.feedback='bad'  THEN 1 ELSE 0 END) AS bad,
               COUNT(*) AS total,
               ROUND(100.0 * SUM(CASE WHEN f.feedback='good' THEN 1 ELSE 0 END)
                           / COUNT(*), 1) AS good_rate
        FROM article_feedback f
        JOIN articles a ON f.article_id = a.id
        WHERE a.source IS NOT NULL AND a.source != ''
        GROUP BY src
        HAVING COUNT(*) >= 1
        ORDER BY good_rate DESC, total DESC
    """)).fetchall()

    top_good = conn.execute(text("""
        SELECT a.title, a.source, a.score_relevance, a.score_importance,
               a.keyword_group_name, a.published_at
        FROM article_feedback f
        JOIN articles a ON f.article_id = a.id
        WHERE f.feedback = 'good'
        ORDER BY a.score_relevance DESC, a.score_importance DESC
        LIMIT 10
    """)).fetchall()

    top_bad = conn.execute(text("""
        SELECT a.title, a.source, a.score_relevance, a.keyword_group_name
        FROM article_feedback f
        JOIN articles a ON f.article_id = a.id
        WHERE f.feedback = 'bad'
        ORDER BY f.created_at DESC
        LIMIT 5
    """)).fetchall()

    conn.close()
    return {
        "total": total, "good": good, "bad": bad,
        "by_group": by_group,
        "by_source": by_source,
        "top_good": top_good,
        "top_bad": top_bad,
    }


# ─────────────────────────────────────────
# 키워드 추천 함수
# ─────────────────────────────────────────
_KR_STOPWORDS = {
    "관련", "위해", "대한", "통해", "따른", "이후", "이상", "이하",
    "지난", "오늘", "내일", "올해", "국내", "해외", "전년", "최근",
    "업체", "회사", "기업", "관계", "이번", "현재", "사업", "진행",
    "계획", "추진", "발표", "완료", "시작", "예정", "기준", "방안",
}
_EN_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "are",
    "was", "has", "its", "not", "new", "said", "will", "more",
    "than", "also", "have", "been", "but", "his", "her", "they",
}


def get_recent_article_titles(days: int = 30) -> list:
    """최근 N일 수집 기사 제목 목록 반환."""
    if not db_available():
        return []
    conn   = get_conn()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows   = conn.execute(
        text(
            "SELECT title FROM articles "
            "WHERE collected_at >= :cutoff AND title IS NOT NULL LIMIT 800"
        ),
        {"cutoff": cutoff},
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]


def get_all_existing_keywords(data: dict) -> set:
    """keywords.json에서 모든 키워드를 소문자 집합으로 반환."""
    kws = set()
    for g in data.get("keyword_groups", []):
        for section in ("query", "query_en"):
            q = g.get(section, {})
            kws.update(w.lower() for w in q.get("must",    []))
            kws.update(w.lower() for w in q.get("any",     []))
            kws.update(w.lower() for w in q.get("exclude", []))
    return kws


def extract_word_frequencies(titles: list, existing_kws: set) -> list:
    """제목에서 자주 등장하는 단어 추출 (기존 키워드·불용어 제외)."""
    counter = Counter()
    for title in titles:
        words = re.findall(r"[가-힣]{2,}|[A-Za-z]{3,}", title)
        counter.update(w.lower() for w in words)

    stopwords = _KR_STOPWORDS | _EN_STOPWORDS
    candidates = [
        (word, cnt)
        for word, cnt in counter.most_common(100)
        if word not in stopwords and word not in existing_kws and cnt >= 2
    ]
    return candidates[:50]


def get_keyword_recommendations(
    candidates: list,
    groups: list,
    titles_sample: list,
) -> list:
    """Claude에 키워드 추천을 요청하고 JSON 목록을 반환."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = Anthropic(api_key=api_key)

    group_summary = "\n".join(
        f"- {g['group_name']}: {', '.join(g.get('keywords_label', []))}"
        for g in groups if g.get("is_active", True)
    )
    word_list    = "\n".join(f"  {w} ({c}회)" for w, c in candidates[:30])
    sample_text  = "\n".join(f"  - {t}" for t in titles_sample[:20])

    prompt = f"""당신은 LNG 화물창·에너지 산업 전문가입니다.
최근 수집된 뉴스 제목을 분석해 추적 가치가 있는 신규 키워드를 추천해주세요.

## 현재 키워드 그룹
{group_summary}

## 자주 등장하지만 아직 미추적 단어
{word_list}

## 뉴스 제목 샘플 (최근 20건)
{sample_text}

반드시 아래 JSON 배열 형식으로만 답해. 다른 말 금지:
[
  {{
    "keyword_kr": "한국어 키워드",
    "keyword_en": "영어 키워드 또는 null",
    "reason": "추천 이유 (1~2문장)",
    "suggested_group": "기존 그룹명 중 가장 적합한 것",
    "priority": "high|medium|low"
  }}
]"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw   = message.content[0].text
    clean = re.sub(r"```(?:json)?|```", "", raw).strip()
    start = clean.find("[")
    end   = clean.rfind("]") + 1
    if start == -1:
        raise ValueError(f"JSON 배열을 찾을 수 없음:\n{raw}")
    return json.loads(clean[start:end])


# ─────────────────────────────────────────
# HTML 뱃지 렌더링
# ─────────────────────────────────────────
def badge(text, color, font_size="13px"):
    return (f'<span style="background:{color};color:white;padding:2px 10px;'
            f'border-radius:12px;margin:2px;font-size:{font_size};'
            f'display:inline-block;">{text}</span>')

def render_badges(items, color, font_size="13px"):
    return "".join(badge(i, color, font_size) for i in items)

def render_tag_badges(tags):
    return "".join(
        badge(t, TAG_COLORS.get(t, DEFAULT_TAG_COLOR), "13px")
        for t in tags
    )

# ─────────────────────────────────────────
# 사이드바
# ─────────────────────────────────────────
with st.sidebar:
    st.title("🔑 키워드 관리")
    st.caption("LNG 화물창 · 암모니아 · 수소 선박")
    st.divider()

    menu = st.radio(
        "메뉴",
        ["📰 뉴스 리스트", "📈 피드백 통계", "🔍 키워드 추천", "📋 키워드 목록", "📊 태그별 현황", "➕ 새 그룹 추가"],
        label_visibility="collapsed"
    )

    # ── 뉴스 리스트 전용 필터 ──────────────────
    news_group_id = None
    news_date_from = None
    news_date_to   = None

    if menu == "📰 뉴스 리스트":
        st.divider()
        st.caption("**필터**")

        kg_rows = load_keyword_groups_from_db()
        kg_map  = {"전체": None, **{name: gid for gid, name in kg_rows}}
        sel_name = st.selectbox("키워드 그룹", options=list(kg_map.keys()))
        news_group_id = kg_map[sel_name]

        today = date.today()
        default_s = today - timedelta(days=DEFAULT_NEWS_DAYS)
        news_date_from = default_s
        news_date_to   = today
        use_date = st.checkbox("날짜 범위 필터 사용", value=True)
        if use_date:

            date_range = st.date_input(
                "발행일 범위",
                value=(default_s, today),
                max_value=today,
            )
            if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
                news_date_from, news_date_to = date_range
            elif isinstance(date_range, (list, tuple)) and len(date_range) == 1:
                news_date_from = date_range[0]
        else:
            news_date_from = None
            news_date_to   = None

        min_rel = st.slider("최소 관련도 점수", 1, 10, 1)

    st.divider()

    data   = load_keywords()
    groups = data.get("keyword_groups", [])

    total  = len(groups)
    active = sum(1 for g in groups if g.get("is_active", True))
    st.metric("전체 그룹", total)
    st.metric("활성 그룹", active)

    # 태그 범례
    st.divider()
    st.caption("**태그 분류 범례**")
    for tag, color in TAG_COLORS.items():
        st.markdown(
            f'<span style="background:{color};color:white;padding:2px 10px;'
            f'border-radius:12px;font-size:12px;">{tag}</span>',
            unsafe_allow_html=True
        )

    meta = data.get("metadata", {})
    if meta.get("last_updated"):
        st.divider()
        st.caption(f"마지막 업데이트\n{meta['last_updated'][:16].replace('T',' ')}")


# ═══════════════════════════════════════════
# 📰 뉴스 리스트
# ═══════════════════════════════════════════
if menu == "📰 뉴스 리스트":
    st.title("📰 AI 분석 뉴스 리스트")
    st.caption(
        f"Claude가 분석한 뉴스를 관련도 순으로 표시합니다. "
        f"기본 조회는 최근 {DEFAULT_NEWS_DAYS}일, 최대 {MAX_ARTICLES_SHOWN}건입니다."
    )
    st.divider()

    articles = load_articles(news_group_id, news_date_from, news_date_to)

    # 최소 관련도 필터 적용
    articles = [a for a in articles if (a["score_relevance"] or 0) >= min_rel]

    if not articles:
        st.info("조건에 맞는 분석된 뉴스가 없습니다. 필터를 조정하거나 배치 분석을 먼저 실행해주세요.")
    else:
        # ── 피드백 상태 초기화 ──────────────────────
        # session_state에 없을 때만 DB에서 로드 (rerun 시 불필요한 DB 조회 방지)
        art_ids = [a["id"] for a in articles]
        fb_key  = f"feedback_map_{hash(tuple(art_ids))}"
        if fb_key not in st.session_state:
            st.session_state[fb_key] = load_feedback_map(art_ids)
        feedback_map: dict = st.session_state[fb_key]

        # ── 통계 요약 ───────────────────────────────
        good_cnt = sum(1 for v in feedback_map.values() if v == "good")
        bad_cnt  = sum(1 for v in feedback_map.values() if v == "bad")
        c1, c2, c3 = st.columns([2, 1, 1])
        c1.caption(f"총 **{len(articles)}**건")
        c2.caption(f"👍 도움됨 **{good_cnt}**건")
        c3.caption(f"👎 관련없음 **{bad_cnt}**건")
        st.markdown("")

        for art in articles:
            art_id    = art["id"]
            rel       = art["score_relevance"]
            imp       = art["score_importance"]
            title     = art["title"]          or "(제목 없음)"
            source    = art["source"]         or "-"
            pub_at    = (art["published_at"]  or "")[:10]
            url       = art["url"]            or ""
            summary   = art["summary"]        or ""
            group_nm  = art["group_name"]     or ""
            current_fb = feedback_map.get(art_id)  # 'good' | 'bad' | None

            # 키워드 파싱
            try:
                kw_list = json.loads(art["keywords"]) if art["keywords"] else []
            except (json.JSONDecodeError, TypeError):
                kw_list = []

            # 카드 컨테이너
            with st.container(border=True):
                # 상단: 점수 뱃지 + 그룹명
                badge_html = (
                    score_badge_html(rel, "관련도")
                    + score_badge_html(imp, "중요도")
                )
                if group_nm:
                    badge_html += (
                        f'<span style="background:#34495e;color:#ecf0f1;padding:3px 10px;'
                        f'border-radius:12px;font-size:12px;margin-left:6px;">'
                        f'{group_nm}</span>'
                    )
                st.markdown(badge_html, unsafe_allow_html=True)

                # 제목 + 메타 / 피드백 버튼 (같은 행)
                left, right = st.columns([6, 1])
                with left:
                    st.markdown(f"**{title}**")
                    st.caption(f"출처: {source}　|　발행일: {pub_at}")
                with right:
                    # 👍 버튼
                    like_label = "✅ 도움됨" if current_fb == "good" else "👍 도움됨"
                    like_type  = "primary"   if current_fb == "good" else "secondary"
                    if st.button(like_label, key=f"like_{art_id}",
                                 type=like_type, use_container_width=True):
                        new_fb = None if current_fb == "good" else "good"
                        if new_fb:
                            save_feedback(art_id, new_fb)
                        else:
                            # 이미 good → 취소
                            conn = get_conn()
                            conn.execute(text("DELETE FROM article_feedback WHERE article_id=:aid"), {"aid": art_id})
                            conn.commit(); conn.close()
                        st.session_state[fb_key][art_id] = new_fb
                        st.rerun()

                    # 👎 버튼
                    bad_label = "❌ 관련없음" if current_fb == "bad" else "👎 관련없음"
                    bad_type  = "primary"    if current_fb == "bad" else "secondary"
                    if st.button(bad_label, key=f"bad_{art_id}",
                                 type=bad_type, use_container_width=True):
                        new_fb = None if current_fb == "bad" else "bad"
                        if new_fb:
                            save_feedback(art_id, new_fb)
                        else:
                            conn = get_conn()
                            conn.execute(text("DELETE FROM article_feedback WHERE article_id=:aid"), {"aid": art_id})
                            conn.commit(); conn.close()
                        st.session_state[fb_key][art_id] = new_fb
                        st.rerun()

                # 펼치기: 요약 + 키워드 + 링크
                with st.expander("AI 요약 · 키워드 · 원문 보기"):
                    if summary:
                        st.markdown("**📝 AI 3줄 요약**")
                        st.markdown(summary)

                    if kw_list:
                        st.markdown("**🏷️ 키워드**")
                        st.markdown(
                            "".join(keyword_tag_html(k) for k in kw_list),
                            unsafe_allow_html=True,
                        )

                    if url:
                        st.markdown("**🔗 원문 링크**")
                        st.markdown(f"[기사 바로가기]({url})")
                    else:
                        st.caption("원문 링크 없음")

        st.markdown("")
        st.caption(f"총 {len(articles)}건 표시됨")


# ═══════════════════════════════════════════
# 📈 피드백 통계
# ═══════════════════════════════════════════
elif menu == "📈 피드백 통계":
    st.title("📈 피드백 통계")
    st.caption("👍 도움됨 / 👎 관련없음 피드백을 집계한 분석 결과입니다.")
    st.divider()

    stats = load_feedback_stats()

    if not stats:
        st.info("아직 피드백 데이터가 없습니다. 뉴스 리스트에서 기사에 피드백을 남겨주세요.")
    else:
        total = stats["total"]
        good  = stats["good"]
        bad   = stats["bad"]
        good_rate = round(100 * good / total, 1) if total else 0

        # ── 요약 메트릭 ─────────────────────────────
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("전체 피드백",  f"{total}건")
        m2.metric("👍 도움됨",    f"{good}건",  f"{good_rate}%")
        m3.metric("👎 관련없음",  f"{bad}건",   f"{round(100*bad/total,1)}%")
        m4.metric("신뢰도",       f"{good_rate}%",
                  "양호" if good_rate >= 70 else ("보통" if good_rate >= 40 else "낮음"))
        st.divider()

        # ── 키워드 그룹별 피드백 ────────────────────
        st.subheader("🗂️ 키워드 그룹별 피드백")
        st.caption("어떤 키워드 그룹이 유용한 뉴스를 더 많이 가져오는지 확인합니다.")

        by_group = stats["by_group"]
        if by_group:
            df_grp = pd.DataFrame(by_group, columns=["그룹", "도움됨", "관련없음", "합계"])
            df_grp = df_grp.set_index("그룹")[["도움됨", "관련없음"]]

            st.bar_chart(
                df_grp,
                color=["#27ae60", "#e74c3c"],
                height=max(300, len(df_grp) * 50),
                horizontal=True,
            )

            # 상세 테이블
            with st.expander("상세 수치 보기"):
                df_detail = pd.DataFrame(by_group, columns=["그룹", "도움됨", "관련없음", "합계"])
                df_detail["도움됨 비율(%)"] = (
                    df_detail["도움됨"] / df_detail["합계"] * 100
                ).round(1)
                df_detail = df_detail.sort_values("도움됨 비율(%)", ascending=False)
                st.dataframe(df_detail, use_container_width=True, hide_index=True)
        st.divider()

        # ── 소스별 신뢰도 ───────────────────────────
        st.subheader("📰 소스별 신뢰도")
        st.caption("어떤 언론사·출처가 업무에 유용한 기사를 많이 제공하는지 확인합니다.")

        by_source = stats["by_source"]
        if by_source:
            df_src = pd.DataFrame(by_source,
                                  columns=["소스", "도움됨", "관련없음", "합계", "도움됨 비율(%)"])

            # 도움됨 비율 막대 그래프
            df_rate = df_src.set_index("소스")[["도움됨", "관련없음"]]
            st.bar_chart(
                df_rate,
                color=["#27ae60", "#e74c3c"],
                height=max(300, len(df_rate) * 50),
                horizontal=True,
            )

            # 신뢰도 % 순위 테이블
            with st.expander("소스별 신뢰도 순위 상세"):
                df_src_disp = df_src.sort_values("도움됨 비율(%)", ascending=False).copy()
                df_src_disp.index = range(1, len(df_src_disp) + 1)
                st.dataframe(df_src_disp, use_container_width=True)
        st.divider()

        # ── 도움됨 상위 기사 ────────────────────────
        st.subheader("🏆 도움됨 상위 기사 TOP 10")
        top_good = stats["top_good"]
        if top_good:
            for i, row in enumerate(top_good, 1):
                title, source, rel, imp, grp, pub = row
                pub_str = (pub or "")[:10]
                rel_html  = score_badge_html(rel, "관련도")
                imp_html  = score_badge_html(imp, "중요도")
                grp_html  = (
                    f'<span style="background:#34495e;color:#ecf0f1;padding:2px 9px;'
                    f'border-radius:10px;font-size:12px;margin-left:4px;">{grp or ""}</span>'
                )
                st.markdown(
                    f"**{i}.** {title or '(제목없음)'}",
                )
                st.markdown(
                    rel_html + imp_html + grp_html
                    + f'<span style="color:#7f8c8d;font-size:12px;margin-left:8px;">'
                    f'{source or ""} · {pub_str}</span>',
                    unsafe_allow_html=True,
                )
                st.markdown("")
        st.divider()

        # ── 관련없음 최근 기사 ──────────────────────
        st.subheader("⚠️ 관련없음 최근 기사")
        st.caption("이 기사들을 참고해 키워드를 정제하거나 제외어를 추가할 수 있습니다.")
        top_bad = stats["top_bad"]
        if top_bad:
            for row in top_bad:
                title, source, rel, grp = row
                st.markdown(
                    f"- **{title or '(제목없음)'}** "
                    f"<span style='color:#7f8c8d;font-size:12px;'>"
                    f"({source or '-'} · 관련도 {rel})</span>",
                    unsafe_allow_html=True,
                )


# ═══════════════════════════════════════════
# 🔍 키워드 추천
# ═══════════════════════════════════════════
elif menu == "🔍 키워드 추천":
    st.title("🔍 AI 키워드 추천")
    st.caption("최근 뉴스에서 자주 등장하지만 아직 추적하지 않는 단어를 Claude가 분석해 추천합니다.")
    st.divider()

    data   = load_keywords()
    groups = data.get("keyword_groups", [])
    active_groups = [g for g in groups if g.get("is_active", True)]
    group_names   = [g["group_name"] for g in active_groups]

    # ── 설정 + 실행 버튼 ────────────────────────
    ctl1, ctl2, ctl3 = st.columns([2, 1, 1])
    with ctl1:
        days = st.slider("분석 기간 (일)", 7, 90, 30, key="kw_days")
    with ctl2:
        st.markdown("<br>", unsafe_allow_html=True)
        run_btn = st.button("🔍 AI 분석 실행", type="primary", use_container_width=True)
    with ctl3:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🗑️ 결과 초기화", use_container_width=True):
            for k in list(st.session_state.keys()):
                if k.startswith("kw_"):
                    del st.session_state[k]
            st.rerun()

    # ── 분석 실행 ────────────────────────────────
    if run_btn:
        with st.spinner("최근 뉴스 제목 분석 중..."):
            titles = get_recent_article_titles(days)

        if not titles:
            st.warning(f"최근 {days}일간 수집된 뉴스가 없습니다.")
            st.stop()

        existing_kws = get_all_existing_keywords(data)
        candidates   = extract_word_frequencies(titles, existing_kws)

        if not candidates:
            st.info("새로운 후보 단어가 없습니다. 기존 키워드가 이미 충분히 포괄적입니다.")
            st.stop()

        with st.spinner(f"Claude에 후보 단어 {len(candidates)}개 분석 요청 중..."):
            try:
                recs = get_keyword_recommendations(candidates, active_groups, titles[:20])
                st.session_state["kw_recs"]   = recs
                st.session_state["kw_meta"]   = {
                    "titles_count":     len(titles),
                    "candidates_count": len(candidates),
                    "days":             days,
                }
                # 추가 완료 상태 초기화
                for k in list(st.session_state.keys()):
                    if k.startswith("kw_added_"):
                        del st.session_state[k]
            except Exception as exc:
                st.error(f"Claude API 오류: {exc}")
                st.stop()

    # ── 결과 렌더링 ──────────────────────────────
    recs = st.session_state.get("kw_recs", [])
    meta = st.session_state.get("kw_meta", {})

    if not recs and not meta:
        st.info("위 버튼을 눌러 분석을 시작하세요.")
        st.stop()

    if meta:
        added_count = sum(
            1 for k, v in st.session_state.items()
            if k.startswith("kw_added_") and v
        )
        st.success(
            f"✅ 최근 **{meta['days']}**일 기사 **{meta['titles_count']}**건 분석 완료 &nbsp;|&nbsp; "
            f"후보 단어 **{meta['candidates_count']}**개 &nbsp;→&nbsp; "
            f"추천 키워드 **{len(recs)}**개 &nbsp;|&nbsp; "
            f"추가 완료 **{added_count}**개"
        )

    if not recs:
        st.info("추천할 키워드가 없습니다.")
        st.stop()

    # 우선순위 순 정렬
    _order = {"high": 0, "medium": 1, "low": 2}
    recs_sorted = sorted(recs, key=lambda x: _order.get(x.get("priority", "low"), 2))

    st.subheader(f"추천 키워드 {len(recs_sorted)}개")
    st.markdown("")

    _p_color = {"high": "#e74c3c", "medium": "#e67e22", "low": "#3498db"}
    _p_label = {"high": "★ 높음", "medium": "◆ 보통", "low": "▷ 낮음"}

    for i, rec in enumerate(recs_sorted):
        kw_kr     = rec.get("keyword_kr", "")
        kw_en     = rec.get("keyword_en") or ""
        reason    = rec.get("reason", "")
        suggested = rec.get("suggested_group", "")
        priority  = rec.get("priority", "medium")
        added_key = f"kw_added_{i}"
        is_added  = st.session_state.get(added_key, False)

        pc = _p_color.get(priority, "#7f8c8d")
        pl = _p_label.get(priority, priority)

        with st.container(border=True):
            # 헤더 행: 키워드 + 우선순위 뱃지
            h_left, h_right = st.columns([7, 2])
            with h_left:
                en_part = (
                    f' &nbsp;<span style="color:#7f8c8d;font-size:14px;">/ {kw_en}</span>'
                    if kw_en else ""
                )
                st.markdown(
                    f'<span style="font-size:20px;font-weight:bold;">{kw_kr}</span>{en_part}',
                    unsafe_allow_html=True,
                )
            with h_right:
                st.markdown(
                    f'<div style="text-align:right;margin-top:4px;">'
                    f'<span style="background:{pc};color:white;padding:3px 10px;'
                    f'border-radius:10px;font-size:12px;">{pl}</span></div>',
                    unsafe_allow_html=True,
                )

            # 추천 이유
            st.caption(f"💡 {reason}")

            # 그룹 선택 + 추가 버튼
            btn_col, sel_col = st.columns([1, 3])
            with sel_col:
                default_idx = (
                    group_names.index(suggested)
                    if suggested in group_names else 0
                )
                selected_group = st.selectbox(
                    "그룹 선택",
                    options=group_names,
                    index=default_idx,
                    key=f"kw_sel_{i}",
                    label_visibility="collapsed",
                )
            with btn_col:
                if is_added:
                    st.success("✅ 추가됨")
                else:
                    if st.button(
                        "➕ 키워드 추가",
                        key=f"kw_add_{i}",
                        type="primary",
                        use_container_width=True,
                    ):
                        # keywords.json에 추가
                        for g in data["keyword_groups"]:
                            if g["group_name"] == selected_group:
                                any_kr = g.setdefault("query",    {}).setdefault("any", [])
                                any_en = g.setdefault("query_en", {}).setdefault("any", [])
                                if kw_kr and kw_kr not in any_kr:
                                    any_kr.append(kw_kr)
                                if kw_en and kw_en not in any_en:
                                    any_en.append(kw_en)
                                break
                        save_keywords(data)
                        st.session_state[added_key] = True
                        st.rerun()


# ═══════════════════════════════════════════
# 📋 키워드 목록
# ═══════════════════════════════════════════
elif menu == "📋 키워드 목록":
    st.title("📋 키워드 그룹 목록")
    st.caption("태그 필터로 원하는 그룹만 골라볼 수 있습니다.")
    st.divider()

    data   = load_keywords()
    groups = data.get("keyword_groups", [])

    if not groups:
        st.info("등록된 키워드 그룹이 없습니다. 사이드바에서 '새 그룹 추가'를 눌러주세요.")
    else:
        # ── 태그 필터 버튼 ──
        all_tags = sorted({t for g in groups for t in g.get("tags", [])})
        selected_tag = st.session_state.get("selected_tag", "전체")

        btn_cols = st.columns(len(all_tags) + 1)
        if btn_cols[0].button(
            "전체", use_container_width=True,
            type="primary" if selected_tag == "전체" else "secondary"
        ):
            st.session_state["selected_tag"] = "전체"
            st.rerun()

        for i, tag in enumerate(all_tags):
            if btn_cols[i + 1].button(
                tag, use_container_width=True,
                type="primary" if selected_tag == tag else "secondary"
            ):
                st.session_state["selected_tag"] = tag
                st.rerun()

        st.markdown("")

        # 필터 적용
        selected_tag = st.session_state.get("selected_tag", "전체")
        filtered = groups if selected_tag == "전체" else [
            g for g in groups if selected_tag in g.get("tags", [])
        ]

        if selected_tag != "전체":
            st.info(f"**{selected_tag}** 태그 그룹 {len(filtered)}개 표시 중")

        # ── 요약 테이블 헤더 ──
        st.subheader("📊 현황 테이블")
        hcols = st.columns([3, 2, 2, 2, 1, 1, 1, 1])
        for col, label in zip(hcols, ["그룹명", "분류 태그", "핵심 키워드", "최근 수집", "7일", "30일", "누적", "상태"]):
            col.markdown(f"**{label}**")
        st.markdown("---")

        for g in filtered:
            stats    = g.get("stats", {})
            last_col = stats.get("last_collected_at", "-")
            if last_col != "-":
                last_col = last_col[:16].replace("T", " ")

            row = st.columns([3, 2, 2, 2, 1, 1, 1, 1])
            row[0].markdown(f"**{g['group_name']}**")
            row[1].markdown(render_tag_badges(g.get("tags", [])),          unsafe_allow_html=True)
            row[2].markdown(render_badges(g.get("keywords_label", [])[:3], LABEL_COLOR, "12px"), unsafe_allow_html=True)
            row[3].markdown(f"`{last_col}`")
            row[4].markdown(f"**{stats.get('article_count_7d',  0)}**건")
            row[5].markdown(f"**{stats.get('article_count_30d', 0)}**건")
            row[6].markdown(f"{stats.get('total_article_count', 0)}건")
            row[7].markdown("🟢" if g.get("is_active", True) else "⛔")

        st.divider()

        # ── 그룹 상세 카드 ──
        st.subheader("🔍 그룹 상세 / 수정")

        for idx, g in enumerate(groups):
            # 필터 적용 — 필터된 그룹만 expander 표시
            if selected_tag != "전체" and selected_tag not in g.get("tags", []):
                continue

            is_active   = g.get("is_active", True)
            status_icon = "🟢" if is_active else "⛔"

            with st.expander(f"{status_icon}  {g['group_name']}  —  {g.get('description','')}"):

                # 태그 뱃지 표시
                st.markdown(
                    "**분류 태그** &nbsp;&nbsp;" + render_tag_badges(g.get("tags", [])) +
                    "&nbsp;&nbsp;&nbsp;**핵심 키워드** &nbsp;&nbsp;" +
                    render_badges(g.get("keywords_label", []), LABEL_COLOR),
                    unsafe_allow_html=True
                )
                st.markdown("")

                tab1, tab2, tab3 = st.tabs(["📌 키워드 구성", "✏️ 수정", "🗑️ 삭제/비활성화"])

                # ── 탭1: 키워드 구성 보기 ──
                with tab1:
                    query    = g.get("query",    {})
                    query_en = g.get("query_en", {})
                    col_kr, col_en = st.columns(2)

                    with col_kr:
                        st.markdown("**🇰🇷 한국어 키워드**")
                        st.markdown("필수 (AND)")
                        st.markdown(render_badges(query.get("must",    []), "#e74c3c"), unsafe_allow_html=True)
                        st.markdown("선택 (OR)")
                        st.markdown(render_badges(query.get("any",     []), "#27ae60"), unsafe_allow_html=True)
                        if query.get("exclude"):
                            st.markdown("제외 (NOT)")
                            st.markdown(render_badges(query.get("exclude", []), "#7f8c8d"), unsafe_allow_html=True)

                    with col_en:
                        st.markdown("**🇺🇸 영어 키워드**")
                        st.markdown("필수 (AND)")
                        st.markdown(render_badges(query_en.get("must",    []), "#e74c3c"), unsafe_allow_html=True)
                        st.markdown("선택 (OR)")
                        st.markdown(render_badges(query_en.get("any",     []), "#27ae60"), unsafe_allow_html=True)
                        if query_en.get("exclude"):
                            st.markdown("제외 (NOT)")
                            st.markdown(render_badges(query_en.get("exclude", []), "#7f8c8d"), unsafe_allow_html=True)

                    st.markdown("")
                    st.markdown(f"생성일: `{g.get('created_at','-')}`  |  ID: `{g.get('id','-')}`")

                # ── 탭2: 수정 ──
                with tab2:
                    with st.form(key=f"edit_{g['id']}"):
                        gid = g["id"]
                        new_name = st.text_input("그룹명", value=g["group_name"],              key=f"e_name_{gid}")
                        new_desc = st.text_input("설명",   value=g.get("description", ""),     key=f"e_desc_{gid}")

                        tag_options = list(TAG_COLORS.keys())
                        new_tags = st.multiselect(
                            "분류 태그 (복수 선택 가능)",
                            options=tag_options,
                            default=[t for t in g.get("tags", []) if t in tag_options],
                            key=f"e_tags_{gid}",
                        )
                        new_kw_label = st.text_input(
                            "핵심 키워드 표시용 (쉼표로 구분)",
                            value=", ".join(g.get("keywords_label", [])),
                            key=f"e_kwlabel_{gid}",
                        )

                        st.markdown("**🇰🇷 한국어 키워드**")
                        c1, c2, c3 = st.columns(3)
                        with c1:
                            new_must_kr = st.text_area("필수(AND)",  value="\n".join(g.get("query",{}).get("must",    [])), height=110, key=f"e_must_kr_{gid}")
                        with c2:
                            new_any_kr  = st.text_area("선택(OR)",   value="\n".join(g.get("query",{}).get("any",     [])), height=110, key=f"e_any_kr_{gid}")
                        with c3:
                            new_exc_kr  = st.text_area("제외(NOT)",  value="\n".join(g.get("query",{}).get("exclude", [])), height=110, key=f"e_exc_kr_{gid}")

                        st.markdown("**🇺🇸 영어 키워드**")
                        e1, e2, e3 = st.columns(3)
                        with e1:
                            new_must_en = st.text_area("필수(AND)",  value="\n".join(g.get("query_en",{}).get("must",    [])), height=110, key=f"e_must_en_{gid}")
                        with e2:
                            new_any_en  = st.text_area("선택(OR)",   value="\n".join(g.get("query_en",{}).get("any",     [])), height=110, key=f"e_any_en_{gid}")
                        with e3:
                            new_exc_en  = st.text_area("제외(NOT)",  value="\n".join(g.get("query_en",{}).get("exclude", [])), height=110, key=f"e_exc_en_{gid}")

                        if st.form_submit_button("💾 저장", use_container_width=True, type="primary"):
                            groups[idx].update({
                                "group_name":     new_name,
                                "description":    new_desc,
                                "tags":           new_tags,
                                "keywords_label": [k.strip() for k in new_kw_label.split(",") if k.strip()],
                                "query":    {"must": parse_lines(new_must_kr), "any": parse_lines(new_any_kr), "exclude": parse_lines(new_exc_kr)},
                                "query_en": {"must": parse_lines(new_must_en), "any": parse_lines(new_any_en), "exclude": parse_lines(new_exc_en)},
                            })
                            data["keyword_groups"] = groups
                            save_keywords(data)
                            st.success("✅ 저장되었습니다.")
                            st.rerun()

                # ── 탭3: 삭제 / 비활성화 ──
                with tab3:
                    st.warning("아래 작업은 신중하게 선택해주세요.")
                    btn1, btn2 = st.columns(2)
                    with btn1:
                        label = "⛔ 비활성화" if is_active else "🟢 활성화"
                        if st.button(label, key=f"toggle_{g['id']}", use_container_width=True):
                            groups[idx]["is_active"] = not is_active
                            data["keyword_groups"] = groups
                            save_keywords(data)
                            st.rerun()
                    with btn2:
                        if st.button("🗑️ 완전 삭제", key=f"del_{g['id']}", use_container_width=True):
                            data["keyword_groups"] = [x for x in groups if x["id"] != g["id"]]
                            save_keywords(data)
                            st.success(f"'{g['group_name']}' 삭제 완료")
                            st.rerun()


# ═══════════════════════════════════════════
# 📊 태그별 현황
# ═══════════════════════════════════════════
elif menu == "📊 태그별 현황":
    st.title("📊 태그별 통합 현황")
    st.caption("같은 태그를 가진 그룹들의 수집 현황을 태그 단위로 묶어서 보여줍니다.")
    st.divider()

    data   = load_keywords()
    groups = data.get("keyword_groups", [])

    # 태그별로 그룹 묶기
    tag_map = {}
    for g in groups:
        for t in g.get("tags", []):
            tag_map.setdefault(t, []).append(g)

    for tag, tag_groups in sorted(tag_map.items()):
        color = TAG_COLORS.get(tag, DEFAULT_TAG_COLOR)
        total_7d  = sum(g.get("stats",{}).get("article_count_7d",  0) for g in tag_groups)
        total_30d = sum(g.get("stats",{}).get("article_count_30d", 0) for g in tag_groups)
        total_all = sum(g.get("stats",{}).get("total_article_count",0) for g in tag_groups)

        st.markdown(
            f'<span style="background:{color};color:white;padding:4px 16px;'
            f'border-radius:14px;font-size:15px;font-weight:bold;">{tag}</span>'
            f'&nbsp;&nbsp;그룹 {len(tag_groups)}개 &nbsp;|&nbsp; '
            f'7일 <b>{total_7d}</b>건 &nbsp;|&nbsp; '
            f'30일 <b>{total_30d}</b>건 &nbsp;|&nbsp; '
            f'누적 <b>{total_all}</b>건',
            unsafe_allow_html=True
        )

        for g in tag_groups:
            s = g.get("stats", {})
            last = s.get("last_collected_at", "-")
            if last != "-":
                last = last[:16].replace("T", " ")
            st.markdown(
                f"&nbsp;&nbsp;&nbsp;&nbsp;└ **{g['group_name']}** &nbsp; "
                f"최근수집: `{last}` &nbsp; "
                f"7일: **{s.get('article_count_7d',0)}**건 / "
                f"30일: **{s.get('article_count_30d',0)}**건"
            )
        st.divider()


# ═══════════════════════════════════════════
# ➕ 새 그룹 추가
# ═══════════════════════════════════════════
elif menu == "➕ 새 그룹 추가":
    st.title("➕ 새 키워드 그룹 추가")
    st.caption("새로운 주제의 키워드 그룹을 등록합니다.")
    st.divider()

    data   = load_keywords()
    groups = data.get("keyword_groups", [])

    with st.form("add_form"):
        new_name = st.text_input("그룹명 *", placeholder="예: GTT 경쟁사 동향")
        new_desc = st.text_input("설명",     placeholder="예: GTT Mark III 관련 시장 뉴스 추적")

        new_tags = st.multiselect(
            "분류 태그 * (복수 선택 가능)",
            options=list(TAG_COLORS.keys())
        )
        new_kw_label = st.text_input(
            "핵심 키워드 표시용 (쉼표로 구분)",
            placeholder="예: GTT, Mark III, 멤브레인"
        )

        st.markdown("**🇰🇷 한국어 키워드**")
        c1, c2, c3 = st.columns(3)
        with c1:
            must_kr = st.text_area("필수(AND) — 줄바꿈으로 구분", height=120, placeholder="예:\nGTT")
        with c2:
            any_kr  = st.text_area("선택(OR) — 줄바꿈으로 구분",  height=120, placeholder="예:\n화물창\n멤브레인\n수주")
        with c3:
            exc_kr  = st.text_area("제외(NOT) — 줄바꿈으로 구분", height=120, placeholder="예:\n주가\n투자")

        st.markdown("**🇺🇸 영어 키워드**")
        e1, e2, e3 = st.columns(3)
        with e1:
            must_en = st.text_area("필수(AND)", height=120, placeholder="예:\nGTT")
        with e2:
            any_en  = st.text_area("선택(OR)",  height=120, placeholder="예:\nMark III\nmembrane containment")
        with e3:
            exc_en  = st.text_area("제외(NOT)", height=120, placeholder="예:\nstock\ninvestment")

        if st.form_submit_button("✅ 그룹 추가", use_container_width=True, type="primary"):
            if not new_name.strip():
                st.error("그룹명은 필수 입력 항목입니다.")
            else:
                new_group = {
                    "id":             generate_id(groups),
                    "group_name":     new_name.strip(),
                    "description":    new_desc.strip(),
                    "is_active":      True,
                    "created_at":     datetime.now().strftime("%Y-%m-%d"),
                    "tags":           new_tags,
                    "keywords_label": [k.strip() for k in new_kw_label.split(",") if k.strip()],
                    "query":    {"must": parse_lines(must_kr), "any": parse_lines(any_kr), "exclude": parse_lines(exc_kr)},
                    "query_en": {"must": parse_lines(must_en), "any": parse_lines(any_en), "exclude": parse_lines(exc_en)},
                    "stats": {
                        "last_collected_at":  "-",
                        "article_count_7d":   0,
                        "article_count_30d":  0,
                        "total_article_count":0
                    }
                }
                data["keyword_groups"].append(new_group)
                save_keywords(data)
                st.success(f"✅ '{new_name}' 그룹이 추가되었습니다!")
                st.balloons()
                st.rerun()
