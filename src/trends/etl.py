from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import (
    GoogleTrend,
    GoogleTrendNews,
    GoogleTrendsHistory,
    KeywordClassification,
    Stock,
    ThemeDailyStats,
    ThemeMapping,
    get_engine,
    init_db,
)
from trends.entity_resolution_etl import run_entity_resolution_etl
from trends.keyword_auto_classification import (
    auto_classify_unclassified_keywords,
    classify_pending_news_theme,
)
from trends.keyword_classification import classify_pending_keywords, classify_pending_news
from trends.theme_study import sync_theme_mapping


def refresh_theme_daily_stats(engine: Engine, stat_date: date | None = None) -> int:
    db_engine = init_db(engine)
    target_date = stat_date or date.today()
    with db_engine.connect() as connection:
        mapping = pd.read_sql(
            select(ThemeMapping.theme_name, ThemeMapping.stock_id).where(ThemeMapping.active == 1),
            connection,
        )
        history = pd.read_sql(
            select(
                GoogleTrendsHistory.theme_name,
                GoogleTrendsHistory.keyword,
                GoogleTrendsHistory.trend_score,
            ).where(GoogleTrendsHistory.trend_date == target_date),
            connection,
        )
        rss = pd.read_sql(
            select(
                GoogleTrend.id.label("trend_id"),
                GoogleTrend.published_at,
                GoogleTrend.fetched_at,
                GoogleTrendNews.id.label("news_id"),
                KeywordClassification.theme_name,
            )
            .outerjoin(GoogleTrendNews, GoogleTrendNews.trend_id == GoogleTrend.id)
            .join(KeywordClassification, KeywordClassification.keyword == GoogleTrend.keyword),
            connection,
        )
    stock_counts = (
        mapping.dropna(subset=["stock_id"]).groupby("theme_name")["stock_id"].nunique()
        if not mapping.empty else pd.Series(dtype=int)
    )
    if not rss.empty:
        rss["stat_date"] = pd.to_datetime(rss["published_at"], errors="coerce").fillna(
            pd.to_datetime(rss["fetched_at"], errors="coerce")
        ).dt.date
        rss = rss[rss["stat_date"] == target_date]
    history_groups = {
        str(theme_name): group for theme_name, group in history.groupby("theme_name", sort=False)
    } if not history.empty else {}
    rss_groups = {
        str(theme_name): group for theme_name, group in rss.groupby("theme_name", sort=False)
    } if not rss.empty else {}
    theme_names = sorted(set(mapping["theme_name"].dropna()) | set(history_groups) | set(rss_groups))
    records = []
    for theme_name in theme_names:
        trend_rows = history_groups.get(theme_name, pd.DataFrame(columns=["keyword", "trend_score"]))
        rss_rows = rss_groups.get(theme_name, pd.DataFrame(columns=["trend_id", "news_id"]))
        scores = pd.to_numeric(trend_rows.get("trend_score", pd.Series(dtype=float)), errors="coerce").dropna()
        records.append({
            "stat_date": target_date,
            "theme_name": theme_name,
            "keyword_count": int(trend_rows["keyword"].nunique()) if not trend_rows.empty else 0,
            "news_count": int(rss_rows["news_id"].nunique()) if not rss_rows.empty else 0,
            "event_count": int(rss_rows["trend_id"].nunique()) if not rss_rows.empty else 0,
            "avg_trend_score": float(scores.mean()) if not scores.empty else None,
            "max_trend_score": float(scores.max()) if not scores.empty else None,
            "stock_count": int(stock_counts.get(theme_name, 0)),
        })
    if not records:
        return 0
    statement = insert(ThemeDailyStats.__table__).values(records)
    statement = statement.on_conflict_do_update(
        index_elements=["stat_date", "theme_name"],
        set_={column: getattr(statement.excluded, column) for column in (
            "keyword_count", "news_count", "event_count", "avg_trend_score", "max_trend_score", "stock_count"
        )},
    )
    with db_engine.begin() as connection:
        connection.execute(statement)
    return len(records)


