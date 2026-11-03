from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from trends.listed_companies import load_listed, name_matches


class NameMatchTests(unittest.TestCase):
    def test_full_names_match_their_official_abbreviations(self) -> None:
        for name, official in [("中國信託金融控股", "中信金"), ("台灣積體電路製造", "台積電"), ("網路家庭", "網家"),
                               ("力晶積成電子製造股份有限公司", "力積電"), ("臻鼎-KY", "臻鼎-KY"), ("元大台灣50", "元大台灣50")]:
            with self.subTest(name=name):
                self.assertTrue(name_matches(name, official))

    def test_other_companies_do_not_match(self) -> None:
        for name, official in [("冠德", "冠西電"), ("華祺", "唐榮"), ("景凱", "泰合"), ("聯發智音", "聯友金屬-創")]:
            with self.subTest(name=name):
                self.assertFalse(name_matches(name, official))


class LoadListedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.cache = Path(self.directory.name) / "data.listed_companies.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_fresh_cache_is_used_without_network(self) -> None:
        self.cache.write_text(json.dumps({"2330": "台積電"}), encoding="utf-8")
        with patch("trends.listed_companies.fetch_listed") as fetch:
            self.assertEqual(load_listed(self.cache), {"2330": "台積電"})
        fetch.assert_not_called()

    def test_stale_cache_is_kept_when_the_network_fails(self) -> None:
        self.cache.write_text(json.dumps({"2330": "台積電"}), encoding="utf-8")
        old = time.time() - 30 * 24 * 3600
        os.utime(self.cache, (old, old))
        with patch("trends.listed_companies.fetch_listed", side_effect=requests.ConnectionError("offline")):
            self.assertEqual(load_listed(self.cache), {"2330": "台積電"})


if __name__ == "__main__":
    unittest.main()
