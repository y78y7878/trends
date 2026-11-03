from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol, Sequence

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import (
    LINK_DIRECT,
    LINK_THEME,
    GoogleTrend,
    GoogleTrendNews,
    KeywordClassification,
    ThemeMapping,
    init_db,
)
from trends.gemini_control import (
    GeminiProgress, gemini_models, get_gemini_limiter, is_daily_quota_error, should_stop_batches,
)
from trends.keyword_mapping import load_keyword_mapping, match_keyword
from trends.theme_study import THEME_DEFINITIONS, load_theme_mapping, normalize_theme_name, theme_basket


ALLOWED_THEMES = (*[normalize_theme_name(theme) for theme in THEME_DEFINITIONS], "其他")
EVENT_TYPES = (
    "營收利多", "新品發表", "法人調升", "法人調降", "政策利多", "政策利空", "市場情緒", "產業趨勢", "其他",
)
BATCH_SIZE = 20
# link_type answers: the keyword is one listed company, an industry word that moves its theme's
# curated basket, or neither (people, unlisted or foreign companies, events, everything else).
LINK_COMPANY = "company"
LINK_INDUSTRY = "industry"
LINK_NONE = "none"


def _daily_quota_exhausted(error: Exception) -> bool:
    return is_daily_quota_error(error)


class KeywordBatchClassifier(Protocol):
    def classify_batch(self, keywords: list[str]) -> dict[str, dict[str, object]]: ...


def _generate_json(
    client, models: str | Sequence[str], prompt: str, task: str = "gemini", items: int = 0
) -> dict[str, object]:
    from google.genai import types

    # Every Gemini request shares one limiter: 5 per minute per model, 429/5xx retry policy,
    # fallback to the next model, run stats.
    limiter = get_gemini_limiter()
    response = limiter.call(lambda model: client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    ), [models] if isinstance(models, str) else models)
    payload = json.loads(response.text or "{}")
    limiter.stats.record_processed(task, items)
    return payload if isinstance(payload, dict) else {}


class GeminiKeywordClassifier:
    task = "keyword"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        load_dotenv()
        resolved_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not resolved_key:
            raise RuntimeError("未設定 GEMINI_API_KEY 或 GOOGLE_API_KEY")
        from google import genai

        self._client = genai.Client(api_key=resolved_key)
        self._models = (model,) if model else gemini_models()

    def classify_batch(self, keywords: list[str]) -> dict[str, dict[str, object]]:
        prompt = f"""
你是台灣 Google Trends 投資研究資料的 keyword normalizer/classifier。
對每個輸入保留原始 keyword，將公司別名或噪音詞正規化為 canonical_keyword。
例如「全友股價」「2305全友」「妖股全友」都應 canonicalize 為「全友」。
主題只能是：{', '.join(ALLOWED_THEMES)}。
sub_theme 使用簡短繁體中文分類；confidence_score 範圍 0 到 1。
link_type 只能是：
- company：關鍵字就是一家台灣上市櫃公司或 ETF（例如「元大金控」「台積電」）。
- industry：直接描述該主題產業本身的用語（技術、原物料、產業名稱）或總體經濟、金融市場指標與政策，熱門時可能帶動該主題的同類股票（例如「保險」「伺服器」「先進封裝」「油價」「升息」「CPI」「外資」）。
- none：其他一切，包括人名、未在台灣上市的外國公司、品牌、事件、娛樂或無法判斷者（例如「謝金河」「Uber」）。
  以下也一律是 none：單一消費產品或型號（例如「AirPods 4」「HR-V」「Tesla Roadster」）、交通工具與軍事裝備（例如「機車」「波音787」「航空母艦」）、生活娛樂與活動（例如「夜市」「燒肉」「演唱會」）、個人理財生活用語（例如「套牢」「四貸同堂」）。
無法判斷時 theme_name 用「其他」、link_type 用 none、confidence_score 用 0，不要臆測公司代號。
只輸出符合 JSON MIME 的物件 {{"items":[{{"keyword":"原始輸入","canonical_keyword":"","theme_name":"","sub_theme":"","link_type":"none","confidence_score":0.0}}]}}。
輸入：{json.dumps(keywords, ensure_ascii=False)}
"""
        payload = _generate_json(self._client, self._models, prompt, task=self.task, items=len(keywords))
        return {
            str(item.get("keyword", "")): item
            for item in payload.get("items", [])
            if isinstance(item, dict) and item.get("keyword")
        }


class GeminiNewsClassifier:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        load_dotenv()
        resolved_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not resolved_key:
            raise RuntimeError("未設定 GEMINI_API_KEY 或 GOOGLE_API_KEY")
        from google import genai

        self._client = genai.Client(api_key=resolved_key)
        self._models = (model,) if model else gemini_models()

    def classify_batch(self, articles: list[dict[str, object]]) -> dict[int, dict[str, object]]:
        prompt = f"""
你是繁體中文財經新聞分類器。分析每筆新聞標題與來源，不得依語言或來源排除資料。
news_sentiment 只能是 Positive、Neutral、Negative。
sentiment_score 是該情緒判斷的信心分數，範圍 0 到 1。
event_type 只能是：{', '.join(EVENT_TYPES)}。
只輸出 JSON {{"items":[{{"news_id":1,"news_sentiment":"Neutral","sentiment_score":0.5,"event_type":"產業趨勢"}}]}}。
新聞資料：{json.dumps(articles, ensure_ascii=False, default=str)}
"""
        payload = _generate_json(self._client, self._models, prompt, task="news", items=len(articles))
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
            "sub_theme": row["sub_theme"], "link_type": LINK_INDUSTRY if row["stock_id"] else LINK_NONE,
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
            "link_type": LINK_COMPANY,
            "confidence_score": 0.7,
            "classification_source": "mapping_normalizer",
            "stock_ids": stock_ids,
        }

    matches = mapping[mapping["normalized"].map(lambda value: len(value) > 1 and value in normalized)]
    if not matches.empty:
        row = matches.loc[matches["normalized"].str.len().idxmax()]
        return {
            "canonical_keyword": row["keyword"], "theme_name": row["theme_name"],
            "sub_theme": row["sub_theme"], "link_type": LINK_INDUSTRY,
            "confidence_score": 0.5, "classification_source": "mapping_normalizer",
        }
    return {
        "canonical_keyword": keyword.strip(), "theme_name": "其他", "sub_theme": "未分類",
        "link_type": LINK_NONE, "confidence_score": 0.0, "classification_source": "fallback",
    }