def get_system_status(engine: Engine) -> dict[str, object]:
    db_engine = init_db(engine)
    with db_engine.connect() as connection:
        total_keywords = connection.scalar(
            select(func.count(func.distinct(GoogleTrend.keyword))).where(
                GoogleTrend.keyword.is_not(None), func.trim(GoogleTrend.keyword) != ""
            )
        ) or 0
        classified_keywords = connection.scalar(
            select(func.count(func.distinct(KeywordClassification.keyword)))
            .join(GoogleTrend, GoogleTrend.keyword == KeywordClassification.keyword)
        ) or 0
        unclassified_keywords = connection.scalar(
            select(func.count(func.distinct(GoogleTrend.keyword)))
            .outerjoin(KeywordClassification, GoogleTrend.keyword == KeywordClassification.keyword)
            .where(
                GoogleTrend.keyword.is_not(None),
                func.trim(GoogleTrend.keyword) != "",
                or_(
                    KeywordClassification.keyword.is_(None),
                    (KeywordClassification.classification_source == "fallback")
                    & (KeywordClassification.theme_name == "其他"),
                ),
            )
        ) or 0
        pending_entities = connection.scalar(
            select(func.count(func.distinct(GoogleTrend.keyword)))
            .outerjoin(KeywordClassification, GoogleTrend.keyword == KeywordClassification.keyword)
            .where(
                GoogleTrend.keyword.is_not(None),
                func.trim(GoogleTrend.keyword) != "",
                or_(
                    KeywordClassification.entity_name.is_(None),
                    KeywordClassification.entity_type.is_(None),
                    KeywordClassification.entity_type == "其他",
                ),
            )
        ) or 0
        total_news = connection.scalar(select(func.count(GoogleTrendNews.id))) or 0
        unanalyzed_news = connection.scalar(
            select(func.count(GoogleTrendNews.id)).where(
                GoogleTrendNews.news_title.is_not(None),
                or_(
                    GoogleTrendNews.news_sentiment.is_(None),
                    GoogleTrendNews.sentiment_score.is_(None),
                    GoogleTrendNews.event_type.is_(None),
                ),
            )
        ) or 0
        analyzed_news = connection.scalar(
            select(func.count(GoogleTrendNews.id)).where(
                GoogleTrendNews.news_sentiment.is_not(None),
                GoogleTrendNews.sentiment_score.is_not(None),
                GoogleTrendNews.event_type.is_not(None),
            )
        ) or 0
        stock_rows = connection.scalar(select(func.count()).select_from(Stock)) or 0
        theme_count = connection.scalar(
            select(func.count(func.distinct(ThemeMapping.theme_name))).where(ThemeMapping.active == 1)
        ) or 0
        tracked_stock_count = connection.scalar(
            select(func.count(func.distinct(ThemeMapping.stock_id))).where(
                ThemeMapping.active == 1, ThemeMapping.stock_id.is_not(None)
            )
        ) or 0
        event_count = connection.scalar(select(func.count(GoogleTrend.id))) or 0
        latest_trend_at = connection.scalar(
            select(func.max(func.coalesce(GoogleTrend.fetched_at, GoogleTrend.published_at)))
        )
        earliest_trend_at = connection.scalar(
            select(func.min(func.coalesce(GoogleTrend.published_at, GoogleTrend.fetched_at)))
        )
        latest_stock_date = connection.scalar(select(func.max(Stock.date)))
        latest_theme_stats_date = connection.scalar(select(func.max(ThemeDailyStats.stat_date)))

    now = datetime.now()
    trend_timestamp = pd.to_datetime(latest_trend_at, errors="coerce") if latest_trend_at else pd.NaT
    if pd.isna(trend_timestamp) or now - trend_timestamp.to_pydatetime().replace(tzinfo=None) > timedelta(hours=24):
        health = "stale"
        health_label = "🔴 超過 24 小時未更新"
    else:
        stock_date = pd.to_datetime(latest_stock_date, errors="coerce") if latest_stock_date else pd.NaT
        stats_date = pd.to_datetime(latest_theme_stats_date, errors="coerce") if latest_theme_stats_date else pd.NaT
        stock_stale = pd.isna(stock_date) or (now.date() - stock_date.date()).days > 3
        stats_stale = pd.isna(stats_date) or (now.date() - stats_date.date()).days > 1
        if unclassified_keywords or unanalyzed_news or pending_entities or stock_stale or stats_stale:
            health = "pending"
            health_label = "🟡 部分資料待更新"
        else:
            health = "healthy"
            health_label = "🟢 狀態正常"

    def format_date(value: object) -> str | None:
        timestamp = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(timestamp) else timestamp.strftime("%Y-%m-%d")

    return {
        "health": health,
        "health_label": health_label,
        "latest_trend_at": trend_timestamp.to_pydatetime() if not pd.isna(trend_timestamp) else None,
        "unclassified_keywords": int(unclassified_keywords),
        "unanalyzed_news": int(unanalyzed_news),
        "pending_entities": int(pending_entities),
        "total_keywords": int(total_keywords),
        "classified_keywords": int(classified_keywords),
        "total_news": int(total_news),
        "analyzed_news": int(analyzed_news),
        "stock_rows": int(stock_rows),
        "theme_count": int(theme_count),
        "tracked_stock_count": int(tracked_stock_count),
        "event_count": int(event_count),
        "latest_stock_date": format_date(latest_stock_date),
        "latest_theme_stats_date": format_date(latest_theme_stats_date),
        "history_start": format_date(earliest_trend_at),
        "history_end": format_date(latest_trend_at),
    }


