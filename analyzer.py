"""
analyzer.py — Claude API를 이용한 뉴스 분석 모듈

사용법:
    from analyzer import analyze_news, run_batch
    result = analyze_news(title, source, published_at)
    run_batch()   # DB에서 미분석 뉴스 일괄 처리
"""

import json
import os
import re
import time
from datetime import datetime

from anthropic import Anthropic
from sqlalchemy import text
from database import get_conn, column_exists

# ─────────────────────────────────────────
# 상수
# ─────────────────────────────────────────
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 512

PROMPT_TEMPLATE = """다음 뉴스를 LNG 운반선 화물창(Cargo Containment System) 개발 연구원 관점에서 평가해줘.
이 연구원은 Mark III / NO96 멤브레인 구조, 피로 해석, 극저온 재료 거동, 슬로싱 하중, 선급(DNV·LR·ABS) 규정을 주로 다룬다.

제목: {title}
출처: {source}
발행일: {published_at}

반드시 아래 JSON 형식으로만 답해. 다른 말은 하지 마:
{{
  "relevance": 관련도 1~10 (에너지/LNG 업계 관련성),
  "importance": 중요도 1~10 (업무에 미치는 영향),
  "summary": "3문장 요약",
  "keywords": ["키워드1", "키워드2", "키워드3"]
}}"""

# ─────────────────────────────────────────
# 내부 유틸
# ─────────────────────────────────────────
def _extract_json(text: str) -> dict:
    """응답 텍스트에서 JSON 객체를 추출·파싱한다."""
    # 코드 블록(```json ... ```) 제거
    clean = re.sub(r"```(?:json)?|```", "", text).strip()

    # 중괄호 범위만 잘라내기 (앞뒤 잡담 방어)
    start = clean.find("{")
    end = clean.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"JSON 객체를 찾을 수 없음. 원문:\n{text}")

    return json.loads(clean[start:end])


def _validate(data: dict) -> dict:
    """필수 키 존재 여부 확인 후 타입 정규화."""
    required = {"relevance", "importance", "summary", "keywords"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"필수 키 누락: {missing}")

    data["relevance"] = int(data["relevance"])
    data["importance"] = int(data["importance"])
    data["summary"] = str(data["summary"])
    data["keywords"] = list(data["keywords"])
    return data


# ─────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────
def analyze_news(title: str, source: str, published_at: str) -> dict:
    """뉴스 한 건을 Claude API로 분석하고 구조화된 dict를 반환한다.

    Returns:
        {
            "relevance": int,       # 에너지/LNG 관련도 1~10
            "importance": int,      # 업무 중요도 1~10
            "summary": str,         # 3문장 요약
            "keywords": list[str],  # 핵심 키워드 3개
        }

    Raises:
        EnvironmentError: ANTHROPIC_API_KEY 미설정 시
        ValueError: JSON 파싱 실패 또는 필수 키 누락 시
        anthropic.APIError: API 호출 자체가 실패했을 때
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("환경 변수 ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = Anthropic(api_key=api_key)

    prompt = PROMPT_TEMPLATE.format(
        title=title,
        source=source,
        published_at=published_at,
    )

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = message.content[0].text

    try:
        data = _extract_json(raw_text)
        return _validate(data)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"Claude 응답을 JSON으로 파싱하지 못했습니다.\n"
            f"원인: {exc}\n"
            f"원문 응답:\n{raw_text}"
        ) from exc


# ─────────────────────────────────────────
# 배치 처리
# ─────────────────────────────────────────
BATCH_LIMIT = 100
DELAY_SEC   = 0.5


def _ensure_keywords_column(conn) -> None:
    """articles 테이블에 keywords 컬럼이 없으면 추가한다."""
    if not column_exists(conn, "articles", "keywords"):
        conn.execute(text("ALTER TABLE articles ADD COLUMN keywords TEXT"))
        conn.commit()


def run_batch(limit: int = BATCH_LIMIT) -> None:
    """미분석(is_analyzed=0) 뉴스를 최대 limit건 분석해 DB에 저장한다."""
    conn = get_conn()

    _ensure_keywords_column(conn)

    rows = conn.execute(
        text(
            "SELECT id, title, source, published_at, collected_at "
            "FROM   articles "
            "WHERE  is_analyzed = 0 "
            "  AND  score_relevance IS NULL "
            "ORDER BY collected_at DESC "
            "LIMIT  :lim"
        ),
        {"lim": limit},
    ).mappings().fetchall()

    total = len(rows)
    if total == 0:
        print("분석할 뉴스가 없습니다.")
        conn.close()
        return

    print(f"총 {total}건 분석 시작")
    print("-" * 50)

    success = 0
    fail    = 0

    for idx, row in enumerate(rows, start=1):
        article_id = row["id"]
        title      = row["title"]       or ""
        source     = row["source"]      or ""
        pub_at     = row["published_at"] or ""

        print(f"[{idx}/{total}] id={article_id} | {title[:40]}", end=" ... ", flush=True)

        try:
            result = analyze_news(title, source, pub_at)

            conn.execute(
                text(
                    "UPDATE articles "
                    "SET    score_relevance  = :rel, "
                    "       score_importance = :imp, "
                    "       summary          = :summary, "
                    "       keywords         = :keywords, "
                    "       analyzed_at      = :analyzed_at, "
                    "       is_analyzed      = 1 "
                    "WHERE  id = :aid"
                ),
                {
                    "rel":         result["relevance"],
                    "imp":         result["importance"],
                    "summary":     result["summary"],
                    "keywords":    json.dumps(result["keywords"], ensure_ascii=False),
                    "analyzed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "aid":         article_id,
                },
            )
            conn.commit()
            print(f"완료 (관련도={result['relevance']}, 중요도={result['importance']})")
            success += 1

        except Exception as exc:
            print(f"오류 — {exc}")
            fail += 1

        if idx < total:
            time.sleep(DELAY_SEC)

    print("-" * 50)
    print(f"배치 완료: 성공 {success}건 / 실패 {fail}건 / 전체 {total}건")
    conn.close()


# ─────────────────────────────────────────
# 단독 실행 테스트
# ─────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="뉴스 분석기")
    parser.add_argument(
        "--batch", action="store_true",
        help="DB 미분석 뉴스 일괄 처리"
    )
    parser.add_argument(
        "--limit", type=int, default=BATCH_LIMIT,
        help=f"배치 최대 건수 (기본 {BATCH_LIMIT})"
    )
    args = parser.parse_args()

    if args.batch:
        run_batch(limit=args.limit)  # db_path 파라미터 제거됨
    else:
        sample = {
            "title": "글로벌 LNG 수요, 2030년까지 연 5% 성장 전망",
            "source": "에너지경제신문",
            "published_at": "2026-04-06",
        }
        print("=== 단일 분석 테스트 ===")
        for k, v in sample.items():
            print(f"  {k}: {v}")
        print()
        result = analyze_news(**sample)
        print(json.dumps(result, ensure_ascii=False, indent=2))