def _validate_keyword_result(keyword: str, result: dict[str, object]) -> dict[str, object]:
    theme_name = normalize_theme_name(result.get("theme_name", "其他"))
    if theme_name not in ALLOWED_THEMES:
        theme_name = "其他"
    canonical = str(result.get("canonical_keyword", keyword)).strip() or keyword.strip()
    confidence = pd.to_numeric(result.get("confidence_score", 0), errors="coerce")
    stock_ids = [str(stock_id) for stock_id in result.get("stock_ids", [])]
    link_type = str(result.get("link_type", LINK_NONE)).strip().casefold()
    if link_type not in (LINK_COMPANY, LINK_INDUSTRY) or theme_name == "其他":
        link_type = LINK_NONE
    if stock_ids:
        link_type = LINK_COMPANY
    return {
        "keyword": keyword,
        "canonical_keyword": canonical[:255],
        "theme_name": theme_name,
        "sub_theme": str(result.get("sub_theme", "未分類")).strip()[:100] or "未分類",
        "stock_related": int(link_type != LINK_NONE),
        "link_type": link_type,
        "confidence_score": float(min(max(confidence if pd.notna(confidence) else 0, 0), 1)),
        "classification_source": str(result.get("classification_source", "gemini"))[:30],
        "stock_ids": stock_ids,
    }


def _reactivating_insert(rows: list[dict[str, object]]):
    """Insert theme_mapping links; a link that already exists (e.g. deactivated by p1_link_type
    as fan-out) is re-enabled with the new link_type instead of being silently skipped."""
    statement = insert(ThemeMapping.__table__).values(rows)
    return statement.on_conflict_do_update(
        index_elements=["theme_name", "keyword", "stock_id", "category"],
        set_={"active": 1, "link_type": statement.excluded.link_type, "sub_theme": statement.excluded.sub_theme},
    )


def _upsert_theme_rows(engine: Engine, result: dict[str, object]) -> None:
    theme_name = str(result["theme_name"])
    canonical = str(result["canonical_keyword"])
    sub_theme = str(result["sub_theme"])
    # A company links only to its own stock (one Gemini cannot name is left to entity
    # resolution); an industry word links to the theme's curated basket, never to every stock
    # that has ever been mapped to the theme.
    link_type = None
    stock_ids: list[str | None] = list(dict.fromkeys(result.get("stock_ids", [])))
    if stock_ids:
        link_type = LINK_DIRECT
    elif result["link_type"] == LINK_INDUSTRY:
        stock_ids = theme_basket(theme_name)
        link_type = LINK_THEME if stock_ids else None
    if not stock_ids:
        stock_ids = [None]
    rows = [{
        "theme_name": theme_name,
        "keyword": canonical,
        "stock_id": stock_id,
        "category": theme_name,
        "sub_theme": sub_theme,
        "active": 1,
        "link_type": link_type if stock_id else None,
    } for stock_id in stock_ids]
    statement = _reactivating_insert(rows)
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
    progress = GeminiProgress.for_engine(db_engine)
    with db_engine.connect() as connection:
            statement = (
                select(GoogleTrend.keyword)
                .outerjoin(KeywordClassification, GoogleTrend.keyword == KeywordClassification.keyword)
                .where(GoogleTrend.keyword.is_not(None), pending_condition)
                .distinct()
                .order_by(GoogleTrend.keyword)
            )
            keywords = connection.execute(statement).scalars().all()
    if ai_classifier is not None:
        # Resume: keywords Gemini already answered stay out, even if a later step put them back
        # into the pending set, so an interrupted backlog continues where it stopped.
        done = progress.done("keyword")
        keywords = [keyword for keyword in keywords if str(keyword) not in done]
    if limit > 0:
        keywords = keywords[:limit]
    if not keywords:
        return 0

    saved = 0
    for start in range(0, len(keywords), max(batch_size, 1)):
        batch = [str(keyword) for keyword in keywords[start:start + max(batch_size, 1)]]
        predictions: dict[str, dict[str, object]] = {}
        ai_failed = False
        stop_run = False
        if ai_classifier is not None:
            try:
                predictions = ai_classifier.classify_batch(batch)
            except Exception as error:
                print(f"Keyword AI batch failed ({len(batch)} keywords): {error}")
                ai_failed = True
                stop_run = should_stop_batches(error)
            else:
                # Only keywords Gemini actually answered count as done; omitted ones retry next run.
                progress.mark("keyword", [keyword for keyword in batch if keyword in predictions])
        if ai_failed:
            if stop_run:
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
            if should_stop_batches(error):
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
