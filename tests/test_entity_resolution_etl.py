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
from trends.theme_study import theme_basket


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
            "創意電子": ("創意電子", "3443", "科技類"),
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
        self.assertTrue(all(row["link_type"] == "direct" for row in mappings if row["stock_id"]))
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

class KeywordLinkTests(TestCase):
    setUp = EntityResolutionETLTests.setUp
    tearDown = EntityResolutionETLTests.tearDown
    add_keywords = EntityResolutionETLTests.add_keywords

    def classify(self, responses: dict[str, dict[str, object]]) -> list[dict]:
        self.add_keywords(list(responses))
        classify_pending_keywords(
            self.engine, classifier=FakeEntityClassifier(responses), limit=100, resolve_entities=False,
        )
        with self.engine.connect() as connection:
            return connection.execute(select(ThemeMapping.__table__)).mappings().all()

    def answer(self, keyword: str, theme: str, link_type: str) -> dict[str, object]:
        return {"keyword": keyword, "canonical_keyword": keyword, "theme_name": theme,
                "sub_theme": "測試", "link_type": link_type, "confidence_score": 0.9}

    def test_industry_word_links_to_the_curated_basket_only(self) -> None:
        rows = self.classify({"保險": self.answer("保險", "金融類", "industry")})

        self.assertEqual(sorted(row["stock_id"] for row in rows), sorted(theme_basket("金融類")))
        self.assertTrue(all(row["link_type"] == "theme" for row in rows))

    def test_people_and_unlisted_companies_get_no_stock(self) -> None:
        rows = self.classify({
            "謝金河": self.answer("謝金河", "金融類", "none"),
            "Uber": self.answer("Uber", "科技類", "none"),
        })

        self.assertEqual({(row["keyword"], row["stock_id"], row["link_type"]) for row in rows},
                         {("謝金河", None, None), ("Uber", None, None)})

    def test_company_without_a_code_is_not_fanned_out(self) -> None:
        rows = self.classify({"某公司": self.answer("某公司", "科技類", "company")})

        self.assertEqual([(row["stock_id"], row["link_type"]) for row in rows], [(None, None)])

    def test_letter_suffixed_etf_codes_resolve(self) -> None:
        resolver = EntityResolver(self.engine)
        result = resolver._normalize_prediction({
            "keyword": "00631L", "canonical_entity": "元大台灣50正2", "entity_type": "ETF",
            "stock_id": "00631l", "confidence_score": 0.9,
        })

        self.assertEqual(result["stock_id"], "00631L")

    def test_ambiguous_everyday_words_never_resolve_to_a_company(self) -> None:
        self.add_keywords(["創意", "創意電子"])
        gemini = FakeEntityClassifier({
            "創意": {"keyword": "創意", "canonical_entity": "創意", "entity_type": "上市公司",
                     "stock_id": "3443", "industry": "ASIC", "confidence_score": 1.0},
        })

        EntityResolver(self.engine, classifier=gemini).run(limit=10)

        with self.engine.connect() as connection:
            links = connection.execute(select(ThemeMapping.keyword, ThemeMapping.stock_id)).all()
        self.assertEqual(set(links), {("創意電子", "3443")})

    def test_deactivated_fan_out_row_is_reenabled_as_a_direct_link(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(insert(ThemeMapping.__table__).values(
                theme_name="金融類", keyword="玉山金", stock_id="2884", category="金融類", active=0,
            ))
        self.add_keywords(["玉山金"])

        EntityResolver(self.engine, classifier=FakeEntityClassifier({})).run(limit=10)

        with self.engine.connect() as connection:
            row = connection.execute(select(ThemeMapping.active, ThemeMapping.link_type)).one()
        self.assertEqual(tuple(row), (1, "direct"))
