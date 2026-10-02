from __future__ import annotations

import csv
import itertools
import re
from pathlib import Path

import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.alpha_signal import parse_traffic
from trends.database import GoogleTrendsHistory, KeywordClassification, ThemeMapping, init_db


DEFAULT_THEME_MAPPING_PATH = Path(__file__).resolve().parents[2] / "theme_mapping.csv"
THEME_DEFINITIONS = {
    "科技類": {
        "keywords": ["AI", "ChatGPT", "OpenAI", "Gemini", "Copilot", "NVIDIA", "輝達", "CoWoS", "HBM", "ASIC", "DRAM", "半導體", "伺服器", "雲端", "資料中心", "聯發科", "台積電"],
        "stock_ids": ["2330", "2454", "2308", "2382", "3231", "6669", "3017", "3324", "3443", "3661", "6533", "NVDA"],
        "subthemes": {"AI": ["AI", "ChatGPT", "OpenAI", "Gemini", "Copilot", "NVIDIA", "輝達"], "半導體": ["CoWoS", "HBM", "ASIC", "DRAM", "半導體", "台積電", "聯發科"], "雲端與資料中心": ["伺服器", "雲端", "資料中心"]},
    },
    "生技醫療類": {
        "keywords": ["疫苗", "癌症", "醫療", "生技", "FDA", "藥證", "基因", "精準醫療"],
        "stock_ids": ["4147", "4128", "6589", "6547", "1795"],
        "subthemes": {"生技醫療": ["疫苗", "癌症", "醫療", "生技", "FDA", "藥證", "基因", "精準醫療"]},
    },
    "遊戲娛樂類": {
        "keywords": ["Steam", "Switch", "Switch2", "PS5", "Xbox", "寶可夢", "原神", "RO", "黑神話", "英雄聯盟", "電競"],
        "stock_ids": ["5478", "3083", "6180", "3546", "3086"],
        "subthemes": {"PC遊戲": ["Steam", "RO"], "主機遊戲": ["Switch", "Switch2", "PS5", "Xbox"], "手遊與電競": ["寶可夢", "原神", "黑神話", "英雄聯盟", "電競"]},
    },
    "食品與消費類": {
        "keywords": ["超商", "統一發票", "飲料", "泡麵", "零食", "咖啡", "餐飲"],
        "stock_ids": ["1216", "1210", "2912", "2727"],
        "subthemes": {"食品與餐飲": ["統一發票", "泡麵", "零食", "餐飲"], "飲料與零售": ["超商", "飲料", "咖啡"]},
    },
    "金融類": {
        "keywords": ["ETF", "0050", "0056", "00919", "00878", "降息", "升息", "Fed", "聯準會", "利率", "本益比", "美債"],
        "stock_ids": ["2881", "2882", "2891", "2886", "0050", "0056", "00878", "00919"],
        "subthemes": {"ETF與退休金": ["ETF", "0050", "0056", "00919", "00878"], "利率與債券": ["降息", "升息", "Fed", "聯準會", "利率", "美債"], "銀行與估值": ["本益比"]},
    },
    "能源與原物料類": {
        "keywords": ["原油", "天然氣", "石化", "鋼鐵", "銅價", "煤"],
        "stock_ids": ["2002", "1301", "1303", "6505"],
        "subthemes": {"能源": ["原油", "天然氣", "煤"], "原物料": ["石化", "鋼鐵", "銅價"]},
    },
    "電商與零售": {
        "keywords": ["蝦皮", "PChome", "momo", "物流", "電商", "網購"],
        "stock_ids": ["8044", "8454", "5903"],
        "subthemes": {"電商": ["蝦皮", "PChome", "momo", "電商", "網購"], "物流與零售": ["物流"]},
    },
    "綠能環保": {
        "keywords": ["太陽能", "風電", "綠能", "ESG", "電動車", "充電樁"],
        "stock_ids": ["2308", "1536", "2233", "1519"],
        "subthemes": {"再生能源": ["太陽能", "風電", "綠能"], "ESG與電動車": ["ESG", "電動車", "充電樁"]},
    },
}
THEME_MAPPING_COLUMNS = ("theme_name", "sub_theme", "keyword", "stock_id", "active", "category")
THEME_ALIASES = {
    "能源與原物料": "能源與原物料類",
    "電商與零售": "電商與零售類",
    "綠能環保": "綠能環保類",
}


