from __future__ import annotations

import argparse
import csv
import json
import os
import re
from pathlib import Path
from typing import Protocol

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from trends.database import (
    EntityMaster,
    GoogleTrend,
    KeywordClassification,
    ThemeMapping,
    get_engine,
    init_db,
)
from trends.keyword_classification import ALLOWED_THEMES, _generate_json
from trends.keyword_mapping import load_keyword_mapping
from trends.theme_study import normalize_theme_name


DEFAULT_ENTITY_SEED_PATH = Path(__file__).resolve().parents[2] / "entity_seed_mapping.csv"
ENTITY_TYPES = (
    "上市公司", "ETF", "金融機構", "產業", "品牌", "名人", "運動賽事", "娛樂影視",
    "政府機構", "交通運輸", "公共議題", "新聞媒體", "其他",
)
RESOLVED_ENTITY_TYPES = set(ENTITY_TYPES) - {"其他"}
MIN_CONFIDENCE = 0.6
BATCH_SIZE = 20


def normalize_entity_keyword(keyword: object) -> str:
    value = str(keyword or "").strip()
    value = re.sub(r"^\d{4,6}\s*", "", value)
    for prefix in ("妖股", "飆股"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    for suffix in ("股價分析", "股票走勢", "股價", "股票", "股市"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _theme_for_industry(industry: str) -> str:
    value = industry.casefold().replace(" ", "")
    rules = (
        ("金融類", ("金融", "銀行", "金控", "證券", "保險", "etf")),
        ("生技醫療類", ("生技", "醫療", "製藥", "藥品", "醫材")),
        ("科技類", ("科技", "電子", "半導體", "asic", "ai", "伺服器", "網通", "記憶體", "pcb", "電腦", "銅箔基板")),
        ("能源與原物料類", ("航運", "能源", "原物料", "石化", "鋼鐵", "銅價", "煤", "原油")),
        ("食品與消費類", ("食品", "餐飲", "消費", "零售")),
        ("電商與零售類", ("電商", "物流", "網購")),
        ("綠能環保類", ("綠能", "環保", "太陽能", "風電", "電動車")),
        ("遊戲娛樂類", ("遊戲", "娛樂", "影視", "電競")),
    )
    for theme_name, terms in rules:
        if any(term in value for term in terms):
            return normalize_theme_name(theme_name)
    return "其他"


class EntityBatchClassifier(Protocol):
    def classify_batch(self, keywords: list[str]) -> dict[str, dict[str, object]]: ...


class GeminiEntityClassifier:
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
請分析以下 Google Trends 關鍵字。先判斷實體類型，再整理 canonical_entity、股票代號及產業。
entity_type 只能是：{', '.join(ENTITY_TYPES)}。
theme_name 只能是：{', '.join(ALLOWED_THEMES)}。
只有確定是台灣上市櫃公司時才填 stock_id；無法確認時留空，不得臆測。
confidence_score 範圍為 0 到 1。無法辨識時 entity_type 填「其他」、confidence_score 填 0。
每個關鍵字一筆，輸出 JSON：{{"items":[{{"keyword":"","canonical_entity":"","entity_type":"","stock_id":"","industry":"","theme_name":"","confidence_score":0.0}}]}}
關鍵字：{json.dumps(keywords, ensure_ascii=False)}
"""
        payload = _generate_json(self._client, self._model, prompt)
        return {
            str(item.get("keyword", "")): item
            for item in payload.get("items", [])
            if isinstance(item, dict) and item.get("keyword")
        }


def _load_entity_seeds(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        return [
            {key: value.strip() for key, value in row.items() if key and value}
            for row in csv.DictReader(file)
            if row.get("entity_name", "").strip()
        ]


def _confidence(value: object) -> float:
    numeric = pd.to_numeric(value, errors="coerce")
    return float(min(max(numeric if pd.notna(numeric) else 0.0, 0.0), 1.0))


class EntityResolver:
    def __init__(
        self,
        engine: Engine,
        classifier: EntityBatchClassifier | None = None,
        seed_path: str | Path = DEFAULT_ENTITY_SEED_PATH,
    ) -> None:
        self.engine = init_db(engine)
        self.classifier = classifier
        self.seeds = _load_entity_seeds(seed_path)
        self.keyword_mapping = load_keyword_mapping()
        self.seed_aliases: dict[str, dict[str, str]] = {}
        for seed in self.seeds:
            for alias in (seed["entity_name"], *seed.get("aliases", "").split("|")):
                normalized = normalize_entity_keyword(alias)
                if normalized:
                    self.seed_aliases[normalized] = seed

        company_rows = self.keyword_mapping[self.keyword_mapping["type"] == "company"]
        self.company_aliases = {
            normalize_entity_keyword(row.keyword): row
            for row in company_rows.itertuples(index=False)
            if normalize_entity_keyword(row.keyword)
        }

    def _get_classifier(self) -> EntityBatchClassifier | None:
        if self.classifier is not None:
            return self.classifier
        try:
            self.classifier = GeminiEntityClassifier()
        except RuntimeError:
            return None
        return self.classifier

    def _resolve_known_company(self, keyword: str) -> dict[str, object] | None:
        normalized = normalize_entity_keyword(keyword)
        seed = self.seed_aliases.get(normalized)
        if seed is not None:
            industry = seed.get("industry", "")
            theme_name = _theme_for_industry(industry)
            if theme_name == "其他":
                seed_theme = normalize_theme_name(seed.get("theme_name", "其他"))
                if seed_theme in ALLOWED_THEMES:
                    theme_name = seed_theme
            return {
                "canonical_entity": seed["entity_name"],
                "entity_type": seed.get("entity_type", "上市公司"),
                "stock_id": seed.get("stock_id", ""),
                "industry": industry,
                "theme_name": theme_name,
                "confidence_score": _confidence(seed.get("confidence_score", "1")),
                "data_source": "entity_seed_mapping",
            }

        alias = self.company_aliases.get(normalized)
        if alias is None:
            return None
        stock_id = str(alias.stock_id)
        rows = self.keyword_mapping[
            (self.keyword_mapping["type"] == "company")
            & (self.keyword_mapping["stock_id"] == stock_id)
        ]
        chinese_names = [
            name for name in rows["keyword"].drop_duplicates().tolist()
            if any(ord(character) > 127 for character in name)
            and not any(term in name for term in ("股價", "妖股", "掃描器"))
        ]
        canonical = min(chinese_names or rows["keyword"].drop_duplicates().tolist(), key=len)
        industry_rows = self.keyword_mapping[
            (self.keyword_mapping["type"] == "industry")
            & (self.keyword_mapping["stock_id"] == stock_id)
        ]
        is_etf = stock_id.startswith("00")
        industry = (
            "ETF" if is_etf else
            min(industry_rows["keyword"].drop_duplicates().tolist(), key=len) if not industry_rows.empty else ""
        )
        return {
            "canonical_entity": canonical,
            "entity_type": "ETF" if is_etf else "上市公司",
            "stock_id": stock_id,
            "industry": industry,
            "theme_name": "金融類" if is_etf else _theme_for_industry(industry),
            "confidence_score": 1.0,
            "data_source": "keyword_theme_mapping",
        }

    def _resolve_industry(self, keyword: str) -> dict[str, object] | None:
        normalized = normalize_entity_keyword(keyword)
        rows = self.keyword_mapping[self.keyword_mapping["type"] == "industry"]
        for industry in rows["keyword"].drop_duplicates().tolist():
            if normalize_entity_keyword(industry) == normalized:
                return {
                    "canonical_entity": industry,
                    "entity_type": "產業",
                    "stock_id": "",
                    "industry": industry,
                    "theme_name": _theme_for_industry(industry),
                    "confidence_score": 0.95,
                    "data_source": "keyword_theme_mapping",
                }
        return None

    def _normalize_prediction(self, prediction: dict[str, object]) -> dict[str, object] | None:
        entity_type = str(prediction.get("entity_type", "其他")).strip()
        canonical = str(prediction.get("canonical_entity", "")).strip()
        if entity_type not in RESOLVED_ENTITY_TYPES or not canonical:
            return None
        confidence = _confidence(prediction.get("confidence_score", 0))
        if confidence < MIN_CONFIDENCE:
            return None
        stock_id = str(prediction.get("stock_id", "")).strip().upper()
        industry = str(prediction.get("industry", "")).strip()
        if entity_type in {"上市公司", "ETF"}:
            if not re.fullmatch(r"\d{4,6}", stock_id):
                return None
            theme_name = "金融類" if entity_type == "ETF" else _theme_for_industry(industry)
        else:
            stock_id = ""
            proposed_theme = normalize_theme_name(prediction.get("theme_name", "其他"))
            theme_name = proposed_theme if proposed_theme in ALLOWED_THEMES else "其他"
        return {
            "canonical_entity": canonical[:255],
            "entity_type": entity_type,
            "stock_id": stock_id,
            "industry": industry[:100],
            "theme_name": theme_name,
            "confidence_score": confidence,
            "data_source": "gemini_entity_resolution",
        }

    def _save_batch(self, results: list[tuple[str, dict[str, object]]]) -> int:
        if not results:
            return 0
        entity_rows = []
        classification_rows = []
        theme_rows = []
        for keyword, result in results:
            entity_rows.append({
                "entity_name": result["canonical_entity"],
                "entity_type": result["entity_type"],
                "stock_id": result["stock_id"] or None,
                "industry": result["industry"] or None,
                "theme_name": result["theme_name"] or None,
                "confidence_score": result["confidence_score"],
                "data_source": result["data_source"],
            })
            is_company = result["entity_type"] in {"上市公司", "ETF"}
            if is_company and result["theme_name"] != "其他":
                theme_rows.append({
                    "theme_name": result["theme_name"],
                    "keyword": result["canonical_entity"],
                    "stock_id": result["stock_id"],
                    "category": result["theme_name"],
                    "sub_theme": result["industry"] or "公司與股票",
                    "active": 1,
                })
            classification_rows.append({
                "keyword": keyword,
                "canonical_keyword": result["canonical_entity"],
                "entity_name": result["canonical_entity"],
                "entity_type": result["entity_type"],
                "theme_name": result["theme_name"] if is_company else "其他",
                "sub_theme": result["industry"] or "未分類",
                "stock_related": int(is_company),
                "confidence_score": result["confidence_score"],
                "classification_source": "entity_resolution" if is_company else "fallback",
                "need_review": int(result["confidence_score"] < 0.85),
            })

        entity_statement = insert(EntityMaster.__table__).values(entity_rows)
        entity_statement = entity_statement.on_conflict_do_update(
            index_elements=["entity_name"],
            set_={column: getattr(entity_statement.excluded, column) for column in (
                "entity_type", "stock_id", "industry", "theme_name", "confidence_score", "data_source"
            )},
        )
        theme_statement = (
            insert(ThemeMapping.__table__).values(theme_rows).on_conflict_do_nothing()
            if theme_rows else None
        )
        classification_statement = insert(KeywordClassification.__table__).values(classification_rows)
        classification_statement = classification_statement.on_conflict_do_update(
            index_elements=["keyword"],
            set_={column: getattr(classification_statement.excluded, column) for column in (
                "canonical_keyword", "entity_name", "entity_type", "theme_name", "sub_theme",
                "stock_related", "confidence_score", "classification_source", "need_review",
            )},
            where=or_(
                KeywordClassification.theme_name == "其他",
                KeywordClassification.theme_name.is_(None),
                KeywordClassification.classification_source == "fallback",
            ),
        )
        with self.engine.begin() as connection:
            company_keywords = [
                keyword for keyword, result in results
                if result["entity_type"] in {"上市公司", "ETF"}
            ]
            if company_keywords:
                previous_names = connection.execute(
                    select(KeywordClassification.canonical_keyword).where(
                        KeywordClassification.keyword.in_(company_keywords)
                    )
                ).scalars().all()
                obsolete_keywords = set(company_keywords) | set(previous_names)
                connection.execute(
                    update(ThemeMapping)
                    .where(
                        ThemeMapping.theme_name == "其他",
                        ThemeMapping.keyword.in_(obsolete_keywords),
                    )
                    .values(active=0)
                )
            connection.execute(entity_statement)
            if theme_statement is not None:
                connection.execute(theme_statement)
            return connection.execute(classification_statement).rowcount or 0

    def run(self, limit: int = 500, batch_size: int = BATCH_SIZE) -> int:
        with self.engine.connect() as connection:
            statement = (
                select(GoogleTrend.keyword)
                .outerjoin(KeywordClassification, KeywordClassification.keyword == GoogleTrend.keyword)
                .where(
                    GoogleTrend.keyword.is_not(None),
                    func.trim(GoogleTrend.keyword) != "",
                    or_(
                        KeywordClassification.keyword.is_(None),
                        KeywordClassification.theme_name == "其他",
                        KeywordClassification.theme_name.is_(None),
                    ),
                )
                .distinct()
                .order_by(GoogleTrend.keyword)
            )
            if limit > 0:
                statement = statement.limit(limit)
            keywords = [str(keyword) for keyword in connection.execute(statement).scalars()]

        if not keywords:
            return 0
        saved = 0
        size = max(batch_size, 1)
        for start in range(0, len(keywords), size):
            batch = keywords[start:start + size]
            results: list[tuple[str, dict[str, object]]] = []
            unresolved = []
            for keyword in batch:
                known = self._resolve_known_company(keyword) or self._resolve_industry(keyword)
                if known is None:
                    unresolved.append(keyword)
                else:
                    results.append((keyword, known))

            classifier = self._get_classifier() if unresolved else None
            if unresolved and classifier is not None:
                try:
                    predictions = classifier.classify_batch(unresolved)
                except Exception as error:
                    print(f"Entity AI batch failed ({len(unresolved)} keywords): {error}")
                    if "quota exceeded" in str(error).casefold() and ("perday" in str(error).casefold() or "per day" in str(error).casefold()):
                        saved += self._save_batch(results)
                        break
                    predictions = {}
                for keyword in unresolved:
                    result = self._normalize_prediction(predictions.get(keyword, {}))
                    if result is not None:
                        results.append((keyword, result))
            saved += self._save_batch(results)
        return saved


def run_entity_resolution_etl(
    engine: Engine | None = None,
    classifier: EntityBatchClassifier | None = None,
    limit: int = 500,
    batch_size: int = BATCH_SIZE,
) -> int:
    return EntityResolver(engine or get_engine(), classifier=classifier).run(limit=limit, batch_size=batch_size)


def build_entity_coverage(engine: Engine, limit: int = 100) -> tuple[dict[str, int | float], pd.DataFrame]:
    db_engine = init_db(engine)
    with db_engine.connect() as connection:
        total = connection.scalar(
            select(func.count(func.distinct(GoogleTrend.keyword))).where(
                GoogleTrend.keyword.is_not(None), func.trim(GoogleTrend.keyword) != ""
            )
        ) or 0
        identified = connection.scalar(
            select(func.count(func.distinct(KeywordClassification.keyword)))
            .join(GoogleTrend, GoogleTrend.keyword == KeywordClassification.keyword)
            .where(
                KeywordClassification.entity_name.is_not(None),
                KeywordClassification.entity_type.is_not(None),
                KeywordClassification.entity_type != "其他",
            )
        ) or 0
        unresolved_statement = (
            select(
                GoogleTrend.keyword.label("keyword"),
                func.count(GoogleTrend.id).label("trend_count"),
            )
            .outerjoin(KeywordClassification, KeywordClassification.keyword == GoogleTrend.keyword)
            .where(
                GoogleTrend.keyword.is_not(None),
                func.trim(GoogleTrend.keyword) != "",
                or_(
                    KeywordClassification.entity_name.is_(None),
                    KeywordClassification.entity_type.is_(None),
                    KeywordClassification.entity_type == "其他",
                ),
            )
            .group_by(GoogleTrend.keyword)
            .order_by(func.count(GoogleTrend.id).desc(), GoogleTrend.keyword)
        )
        if limit > 0:
            unresolved_statement = unresolved_statement.limit(limit)
        unresolved = pd.read_sql(unresolved_statement, connection)
    summary: dict[str, int | float] = {
        "total_keywords": int(total),
        "identified_entities": int(identified),
        "unidentified_entities": max(int(total) - int(identified), 0),
        "recognition_rate": float(identified / total) if total else 0.0,
    }
    return summary, unresolved


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="執行 Google Trends Entity Resolution ETL")
    parser.add_argument("--limit", type=int, default=500, help="每次最多處理的不同關鍵字；0 代表不限")
    args = parser.parse_args()
    print(f"Resolved entities: {run_entity_resolution_etl(limit=args.limit)}")