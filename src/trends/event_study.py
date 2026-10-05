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


def detect_research_events(
    daily_heat: pd.DataFrame,
    daily_news: pd.DataFrame,
    heat_growth_threshold: float = 0.5,
    news_multiple_threshold: float = 2.0,
    minimum_news_count: int = 3,
) -> pd.DataFrame:
    columns = ["event_date", "keyword", "event_heat", "news_count", "heat_change", "trigger"]
    heat = daily_heat.copy()
    news = daily_news.copy()
    if heat.empty:
        heat = pd.DataFrame(columns=["date", "keyword", "heat"])
    if news.empty:
        news = pd.DataFrame(columns=["date", "keyword", "news_count"])
    if not heat.empty:
        heat["date"] = pd.to_datetime(heat["date"], errors="coerce").dt.normalize()
        heat["heat"] = pd.to_numeric(heat["heat"], errors="coerce")
        heat = heat.dropna(subset=["date", "keyword", "heat"])
        heat = heat.groupby(["date", "keyword"], as_index=False)["heat"].mean()
    if not news.empty:
        news["date"] = pd.to_datetime(news["date"], errors="coerce").dt.normalize()
        news = news.dropna(subset=["date", "keyword"])
        if "news_id" in news:
            news = news.drop_duplicates("news_id")
        news = news.groupby(["date", "keyword"], as_index=False).size().rename(columns={"size": "news_count"})

    if heat.empty and news.empty:
        return pd.DataFrame(columns=columns)

    keywords = sorted(set(heat.get("keyword", [])) | set(news.get("keyword", [])))
    output: list[dict] = []
    for keyword in keywords:
        keyword_heat = heat.loc[heat["keyword"].eq(keyword)].set_index("date")["heat"]
        keyword_news = news.loc[news["keyword"].eq(keyword)].set_index("date")["news_count"]
        observed_dates = keyword_heat.index.union(keyword_news.index)
        dates = pd.date_range(observed_dates.min(), observed_dates.max(), freq="D")
        heat_series = keyword_heat.reindex(dates)
        news_series = keyword_news.reindex(dates, fill_value=0)
        prior_heat = heat_series.rolling(7, min_periods=3).mean().shift(1)
        prior_news = news_series.rolling(7, min_periods=3).mean().shift(1)
        heat_change = heat_series.div(prior_heat).sub(1)
        heat_trigger = heat_change.ge(heat_growth_threshold) & prior_heat.gt(0)
        news_trigger = (
            news_series.ge(minimum_news_count)
            & news_series.ge(prior_news * news_multiple_threshold)
            & prior_news.notna()
        )

        for event_date in dates[heat_trigger | news_trigger]:
            reasons = []
            if heat_trigger.loc[event_date]:
                reasons.append("搜尋熱度上升")
            if news_trigger.loc[event_date]:
                reasons.append("新聞量增加")
            output.append({
                "event_date": event_date,
                "keyword": keyword,
                "event_heat": heat_series.loc[event_date],
                "news_count": int(news_series.loc[event_date]),
                "heat_change": heat_change.loc[event_date],
                "trigger": "、".join(reasons),
            })

    return pd.DataFrame(output, columns=columns).sort_values(
        ["event_date", "keyword"], ascending=[False, True]
    ).reset_index(drop=True)


def calculate_event_performance(events: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    return_columns = [f"return_{days}d" for days in RETURN_DAYS]
    columns = [
        *events.columns,
        "market_date",
        *return_columns,
        "max_gain_10d",
        "max_loss_10d",
        "reaction_days",
    ]
    if events.empty or prices.empty:
        return pd.DataFrame(columns=columns)

    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    for column in ("close", "high", "low"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "stock_id", "close"])
    price_groups = {
        str(stock_id): group.sort_values("date").reset_index(drop=True)
        for stock_id, group in frame.groupby("stock_id", sort=False)
    }
    output: list[dict] = []
    for event in events.to_dict(orient="records"):
        stock_prices = price_groups.get(str(event.get("stock_id", "")))
        if stock_prices is None:
            continue
        event_date = pd.to_datetime(event.get("event_date"), errors="coerce")
        if pd.isna(event_date):
            continue
        market_dates = stock_prices["date"].to_numpy(dtype="datetime64[ns]")
        position = int(market_dates.searchsorted(event_date.normalize().to_datetime64(), side="left"))
        if position >= len(stock_prices):
            continue
        base_close = stock_prices.iloc[position]["close"]
        if pd.isna(base_close) or base_close == 0:
            continue

        result = {**event, "market_date": stock_prices.iloc[position]["date"]}
        for days in RETURN_DAYS:
            future_position = position + days
            result[f"return_{days}d"] = (
                stock_prices.iloc[future_position]["close"] / base_close - 1
                if future_position < len(stock_prices)
                else pd.NA
            )
        future_prices = stock_prices.iloc[position + 1:position + 11]
        result["max_gain_10d"] = future_prices["high"].max() / base_close - 1 if future_prices["high"].notna().any() else pd.NA
        result["max_loss_10d"] = future_prices["low"].min() / base_close - 1 if future_prices["low"].notna().any() else pd.NA
        future_closes = future_prices["close"].dropna()
        if future_closes.empty:
            result["reaction_days"] = pd.NA
        else:
            cumulative_returns = future_closes.div(base_close).sub(1)
            result["reaction_days"] = int(cumulative_returns.abs().idxmax() - position)
        output.append(result)

    return pd.DataFrame(output, columns=columns)


def run_event_study(engine: Engine, mapping_path: str | Path = DEFAULT_MAPPING_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    events = build_event_frame(engine, mapping_path)
    persist_event_analysis(engine, events)
    return events, summarize_event_study(events)