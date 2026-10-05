from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Protocol

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import (
    GoogleTrend,
    GoogleTrendNews,
    KeywordClassification,
    ThemeMapping,
    init_db,
)
from trends.keyword_mapping import load_keyword_mapping, match_keyword
from trends.theme_study import THEME_DEFINITIONS, load_theme_mapping, normalize_theme_name


ALLOWED_THEMES = (*[normalize_theme_name(theme) for theme in THEME_DEFINITIONS], "其他")
EVENT_TYPES = (
    "營收利多", "新品發表", "法人調升", "法人調降", "政策利多", "政策利空", "市場情緒", "產業趨勢", "其他",
)
BATCH_SIZE = 20


def _daily_quota_exhausted(error: Exception) -> bool:
    message = str(error).casefold()
    return "quota exceeded" in message and ("perday" in message or "per day" in message)


class KeywordBatchClassifier(Protocol):
    def classify_batch(self, keywords: list[str]) -> dict[str, dict[str, object]]: ...


def _generate_json(client, model: str, prompt: str) -> dict[str, object]:
    from google.genai import types

    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            payload = json.loads(response.text or "{}")
            return payload if isinstance(payload, dict) else {}
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            if status_code == 429 and _daily_quota_exhausted(error):
                raise
            if status_code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return {}


class GeminiKeywordClassifier:
    def __init__(self, api_key: str | None = None, model: str = "gemini-3.6-flash") -> None:
        load_dotenv()
        resolved_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not resolved_key:
            raise RuntimeError("未設定 GEMINI_API_KEY 或 GOOGLE_API_KEY")
        from google import genai

        self._client = genai.Client(api_key=resolved_key)
        self._model = model

    def classify_batch(self, keywords: list[str]) -> dict[str, dict[str, object]]:
        prompt = f"""
你是台灣 Google Trends 投資研究資料的 keyword normalizer/classifier。
對每個輸入保留原始 keyword，將公司別名或噪音詞正規化為 canonical_keyword。
例如「全友股價」「2305全友」「妖股全友」都應 canonicalize 為「全友」。
主題只能是：{', '.join(ALLOWED_THEMES)}。
sub_theme 使用簡短繁體中文分類；stock_related 只能為 0 或 1；confidence_score 範圍 0 到 1。
無法判斷時 theme_name 用「其他」、confidence_score 用 0，不要臆測公司代號。
只輸出符合 JSON MIME 的物件 {{"items":[{{"keyword":"原始輸入","canonical_keyword":"","theme_name":"","sub_theme":"","stock_related":0,"confidence_score":0.0}}]}}。
輸入：{json.dumps(keywords, ensure_ascii=False)}
"""
        payload = _generate_json(self._client, self._model, prompt)
        return {
            str(item.get("keyword", "")): item
            for item in payload.get("items", [])
            if isinstance(item, dict) and item.get("keyword")
        }


class GeminiNewsClassifier:
    def __init__(self, api_key: str | None = None, model: str = "gemini-3.6-flash") -> None:
        load_dotenv()
        resolved_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not resolved_key:
            raise RuntimeError("未設定 GEMINI_API_KEY 或 GOOGLE_API_KEY")
        from google import genai

        self._client = genai.Client(api_key=resolved_key)
        self._model = model

    def classify_batch(self, articles: list[dict[str, object]]) -> dict[int, dict[str, object]]:
        prompt = f"""
你是繁體中文財經新聞分類器。分析每筆新聞標題與來源，不得依語言或來源排除資料。
news_sentiment 只能是 Positive、Neutral、Negative。
sentiment_score 是該情緒判斷的信心分數，範圍 0 到 1。
event_type 只能是：{', '.join(EVENT_TYPES)}。
只輸出 JSON {{"items":[{{"news_id":1,"news_sentiment":"Neutral","sentiment_score":0.5,"event_type":"產業趨勢"}}]}}。
新聞資料：{json.dumps(articles, ensure_ascii=False, default=str)}
"""
        payload = _generate_json(self._client, self._model, prompt)
        return {
            int(item["news_id"]): item
            for item in payload.get("items", [])
            if isinstance(item, dict) and item.get("news_id") is not None
        }


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _rule_classify(
    keyword: str,
    theme_mapping: pd.DataFrame,
    company_mapping: pd.DataFrame,
) -> dict[str, object]:
    normalized = _normalize(keyword)
    mapping = theme_mapping.copy()
    mapping["normalized"] = mapping["keyword"].map(_normalize)
    exact = mapping[mapping["normalized"].eq(normalized)]
    if not exact.empty:
        row = exact.iloc[0]
        return {
            "canonical_keyword": row["keyword"], "theme_name": row["theme_name"],
            "sub_theme": row["sub_theme"], "stock_related": int(bool(row["stock_id"])),
            "confidence_score": 1.0, "classification_source": "theme_mapping",
        }

    canonical, event_type, stock_ids, _ = match_keyword(keyword, company_mapping)
    if event_type == "company" and stock_ids:
        company_names = company_mapping.loc[
            (company_mapping["type"] == "company")
            & company_mapping["stock_id"].isin(stock_ids),
            "keyword",
        ].drop_duplicates().tolist()
        chinese_names = [name for name in company_names if any(ord(char) > 127 for char in name)]
        selected_name = min(chinese_names or company_names or [canonical], key=len)
        return {
            "canonical_keyword": selected_name,
            "theme_name": "科技類" if stock_ids[0] in THEME_DEFINITIONS["科技類"]["stock_ids"] else "其他",
            "sub_theme": "公司與股票",
            "stock_related": 1,
            "confidence_score": 0.7,
            "classification_source": "mapping_normalizer",
            "stock_ids": stock_ids,
        }

    matches = mapping[mapping["normalized"].map(lambda value: len(value) > 1 and value in normalized)]
    if not matches.empty:
        row = matches.loc[matches["normalized"].str.len().idxmax()]
        return {
            "canonical_keyword": row["keyword"], "theme_name": row["theme_name"],
            "sub_theme": row["sub_theme"], "stock_related": 1,
            "confidence_score": 0.5, "classification_source": "mapping_normalizer",
        }
    return {
        "canonical_keyword": keyword.strip(), "theme_name": "其他", "sub_theme": "未分類",
        "stock_related": 0, "confidence_score": 0.0, "classification_source": "fallback",
    }


