from __future__ import annotations

import io
import sqlite3
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from trends.database import RSS_UNIQUE_INDEXES, get_engine, init_db
from trends.rss_collector import CollectorNotReady, collect_once, main, parse_feed, store_items


def feed(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>'
        + "".join(items)
        + "</channel></rss>"
    )


def item(keyword: str, pub_date: str | None, traffic: str = "200+", news: tuple[str, ...] = ("https://n/1",)) -> str:
    news_xml = "".join(
        f"<ht:news_item><ht:news_item_title>{keyword} 新聞 {url}</ht:news_item_title>"
        f"<ht:news_item_url>{url}</ht:news_item_url><ht:news_item_source>來源</ht:news_item_source></ht:news_item>"
        for url in news
    )
    pub = f"<pubDate>{pub_date}</pubDate>" if pub_date is not None else ""
    return f"<item><title>{keyword}</title><ht:approx_traffic>{traffic}</ht:approx_traffic>{pub}{news_xml}</item>"


FEED = feed(
    item("台積電", "Mon, 5 Oct 2026 20:40:00 -0700", news=("https://n/1", "https://n/2")),
    item("AI", "Tue, 6 Oct 2026 03:10:00 +0000"),
)


class ParseFeedTests(unittest.TestCase):
    def test_pubdate_is_converted_to_canonical_utc(self) -> None:
        items, skipped = parse_feed(FEED)

        self.assertEqual(skipped, 0)
        self.assertEqual([(i.keyword, i.published_at) for i in items], [
            ("台積電", "2026-10-06 03:40:00"),
            ("AI", "2026-10-06 03:10:00"),
        ])
        self.assertEqual([news.url for news in items[0].news], ["https://n/1", "https://n/2"])

    def test_items_without_usable_pubdate_are_skipped(self) -> None:
        items, skipped = parse_feed(feed(
            item("缺日期", None),
            item("無時區", "Mon, 5 Oct 2026 20:40:00 -0000"),
            item("壞格式", "yesterday"),
            item("", "Mon, 5 Oct 2026 20:40:00 -0700"),
        ))

        self.assertEqual((items, skipped), ([], 4))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "fresh.db"
        self.engine = init_db(get_engine(self.path))

    def tearDown(self) -> None:
        self.engine.dispose()
        self.directory.cleanup()

    def rows(self, sql: str) -> list[tuple]:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_second_run_is_idempotent_and_refreshes_traffic(self) -> None:
        first = collect_once(self.engine, xml_text=FEED)
        second = collect_once(self.engine, xml_text=feed(
            item("台積電", "Mon, 5 Oct 2026 20:40:00 -0700", traffic="500+", news=("https://n/1", "https://n/2")),
            item("AI", "Tue, 6 Oct 2026 03:10:00 +0000"),
        ))

        self.assertEqual((first.new_trends, first.new_news), (2, 3))
        self.assertEqual((second.new_trends, second.updated_trends, second.new_news), (0, 2, 0))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM google_trends")[0][0], 2)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM google_trends_news")[0][0], 3)
        self.assertEqual(self.rows("SELECT approx_traffic FROM google_trends WHERE keyword = '台積電'")[0][0], "500+")

    def test_first_observation_and_analysed_sentiment_are_kept(self) -> None:
        store_items(self.engine, parse_feed(FEED)[0], fetched_at="2026-10-06 03:50:00")
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE google_trends_news SET news_sentiment = 'Positive' WHERE news_url = 'https://n/1'")
        connection.commit()
        connection.close()

        store_items(self.engine, parse_feed(FEED)[0], fetched_at="2026-10-06 04:00:00")

        self.assertEqual(
            self.rows("SELECT fetched_at FROM google_trends WHERE keyword = '台積電'")[0][0], "2026-10-06 03:50:00"
        )
        self.assertEqual(
            self.rows("SELECT news_sentiment FROM google_trends_news WHERE news_url = 'https://n/1'")[0][0], "Positive"
        )

    def test_new_news_attaches_to_existing_trend_via_returning(self) -> None:
        store_items(self.engine, parse_feed(FEED)[0])
        trend_id = self.rows("SELECT trend_id FROM google_trends WHERE keyword = '台積電'")[0][0]

        result = store_items(self.engine, parse_feed(feed(
            item("台積電", "Mon, 5 Oct 2026 20:40:00 -0700", news=("https://n/3",)),
        ))[0])

        self.assertEqual((result.new_trends, result.new_news), (0, 1))
        self.assertEqual(
            self.rows("SELECT trend_id FROM google_trends_news WHERE news_url = 'https://n/3'")[0][0], trend_id
        )

    def test_retrend_with_new_pubdate_is_a_new_row(self) -> None:
        store_items(self.engine, parse_feed(FEED)[0])

        result = store_items(self.engine, parse_feed(feed(item("台積電", "Wed, 7 Oct 2026 08:00:00 -0700")))[0])

        self.assertEqual(result.new_trends, 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM google_trends WHERE keyword = '台積電'")[0][0], 2)

    def test_timestamps_are_canonical_utc(self) -> None:
        collect_once(self.engine, xml_text=FEED)

        lengths = self.rows(
            "SELECT DISTINCT length(published_at), length(fetched_at) FROM google_trends "
            "UNION SELECT DISTINCT length(fetched_at), 19 FROM google_trends_news"
        )
        self.assertEqual(lengths, [(19, 19)])


class NotReadyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "legacy.db"
        engine = init_db(get_engine(self.path))
        engine.dispose()
        connection = sqlite3.connect(self.path)
        for index in RSS_UNIQUE_INDEXES:
            connection.execute(f"DROP INDEX {index}")
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()
        self.engine = get_engine(self.path)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.directory.cleanup()

    def test_refuses_legacy_database_without_writing(self) -> None:
        with self.assertRaises(CollectorNotReady):
            collect_once(self.engine, xml_text=FEED)
        with self.assertRaises(CollectorNotReady):
            store_items(self.engine, parse_feed(FEED)[0])

        connection = sqlite3.connect(self.path)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM google_trends").fetchone()[0], 0)
        connection.close()

    def test_cli_once_exits_2_and_never_fetches(self) -> None:
        with (
            patch("trends.rss_collector.get_engine", return_value=self.engine),
            patch("trends.rss_collector.fetch_feed") as fetch,
            redirect_stdout(io.StringIO()),
        ):
            code = main(["--once"])

        self.assertEqual(code, 2)
        fetch.assert_not_called()


class DbOptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.copy = self.root / "rehearsal_p0_1.db"
        init_db(get_engine(self.copy)).dispose()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_main(self, *argv: str) -> tuple[int, object]:
        with patch("trends.rss_collector.fetch_feed", return_value=FEED) as fetch, redirect_stdout(io.StringIO()):
            code = main(list(argv))
        return code, fetch

    def test_once_writes_to_the_given_database(self) -> None:
        code, _ = self.run_main("--once", "--db", str(self.copy))

        connection = sqlite3.connect(self.copy)
        count = connection.execute("SELECT COUNT(*) FROM google_trends").fetchone()[0]
        connection.close()
        self.assertEqual((code, count), (0, 2))

    def test_missing_database_is_not_created(self) -> None:
        missing = self.root / "typo.db"

        code, fetch = self.run_main("--once", "--db", str(missing))

        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())
        fetch.assert_not_called()

    def test_legacy_database_is_refused(self) -> None:
        connection = sqlite3.connect(self.copy)
        for index in RSS_UNIQUE_INDEXES:
            connection.execute(f"DROP INDEX {index}")
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()

        code, fetch = self.run_main("--once", "--db", str(self.copy))

        self.assertEqual(code, 2)
        fetch.assert_not_called()

    def test_default_uses_project_database(self) -> None:
        engine = get_engine(self.copy)
        try:
            with patch("trends.rss_collector.get_engine", return_value=engine) as default_engine:
                code, _ = self.run_main("--once")
        finally:
            engine.dispose()

        self.assertEqual(code, 0)
        default_engine.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
