from __future__ import annotations

import argparse
from datetime import date

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select
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
from trends.keyword_auto_classification import auto_classify_unclassified_keywords
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


def run_daily_etl(
    engine: Engine | None = None,
    keyword_limit: int = 200,
    news_limit: int = 200,
    history_days: int = 90,
) -> dict[str, int]:
    db_engine = init_db(engine or get_engine())
    mapping_rows = sync_theme_mapping(db_engine)
    keyword_rows = classify_pending_keywords(db_engine, limit=keyword_limit)
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
    stats_rows = refresh_theme_daily_stats(db_engine)
    return {
        "mapping_rows": mapping_rows,
        "classified_keywords": keyword_rows,
        "quality_classified_keywords": quality_rows,
        "history_rows": history_rows,
        "classified_news": news_rows,
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