def normalize_theme_name(value: object) -> str:
    compact = re.sub(r"\s+", "", str(value))
    aliases = {re.sub(r"\s+", "", old): new for old, new in THEME_ALIASES.items()}
    return aliases.get(compact, compact)


THEME_DEFINITIONS = {
    normalize_theme_name(theme_name): definition
    for theme_name, definition in THEME_DEFINITIONS.items()
}


def _subtheme_for(theme_name: str, keyword: str) -> str:
    for sub_theme, keywords in THEME_DEFINITIONS.get(theme_name, {}).get("subthemes", {}).items():
        if keyword in keywords:
            return sub_theme
    return "其他"


def ensure_theme_mapping_csv(path: str | Path = DEFAULT_THEME_MAPPING_PATH) -> Path:
    mapping_path = Path(path)
    if mapping_path.exists():
        with mapping_path.open(encoding="utf-8-sig", newline="") as file:
            existing_rows = list(csv.DictReader(file))
        if (
            all(column in (existing_rows[0] if existing_rows else {}) for column in THEME_MAPPING_COLUMNS)
            and all(
                row.get("theme_name", "") == normalize_theme_name(row.get("theme_name", ""))
                and row.get("category", "") == normalize_theme_name(row.get("category", ""))
                for row in existing_rows
            )
        ):
            return mapping_path
        rows = []
        for row in existing_rows:
            theme_name = normalize_theme_name(row.get("theme_name", ""))
            keyword = row.get("keyword", "").strip()
            row.update({
                "theme_name": theme_name,
                "sub_theme": row.get("sub_theme") or _subtheme_for(theme_name, keyword),
                "active": row.get("active") or "1",
                "category": normalize_theme_name(row.get("category") or theme_name),
            })
            rows.append(row)
    else:
        mapping_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for theme_name, definition in THEME_DEFINITIONS.items():
            subthemes = {
                keyword: sub_theme
                for sub_theme, keywords in definition["subthemes"].items()
                for keyword in keywords
            }
            for keyword, stock_id in itertools.product(definition["keywords"], definition["stock_ids"]):
                rows.append({
                    "theme_name": theme_name,
                    "sub_theme": subthemes.get(keyword, "其他"),
                    "keyword": keyword,
                    "stock_id": stock_id,
                    "active": "1",
                    "category": theme_name,
                })
    temporary_path = mapping_path.with_suffix(f"{mapping_path.suffix}.tmp")
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=THEME_MAPPING_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temporary_path.replace(mapping_path)
    return mapping_path


def load_theme_mapping(path: str | Path = DEFAULT_THEME_MAPPING_PATH) -> pd.DataFrame:
    mapping_path = ensure_theme_mapping_csv(path)
    frame = pd.read_csv(mapping_path, dtype=str, keep_default_na=False)
    for column in THEME_MAPPING_COLUMNS:
        if column not in frame:
            frame[column] = ""
    frame = frame[list(THEME_MAPPING_COLUMNS)].apply(lambda column: column.str.strip())
    frame["theme_name"] = frame["theme_name"].map(normalize_theme_name)
    frame["sub_theme"] = frame.apply(
        lambda row: row["sub_theme"] or _subtheme_for(row["theme_name"], row["keyword"]), axis=1
    )
    frame["active"] = frame["active"].replace("", "1")
    frame["category"] = frame["category"].map(
        lambda value: normalize_theme_name(value) if value else ""
    ).where(frame["category"].ne(""), frame["theme_name"])
    return frame[frame["theme_name"].ne("") & frame["keyword"].ne("")].drop_duplicates().reset_index(drop=True)


