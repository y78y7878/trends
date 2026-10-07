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


def _label_event(event_item: pd.Series | dict[str, object]) -> str:
    if isinstance(event_item, pd.Series):
        keyword = event_item.get("keyword") or "事件"
        event_date = event_item.get("event_date")
    else:
        keyword = event_item.get("keyword") or "事件"
        event_date = event_item.get("event_date")
    if pd.isna(event_date):
        return str(keyword)
    try:
        event_date = pd.to_datetime(event_date).strftime("%Y-%m-%d")
    except Exception:
        event_date = str(event_date)
    return f"{keyword}（{event_date}）"


def build_event_research_summary(performance: pd.DataFrame) -> dict[str, float | int | str | None]:
    if performance.empty:
        return {
            "event_count": 0,
            "positive_return_ratio_pct": None,
            "negative_return_ratio_pct": None,
            "avg_reaction_days": None,
            "best_event": "N/A",
            "worst_event": "N/A",
            "avg_return_1d_pct": None,
            "avg_return_3d_pct": None,
            "avg_return_5d_pct": None,
            "avg_return_10d_pct": None,
        }

    metric_columns = ["return_1d", "return_3d", "return_5d", "return_10d"]
    summary = {
        "event_count": int(len(performance)),
        "positive_return_ratio_pct": None,
        "negative_return_ratio_pct": None,
        "avg_reaction_days": None,
        "best_event": "N/A",
        "worst_event": "N/A",
    }

    returns_10d = pd.to_numeric(performance.get("return_10d", pd.Series(dtype=float)), errors="coerce").dropna()
    if not returns_10d.empty:
        summary["positive_return_ratio_pct"] = float((returns_10d > 0).mean() * 100)
        summary["negative_return_ratio_pct"] = float((returns_10d < 0).mean() * 100)

    reaction_days = pd.to_numeric(performance.get("reaction_days", pd.Series(dtype=float)), errors="coerce").dropna()
    reaction_days = reaction_days[(reaction_days >= 0) & (reaction_days <= 30)]
    if not reaction_days.empty:
        summary["avg_reaction_days"] = float(reaction_days.mean())

    if "return_10d" in performance.columns:
        ten_day_returns = pd.to_numeric(performance["return_10d"], errors="coerce")
        if ten_day_returns.notna().any():
            best_idx = ten_day_returns.idxmax()
            worst_idx = ten_day_returns.idxmin()
            summary["best_event"] = _label_event(performance.loc[best_idx])
            summary["worst_event"] = _label_event(performance.loc[worst_idx])

    for column in metric_columns:
        returns = pd.to_numeric(performance.get(column, pd.Series(dtype=float)), errors="coerce").dropna()
        suffix = column.replace("return_", "")
        summary[f"avg_return_{suffix}_pct"] = float(returns.mean() * 100) if not returns.empty else None
    return summary


def _format_metric(value: object, suffix: str) -> str:
    if value is None or pd.isna(value):
        return "資料不足"
    return f"{float(value):.1f}{suffix}"


