"""
reporter.py — 일일 뉴스 PDF 리포트 생성 및 SendGrid 발송

실행:
    python reporter.py --dry-run
    python reporter.py --limit 20 --min-relevance 6
"""

import argparse
import base64
import html
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Attachment,
    Disposition,
    FileContent,
    FileName,
    FileType,
    Mail,
)
from sqlalchemy import text

from database import get_conn

load_dotenv()

DEFAULT_DAYS = 30
DEFAULT_LIMIT = 20
DEFAULT_MIN_RELEVANCE = 6
DEFAULT_DEDUPE_THRESHOLD = 0.88


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _normalize_title(title: str) -> str:
    """동일 이슈 판별을 위해 기사 제목을 단순화한다."""
    text_value = (title or "").lower()
    text_value = re.sub(r"\s*[-|–—]\s*[^-|–—]{2,30}$", "", text_value)
    text_value = re.sub(r"\[[^\]]+\]|\([^)]+\)", " ", text_value)
    text_value = re.sub(r"[^0-9a-z가-힣]+", " ", text_value)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return text_value


def _is_duplicate(normalized_title: str, seen_titles: list[str], threshold: float) -> bool:
    if not normalized_title:
        return False
    for seen in seen_titles:
        if normalized_title == seen:
            return True
        if SequenceMatcher(None, normalized_title, seen).ratio() >= threshold:
            return True
    return False