def run_full_update(engine: Engine | None = None) -> dict[str, int]:
    db_engine = init_db(engine or get_engine())
    from trends.stock_collector import fetch_and_store

    stock_rows = fetch_and_store(engine=db_engine)
    entity_rows = run_entity_resolution_etl(db_engine, limit=0)
    keyword_rows = classify_pending_keywords(db_engine, limit=0, resolve_entities=False)
    news_rows = classify_pending_news(db_engine, limit=0)
    theme_rows = refresh_theme_daily_stats(db_engine)
    status = get_system_status(db_engine)
    return {
        "stock_rows": stock_rows,
        "resolved_entities": entity_rows,
        "classified_keywords": keyword_rows,
        "classified_news": news_rows,
        "theme_stats": theme_rows,
        "unclassified_keywords": int(status["unclassified_keywords"]),
        "unanalyzed_news": int(status["unanalyzed_news"]),
    }


def run_daily_etl(
    engine: Engine | None = None,
    keyword_limit: int = 200,
    news_limit: int = 200,
    history_days: int = 90,
) -> dict[str, int]:
    db_engine = init_db(engine or get_engine())
    mapping_rows = sync_theme_mapping(db_engine)
    entity_rows = run_entity_resolution_etl(db_engine, limit=keyword_limit)
    keyword_rows = classify_pending_keywords(db_engine, limit=keyword_limit, resolve_entities=False)
    quality_rows = 0
    try:
        quality_rows = auto_classify_unclassified_keywords(db_engine, limit=100)
    except RuntimeError as error:
        print(f"Auto keyword classification skipped: {error}")
    from trends.trend_history_collector import collect_trend_history

    history_rows = collect_trend_history(db_engine, days=history_days, classify_keywords=False)
    try:
        news_rows = classify_pending_news(db_engine, limit=news_limit)
    except RuntimeError as error:
        print(f"News AI classification skipped: {error}")
        news_rows = 0
    theme_news_rows = classify_pending_news_theme(db_engine, limit=max(200, news_limit))
    stats_rows = refresh_theme_daily_stats(db_engine)
    return {
        "mapping_rows": mapping_rows,
        "resolved_entities": entity_rows,
        "classified_keywords": keyword_rows,
        "quality_classified_keywords": quality_rows,
        "history_rows": history_rows,
        "classified_news": news_rows,
        "classified_news_theme": theme_news_rows,
        "daily_stat_themes": stats_rows,
    }


def schedule_daily_etl() -> None:
    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        run_daily_etl,
        "cron",
        hour=20,
        minute=0,
        id="daily-theme-warehouse-etl",
        max_instances=1,
        coalesce=True,
    )
    print(f"Initial ETL completed: {run_daily_etl()}")
    scheduler.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="執行 Google Trends 主題研究 Warehouse ETL")
    parser.add_argument("--schedule", action="store_true", help="立即執行並每天台北時間 20:00 排程")
    parser.add_argument("--keyword-limit", type=int, default=200)
    parser.add_argument("--news-limit", type=int, default=200, help="每次分類新聞上限；0 代表不限")
    parser.add_argument("--history-days", type=int, default=90)
    args = parser.parse_args()
    if args.schedule:
        schedule_daily_etl()
    else:
        result = run_daily_etl(
            keyword_limit=args.keyword_limit,
            news_limit=args.news_limit,
            history_days=args.history_days,
        )
        print(f"Theme ETL completed: {result}")