def build_event_research_overview(theme_name: str, start_date: date | pd.Timestamp, end_date: date | pd.Timestamp, performance: pd.DataFrame) -> str:
    if performance.empty:
        return f"{theme_name}事件研究：目前沒有足夠事件樣本，無法形成有說服力的市場反應結論。"
    summary = build_event_research_summary(performance)
    avg_10d = summary.get("avg_return_10d_pct")
    positive_ratio = summary.get("positive_return_ratio_pct")
    negative_ratio = summary.get("negative_return_ratio_pct")
    avg_reaction = summary.get("avg_reaction_days")
    if avg_10d is None:
        conclusion = "目前資料不足，無法穩定判斷事件與股價後續關聯。"
    elif avg_10d > 0:
        conclusion = f"{theme_name}熱門事件與股價存在輕度正向關聯，事件後 10 日平均表現呈現正值。"
    elif avg_10d < 0:
        conclusion = f"{theme_name}熱門事件在本期間內未呈現明顯正向關聯，後續股價反應偏弱或負向。"
    else:
        conclusion = f"{theme_name}熱門事件在本期間內的價格反應接近中性，表示事件影響較弱。"

    start_label = pd.Timestamp(start_date).strftime("%Y-%m-%d") if not isinstance(start_date, str) else str(start_date)
    end_label = pd.Timestamp(end_date).strftime("%Y-%m-%d") if not isinstance(end_date, str) else str(end_date)
    return (
        f"{theme_name}事件研究\n"
        f"研究期間：{start_label} ~ {end_label}\n"
        f"事件數量：{summary['event_count']}\n"
        f"正報酬事件：{_format_metric(positive_ratio, '%')}\n"
        f"負報酬事件：{_format_metric(negative_ratio, '%')}\n"
        f"平均首次反應天數：{_format_metric(avg_reaction, ' 天')}\n"
        f"平均1日報酬：{_format_metric(summary['avg_return_1d_pct'], '%')}\n"
        f"平均3日報酬：{_format_metric(summary['avg_return_3d_pct'], '%')}\n"
        f"平均5日報酬：{_format_metric(summary['avg_return_5d_pct'], '%')}\n"
        f"平均10日報酬：{_format_metric(summary['avg_return_10d_pct'], '%')}\n\n"
        f"AI結論：{conclusion}"
    )


def build_hypothesis_validation(performance: pd.DataFrame) -> list[dict[str, str | float | int]]:
    validations: list[dict[str, str | float | int]] = []
    if performance.empty:
        return []

    ten_day_returns = pd.to_numeric(performance.get("return_10d", pd.Series(dtype=float)), errors="coerce").dropna()
    positive_ratio = (ten_day_returns.gt(0).mean() * 100) if not ten_day_returns.empty else 0.0
    if ten_day_returns.empty:
        status = "資料不足"
        reason = "10日報酬樣本不足。"
    elif positive_ratio >= 55:
        status = "成立"
        reason = f"{len(performance)} 個事件中，{positive_ratio:.1f}% 於 10 日內產生正報酬。"
    elif positive_ratio >= 40:
        status = "部分成立"
        reason = f"{len(performance)} 個事件中，{positive_ratio:.1f}% 於 10 日內產生正報酬。"
    else:
        status = "不成立"
        reason = f"{len(performance)} 個事件中，{positive_ratio:.1f}% 於 10 日內產生正報酬。"
    validations.append({
        "title": "假說1：當搜尋熱度顯著提升時，股票較容易出現正報酬。",
        "status": status,
        "basis": reason,
    })

    positive_count = int((performance.get("news_sentiment", pd.Series(dtype=str)).fillna("Neutral").str.title() == "Positive").sum())
    negative_count = int((performance.get("news_sentiment", pd.Series(dtype=str)).fillna("Neutral").str.title() == "Negative").sum())
    if positive_count < 5 and negative_count < 5:
        validations.append({
            "title": "假說2：正面新聞影響大於負面新聞。",
            "status": "資料不足",
            "basis": "Positive 與 Negative 樣本數均不足，無法比較不同情緒的市場效應。",
        })
    else:
        positive_group = performance[performance.get("news_sentiment", pd.Series(dtype=str)).fillna("Neutral").str.title().eq("Positive")]
        negative_group = performance[performance.get("news_sentiment", pd.Series(dtype=str)).fillna("Neutral").str.title().eq("Negative")]
        pos_mean = positive_group["return_10d"].mean() if not positive_group.empty else float("nan")
        neg_mean = negative_group["return_10d"].mean() if not negative_group.empty else float("nan")
        if pd.isna(pos_mean) or pd.isna(neg_mean):
            status = "資料不足"
            basis = "Positive 或 Negative 樣本數不足。"
        elif pos_mean > neg_mean:
            status = "成立"
            basis = f"Positive 事件平均 10 日報酬為 {pos_mean * 100:.1f}%；Negative 事件平均 10 日報酬為 {neg_mean * 100:.1f}% 。"
        else:
            status = "不成立"
            basis = f"Positive 事件平均 10 日報酬為 {pos_mean * 100:.1f}%；Negative 事件平均 10 日報酬為 {neg_mean * 100:.1f}% 。"
        validations.append({
            "title": "假說2：正面新聞影響大於負面新聞。",
            "status": status,
            "basis": basis,
        })

    if "theme" in performance.columns and performance["theme"].notna().any():
        grouped = performance.groupby("theme")["reaction_days"].apply(lambda values: pd.to_numeric(values, errors="coerce").dropna()[(pd.to_numeric(values, errors="coerce").dropna() >= 0) & (pd.to_numeric(values, errors="coerce").dropna() <= 30)].mean())
        themes = grouped.sort_values().dropna()
        if not themes.empty:
            theme_text = ", ".join(f"{name}：{value:.0f}天" for name, value in themes.items())
            validations.append({
                "title": "假說3：不同主題存在不同市場反應速度。",
                "status": "成立",
                "basis": theme_text,
            })
        else:
            validations.append({
                "title": "假說3：不同主題存在不同市場反應速度。",
                "status": "資料不足",
                "basis": "主題層級首次反應天數不足，無法判斷主題差異。",
            })
    else:
        validations.append({
            "title": "假說3：不同主題存在不同市場反應速度。",
            "status": "資料不足",
            "basis": "本視角未含主題分層資料，無法比較主題差異。",
        })

    return validations


