from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import EventAnalysis, GoogleTrend, GoogleTrendNews, Stock, init_db
from trends.features import RETURN_DAYS, add_price_features
from trends.keyword_mapping import DEFAULT_MAPPING_PATH, load_keyword_mapping, match_stock


def build_event_frame(
    engine: Engine,
    mapping_path: str | Path = DEFAULT_MAPPING_PATH,
    threshold: int = 78,
) -> pd.DataFrame:
    db_engine = init_db(engine)
    with db_engine.connect() as connection:
        events = pd.read_sql(
            select(
                GoogleTrend.id.label("trend_id"),
                GoogleTrend.keyword,
                GoogleTrend.approx_traffic,
                GoogleTrend.published_at,
                GoogleTrendNews.id.label("news_id"),
                GoogleTrendNews.news_title,
                GoogleTrendNews.news_source,
                GoogleTrendNews.news_url,
                GoogleTrendNews.news_sentiment,
            )
            .join(GoogleTrendNews, GoogleTrendNews.trend_id == GoogleTrend.id),
            connection,
        )
        prices = pd.read_sql(select(Stock), connection)

    columns = [
        "trend_id", "news_id", "keyword", "stock_id", "event_date", "market_date",
        "approx_traffic", "news_title", "news_source", "news_url", "news_sentiment",
        *[f"return_{days}d" for days in RETURN_DAYS],
        *[f"future_return_{days}d" for days in RETURN_DAYS],
        "volume_change",
    ]
    if events.empty or prices.empty:
        return pd.DataFrame(columns=columns)

    mappings = load_keyword_mapping(mapping_path)
    events["stock_id"] = events["keyword"].map(lambda keyword: match_stock(str(keyword), mappings, threshold)[0])
    events["published_at"] = pd.to_datetime(events["published_at"], errors="coerce")
    events = events.dropna(subset=["stock_id", "published_at"])
    if events.empty:
        return pd.DataFrame(columns=columns)

    features = add_price_features(prices)
    features["date"] = pd.to_datetime(features["date"])
    output: list[dict] = []
    for stock_id, stock_events in events.groupby("stock_id"):
        stock_prices = features[features["stock_id"] == stock_id].sort_values("date").reset_index(drop=True)
        if stock_prices.empty:
            continue
        trading_dates = stock_prices["date"].to_numpy(dtype="datetime64[ns]")
        for event in stock_events.itertuples(index=False):
            published_date = event.published_at.normalize()
            position = int(trading_dates.searchsorted(published_date.to_datetime64(), side="left"))
            if position >= len(stock_prices):
                continue
            market_row = stock_prices.iloc[position]
            record = {
                "trend_id": int(event.trend_id),
                "news_id": int(event.news_id),
                "keyword": event.keyword,
                "stock_id": stock_id,
                "event_date": published_date.date(),
                "market_date": market_row["date"].date(),
                "approx_traffic": event.approx_traffic,
                "news_title": event.news_title,
                "news_source": event.news_source,
                "news_url": event.news_url,
                "news_sentiment": event.news_sentiment,
                "volume_change": market_row["volume_change"],
            }
            for days in RETURN_DAYS:
                record[f"return_{days}d"] = market_row[f"return_{days}d"]
                record[f"future_return_{days}d"] = market_row[f"future_return_{days}d"]
            output.append(record)
    return pd.DataFrame(output, columns=columns)


def persist_event_analysis(engine: Engine, event_frame: pd.DataFrame) -> int:
    db_engine = init_db(engine)
    with db_engine.begin() as connection:
        connection.execute(delete(EventAnalysis))
        if event_frame.empty:
            return 0
        rows = event_frame[
            [
                "trend_id", "news_id", "keyword", "stock_id", "event_date", "news_sentiment",
                *[f"return_{days}d" for days in RETURN_DAYS],
                *[f"future_return_{days}d" for days in RETURN_DAYS],
            ]
        ].where(pd.notna(event_frame), None).to_dict(orient="records")
        connection.execute(insert(EventAnalysis.__table__), rows)
    return len(event_frame)


def summarize_event_study(event_frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for days in RETURN_DAYS:
        column = f"future_return_{days}d"
        returns = pd.to_numeric(event_frame.get(column, pd.Series(dtype=float)), errors="coerce").dropna()
        rows.append(
            {
                "holding_days": days,
                "event_count": int(returns.size),
                "avg_return_pct": returns.mean() * 100 if not returns.empty else None,
                "win_rate_pct": (returns.gt(0).mean() * 100) if not returns.empty else None,
                "max_gain_pct": returns.max() * 100 if not returns.empty else None,
                "max_loss_pct": returns.min() * 100 if not returns.empty else None,
            }
        )
    return pd.DataFrame(rows)


def run_event_study(engine: Engine, mapping_path: str | Path = DEFAULT_MAPPING_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    events = build_event_frame(engine, mapping_path)
    persist_event_analysis(engine, events)
    return events, summarize_event_study(events)