def sync_theme_mapping(engine: Engine, path: str | Path = DEFAULT_THEME_MAPPING_PATH) -> int:
    db_engine = init_db(engine)
    frame = load_theme_mapping(path)
    rows = frame.where(frame.ne(""), None).to_dict(orient="records")
    with db_engine.begin() as connection:
        for table in (ThemeMapping, KeywordClassification, GoogleTrendsHistory):
            existing_names = connection.execute(select(table.theme_name).distinct()).scalars().all()
            for existing_name in existing_names:
                canonical_name = normalize_theme_name(existing_name)
                if canonical_name != existing_name:
                    values = {"theme_name": canonical_name}
                    if table is ThemeMapping:
                        values["category"] = canonical_name
                    connection.execute(update(table).where(
                        table.theme_name == existing_name
                    ).values(**values))
        if rows:
            statement = insert(ThemeMapping.__table__).values(rows).on_conflict_do_nothing()
            connection.execute(statement)
    return len(rows)


def build_theme_definitions(
    theme_mapping: pd.DataFrame,
    keyword_mapping: pd.DataFrame | None = None,
) -> dict[str, dict[str, list[str]]]:
    themes: dict[str, dict[str, list[str]]] = {}
    if not theme_mapping.empty:
        if "active" in theme_mapping:
            theme_mapping = theme_mapping[theme_mapping["active"].astype(str).isin(("1", "True", "true"))]
        for theme_name, rows in theme_mapping.groupby("theme_name", sort=False):
            themes[normalize_theme_name(theme_name)] = {
                "keywords": list(dict.fromkeys(rows["keyword"].astype(str))),
                "stock_ids": list(dict.fromkeys(
                    rows.loc[rows["stock_id"].notna() & rows["stock_id"].astype(str).ne(""), "stock_id"].astype(str)
                )),
            }
    if keyword_mapping is not None and not keyword_mapping.empty:
        legacy_themes = keyword_mapping[keyword_mapping["type"] == "theme"]
        for theme, rows in legacy_themes.groupby("keyword", sort=True):
            definition = themes.setdefault(str(theme), {"keywords": [str(theme)], "stock_ids": []})
            definition["stock_ids"] = list(dict.fromkeys([*definition["stock_ids"], *rows["stock_id"].astype(str)]))
    return themes


def load_theme_definitions(engine: Engine) -> dict[str, dict[str, list[str]]]:
    with engine.connect() as connection:
        mapping = pd.read_sql(select(ThemeMapping), connection)
    return build_theme_definitions(mapping)


def _normalize_keyword(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value).casefold(), flags=re.UNICODE)


def _keyword_lookup(keywords: list[str]) -> dict[str, str]:
    return {_normalize_keyword(keyword): keyword for keyword in keywords if _normalize_keyword(keyword)}


def build_daily_theme_heat(observations: pd.DataFrame, keywords: list[str]) -> pd.DataFrame:
    columns = ["date", "keyword", "heat"]
    if observations.empty or not keywords:
        return pd.DataFrame(columns=columns)

    frame = observations.copy()
    if {"trend_date", "trend_score"}.issubset(frame.columns):
        frame["date"] = pd.to_datetime(frame["trend_date"], errors="coerce").dt.normalize()
        frame["heat"] = pd.to_numeric(frame["trend_score"], errors="coerce")
    else:
        frame["published_at"] = pd.to_datetime(frame["published_at"], errors="coerce")
        frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], errors="coerce")
        frame["date"] = frame["fetched_at"].fillna(frame["published_at"]).dt.normalize()
        frame["heat"] = frame["approx_traffic"].map(parse_traffic)
    keyword_lookup = _keyword_lookup(keywords)
    frame["keyword"] = frame["keyword"].map(
        lambda value: keyword_lookup.get(_normalize_keyword(value))
    )
    frame = frame.dropna(subset=["date", "keyword"])
    if frame.empty:
        return pd.DataFrame(columns=columns)
    return (
        frame.groupby(["date", "keyword"], as_index=False)["heat"]
        .max()
        .sort_values(["date", "keyword"])
        .reset_index(drop=True)
    )