def normalize_breakout_day(value: object) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    day_value = int(float(value))
    if day_value == 0:
        return "Day0"
    return f"{day_value}"


def build_event_research_text(event: pd.Series | dict[str, object], performance: pd.DataFrame) -> str:
    if event is None:
        return "尚未選擇事件，請從列表中選擇一個事件來查看解讀。"

    if isinstance(event, dict):
        event_row = pd.Series(event)
    else:
        event_row = event

    keyword = str(event_row.get("keyword") or "事件")
    event_date = pd.to_datetime(event_row.get("event_date"), errors="coerce")
    event_date_str = event_date.strftime("%Y-%m-%d") if pd.notna(event_date) else "未知日期"

    heat_value = pd.to_numeric(event_row.get("event_heat"), errors="coerce")
    heat_change = pd.to_numeric(event_row.get("heat_change"), errors="coerce")
    previous_heat = None
    if pd.notna(heat_value) and pd.notna(heat_change):
        previous_heat = float(heat_value / (1 + heat_change)) if abs(heat_change) > 0 else float(heat_value)

    heat_text = "熱度資料不足"
    if pd.notna(heat_value):
        if previous_heat is not None:
            direction = "上升" if (pd.notna(heat_change) and heat_change >= 0) else "下降"
            heat_text = f"熱度由{int(round(previous_heat))}{direction}至{int(round(float(heat_value)))}"
        else:
            heat_text = f"熱度為{int(round(float(heat_value)))}"

    sentiment_value = event_row.get("news_sentiment") or "N/A"
    sentiment_text = f"新聞情緒為{sentiment_value}。" if sentiment_value not in {"N/A", "", None} else "新聞情緒資料尚不足。"

    return_3d = pd.to_numeric(event_row.get("return_3d"), errors="coerce")
    if pd.notna(return_3d):
        stock_move = f"事件後3日股價累積{('上漲' if return_3d > 0 else '下跌')}{abs(return_3d * 100):.1f}%"
    else:
        stock_move = "事件後3日股價變化資料尚不足。"

    ma20_day = pd.to_numeric(event_row.get("ma20_breakout_day"), errors="coerce")
    ma60_day = pd.to_numeric(event_row.get("ma60_breakout_day"), errors="coerce")
    if pd.notna(ma20_day):
        ma20_text = f"事件後第{normalize_breakout_day(ma20_day)}日突破MA20。" if int(float(ma20_day)) != 0 else "事件當天突破MA20。"
    elif "ma20" in event_row and pd.notna(event_row.get("ma20")):
        ma20_text = "事件後已突破MA20。"
    else:
        ma20_text = "事件後未出現 MA20 突破訊號。"
    if pd.notna(ma60_day):
        ma60_text = f"事件後第{normalize_breakout_day(ma60_day)}日突破MA60。" if int(float(ma60_day)) != 0 else "事件當天突破MA60。"
    elif "ma60" in event_row and pd.notna(event_row.get("ma60")):
        ma60_text = "事件後已突破MA60。"
    else:
        ma60_text = "事件後未出現 MA60 突破訊號。"

    if pd.notna(heat_change) and heat_change > 0 and pd.notna(return_3d) and return_3d > 0:
        hypothesis_text = "符合熱度上升後股價正向反應假說。"
    elif pd.notna(heat_change) and heat_change > 0 and pd.notna(return_3d) and return_3d <= 0:
        hypothesis_text = "熱度上升，但事件後3日股價未呈現正向反應，未符合熱度上升後股價正向反應假說。"
    else:
        hypothesis_text = "資料不足，無法判定是否符合熱度上升後股價正向反應假說。"

    return (
        f"{keyword}事件於{event_date_str}發生。"
        f"{heat_text}。"
        f"{sentiment_text}"
        f"{stock_move}。"
        f"{ma20_text}"
        f"{ma60_text}"
        f"{hypothesis_text}"
    )


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
        "ma20_breakout_day",
        "ma60_breakout_day",
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
        stock_prices = stock_prices.copy()
        stock_prices["ma20"] = stock_prices["close"].rolling(20, min_periods=1).mean()
        stock_prices["ma60"] = stock_prices["close"].rolling(60, min_periods=1).mean()

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
        result["ma20_breakout_day"] = pd.NA
        result["ma60_breakout_day"] = pd.NA
        for offset in range(0, min(30, len(stock_prices) - position) + 1):
            current_index = position + offset
            if current_index >= len(stock_prices):
                break
            current_row = stock_prices.iloc[current_index]
            previous_index = current_index - 1
            previous_row = stock_prices.iloc[previous_index] if previous_index >= 0 else None
            if previous_row is None and current_index == position:
                if current_row["close"] > current_row["ma20"]:
                    result["ma20_breakout_day"] = 0
                if current_row["close"] > current_row["ma60"]:
                    result["ma60_breakout_day"] = 0
                continue
            if previous_row is not None:
                if pd.isna(result["ma20_breakout_day"]) and current_row["close"] > current_row["ma20"] and previous_row["close"] <= previous_row["ma20"]:
                    result["ma20_breakout_day"] = offset
                if pd.isna(result["ma60_breakout_day"]) and current_row["close"] > current_row["ma60"] and previous_row["close"] <= previous_row["ma60"]:
                    result["ma60_breakout_day"] = offset
            if not pd.isna(result["ma20_breakout_day"]) and not pd.isna(result["ma60_breakout_day"]):
                break

        future_closes = stock_prices.iloc[position + 1:position + 31]["close"].dropna()
        reaction_days = pd.NA
        if not future_closes.empty:
            daily_returns = future_closes.div(base_close).sub(1)
            significant = daily_returns.abs().ge(0.01)
            if significant.any():
                first_signal = significant.idxmax()
                reaction_days = int(first_signal - position)
            if reaction_days is not pd.NA and reaction_days > 30:
                reaction_days = pd.NA
        result["reaction_days"] = reaction_days
        output.append(result)

    return pd.DataFrame(output, columns=columns)


def run_event_study(engine: Engine, mapping_path: str | Path = DEFAULT_MAPPING_PATH) -> tuple[pd.DataFrame, pd.DataFrame]:
    events = build_event_frame(engine, mapping_path)
    persist_event_analysis(engine, events)
    return events, summarize_event_study(events)