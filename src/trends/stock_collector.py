from __future__ import annotations

import argparse
import re
from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import Stock, ThemeMapping, get_engine, init_db
from trends.keyword_mapping import get_stock_pool
from trends.theme_study import load_theme_mapping


DEFAULT_STOCKS = sorted(set(get_stock_pool()) | set(load_theme_mapping()["stock_id"]))
# Taiwan codes: 4-6 digits, ETFs may end in one letter (00631L, 00953B). Listed on TWSE (.TW) or,
# when that returns nothing, TPEx (.TWO).
TAIWAN_CODE = re.compile(r"\d{4,6}[A-Z]?")


def stock_pool(engine: Engine) -> list[str]:
    """CSV stocks plus every stock an active theme_mapping link points to, so companies found by
    entity resolution get prices too (the CSVs alone missed them)."""
    with engine.connect() as connection:
        linked = connection.execute(
            select(ThemeMapping.stock_id).where(ThemeMapping.active == 1, ThemeMapping.stock_id.is_not(None)).distinct()
        ).scalars().all()
    return sorted({stock_id for stock_id in DEFAULT_STOCKS if stock_id} | {str(stock_id).strip().upper() for stock_id in linked if str(stock_id).strip()})


def to_yfinance_ticker(stock_id: str) -> str:
    stock_id = stock_id.strip().upper()
    if TAIWAN_CODE.fullmatch(stock_id):
        return f"{stock_id}.TW"
    return stock_id


def download_prices(stock_id: str, start: date | None = None) -> pd.DataFrame:
    ticker = to_yfinance_ticker(stock_id)
    options = {"auto_adjust": False, "actions": False}
    if start is None:
        options["period"] = "5y"
    else:
        options["start"] = start.isoformat()
        options["end"] = (date.today() + timedelta(days=1)).isoformat()

    history = yf.Ticker(ticker).history(**options)
    if history.empty and TAIWAN_CODE.fullmatch(stock_id.strip().upper()):
        history = yf.Ticker(f"{stock_id.strip()}.TWO").history(**options)
    if history.empty:
        return pd.DataFrame(columns=["date", "stock_id", "open", "high", "low", "close", "adj_close", "volume"])

    history = history.copy()
    if getattr(history.index, "tz", None) is not None:
        history.index = history.index.tz_localize(None)
    history.index.name = "date"
    history.columns = [str(column).lower().replace(" ", "_") for column in history.columns]
    history = history.rename(columns={"adj_close": "adj_close"})
    if "adj_close" not in history:
        history["adj_close"] = history["close"]
    history["stock_id"] = stock_id.upper()
    result = history.reset_index()
    result["date"] = pd.to_datetime(result["date"]).dt.date
    return result[["date", "stock_id", "open", "high", "low", "close", "adj_close", "volume"]]


def save_prices(engine: Engine, prices: pd.DataFrame) -> int:
    if prices.empty:
        return 0

    columns = ["date", "stock_id", "open", "high", "low", "close", "adj_close", "volume"]
    records = prices[columns].where(pd.notna(prices), None).to_dict(orient="records")
    statement = insert(Stock.__table__).values(records)
    statement = statement.on_conflict_do_update(
        index_elements=["date", "stock_id"],
        set_={column: getattr(statement.excluded, column) for column in columns[2:]},
    )
    with engine.begin() as connection:
        connection.execute(statement)
    return len(records)


def fetch_and_store(stock_ids: list[str] | None = None, engine: Engine | None = None) -> int:
    db_engine = init_db(engine or get_engine())
    updated = 0
    for stock_id in stock_ids or stock_pool(db_engine):
        with db_engine.connect() as connection:
            latest_date = connection.execute(
                select(func.max(Stock.date)).where(Stock.stock_id == stock_id.upper())
            ).scalar_one_or_none()
        start = latest_date - timedelta(days=14) if latest_date else None
        updated += save_prices(db_engine, download_prices(stock_id, start=start))
    return updated


def schedule_daily_collection() -> None:
    scheduler = BlockingScheduler(timezone="Asia/Taipei")
    scheduler.add_job(fetch_and_store, "cron", hour=18, minute=0, id="daily-stock-prices")
    fetch_and_store()
    scheduler.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="更新 Google Trends 事件研究用日行情")
    parser.add_argument("--schedule", action="store_true", help="每天台北時間 18:00 更新")
    args = parser.parse_args()
    if args.schedule:
        schedule_daily_collection()
    else:
        print(f"已寫入 {fetch_and_store():,} 筆行情")