def build_relative_returns(prices: pd.DataFrame, stock_ids: list[str]) -> pd.DataFrame:
    columns = ["date", "stock_id", "base_100", "close"]
    if prices.empty or not stock_ids:
        return pd.DataFrame(columns=columns)
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["stock_id"] = frame["stock_id"].astype(str)
    adjusted = pd.to_numeric(frame.get("adj_close", frame["close"]), errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    frame["close"] = adjusted.where(adjusted.notna(), close)
    frame = frame[frame["stock_id"].isin(set(map(str, stock_ids)))].dropna(subset=["date", "close"])
    frame = frame.sort_values(["stock_id", "date"]).drop_duplicates(["stock_id", "date"])
    frame["base_100"] = frame.groupby("stock_id")["close"].transform(lambda values: values / values.iloc[0] * 100)
    return frame[columns].reset_index(drop=True)


def build_stock_technicals(prices: pd.DataFrame, stock_ids: list[str]) -> pd.DataFrame:
    columns = ["date", "stock_id", "close", "base_100", "ma5", "ma20", "ma60", "volume", "rsi", "macd", "macd_signal"]
    if prices.empty or not stock_ids:
        return pd.DataFrame(columns=columns)
    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["stock_id"] = frame["stock_id"].astype(str)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame.get("volume", 0), errors="coerce")
    frame = frame[frame["stock_id"].isin(set(map(str, stock_ids)))].dropna(subset=["date", "close"])
    rows = []
    for stock_id, group in frame.sort_values("date").groupby("stock_id", sort=False):
        group = group.drop_duplicates("date").copy()
        baseline = group["close"].iloc[0]
        group["base_100"] = group["close"] / baseline * 100
        for window in (5, 20, 60):
            group[f"ma{window}"] = group["close"].rolling(window).mean() / baseline * 100
        changes = group["close"].diff()
        average_gain = changes.clip(lower=0).rolling(14, min_periods=14).mean()
        average_loss = -changes.clip(upper=0).rolling(14, min_periods=14).mean()
        relative_strength = average_gain / average_loss.replace(0, float("nan"))
        group["rsi"] = 100 - 100 / (1 + relative_strength)
        group.loc[average_loss.eq(0) & average_gain.gt(0), "rsi"] = 100
        fast = group["close"].ewm(span=12, adjust=False).mean()
        slow = group["close"].ewm(span=26, adjust=False).mean()
        group["macd"] = fast - slow
        group["macd_signal"] = group["macd"].ewm(span=9, adjust=False).mean()
        rows.append(group.assign(stock_id=stock_id)[columns])
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=columns)


def build_heat_price_correlations(
    daily_heat: pd.DataFrame,
    prices: pd.DataFrame,
    stock_ids: list[str],
    min_observations: int = 5,
) -> pd.DataFrame:
    columns = ["stock_id", "keyword", "pearson", "sample_count"]
    if daily_heat.empty or prices.empty or not stock_ids:
        return pd.DataFrame(columns=columns)
    price_frame = prices.copy()
    price_frame["date"] = pd.to_datetime(price_frame["date"], errors="coerce").dt.normalize()
    price_frame["stock_id"] = price_frame["stock_id"].astype(str)
    price_frame["close"] = pd.to_numeric(price_frame["close"], errors="coerce")
    price_frame = price_frame.dropna(subset=["date", "close"])
    heat = daily_heat.copy()
    heat["date"] = pd.to_datetime(heat["date"], errors="coerce").dt.normalize()
    rows = []
    for stock_id in dict.fromkeys(map(str, stock_ids)):
        stock = price_frame[price_frame["stock_id"].eq(stock_id)].sort_values("date").drop_duplicates("date").copy()
        stock["stock_return"] = stock["close"].pct_change()
        for keyword, keyword_heat in heat.groupby("keyword", sort=False):
            joined = stock[["date", "stock_return"]].merge(
                keyword_heat[["date", "heat"]], on="date", how="inner"
            ).dropna(subset=["stock_return", "heat"])
            correlation = joined["heat"].corr(joined["stock_return"]) if len(joined) >= min_observations else float("nan")
            rows.append({"stock_id": stock_id, "keyword": keyword, "pearson": correlation, "sample_count": len(joined)})
    return pd.DataFrame(rows, columns=columns)