def query_report_candidates(days: int, min_relevance: int) -> list[dict]:
    """최근 N일 이내 발행 또는 수집된 분석 완료 기사 후보를 조회한다."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_conn()
    rows = conn.execute(
        text(
            "SELECT id, title, url, source, published_at, collected_at, "
            "       score_relevance, score_importance, summary, keywords, "
            "       keyword_group_name "
            "FROM articles "
            "WHERE is_analyzed = 1 "
            "  AND COALESCE(score_relevance, 0) >= :min_relevance "
            "  AND ("
            "       published_at >= :cutoff "
            "       OR (published_at IS NULL AND collected_at >= :cutoff) "
            "       OR (published_at = '' AND collected_at >= :cutoff)"
            "  ) "
            "ORDER BY score_relevance DESC, score_importance DESC, published_at DESC"
        ),
        {"cutoff": cutoff, "min_relevance": min_relevance},
    ).mappings().fetchall()
    conn.close()
    return [dict(row) for row in rows]


def dedupe_articles(
    articles: list[dict],
    limit: int,
    threshold: float,
) -> tuple[list[dict], int]:
    """URL과 유사 제목 기준으로 같은 내용의 기사를 제거한다."""
    unique_articles: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: list[str] = []
    duplicate_count = 0

    for article in articles:
        url = (article.get("url") or "").strip()
        normalized_title = _normalize_title(article.get("title") or "")

        if url and url in seen_urls:
            duplicate_count += 1
            continue
        if _is_duplicate(normalized_title, seen_titles, threshold):
            duplicate_count += 1
            continue

        unique_articles.append(article)
        if url:
            seen_urls.add(url)
        if normalized_title:
            seen_titles.append(normalized_title)

    return unique_articles[:limit], duplicate_count


def _keyword_text(raw_keywords: str | None) -> str:
    if not raw_keywords:
        return ""
    try:
        parsed = json.loads(raw_keywords)
    except json.JSONDecodeError:
        return str(raw_keywords)
    if isinstance(parsed, list):
        return ", ".join(str(item) for item in parsed)
    return str(parsed)


def _paragraph(text_value: str, style):
    from reportlab.platypus import Paragraph

    return Paragraph(html.escape(text_value or ""), style)


def build_pdf(
    articles: list[dict],
    output_path: str | Path,
    *,
    total_candidates: int,
    duplicate_count: int,
    days: int,
    min_relevance: int,
) -> Path:
    """일일 뉴스 리포트 PDF를 생성한다."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))

    output = Path(output_path)
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="KoreanTitle",
            parent=styles["Title"],
            fontName="HYGothic-Medium",
            fontSize=18,
            leading=24,
            alignment=TA_CENTER,
        )
    )
    styles.add(
        ParagraphStyle(
            name="KoreanBody",
            parent=styles["BodyText"],
            fontName="HYSMyeongJo-Medium",
            fontSize=9,
            leading=13,
        )
    )
    styles.add(
        ParagraphStyle(
            name="KoreanSmall",
            parent=styles["BodyText"],
            fontName="HYSMyeongJo-Medium",
            fontSize=8,
            leading=11,
        )
    )

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
    )

    story = [
        _paragraph("AI 뉴스 일일 리포트", styles["KoreanTitle"]),
        Spacer(1, 5 * mm),
        _paragraph(
            f"생성 시각: {_now_str()} | 조회 기준: 최근 {days}일 | "
            f"최소 관련도: {min_relevance}",
            styles["KoreanBody"],
        ),
        _paragraph(
            f"후보 기사: {total_candidates}건 | 중복 제외: {duplicate_count}건 | "
            f"리포트 포함: {len(articles)}건",
            styles["KoreanBody"],
        ),
        Spacer(1, 6 * mm),
    ]

    if not articles:
        story.append(_paragraph("조건에 맞는 분석 완료 기사가 없습니다.", styles["KoreanBody"]))
        doc.build(story)
        return output

    table_data = [[
        _paragraph("No", styles["KoreanSmall"]),
        _paragraph("기사", styles["KoreanSmall"]),
        _paragraph("점수", styles["KoreanSmall"]),
        _paragraph("요약", styles["KoreanSmall"]),
    ]]

    for idx, article in enumerate(articles, start=1):
        published = (article.get("published_at") or article.get("collected_at") or "")[:10]
        meta = (
            f"{article.get('title') or '(제목 없음)'}\n"
            f"출처: {article.get('source') or '-'} | 발행일: {published} | "
            f"그룹: {article.get('keyword_group_name') or '-'}\n"
            f"키워드: {_keyword_text(article.get('keywords'))}\n"
            f"URL: {article.get('url') or '-'}"
        )
        score = (
            f"관련도 {article.get('score_relevance') or '-'}\n"
            f"중요도 {article.get('score_importance') or '-'}"
        )
        table_data.append([
            _paragraph(str(idx), styles["KoreanSmall"]),
            _paragraph(meta, styles["KoreanSmall"]),
            _paragraph(score, styles["KoreanSmall"]),
            _paragraph(article.get("summary") or "", styles["KoreanSmall"]),
        ])

    table = Table(table_data, colWidths=[10 * mm, 70 * mm, 24 * mm, 78 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    doc.build(story)
    return output


def send_email_with_pdf(
    pdf_path: str | Path,
    *,
    subject: str,
    html_content: str,
) -> int:
    """SendGrid로 PDF 첨부 이메일을 발송하고 status code를 반환한다."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("REPORT_FROM_EMAIL")
    to_emails_raw = os.environ.get("REPORT_TO_EMAILS", "")
    to_emails = [email.strip() for email in to_emails_raw.split(",") if email.strip()]

    missing = []
    if not api_key:
        missing.append("SENDGRID_API_KEY")
    if not from_email:
        missing.append("REPORT_FROM_EMAIL")
    if not to_emails:
        missing.append("REPORT_TO_EMAILS")
    if missing:
        raise EnvironmentError(f"필수 이메일 환경변수 누락: {', '.join(missing)}")

    message = Mail(
        from_email=from_email,
        to_emails=to_emails,
        subject=subject,
        html_content=html_content,
    )

    pdf = Path(pdf_path)
    encoded = base64.b64encode(pdf.read_bytes()).decode()
    message.attachment = Attachment(
        FileContent(encoded),
        FileName(pdf.name),
        FileType("application/pdf"),
        Disposition("attachment"),
    )

    response = SendGridAPIClient(api_key).send(message)
    return response.status_code


def _record_report(
    *,
    report_date: str,
    status: str,
    article_count: int,
    duplicate_count: int,
    pdf_path: str,
    error_message: str | None = None,
) -> None:
    conn = get_conn()
    conn.execute(
        text(
            "INSERT INTO daily_reports "
            "(report_date, status, article_count, duplicate_count, pdf_path, "
            " error_message, created_at) "
            "VALUES (:report_date, :status, :article_count, :duplicate_count, "
            "        :pdf_path, :error_message, :created_at)"
        ),
        {
            "report_date": report_date,
            "status": status,
            "article_count": article_count,
            "duplicate_count": duplicate_count,
            "pdf_path": pdf_path,
            "error_message": error_message,
            "created_at": _now_str(),
        },
    )
    conn.commit()
    conn.close()


def run_report(
    *,
    days: int,
    limit: int,
    min_relevance: int,
    dedupe_threshold: float,
    dry_run: bool,
    output_dir: str | Path | None,
) -> Path:
    candidates = query_report_candidates(days, min_relevance)
    articles, duplicate_count = dedupe_articles(candidates, limit, dedupe_threshold)

    report_date = datetime.now().strftime("%Y-%m-%d")
    target_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    target_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = target_dir / f"daily-news-report-{report_date}.pdf"

    build_pdf(
        articles,
        pdf_path,
        total_candidates=len(candidates),
        duplicate_count=duplicate_count,
        days=days,
        min_relevance=min_relevance,
    )

    if dry_run:
        print(f"[DRY RUN] PDF 생성 완료: {pdf_path}")
        print(f"[DRY RUN] 후보 {len(candidates)}건 / 중복 제외 {duplicate_count}건 / 포함 {len(articles)}건")
        return pdf_path

    try:
        status_code = send_email_with_pdf(
            pdf_path,
            subject=f"AI 뉴스 일일 리포트 - {report_date}",
            html_content=(
                f"<p>AI 뉴스 일일 리포트입니다.</p>"
                f"<p>후보 {len(candidates)}건 중 중복 {duplicate_count}건을 제외하고 "
                f"{len(articles)}건을 첨부 PDF에 포함했습니다.</p>"
            ),
        )
        _record_report(
            report_date=report_date,
            status=f"sent:{status_code}",
            article_count=len(articles),
            duplicate_count=duplicate_count,
            pdf_path=str(pdf_path),
        )
        print(f"이메일 발송 완료: status={status_code}, pdf={pdf_path}")
        return pdf_path
    except Exception as exc:
        _record_report(
            report_date=report_date,
            status="failed",
            article_count=len(articles),
            duplicate_count=duplicate_count,
            pdf_path=str(pdf_path),
            error_message=str(exc),
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="일일 뉴스 PDF 리포트 생성 및 이메일 발송")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="최근 N일 기사만 포함")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="PDF 포함 최대 기사 수")
    parser.add_argument("--min-relevance", type=int, default=DEFAULT_MIN_RELEVANCE, help="최소 관련도")
    parser.add_argument(
        "--dedupe-threshold",
        type=float,
        default=DEFAULT_DEDUPE_THRESHOLD,
        help="제목 유사도 중복 제거 기준값",
    )
    parser.add_argument("--dry-run", action="store_true", help="PDF만 생성하고 이메일은 발송하지 않음")
    parser.add_argument("--output-dir", type=str, default=None, help="PDF 저장 폴더")
    args = parser.parse_args()

    run_report(
        days=args.days,
        limit=args.limit,
        min_relevance=args.min_relevance,
        dedupe_threshold=args.dedupe_threshold,
        dry_run=args.dry_run,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
