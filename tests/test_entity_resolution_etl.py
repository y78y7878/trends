from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from sqlalchemy import select
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
from trends.entity_resolution_etl import (
    EntityResolver,
    build_entity_coverage,
    normalize_entity_keyword,
)
from trends.keyword_classification import classify_pending_keywords


class FakeEntityClassifier:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def classify_batch(self, keywords: list[str]) -> dict[str, dict[str, object]]:
        self.calls.append(keywords)
        return {keyword: self.responses[keyword] for keyword in keywords if keyword in self.responses}


class FakeThemeClassifier:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def classify_batch(self, keywords: list[str]) -> dict[str, dict[str, object]]:
        self.calls.append(keywords)
        return {}


class EntityResolutionETLTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "entity-resolution.db"
        self.engine: Engine = get_engine(database_path)
        init_db(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def add_keywords(self, keywords: list[str]) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(GoogleTrend.__table__),
                [{"keyword": keyword} for keyword in keywords],
            )
            unique_keywords = list(dict.fromkeys(keywords))
            connection.execute(
                insert(KeywordClassification.__table__),
                [{
                    "keyword": keyword,
                    "canonical_keyword": keyword,
                    "theme_name": "其他",
                    "sub_theme": "未分類",
                    "stock_related": 0,
                    "confidence_score": 0.0,
                    "classification_source": "fallback",
                } for keyword in unique_keywords],
            )

    def test_keyword_variants_normalize_to_company_names(self) -> None:
        variants = {
            "緯穎股價": "緯穎",
            "萬海股價": "萬海",
            "2305全友": "全友",
            "妖股全友": "全友",
            "台新金股價": "台新金",
        }
        for value, expected in variants.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_entity_keyword(value), expected.casefold())

    def test_seed_companies_resolve_before_theme_classifier(self) -> None:
        expected = {
            "緯穎股價": ("緯穎", "6669", "科技類"),
            "玉山金": ("玉山金", "2884", "金融類"),
            "台新金股價": ("台新金", "2887", "金融類"),
            "創意": ("創意", "3443", "科技類"),
            "2305全友": ("全友", "2305", "科技類"),
            "妖股全友": ("全友", "2305", "科技類"),
            "台燿": ("台燿", "6274", "科技類"),
            "凱基金": ("凱基金", "2883", "金融類"),
            "合作金庫": ("合庫金", "5880", "金融類"),
            "萬海股價": ("萬海", "2615", "能源與原物料類"),
            "康霈": ("康霈", "6919", "生技醫療類"),
            "四維航": ("四維航", "5608", "能源與原物料類"),
            "元大台灣50": ("元大台灣50", "0050", "金融類"),
        }
        self.add_keywords(list(expected))
        with self.engine.begin() as connection:
            connection.execute(
                insert(ThemeMapping.__table__).values({
                    "theme_name": "其他",
                    "keyword": "萬海股價",
                    "category": "其他",
                    "sub_theme": "未分類",
                    "active": 1,
                })
            )
        theme_classifier = FakeThemeClassifier()

        classified = classify_pending_keywords(self.engine, classifier=theme_classifier, limit=100)

        self.assertEqual(classified, 0)
        self.assertEqual(theme_classifier.calls, [])
        with self.engine.connect() as connection:
            rows = connection.execute(select(KeywordClassification.__table__)).mappings().all()
            entities = connection.execute(select(EntityMaster.__table__)).mappings().all()
            mappings = connection.execute(select(ThemeMapping.__table__)).mappings().all()
        by_keyword = {row["keyword"]: row for row in rows}
        entities_by_name = {entity["entity_name"]: entity for entity in entities}
        mapped_pairs = {(row["keyword"], row["stock_id"], row["theme_name"]) for row in mappings}
        for keyword, (entity_name, stock_id, theme_name) in expected.items():
            with self.subTest(keyword=keyword):
                row = by_keyword[keyword]
                entity = entities_by_name[entity_name]
                self.assertEqual(row["entity_name"], entity_name)
                self.assertEqual(row["entity_type"], "ETF" if stock_id == "0050" else "上市公司")
                self.assertEqual(row["theme_name"], theme_name)
                self.assertEqual(row["classification_source"], "entity_resolution")
                self.assertEqual(entity["stock_id"], stock_id)
                self.assertIn((entity_name, stock_id, theme_name), mapped_pairs)
        self.assertEqual(len(entities), 12)
        self.assertEqual(
            {entity["entity_name"]: entity["stock_id"] for entity in entities}["萬海"],
            "2615",
        )
        self.assertEqual(
            next(row["active"] for row in mappings if row["theme_name"] == "其他"),
            0,
        )

    def test_gemini_entities_are_saved_and_unknowns_appear_in_coverage(self) -> None:
        self.add_keywords(["高雄捷運", "許藍方", "許藍方"])
        classifier = FakeEntityClassifier({
            "高雄捷運": {
                "canonical_entity": "高雄捷運",
                "entity_type": "交通運輸",
                "stock_id": "",
                "industry": "大眾運輸",
                "theme_name": "其他",
                "confidence_score": 0.95,
            },
            "許藍方": {
                "canonical_entity": "許藍方",
                "entity_type": "其他",
                "stock_id": "",
                "industry": "",
                "theme_name": "其他",
                "confidence_score": 0.95,
            },
        })

        saved = EntityResolver(self.engine, classifier=classifier).run(limit=100)
        summary, unresolved = build_entity_coverage(self.engine)

        self.assertEqual(saved, 1)
        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(set(classifier.calls[0]), {"高雄捷運", "許藍方"})
        self.assertEqual(summary["total_keywords"], 2)
        self.assertEqual(summary["identified_entities"], 1)
        self.assertEqual(summary["unidentified_entities"], 1)
        self.assertEqual(summary["recognition_rate"], 0.5)
        self.assertEqual(unresolved.iloc[0]["keyword"], "許藍方")
        self.assertEqual(unresolved.iloc[0]["trend_count"], 2)


if __name__ == "__main__":
    import unittest

    unittest.main()