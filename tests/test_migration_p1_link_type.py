from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trends.database import get_engine, init_db
from trends.migrations._common import report, run_migration
from trends.migrations.p1_link_type import MIGRATION


class LinkTypeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "data.db"
        init_db(get_engine(self.path)).dispose()
        connection = sqlite3.connect(self.path)
        rows = [
            ("金融類", "保險", "2881"),       # fan-out (not in theme_mapping.csv)
            ("金融類", "ETF", "0050"),        # curated (theme_mapping.csv)
            ("金融類", "玉山金", "2884"),     # company in entity_master
            ("科技類", "Uber", "2330"),       # fan-out
            ("科技類", "創意", "3443"),       # retired seed alias
        ]
        connection.executemany(
            "INSERT INTO theme_mapping (theme_name, keyword, stock_id, category, active) VALUES (?, ?, ?, ?, 1)",
            [(theme, keyword, stock, theme) for theme, keyword, stock in rows],
        )
        connection.executemany(
            "INSERT INTO entity_master (entity_name, entity_type, stock_id, confidence_score) VALUES (?, '上市公司', ?, 1)",
            [("玉山金", "2884"), ("創意", "3443")],
        )
        connection.executemany(
            "INSERT INTO keyword_classification (keyword, canonical_keyword, theme_name, sub_theme, stock_related, "
            "confidence_score, classification_source) VALUES (?, ?, ?, '測試', 1, 0.9, 'gemini')",
            [("Uber叫車", "Uber", "科技類"), ("ETF", "ETF", "金融類")],
        )
        connection.commit()
        connection.close()
        self.progress = self.path.with_name("data.gemini_progress.json")
        self.progress.write_text(json.dumps({"keyword": {"Uber叫車": "x", "ETF": "x"}}), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def rows(self, sql: str) -> list[tuple]:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_report_is_read_only(self) -> None:
        result = report(self.path, MIGRATION)

        self.assertEqual(result["summary"]["deactivated_rows"], 3)
        self.assertEqual(self.rows("SELECT count(*) FROM theme_mapping WHERE active = 1")[0][0], 5)

    def test_apply_types_links_deactivates_fan_out_and_requeues(self) -> None:
        run_migration(self.path, MIGRATION)

        self.assertEqual(
            set(self.rows("SELECT keyword, link_type FROM theme_mapping WHERE active = 1")),
            {("ETF", "theme"), ("玉山金", "direct")},
        )
        self.assertEqual(
            self.rows("SELECT theme_name, classification_source FROM keyword_classification WHERE keyword = 'Uber叫車'"),
            [("其他", "fallback")],
        )
        self.assertEqual(
            self.rows("SELECT classification_source FROM keyword_classification WHERE keyword = 'ETF'"), [("gemini",)]
        )
        self.assertEqual(json.loads(self.progress.read_text(encoding="utf-8"))["keyword"], {"ETF": "x"})
        self.assertEqual(self.rows("PRAGMA user_version")[0][0], 3)


if __name__ == "__main__":
    unittest.main()