def build_cross_correlations(
    daily_heat: pd.DataFrame,
    prices: pd.DataFrame,
    stock_ids: list[str],
    min_lag: int = -10,
    max_lag: int = 10,
    min_observations: int = 5,
) -> pd.DataFrame:
    columns = ["stock_id", "keyword", "best_lag", "best_correlation", "sample_count"]
    if daily_heat.empty or prices.empty or not stock_ids:
        return pd.DataFrame(columns=columns)
    price_frame = prices.copy()
    price_frame["date"] = pd.to_datetime(price_frame["date"], errors="coerce").dt.normalize()
    price_frame["stock_id"] = price_frame["stock_id"].astype(str)
    price_frame["close"] = pd.to_numeric(price_frame["close"], errors="coerce")
    price_frame = price_frame.dropna(subset=["date", "close"])
    heat = daily_heat.copy()
    heat["date"] = pd.to_datetime(heat["date"], errors="coerce").dt.normalize()
    rows = []
    for stock_id in dict.fromkeys(map(str, stock_ids)):
        stock = price_frame[price_frame["stock_id"].eq(stock_id)].sort_values("date").drop_duplicates("date").copy()
        stock["stock_return"] = stock["close"].pct_change()
        for keyword, keyword_heat in heat.groupby("keyword", sort=False):
            aligned = stock[["date", "stock_return"]].merge(
                keyword_heat[["date", "heat"]], on="date", how="left"
            )
            lag_results = []
            for lag in range(min_lag, max_lag + 1):
                paired = pd.DataFrame({
                    "heat": aligned["heat"],
                    "stock_return": aligned["stock_return"].shift(-lag),
                }).dropna()
                if len(paired) < min_observations:
                    continue
                correlation = paired["heat"].corr(paired["stock_return"])
                if pd.notna(correlation):
                    lag_results.append((lag, float(correlation), len(paired)))
            if lag_results:
                best_lag, best_correlation, sample_count = max(
                    lag_results,
                    key=lambda result: (abs(result[1]), -abs(result[0]), -result[0]),
                )
            else:
                best_lag, best_correlation, sample_count = None, float("nan"), 0
            rows.append({
                "stock_id": stock_id,
                "keyword": keyword,
                "best_lag": best_lag,
                "best_correlation": best_correlation,
                "sample_count": sample_count,
            })
    return pd.DataFrame(rows, columns=columns)


def build_theme_coverage(observations: pd.DataFrame, theme_mapping: pd.DataFrame) -> pd.DataFrame:
    columns = ["theme_name", "event_count", "news_count"]
    if theme_mapping.empty:
        return pd.DataFrame(columns=columns)
    counts = pd.DataFrame(columns=columns)
    if not observations.empty:
        trends = observations[[column for column in ("trend_id", "news_id", "keyword") if column in observations]].copy()
        if "classification_theme" in observations:
            trends["theme_name"] = observations["classification_theme"]
            trends = trends.dropna(subset=["theme_name"])
        else:
            lookup = theme_mapping[["theme_name", "keyword"]].drop_duplicates().copy()
            keyword_lookup: dict[str, list[tuple[str, str]]] = {}
            for row in lookup.itertuples(index=False):
                keyword_lookup.setdefault(_normalize_keyword(row.keyword), []).append((row.theme_name, row.keyword))
            trends["normalized_keyword"] = trends["keyword"].map(_normalize_keyword)
            trends["theme_pairs"] = trends["normalized_keyword"].map(lambda value: keyword_lookup.get(value, []))
            trends = trends.explode("theme_pairs").dropna(subset=["theme_pairs"])
            if not trends.empty:
                trends["theme_name"] = trends["theme_pairs"].map(lambda pair: pair[0])
        if not trends.empty:
            counts = trends.groupby("theme_name", as_index=False).agg(
                event_count=("trend_id", "nunique"), news_count=("news_id", "nunique")
            )
    all_themes = pd.DataFrame({"theme_name": theme_mapping["theme_name"].drop_duplicates()})
    return all_themes.merge(counts, on="theme_name", how="left").fillna({"event_count": 0, "news_count": 0})


