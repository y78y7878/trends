from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from trends.database import (
    EntityMaster,
    GoogleTrend,
    GoogleTrendNews,
    GoogleTrendsHistory,
    KeywordClassification,
    Stock,
    ThemeDailyStats,
    ThemeMapping,
    get_engine,
    init_db,
)
from trends.health_report import (
    FAIL,
    OK,
    UNKNOWN,
    WARN,
    build_report,
    expected_trading_day,
    main,
    parse_blocks,
    parse_etl_result,
    parse_runs,
    render_markdown,
)


NOW = datetime(2026, 10, 15, 8, 0)  # Thursday 08:00 Asia/Taipei

HEALTHY_ETL_LOG = """\
[2026/10/14 20:00:01] etl start
[INFO] keyword=A theme=科技類 success rows=89
Completed
Success: 1
Failed: 0
Skipped: 0
Not attempted: 0
Rows written: 89
Gemini Stats
Requests: 12
Processed: 220 (keyword 200, news 20)
Skipped: 0 (keywords 0, news 0)
429 Count: 1
503 Count: 0
Wait Time: 95 seconds
Stopped: no
Theme ETL completed: {'resolved_entities': 3, 'classified_keywords': 200, 'history_rows': 89, 'classified_news': 20, 'daily_stat_themes': 9}
[2026/10/14 20:09:44] etl exit 0
"""

HEALTHY_STOCK_LOG = """\
[2026/10/14 18:30:02] stock_collector start
$3083.TW: possibly delisted; no timezone found
已寫入 1,050 筆行情
[2026/10/14 18:31:10] stock_collector exit 0
"""


def rss_log(*stamps: str, failed: int = 0) -> str:
    return "".join(
        f"{stamp},123 INFO RSS collected: items=10 new_trends=1 updated_trends=9 new_news=3 skipped=0 failed={failed}\n"
        for stamp in stamps
    )


def component(report, name: str):
    return next(item for item in report.components if item.name == name)


class LogParsingTests(TestCase):
    def test_runs_are_split_by_markers_and_tolerate_locale_weekday(self) -> None:
        lines = [
            "[2026/10/15 ��  0:49:12.45] etl start",  # old %date% header with a cp950 weekday
            "Theme ETL completed: {'history_rows': 5}",
            "[2026/10/15  1:06:59] etl start",
            "partial output",
        ]

        runs = parse_runs(lines, "etl")

        self.assertEqual([run.started_at for run in runs], [datetime(2026, 10, 15, 0, 49, 12), datetime(2026, 10, 15, 1, 6, 59)])
        self.assertEqual([run.exit_code for run in runs], [None, None])
        self.assertEqual(parse_etl_result(runs[0].lines), {"history_rows": 5})

    def test_exit_code_is_attached_to_its_run(self) -> None:
        runs = parse_runs(HEALTHY_ETL_LOG.splitlines(), "etl")

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].exit_code, 0)

    def test_summary_blocks_are_separated(self) -> None:
        blocks = parse_blocks(HEALTHY_ETL_LOG.splitlines())

        self.assertEqual(blocks["history"]["status"], "Completed")
        self.assertEqual(blocks["history"]["Skipped"], "0")
        self.assertEqual(blocks["gemini"]["Processed"], "220 (keyword 200, news 20)")
        self.assertEqual(blocks["gemini"]["Stopped"], "no")


class TradingDayTests(TestCase):
    def test_before_close_collection_expects_previous_weekday(self) -> None:
        self.assertEqual(expected_trading_day(datetime(2026, 10, 15, 8, 0)), date(2026, 10, 14))
        self.assertEqual(expected_trading_day(datetime(2026, 10, 15, 19, 0)), date(2026, 10, 15))

    def test_weekend_expects_friday(self) -> None:
        self.assertEqual(expected_trading_day(datetime(2026, 10, 18, 12, 0)), date(2026, 10, 16))
        self.assertEqual(expected_trading_day(datetime(2026, 10, 19, 9, 0)), date(2026, 10, 16))


class HealthReportTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.db_path = root / "data.db"
        self.log_dir = root / "logs"
        self.log_dir.mkdir()
        self.engine = init_db(get_engine(self.db_path))
        self.write_log("rss_collector.log", rss_log("2026-10-15 07:40:00", "2026-10-15 07:50:00"))
        self.write_log("stock_collector.log", HEALTHY_STOCK_LOG)
        self.write_log("etl.log", HEALTHY_ETL_LOG)
        self.seed_healthy_database()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary_directory.cleanup()

    def write_log(self, name: str, text: str) -> None:
        (self.log_dir / name).write_text(text, encoding="utf-8")

    def insert(self, table, **values) -> None:
        with self.engine.begin() as connection:
            connection.execute(table.__table__.insert().values(**values))

    def seed_healthy_database(self) -> None:
        fetched = datetime(2026, 10, 14, 23, 50)  # 07:50 Taipei
        with self.engine.begin() as connection:
            trend_id = connection.execute(GoogleTrend.__table__.insert().values(
                keyword="A", approx_traffic="200+", published_at=fetched, fetched_at=fetched,
            )).inserted_primary_key[0]
            connection.execute(GoogleTrendNews.__table__.insert().values(
                trend_id=trend_id, news_title="A 新聞", news_url="https://example.com/a", news_source="測試",
                fetched_at=fetched, news_sentiment="Positive", sentiment_score=0.8, event_type="其他",
            ))
        self.insert(KeywordClassification, keyword="A", canonical_keyword="A", theme_name="科技類", sub_theme="半導體",
                    stock_related=1, confidence_score=0.9, classification_source="gemini", created_at=fetched)
        self.insert(ThemeMapping, theme_name="科技類", keyword="A", stock_id="2330", category="company", active=1)
        self.insert(GoogleTrendsHistory, keyword="A", theme_name="科技類", trend_date=date(2026, 10, 14), trend_score=50)
        self.insert(Stock, date=date(2026, 10, 14), stock_id="2330", open=1, high=1, low=1, close=1, adj_close=1, volume=1)
        self.insert(ThemeDailyStats, stat_date=date(2026, 10, 14), theme_name="科技類", keyword_count=1, news_count=1,
                    event_count=1, stock_count=1)
        self.insert(EntityMaster, entity_name="A", entity_type="上市公司", created_at=fetched)

    def report(self):
        return build_report(self.db_path, self.log_dir, now=NOW)

    def test_healthy_system_scores_100(self) -> None:
        report = self.report()

        self.assertEqual({item.name: item.status for item in report.components}, {
            "RSS": OK, "Stocks": OK, "Google Trends": OK, "Gemini": OK, "ETL": OK,
        })
        self.assertEqual(report.score, 100)
        self.assertEqual(report.grade, "🟢 健康")
        self.assertFalse(report.has_failure)

    def test_today_counts_and_coverage(self) -> None:
        report = self.report()

        self.assertEqual(report.today["RSS 熱搜"], 1)
        self.assertEqual(report.today["RSS 新聞"], 1)
        self.assertEqual(report.today["關鍵字分類"], 1)
        self.assertEqual(report.coverage["分類覆蓋率"], (1, 1))
        self.assertEqual(report.coverage["情緒覆蓋率"], (1, 1))
        self.assertEqual(report.coverage["熱度覆蓋率（近 3 天有熱度的主題關鍵字）"], (1, 1))
        self.assertEqual(report.coverage["熱度覆蓋率（其中有對應股票，事件研究使用）"], (1, 1))

    def test_stock_heat_coverage_excludes_keywords_without_stocks(self) -> None:
        self.insert(ThemeMapping, theme_name="其他", keyword="路人", stock_id=None, category="其他", active=1)
        self.insert(GoogleTrendsHistory, keyword="路人", theme_name="其他", trend_date=date(2026, 10, 14), trend_score=9)
        self.insert(ThemeMapping, theme_name="金融類", keyword="玉山金", stock_id="2884", category="company", active=1)

        report = self.report()

        self.assertEqual(report.coverage["熱度覆蓋率（近 3 天有熱度的主題關鍵字）"], (2, 3))
        self.assertEqual(report.coverage["熱度覆蓋率（其中有對應股票，事件研究使用）"], (1, 2))
        self.assertEqual(component(report, "Stocks").last_success, "2026-10-14 18:30")

    def test_gemini_processing_is_summed_for_today(self) -> None:
        self.write_log("etl.log", HEALTHY_ETL_LOG.replace("2026/10/14", "2026/10/15"))

        report = self.report()

        self.assertEqual(report.gemini_today["runs"], 1)
        self.assertEqual((report.gemini_today["requests"], report.gemini_today["processed"]), (12, 220))
        self.assertEqual((report.gemini_today["429"], report.gemini_today["wait"]), (1, 95))

    def test_stale_rss_fails(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(GoogleTrend.__table__.update().values(fetched_at=datetime(2026, 10, 14, 20, 0)))

        self.assertEqual(component(self.report(), "RSS").status, FAIL)

    def test_rss_errors_in_last_day_warn_and_are_listed(self) -> None:
        self.write_log("rss_collector.log", rss_log("2026-10-15 07:50:00")
                       + "2026-10-15 07:55:00,001 ERROR RSS fetch failed: timeout\n")

        report = self.report()

        self.assertEqual(component(report, "RSS").status, WARN)
        self.assertTrue(any("RSS fetch failed" in line for line in report.recent_errors))

    def test_stocks_one_weekday_behind_warns_and_older_fails(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(Stock.__table__.update().values(date=date(2026, 10, 13)))
        self.assertEqual(component(self.report(), "Stocks").status, WARN)

        with self.engine.begin() as connection:
            connection.execute(Stock.__table__.update().values(date=date(2026, 10, 9)))
        self.assertEqual(component(self.report(), "Stocks").status, FAIL)

    def test_google_trends_rate_limit_warns_and_low_coverage_fails(self) -> None:
        self.write_log("etl.log", HEALTHY_ETL_LOG.replace("Completed", "Stopped: Google Trends rate limited (HTTP 429)"))
        self.assertEqual(component(self.report(), "Google Trends").status, WARN)

        for index in range(9):
            self.insert(ThemeMapping, theme_name="科技類", keyword=f"stale{index}", stock_id="2330", category="company", active=1)
        self.assertEqual(component(self.report(), "Google Trends").status, FAIL)

    def test_gemini_stopped_without_progress_fails(self) -> None:
        self.write_log("etl.log", HEALTHY_ETL_LOG.replace("Processed: 220 (keyword 200, news 20)", "Processed: 0")
                       .replace("Stopped: no", "Stopped: HTTP 503 after retries"))

        report = self.report()

        self.assertEqual(component(report, "Gemini").status, FAIL)
        self.assertIn("HTTP 503 after retries", " ".join(component(report, "Gemini").details))

    def test_gemini_working_but_low_sentiment_coverage_warns(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(GoogleTrendNews.__table__.update().values(news_sentiment=None))

        self.assertEqual(component(self.report(), "Gemini").status, WARN)

    def test_gemini_without_stats_is_unknown(self) -> None:
        self.write_log("etl.log", "[2026/10/14 20:00:01] etl start\nTheme ETL completed: {}\n[2026/10/14 20:01:00] etl exit 0\n")

        self.assertEqual(component(self.report(), "Gemini").status, UNKNOWN)

    def test_etl_failures(self) -> None:
        self.write_log("etl.log", HEALTHY_ETL_LOG.replace("etl exit 0", "etl exit 1"))
        self.assertEqual(component(self.report(), "ETL").status, FAIL)

        self.write_log("etl.log", "[2026/10/15 07:00:00] etl start\nstill running\n")
        self.assertEqual(component(self.report(), "ETL").status, WARN)

        self.write_log("etl.log", HEALTHY_ETL_LOG.replace("2026/10/14", "2026/10/13"))
        self.assertEqual(component(self.report(), "ETL").status, FAIL)

    def test_trends_rate_limit_exit_code_is_not_an_etl_failure(self) -> None:
        self.write_log("etl.log", HEALTHY_ETL_LOG.replace("etl exit 0", "etl exit 3"))

        self.assertEqual(component(self.report(), "ETL").status, OK)

    def test_report_never_writes_to_the_database(self) -> None:
        self.engine.dispose()
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        self.report()

        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)

    def test_markdown_contains_every_section(self) -> None:
        text = render_markdown(self.report())

        for heading in ("系統健康度：100 / 100", "| RSS | ✅ OK", "今日新增資料量", "Gemini 今日處理量", "覆蓋率", "最近錯誤"):
            self.assertIn(heading, text)

    def test_cli_writes_report_and_returns_failure_code(self) -> None:
        # main() uses the real clock; RSS data from 2020 is stale on any date.
        with self.engine.begin() as connection:
            connection.execute(GoogleTrend.__table__.update().values(fetched_at=datetime(2020, 1, 1)))
        self.engine.dispose()

        code = main(["--db", str(self.db_path), "--logs", str(self.log_dir)])

        self.assertEqual(code, 1)
        self.assertIn("Project Health Report", (self.log_dir / "health_report.md").read_text(encoding="utf-8"))
