from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import (
    ClassificationLog,
    GoogleTrend,
    GoogleTrendNews,
    KeywordClassification,
    NewsThemeClassification,
    get_engine,
    init_db,
)
from trends.keyword_classification import GeminiKeywordClassifier

ALLOWED_THEMES = (
    "科技類",
    "生技醫療類",
    "遊戲娛樂類",
    "食品與消費類",
    "金融類",
    "能源與原物料類",
    "電商與零售類",
    "綠能環保類",
    "其他",
)


def _coerce_confidence(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, numeric))


def _validate_prediction(keyword: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    item = payload or {}
    theme_name = str(item.get("theme_name") or "其他").strip()
    if theme_name not in ALLOWED_THEMES:
        theme_name = "其他"
    canonical_keyword = str(item.get("canonical_keyword") or keyword or "").strip() or keyword.strip()
    sub_theme = str(item.get("sub_theme") or "其他").strip() or "其他"
    stock_related = int(bool(item.get("stock_related", 0)))
    confidence_score = _coerce_confidence(item.get("confidence_score", 0))
    need_review = int(confidence_score < 0.6)
    return {
        "keyword": keyword,
        "canonical_keyword": canonical_keyword[:255],
        "theme_name": theme_name,
        "sub_theme": sub_theme[:100],
        "stock_related": stock_related,
        "confidence_score": confidence_score,
        "need_review": need_review,
        "classification_result": json.dumps({
            "canonical_keyword": canonical_keyword,
            "theme_name": theme_name,
            "sub_theme": sub_theme,
            "stock_related": stock_related,
            "confidence_score": confidence_score,
        }, ensure_ascii=False, sort_keys=True),
    }


def get_unclassified_keywords(engine: Engine, limit: int = 100) -> list[str]:
    db_engine = init_db(engine)
    with db_engine.connect() as connection:
        rows = connection.execute(
            select(
                GoogleTrend.keyword,
                func.count(GoogleTrend.id).label("occurrence_count"),
            )
            .outerjoin(KeywordClassification, GoogleTrend.keyword == KeywordClassification.keyword)
            .where(GoogleTrend.keyword.is_not(None))
            .where(
                or_(
                    KeywordClassification.keyword.is_(None),
                    KeywordClassification.theme_name.is_(None),
                    KeywordClassification.theme_name == "其他",
                )
            )
            .group_by(GoogleTrend.keyword)
            .order_by(func.count(GoogleTrend.id).desc())
            .limit(limit if limit > 0 else 100)
        ).all()
    return [str(row.keyword) for row in rows]


def _upsert_keyword_classification(db_engine: Engine, result: dict[str, Any]) -> None:
    statement = insert(KeywordClassification.__table__).values({
        "keyword": result["keyword"],
        "canonical_keyword": result["canonical_keyword"],
        "theme_name": result["theme_name"],
        "sub_theme": result["sub_theme"],
        "stock_related": result["stock_related"],
        "confidence_score": result["confidence_score"],
        "classification_source": "auto_quality",
        "need_review": result["need_review"],
        "reviewed_at": None,
    })
    statement = statement.on_conflict_do_update(
        index_elements=["keyword"],
        set_={
            "canonical_keyword": statement.excluded.canonical_keyword,
            "theme_name": statement.excluded.theme_name,
            "sub_theme": statement.excluded.sub_theme,
            "stock_related": statement.excluded.stock_related,
            "confidence_score": statement.excluded.confidence_score,
            "classification_source": statement.excluded.classification_source,
            "need_review": statement.excluded.need_review,
            "reviewed_at": statement.excluded.reviewed_at,
        },
    )
    with db_engine.begin() as connection:
        connection.execute(statement)


def _log_classification(db_engine: Engine, result: dict[str, Any]) -> None:
    with db_engine.begin() as connection:
        connection.execute(
            ClassificationLog.__table__.insert().values(
                keyword=result["keyword"],
                canonical_keyword=result["canonical_keyword"],
                theme_name=result["theme_name"],
                sub_theme=result["sub_theme"],
                stock_related=result["stock_related"],
                confidence_score=result["confidence_score"],
                classification_result=result["classification_result"],
                need_review=result["need_review"],
            )
        )


def auto_classify_unclassified_keywords(
    engine: Engine | None = None,
    limit: int = 100,
    classifier: GeminiKeywordClassifier | None = None,
) -> int:
    db_engine = init_db(engine or get_engine())
    keywords = get_unclassified_keywords(db_engine, limit=limit)
    if not keywords:
        return 0

    ai_classifier = classifier
    if ai_classifier is None:
        try:
            ai_classifier = GeminiKeywordClassifier()
        except RuntimeError as error:
            raise RuntimeError("未設定 GEMINI_API_KEY 或 GOOGLE_API_KEY，無法自動分類未分類關鍵字") from error

    processed = 0
    for start in range(0, len(keywords), 20):
        batch = keywords[start:start + 20]
        try:
            predictions = ai_classifier.classify_batch(batch)
        except Exception as error:  # pragma: no cover - network/API failure path
            print(f"Auto keyword classification batch failed ({len(batch)} keywords): {error}")
            break
        for keyword in batch:
            payload = predictions.get(keyword) or {}
            result = _validate_prediction(keyword, payload if isinstance(payload, dict) else {})
            _upsert_keyword_classification(db_engine, result)
            _log_classification(db_engine, result)
            processed += 1
    return processed


def build_keyword_quality_summary(engine: Engine | None = None) -> dict[str, float | int]:
    db_engine = init_db(engine or get_engine())
    with db_engine.connect() as connection:
        total_keywords = connection.execute(
            select(func.count()).select_from(KeywordClassification)
        ).scalar_one() or 0
        classified_keywords = connection.execute(
            select(func.count()).select_from(KeywordClassification).where(
                KeywordClassification.theme_name.is_not(None),
                KeywordClassification.theme_name != "其他",
            )
        ).scalar_one() or 0
        unclassified_keywords = max(total_keywords - classified_keywords, 0)
        coverage_rate = (classified_keywords / total_keywords) if total_keywords else 0.0
        need_review = connection.execute(
            select(func.count()).select_from(KeywordClassification).where(KeywordClassification.need_review == 1)
        ).scalar_one() or 0
    return {
        "total_keywords": int(total_keywords),
        "classified_keywords": int(classified_keywords),
        "unclassified_keywords": int(unclassified_keywords),
        "coverage_rate": float(coverage_rate),
        "need_review_keywords": int(need_review),
    }


def classify_pending_news_theme(
    engine: Engine | None = None,
    limit: int = 200,
) -> int:
    db_engine = init_db(engine or get_engine())
    with db_engine.connect() as connection:
        rows = connection.execute(
            select(
                GoogleTrendNews.id.label("news_id"),
                GoogleTrend.keyword,
                GoogleTrendNews.news_title,
                GoogleTrendNews.news_source,
                GoogleTrendNews.news_sentiment,
                GoogleTrendNews.event_type,
                KeywordClassification.theme_name,
                KeywordClassification.sub_theme,
                KeywordClassification.confidence_score,
            )
            .join(GoogleTrend, GoogleTrend.id == GoogleTrendNews.trend_id)
            .outerjoin(KeywordClassification, KeywordClassification.keyword == GoogleTrend.keyword)
            .outerjoin(NewsThemeClassification, NewsThemeClassification.news_id == GoogleTrendNews.id)
            .where(
                GoogleTrendNews.news_title.is_not(None),
                NewsThemeClassification.news_id.is_(None),
            )
            .order_by(GoogleTrendNews.id)
            .limit(limit if limit > 0 else None)
        ).all()
    if not rows:
        return 0

    payload = []
    for row in rows:
        theme_name = str(row.theme_name or "其他").strip() or "其他"
        sub_theme = str(row.sub_theme or "其他").strip() or "其他"
        sentiment = str(row.news_sentiment or "Neutral").strip() or "Neutral"
        event_type = str(row.event_type or "其他").strip() or "其他"
        confidence_score = float(row.confidence_score or 0.0)
        payload.append({
            "news_id": int(row.news_id),
            "keyword": str(row.keyword or ""),
            "theme_name": theme_name,
            "sub_theme": sub_theme,
            "sentiment": sentiment,
            "event_type": event_type,
            "confidence_score": max(0.0, min(1.0, confidence_score)),
        })
    if not payload:
        return 0
    with db_engine.begin() as connection:
        connection.execute(
            NewsThemeClassification.__table__.insert(),
            [
                {
                    "news_id": item["news_id"],
                    "keyword": item["keyword"],
                    "theme_name": item["theme_name"],
                    "sub_theme": item["sub_theme"],
                    "sentiment": item["sentiment"],
                    "event_type": item["event_type"],
                    "confidence_score": item["confidence_score"],
                }
                for item in payload
            ],
        )
    return len(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="自動補齊未分類關鍵字")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    engine = init_db(get_engine())
    count = auto_classify_unclassified_keywords(engine, limit=args.limit)
    print(f"Auto-classified {count} keywords")
