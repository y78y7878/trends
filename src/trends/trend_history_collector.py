from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import case, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import GoogleTrendsHistory, ThemeMapping, get_engine, init_db
from trends.keyword_classification import classify_pending_keywords
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


EXIT_RATE_LIMITED = 3


@dataclass
class HistoryRunResult:
    rows: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    not_attempted: int = 0
    rate_limited: bool = False

    def summary(self) -> str:
        status = "Stopped: Google Trends rate limited (HTTP 429)" if self.rate_limited else "Completed"
        return (
            f"{status}\n"
            f"Success: {self.success}\n"
            f"Failed: {self.failed}\n"
            f"Skipped: {self.skipped}\n"
            f"Not attempted: {self.not_attempted}\n"
            f"Rows written: {self.rows}"
        )


def _log(level: str, message: str) -> None:
    print(f"[{level}] {message}", flush=True)


def _is_rate_limited(error: Exception) -> bool:
    # pytrends raises TooManyRequestsError (a ResponseError carrying the HTTP response) on 429.
    return getattr(getattr(error, "response", None), "status_code", None) == 429


def run_history_collection(
    engine: Engine | None = None,
    days: int = 90,
    request_delay: float = 1.5,
    trend_client=None,
    classify_keywords: bool = True,
) -> HistoryRunResult:
    db_engine = init_db(engine or get_engine())
    if classify_keywords:
        classify_pending_keywords(db_engine, limit=200)
    sync_theme_mapping(db_engine)
    with db_engine.connect() as connection:
        mappings = pd.read_sql(
            select(
                ThemeMapping.theme_name,
                ThemeMapping.keyword,
                func.max(case((func.coalesce(ThemeMapping.stock_id, "") != "", 1), else_=0)).label("has_stock"),
            )
            .where(ThemeMapping.active == 1)
            .group_by(ThemeMapping.theme_name, ThemeMapping.keyword)
            .order_by(ThemeMapping.theme_name, ThemeMapping.keyword),
            connection,
        )
        latest = pd.read_sql(
            select(
                GoogleTrendsHistory.theme_name,
                GoogleTrendsHistory.keyword,
                func.max(GoogleTrendsHistory.trend_date).label("latest_date"),
            ).group_by(GoogleTrendsHistory.theme_name, GoogleTrendsHistory.keyword),
            connection,
        )
    outcome = HistoryRunResult()
    if mappings.empty:
        _log("INFO", "no active theme keywords; nothing to collect")
        return outcome

    end_date = date.today()
    start_date = end_date - timedelta(days=max(days - 1, 0))
    timeframe = f"{start_date.isoformat()} {end_date.isoformat()}"
    # Today's point is partial and dropped, so a keyword whose history reaches yesterday is current.
    up_to_date_since = end_date - timedelta(days=1)
    latest_dates = {
        (row.theme_name, row.keyword): pd.to_datetime(row.latest_date).date()
        for row in latest.itertuples(index=False)
        if pd.notna(row.latest_date)
    }
    pending = []
    for row in mappings.itertuples(index=False):
        latest_date = latest_dates.get((row.theme_name, row.keyword))
        if latest_date is not None and latest_date >= up_to_date_since:
            _log("SKIP", f"keyword={row.keyword} theme={row.theme_name} already updated (latest={latest_date})")
            outcome.skipped += 1
        else:
            pending.append(row)
    if not pending:
        return outcome
    # Rotation: a run usually ends at Google's first 429 after ~25-100 keywords. History is only
    # the pre-event baseline for stock-mapped keywords (RSS decides event day and size), so those
    # go first; within each group the stalest (never collected first) go first. Alphabetical
    # order kept re-querying the same 「其他」 keywords and starved the rest.
    pending.sort(key=lambda row: (
        -int(row.has_stock),
        latest_dates.get((row.theme_name, row.keyword), date.min),
        row.theme_name,
        row.keyword,
    ))

    if trend_client is None:
        from pytrends.request import TrendReq

        trend_client = TrendReq(
            hl="zh-TW",
            tz=480,
            timeout=(10, 30),
        )

    for index, row in enumerate(pending):
        for attempt in range(3):
            try:
                trend_client.build_payload([row.keyword], timeframe=timeframe, geo="TW")
                result = trend_client.interest_over_time()
                written = 0
                if not result.empty and row.keyword in result:
                    columns = [row.keyword, "isPartial"] if "isPartial" in result else [row.keyword]
                    history = result[columns].rename(columns={row.keyword: "trend_score"}).reset_index()
                    history = history.rename(columns={history.columns[0]: "trend_date"})
                    if "isPartial" in history:
                        history = history[~history["isPartial"].astype(bool)]
                    written = save_history(db_engine, row.theme_name, row.keyword, history)
                outcome.rows += written
                outcome.success += 1
                _log("INFO", f"keyword={row.keyword} theme={row.theme_name} success rows={written}")
                break
            except Exception as error:
                if _is_rate_limited(error):
                    # Retrying or moving on to the next keyword only prolongs Google's block.
                    outcome.failed += 1
                    outcome.rate_limited = True
                    outcome.not_attempted = len(pending) - index - 1
                    _log("ERROR", f"keyword={row.keyword} theme={row.theme_name} rate limited; stopping run")
                    return outcome
                if attempt == 2:
                    outcome.failed += 1
                    _log("ERROR", f"keyword={row.keyword} theme={row.theme_name} failed: {error}")
                else:
                    time.sleep(2 ** (attempt + 1))
        if request_delay > 0:
            time.sleep(request_delay)
    return outcome


def collect_trend_history(
    engine: Engine | None = None,
    days: int = 90,
    request_delay: float = 1.5,
    trend_client=None,
    classify_keywords: bool = True,
) -> int:
    return run_history_collection(
        engine,
        days=days,
        request_delay=request_delay,
        trend_client=trend_client,
        classify_keywords=classify_keywords,
    ).rows


def collect_and_report(**kwargs) -> HistoryRunResult:
    outcome = run_history_collection(**kwargs)
    print(outcome.summary(), flush=True)
    return outcome


def schedule_daily_collection() -> None:
    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        collect_and_report,
        "cron",
        hour=19,
        minute=0,
        id="daily-google-trends-history",
        max_instances=1,
        coalesce=True,
    )
    collect_and_report()
    scheduler.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="收集主題關鍵字近 90 日 Google Trends 歷史熱度")
    parser.add_argument("--schedule", action="store_true", help="立即回補並每天台北時間 19:00 更新")
    args = parser.parse_args()
    if args.schedule:
        schedule_daily_collection()
    else:
        sys.exit(EXIT_RATE_LIMITED if collect_and_report().rate_limited else 0)