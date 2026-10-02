from __future__ import annotations

import argparse
import time
from datetime import date, timedelta

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import GoogleTrendsHistory, ThemeMapping, get_engine, init_db
from trends.theme_study import sync_theme_mapping


def save_history(engine: Engine, theme_name: str, keyword: str, history: pd.DataFrame) -> int:
    if history.empty:
        return 0
    frame = history.copy()
    frame["trend_date"] = pd.to_datetime(frame["trend_date"], errors="coerce").dt.date
    frame["trend_score"] = pd.to_numeric(frame["trend_score"], errors="coerce").fillna(0).astype(int)
    frame = frame.dropna(subset=["trend_date"])
    if frame.empty:
        return 0
    records = [
        {"theme_name": theme_name, "keyword": keyword, **row}
        for row in frame[["trend_date", "trend_score"]].to_dict(orient="records")
    ]
    statement = insert(GoogleTrendsHistory.__table__).values(records)
    statement = statement.on_conflict_do_update(
        index_elements=["keyword", "theme_name", "trend_date"],
        set_={"trend_score": statement.excluded.trend_score},
    )
    with engine.begin() as connection:
        connection.execute(statement)
    return len(records)


def collect_trend_history(
    engine: Engine | None = None,
    days: int = 90,
    request_delay: float = 1.5,
    trend_client=None,
) -> int:
    db_engine = init_db(engine or get_engine())
    sync_theme_mapping(db_engine)
    with db_engine.connect() as connection:
        mappings = pd.read_sql(
            select(ThemeMapping.theme_name, ThemeMapping.keyword).distinct().order_by(
                ThemeMapping.theme_name, ThemeMapping.keyword
            ),
            connection,
        )
    if mappings.empty:
        return 0

    if trend_client is None:
        from pytrends.request import TrendReq

        trend_client = TrendReq(
            hl="zh-TW",
            tz=480,
            timeout=(10, 30),
        )

    end_date = date.today()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    timeframe = f"{start_date.isoformat()} {end_date.isoformat()}"
    written = 0
    for row in mappings.itertuples(index=False):
        for attempt in range(3):
            try:
                trend_client.build_payload([row.keyword], timeframe=timeframe, geo="TW")
                result = trend_client.interest_over_time()
                if not result.empty and row.keyword in result:
                    history = result[[row.keyword]].rename(columns={row.keyword: "trend_score"}).reset_index()
                    history = history.rename(columns={history.columns[0]: "trend_date"})
                    if "isPartial" in history:
                        history = history[~history["isPartial"].astype(bool)]
                    written += save_history(db_engine, row.theme_name, row.keyword, history)
                break
            except Exception as error:
                if attempt == 2:
                    print(f"Failed to collect {row.theme_name}/{row.keyword}: {error}")
                else:
                    time.sleep(2 ** (attempt + 1))
        if request_delay > 0:
            time.sleep(request_delay)
    return written


def schedule_daily_collection() -> None:
    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        collect_trend_history,
        "cron",
        hour=19,
        minute=0,
        id="daily-google-trends-history",
        max_instances=1,
        coalesce=True,
    )
    print(f"Initial history load wrote {collect_trend_history():,} rows")
    scheduler.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="收集主題關鍵字近 90 日 Google Trends 歷史熱度")
    parser.add_argument("--schedule", action="store_true", help="立即回補並每天台北時間 19:00 更新")
    args = parser.parse_args()
    if args.schedule:
        schedule_daily_collection()
    else:
        print(f"已寫入 {collect_trend_history():,} 筆歷史熱度")