def build_unclassified_keywords(observations: pd.DataFrame, theme_mapping: pd.DataFrame, limit: int = 100) -> pd.DataFrame:
    columns = ["keyword", "event_count"]
    if observations.empty or "keyword" not in observations:
        return pd.DataFrame(columns=columns)
    if "classification_theme" in observations:
        frame = observations.loc[
            observations["classification_theme"].isna() | observations["classification_theme"].eq("其他"),
            [column for column in ("trend_id", "keyword") if column in observations],
        ].dropna(subset=["keyword"])
        return frame.drop_duplicates(["trend_id", "keyword"]).groupby("keyword", as_index=False).agg(
            event_count=("trend_id", "nunique")
        ).sort_values("event_count", ascending=False).head(limit).reset_index(drop=True)
    mapped = {_normalize_keyword(value) for value in theme_mapping.get("keyword", pd.Series(dtype=str))}
    frame = observations[[column for column in ("trend_id", "keyword") if column in observations]].dropna(subset=["keyword"])
    frame["normalized_keyword"] = frame["keyword"].map(_normalize_keyword)
    frame = frame[~frame["normalized_keyword"].isin(mapped)].drop_duplicates(["trend_id", "normalized_keyword"])
    return frame.groupby("keyword", as_index=False).agg(event_count=("trend_id", "nunique")).sort_values(
        "event_count", ascending=False
    ).head(limit).reset_index(drop=True)


def build_post_news_returns(
    news: pd.DataFrame,
    prices: pd.DataFrame,
    stock_ids: list[str],
    holding_days: int = 5,
) -> pd.DataFrame:
    columns = ["date", "keyword", "news_title", "news_source", "avg_future_return", "stock_count"]
    if news.empty or prices.empty or not stock_ids:
        return pd.DataFrame(columns=columns)
    price_frame = prices.copy()
    price_frame["date"] = pd.to_datetime(price_frame["date"], errors="coerce").dt.normalize()
    price_frame["stock_id"] = price_frame["stock_id"].astype(str)
    price_frame["close"] = pd.to_numeric(price_frame["close"], errors="coerce")
    price_groups = {
        stock_id: group.sort_values("date").dropna(subset=["close"]).drop_duplicates("date").reset_index(drop=True)
        for stock_id, group in price_frame[price_frame["stock_id"].isin(set(map(str, stock_ids)))].groupby("stock_id")
    }
    rows = []
    for article in news.itertuples(index=False):
        article_date = pd.to_datetime(article.date, errors="coerce")
        returns = []
        for stock_prices in price_groups.values():
            dates = stock_prices["date"].to_numpy(dtype="datetime64[ns]")
            entry_position = int(dates.searchsorted(article_date.normalize().to_datetime64(), side="right"))
            exit_position = entry_position + holding_days
            if entry_position >= len(stock_prices) or exit_position >= len(stock_prices):
                continue
            entry_close = stock_prices.iloc[entry_position]["close"]
            exit_close = stock_prices.iloc[exit_position]["close"]
            if pd.notna(entry_close) and entry_close != 0 and pd.notna(exit_close):
                returns.append(exit_close / entry_close - 1)
        rows.append({
            "date": article_date,
            "keyword": article.keyword,
            "news_title": article.news_title,
            "news_source": article.news_source,
            "avg_future_return": sum(returns) / len(returns) if returns else float("nan"),
            "stock_count": len(returns),
        })
    return pd.DataFrame(rows, columns=columns)


def build_theme_news_timeline(observations: pd.DataFrame, keywords: list[str]) -> pd.DataFrame:
    columns = ["date", "keyword", "news_title", "news_source", "news_url"]
    if observations.empty or not keywords:
        return pd.DataFrame(columns=columns)

    frame = observations.copy()
    source_keyword = "canonical_keyword" if "canonical_keyword" in frame else "keyword"
    frame["date"] = pd.to_datetime(frame["published_at"], errors="coerce")
    fetched_at = pd.to_datetime(frame["fetched_at"], errors="coerce")
    frame["date"] = frame["date"].fillna(fetched_at)
    frame["date"] = frame["date"].dt.normalize()
    keyword_lookup = _keyword_lookup(keywords)
    frame["keyword"] = frame[source_keyword].fillna(frame["keyword"]).map(
        lambda value: keyword_lookup.get(_normalize_keyword(value))
    )
    frame = frame.dropna(subset=["date", "keyword"])
    frame = frame[frame["news_title"].fillna("").astype(str).str.strip().ne("")]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    return frame[columns].drop_duplicates(["date", "keyword", "news_title", "news_source"]).sort_values(
        ["date", "keyword"]
    ).reset_index(drop=True)