def _validate_keyword_result(keyword: str, result: dict[str, object]) -> dict[str, object]:
    theme_name = normalize_theme_name(result.get("theme_name", "其他"))
    if theme_name not in ALLOWED_THEMES:
        theme_name = "其他"
    canonical = str(result.get("canonical_keyword", keyword)).strip() or keyword.strip()
    confidence = pd.to_numeric(result.get("confidence_score", 0), errors="coerce")
    stock_related = pd.to_numeric(result.get("stock_related", 0), errors="coerce")
    return {
        "keyword": keyword,
        "canonical_keyword": canonical[:255],
        "theme_name": theme_name,
        "sub_theme": str(result.get("sub_theme", "未分類")).strip()[:100] or "未分類",
        "stock_related": int(pd.notna(stock_related) and stock_related != 0),
        "confidence_score": float(min(max(confidence if pd.notna(confidence) else 0, 0), 1)),
        "classification_source": str(result.get("classification_source", "gemini"))[:30],
        "stock_ids": [str(stock_id) for stock_id in result.get("stock_ids", [])],
    }


def _theme_stock_pool(engine: Engine, theme_name: str) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(ThemeMapping.stock_id).where(
                ThemeMapping.theme_name == theme_name,
                ThemeMapping.active == 1,
                ThemeMapping.stock_id.is_not(None),
            ).distinct()
        ).scalars().all()
    return [str(stock_id) for stock_id in rows]


def _upsert_theme_rows(engine: Engine, result: dict[str, object]) -> None:
    theme_name = str(result["theme_name"])
    canonical = str(result["canonical_keyword"])
    sub_theme = str(result["sub_theme"])
    stock_ids = list(dict.fromkeys(result.get("stock_ids", [])))
    if result["stock_related"] and not stock_ids:
        stock_ids = _theme_stock_pool(engine, theme_name)
    if not stock_ids:
        stock_ids = [None]
    rows = [{
        "theme_name": theme_name,
        "keyword": canonical,
        "stock_id": stock_id,
        "category": theme_name,
        "sub_theme": sub_theme,
        "active": 1,
    } for stock_id in stock_ids]
    statement = insert(ThemeMapping.__table__).values(rows).on_conflict_do_nothing()
    with engine.begin() as connection:
        previous = connection.execute(
            select(KeywordClassification.canonical_keyword, KeywordClassification.theme_name)
            .where(
                KeywordClassification.keyword == result["keyword"],
                KeywordClassification.classification_source == "fallback",
            )
        ).first()
        if previous and previous.theme_name == "其他":
            connection.execute(update(ThemeMapping).where(
                ThemeMapping.theme_name == "其他",
                ThemeMapping.keyword == previous.canonical_keyword,
            ).values(active=0))
        connection.execute(statement)


