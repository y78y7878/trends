"""Google Trends TW RSS collector with idempotent writes.

- Timestamps are written as canonical naive UTC (``YYYY-MM-DD HH:MM:SS``).
- ``google_trends``: upsert on (keyword, published_at); only approx_traffic is refreshed, so
  fetched_at keeps the first observation. ``RETURNING trend_id`` links news to the existing row.
- ``google_trends_news``: insert-or-ignore on (trend_id, news_url), so analysed sentiment is never
  overwritten.
- Refuses to run until the database is migrated (user_version >= 2 and unique indexes present).

Usage::

    python -m trends.rss_collector --once        # one fetch, for Windows Task Scheduler (run.bat)
    python -m trends.rss_collector --schedule    # long-running, every 10 minutes
    python -m trends.rss_collector --once --db backups\\rehearsal_p0_1.db   # verify against a copy
"""
from __future__ import annotations

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import text
from sqlalchemy.engine import Engine

from trends.database import get_engine, init_db, rss_schema_ready
from trends.timeutil import format_db_datetime, utc_now


RSS_URL = "https://trends.google.com/trending/rss?geo=TW"
NAMESPACES = {"ht": "https://trends.google.com/trending/rss"}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEFAULT_INTERVAL_MINUTES = 10

logger = logging.getLogger(__name__)

UPSERT_TREND = text(
    "INSERT INTO google_trends (keyword, approx_traffic, published_at, fetched_at) "
    "VALUES (:keyword, :approx_traffic, :published_at, :fetched_at) "
    "ON CONFLICT (keyword, published_at) DO UPDATE SET approx_traffic = excluded.approx_traffic "
    "RETURNING trend_id"
)
TREND_EXISTS = text("SELECT 1 FROM google_trends WHERE keyword = :keyword AND published_at = :published_at")
INSERT_NEWS = text(
    "INSERT INTO google_trends_news (trend_id, news_title, news_url, news_source, fetched_at) "
    "VALUES (:trend_id, :news_title, :news_url, :news_source, :fetched_at) "
    "ON CONFLICT (trend_id, news_url) DO NOTHING"
)


class CollectorNotReady(RuntimeError):
    pass


@dataclass
class NewsItem:
    title: str
    url: str
    source: str


@dataclass
class TrendItem:
    keyword: str
    approx_traffic: str
    published_at: str
    news: list[NewsItem] = field(default_factory=list)


@dataclass
class CollectResult:
    items: int = 0
    new_trends: int = 0
    updated_trends: int = 0
    new_news: int = 0
    skipped_items: int = 0
    failed_items: int = 0


