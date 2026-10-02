from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import EventAnalysis, GoogleTrend, GoogleTrendNews, Stock, init_db
from trends.features import RETURN_DAYS, add_price_features
from trends.keyword_mapping import DEFAULT_MAPPING_PATH, load_keyword_mapping, match_keyword


RESEARCH_START_DATE = date(2026, 8, 21)


def build_event_frame(
    engine: Engine,
    mapping_path: str | Path = DEFAULT_MAPPING_PATH,
    threshold: int = 88,
    start_date: date = RESEARCH_START_DATE,
) -> pd.DataFrame:
    db_engine = init_db(engine)
    with db_engine.connect() as connection:
        events = pd.read_sql(
            select(
                GoogleTrend.id.label("trend_id"),
                GoogleTrend.keyword,
                GoogleTrend.approx_traffic,
                GoogleTrend.published_at,
                GoogleTrend.fetched_at,
                GoogleTrendNews.id.label("news_id"),
                GoogleTrendNews.news_title,
                GoogleTrendNews.news_source,
                GoogleTrendNews.news_url,
                GoogleTrendNews.news_sentiment,
            )
            .join(GoogleTrendNews, GoogleTrendNews.trend_id == GoogleTrend.id),
            connection,
        )
        prices = pd.read_sql(select(Stock).where(Stock.date >= start_date), connection)

    columns = [
        "trend_id", "news_id", "keyword", "matched_keyword", "event_type", "stock_id", "event_date", "market_date",
        "approx_traffic", "news_title", "news_source", "news_url", "news_sentiment",
        *[f"return_{days}d" for days in RETURN_DAYS],
        *[f"future_return_{days}d" for days in RETURN_DAYS],
        "volume_change",
    ]
    if events.empty or prices.empty:
        return pd.DataFrame(columns=columns)

    mappings = load_keyword_mapping(mapping_path)
    matches = events["keyword"].map(
        lambda keyword: match_keyword(str(keyword), mappings, threshold)
    )
    events[["matched_keyword", "event_type", "stock_ids", "match_score"]] = pd.DataFrame(
        matches.tolist(), index=events.index
    )
    events = events.explode("stock_ids").rename(columns={"stock_ids": "stock_id"})
    events["published_at"] = pd.to_datetime(events["published_at"], errors="coerce")
    events["fetched_at"] = pd.to_datetime(events["fetched_at"], errors="coerce")
    events["observed_at"] = events["fetched_at"].fillna(events["published_at"])
    start_timestamp = pd.Timestamp(start_date)
    events = events.dropna(subset=["stock_id", "published_at", "observed_at"])
    events["stock_id"] = events["stock_id"].astype(str)
    events = events[
        events["published_at"].ge(start_timestamp)
        & events["observed_at"].ge(start_timestamp)
    ]
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
                "matched_keyword": event.matched_keyword,
                "event_type": event.event_type,
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


def build_event_aligned_returns(
    clusters: pd.DataFrame,
    prices: pd.DataFrame,
    start_date: date = RESEARCH_START_DATE,
) -> pd.DataFrame:
    columns = [
        "cluster_id", "stock_id", "event_date", "market_date", "day_offset",
        "close", "base_close", "relative_return",
    ]
    if clusters.empty or prices.empty:
        return pd.DataFrame(columns=columns)

    price_frame = prices.copy()
    price_frame["date"] = pd.to_datetime(price_frame["date"], errors="coerce")
    price_frame["close"] = pd.to_numeric(price_frame["close"], errors="coerce")
    price_frame = price_frame.dropna(subset=["date", "close"])
    price_frame = price_frame[price_frame["date"].dt.date >= start_date]
    if price_frame.empty:
        return pd.DataFrame(columns=columns)

    price_groups = {
        str(stock_id): group.sort_values("date").reset_index(drop=True)
        for stock_id, group in price_frame.groupby("stock_id", sort=False)
    }
    output: list[dict] = []
    for cluster in clusters.itertuples(index=False):
        event_timestamp = pd.to_datetime(cluster.first_seen, errors="coerce")
        if pd.isna(event_timestamp) or event_timestamp.date() < start_date:
            continue
        event_date = event_timestamp.normalize()
        for stock_id in cluster.stock_ids or []:
            stock_prices = price_groups.get(str(stock_id))
            if stock_prices is None or stock_prices.empty:
                continue
            market_dates = stock_prices["date"].to_numpy(dtype="datetime64[ns]")
            day_zero = int(market_dates.searchsorted(event_date.to_datetime64(), side="left"))
            if day_zero >= len(stock_prices):
                continue
            base_close = stock_prices.iloc[day_zero]["close"]
            if pd.isna(base_close) or base_close == 0:
                continue
            for day_offset in (0, 1, 3, 5, 10):
                position = day_zero + day_offset
                if position >= len(stock_prices):
                    continue
                market_row = stock_prices.iloc[position]
                close = market_row["close"]
                output.append({
                    "cluster_id": cluster.event_cluster_id,
                    "stock_id": str(stock_id),
                    "event_date": event_date.date(),
                    "market_date": market_row["date"].date(),
                    "day_offset": day_offset,
                    "close": close,
                    "base_close": base_close,
                    "relative_return": close / base_close - 1,
                })
    return pd.DataFrame(output, columns=columns)


def build_return_comparison(
    event_aligned_returns: pd.DataFrame,
    cluster_id: str | None = None,
) -> pd.DataFrame:
    columns = ["stock_id", "day1", "day3", "day5", "day10"]
    if event_aligned_returns.empty:
        return pd.DataFrame(columns=columns)
    frame = event_aligned_returns
    if cluster_id is not None:
        frame = frame[frame["cluster_id"] == cluster_id]
    frame = frame[frame["day_offset"].isin((1, 3, 5, 10))]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    comparison = frame.pivot_table(
        index="stock_id", columns="day_offset", values="relative_return", aggfunc="first"
    ).rename(columns={1: "day1", 3: "day3", 5: "day5", 10: "day10"})
    for column in columns[1:]:
        if column not in comparison:
            comparison[column] = pd.NA
    return comparison[columns[1:]].reset_index().sort_values("day1", ascending=False, na_position="last")


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