def classify_pending_keywords(
    engine: Engine,
    classifier: KeywordBatchClassifier | None = None,
    batch_size: int = BATCH_SIZE,
    limit: int = 500,
    resolve_entities: bool = True,
) -> int:
    db_engine = init_db(engine)
    if resolve_entities:
        from trends.entity_resolution_etl import run_entity_resolution_etl

        run_entity_resolution_etl(db_engine, limit=limit)
    mapping = load_theme_mapping()
    company_mapping = load_keyword_mapping()
    ai_classifier = classifier
    if ai_classifier is None:
        try:
            ai_classifier = GeminiKeywordClassifier()
        except RuntimeError:
            ai_classifier = None
    pending_condition = KeywordClassification.keyword.is_(None)
    if ai_classifier is not None:
        pending_condition = or_(
            pending_condition,
            and_(
                KeywordClassification.classification_source == "fallback",
                KeywordClassification.theme_name == "其他",
            ),
        )
    with db_engine.connect() as connection:
        keywords = connection.execute(
            select(GoogleTrend.keyword)
            .outerjoin(KeywordClassification, GoogleTrend.keyword == KeywordClassification.keyword)
            .where(GoogleTrend.keyword.is_not(None), pending_condition)
            .distinct()
            .order_by(GoogleTrend.keyword)
            .limit(limit)
        ).scalars().all()
    if not keywords:
        return 0

    saved = 0
    for start in range(0, len(keywords), max(batch_size, 1)):
        batch = [str(keyword) for keyword in keywords[start:start + max(batch_size, 1)]]
        predictions: dict[str, dict[str, object]] = {}
        ai_failed = False
        daily_quota_hit = False
        if ai_classifier is not None:
            try:
                predictions = ai_classifier.classify_batch(batch)
            except Exception as error:
                print(f"Keyword AI batch failed ({len(batch)} keywords): {error}")
                ai_failed = True
                daily_quota_hit = _daily_quota_exhausted(error)
        if ai_failed:
            if daily_quota_hit:
                break
            continue
        rows = []
        for keyword in batch:
            prediction = predictions.get(keyword)
            if prediction is None:
                prediction = _rule_classify(keyword, mapping, company_mapping)
            result = _validate_keyword_result(keyword, prediction)
            rows.append({key: result[key] for key in (
                "keyword", "canonical_keyword", "theme_name", "sub_theme", "stock_related",
                "confidence_score", "classification_source",
            )})
            _upsert_theme_rows(db_engine, result)
        statement = insert(KeywordClassification.__table__).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["keyword"],
            set_={column: getattr(statement.excluded, column) for column in rows[0] if column != "keyword"},
            where=KeywordClassification.classification_source == "fallback",
        )
        with db_engine.begin() as connection:
            saved += connection.execute(statement).rowcount or 0
    return saved


def classify_pending_news(
    engine: Engine,
    classifier=None,
    batch_size: int = BATCH_SIZE,
    limit: int = 200,
) -> int:
    db_engine = init_db(engine)
    with db_engine.connect() as connection:
        articles = connection.execute(
            select(GoogleTrendNews.id, GoogleTrendNews.news_title, GoogleTrendNews.news_source)
            .where(
                GoogleTrendNews.news_title.is_not(None),
                or_(
                    GoogleTrendNews.news_sentiment.is_(None),
                    GoogleTrendNews.sentiment_score.is_(None),
                    GoogleTrendNews.event_type.is_(None),
                ),
            )
            .order_by(GoogleTrendNews.id)
            .limit(limit if limit > 0 else None)
        ).all()
    if not articles:
        return 0

    ai_classifier = classifier
    if ai_classifier is None:
        try:
            ai_classifier = GeminiNewsClassifier()
        except RuntimeError:
            ai_classifier = None
    if ai_classifier is None:
        raise RuntimeError("AI 新聞分類需要設定 GEMINI_API_KEY 或 GOOGLE_API_KEY")

    updated = 0
    for start in range(0, len(articles), max(batch_size, 1)):
        batch = [{"news_id": int(row.news_id), "news_title": row.news_title, "news_source": row.news_source} for row in articles[start:start + max(batch_size, 1)]]
        try:
            predictions = ai_classifier.classify_batch(batch)
        except Exception as error:
            print(f"News AI batch failed ({len(batch)} articles): {error}")
            if _daily_quota_exhausted(error):
                break
            continue
        updates = []
        for article in batch:
            result = predictions.get(article["news_id"])
            if result is None:
                continue
            sentiment = str(result.get("news_sentiment", "Neutral"))
            if sentiment not in {"Positive", "Neutral", "Negative"}:
                sentiment = "Neutral"
            event_type = str(result.get("event_type", "其他"))
            if event_type not in EVENT_TYPES:
                event_type = "其他"
            score = pd.to_numeric(result.get("sentiment_score", 0.5), errors="coerce")
            updates.append({
                "news_id": article["news_id"],
                "news_sentiment": sentiment,
                "sentiment_score": float(min(max(score if pd.notna(score) else 0.5, 0), 1)),
                "event_type": event_type,
            })
        if updates:
            with db_engine.begin() as connection:
                for update_row in updates:
                    connection.execute(
                        GoogleTrendNews.__table__.update()
                        .where(GoogleTrendNews.id == update_row["news_id"])
                        .where(or_(
                            GoogleTrendNews.news_sentiment.is_(None),
                            GoogleTrendNews.sentiment_score.is_(None),
                            GoogleTrendNews.event_type.is_(None),
                        ))
                        .values(**{key: value for key, value in update_row.items() if key != "news_id"})
                    )
            updated += len(updates)
    return updated