def fetch_feed(timeout: float = 10) -> str:
    response = requests.get(RSS_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def _published_utc(raw: str | None) -> str | None:
    """RFC 2822 pubDate (e.g. ``Mon, 5 Oct 2026 20:40:00 -0700``) -> canonical UTC string."""
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return format_db_datetime(parsed)


def parse_feed(xml_text: str) -> tuple[list[TrendItem], int]:
    """Return parsed items and how many were skipped for a missing keyword or unusable pubDate."""
    root = ET.fromstring(xml_text)
    items: list[TrendItem] = []
    skipped = 0
    for element in root.findall(".//item"):
        keyword = (element.findtext("title") or "").strip()
        published_at = _published_utc(element.findtext("pubDate"))
        if not keyword or published_at is None:
            skipped += 1
            logger.warning("Skipping RSS item keyword=%r pubDate=%r", keyword, element.findtext("pubDate"))
            continue
        traffic = (element.findtext("ht:approx_traffic", namespaces=NAMESPACES) or "0+").strip()
        news = []
        for news_element in element.findall("ht:news_item", NAMESPACES):
            url = (news_element.findtext("ht:news_item_url", default="", namespaces=NAMESPACES) or "").strip()
            if not url:
                continue
            news.append(NewsItem(
                title=(news_element.findtext("ht:news_item_title", default="", namespaces=NAMESPACES) or "").strip(),
                url=url,
                source=(news_element.findtext("ht:news_item_source", default="", namespaces=NAMESPACES) or "").strip(),
            ))
        items.append(TrendItem(keyword=keyword, approx_traffic=traffic, published_at=published_at, news=news))
    return items, skipped


def store_items(engine: Engine, items: list[TrendItem], fetched_at: str | None = None) -> CollectResult:
    """Write items idempotently; each item has its own transaction so one bad item cannot sink the batch."""
    if not rss_schema_ready(engine):
        raise CollectorNotReady(
            "資料庫尚未完成 P0 遷移（需要 user_version >= 2 與 RSS 唯一索引）；請依 Runbook 執行 p0_2_timezone 與 p0_1_dedupe_rss"
        )
    observed_at = fetched_at or format_db_datetime(utc_now())
    result = CollectResult(items=len(items))
    for item in items:
        try:
            with engine.begin() as connection:
                key = {"keyword": item.keyword, "published_at": item.published_at}
                existed = connection.execute(TREND_EXISTS, key).first() is not None
                trend_id = connection.execute(UPSERT_TREND, {
                    **key, "approx_traffic": item.approx_traffic, "fetched_at": observed_at,
                }).scalar_one()
                for news in item.news:
                    result.new_news += connection.execute(INSERT_NEWS, {
                        "trend_id": trend_id,
                        "news_title": news.title,
                        "news_url": news.url,
                        "news_source": news.source,
                        "fetched_at": observed_at,
                    }).rowcount or 0
            if existed:
                result.updated_trends += 1
            else:
                result.new_trends += 1
        except Exception:
            result.failed_items += 1
            logger.exception("Failed to store RSS item %r", item.keyword)
    return result


def collect_once(engine: Engine | None = None, xml_text: str | None = None) -> CollectResult:
    db_engine = init_db(engine or get_engine())
    if not rss_schema_ready(db_engine):
        raise CollectorNotReady("資料庫尚未完成 P0 遷移；收集器拒絕寫入")
    items, skipped = parse_feed(xml_text if xml_text is not None else fetch_feed())
    result = store_items(db_engine, items)
    result.skipped_items = skipped
    logger.info(
        "RSS collected: items=%d new_trends=%d updated_trends=%d new_news=%d skipped=%d failed=%d",
        result.items, result.new_trends, result.updated_trends, result.new_news,
        result.skipped_items, result.failed_items,
    )
    return result


def _engine_for(db_path: Path | None) -> Engine:
    """Default: the project data.db. An explicit path must already exist, so a typo cannot
    silently create (and start filling) a brand-new database."""
    if db_path is None:
        return get_engine()
    if not Path(db_path).is_file():
        raise CollectorNotReady(f"資料庫不存在：{db_path}")
    return get_engine(db_path)


def _scheduled_run(engine: Engine) -> None:
    try:
        collect_once(engine)
    except Exception:
        logger.exception("RSS collection run failed")


def run_schedule(interval_minutes: int = DEFAULT_INTERVAL_MINUTES, db_path: Path | None = None) -> None:
    db_engine = init_db(_engine_for(db_path))
    if not rss_schema_ready(db_engine):
        raise CollectorNotReady("資料庫尚未完成 P0 遷移；收集器拒絕啟動")
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _scheduled_run, "interval", args=[db_engine], minutes=interval_minutes, id="google-trends-rss",
        next_run_time=utc_now(), max_instances=1, coalesce=True,
    )
    logger.info("RSS collector scheduled every %d minutes; Ctrl+C to stop", interval_minutes)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("RSS collector stopped")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Google Trends TW RSS collector (idempotent)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="fetch once and exit (exit code 1 on failure)")
    mode.add_argument("--schedule", action="store_true", help="run every --interval-minutes until stopped")
    parser.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument(
        "--db", type=Path, default=None,
        help="existing SQLite file to write to (default: project data.db), e.g. a rehearsal copy",
    )
    args = parser.parse_args(argv)
    try:
        if args.once:
            engine = _engine_for(args.db)
            try:
                result = collect_once(engine)
            finally:
                engine.dispose()
            return 1 if result.failed_items else 0
        run_schedule(args.interval_minutes, args.db)
        return 0
    except CollectorNotReady as error:
        logger.error("%s", error)
        return 2
    except requests.RequestException as error:
        logger.error("RSS fetch failed: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
