"""P0-1: remove duplicate RSS rows written by the non-idempotent collector.

Requires P0-2 (user_version=1) so that ``published_at`` is canonical UTC and the natural keys
compare reliably. Rules:
- ``google_trends`` group key (keyword, published_at): keep MIN(trend_id) (first observation,
  earliest fetched_at); its approx_traffic becomes the value from MAX(trend_id) (latest fetch).
- ``google_trends_news`` is re-pointed to surviving trends, then grouped by (trend_id, news_url):
  keep MIN(news_id). If the survivor has no sentiment but a duplicate does, the duplicate's
  sentiment/sentiment_score/event_type are copied. Conflicting sentiments abort the migration.

Usage::

    python -m trends.migrations.p0_1_dedupe_rss --report
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from trends.migrations._common import MigrationError, Plan, main, row_count, table_exists


DERIVED_TABLES = ("event_analysis", "news_theme_classification")
SENTIMENT_COLUMNS = ("news_sentiment", "sentiment_score", "event_type")


def _has_value(value: object) -> bool:
    return value is not None and str(value).strip() != ""


class DedupeRssMigration:
    name = "p0_1_dedupe_rss"
    from_version = 1
    to_version = 2

    def check_preconditions(self, connection: sqlite3.Connection) -> list[str]:
        failures = []
        for table in ("google_trends", "google_trends_news"):
            if not table_exists(connection, table):
                failures.append(f"missing table {table}")
        if failures:
            return failures
        for table in DERIVED_TABLES:
            if row_count(connection, table):
                failures.append(f"{table} must be empty (has {row_count(connection, table)} rows)")
        non_canonical = connection.execute(
            "SELECT (SELECT COUNT(*) FROM google_trends WHERE length(published_at) <> 19 OR length(fetched_at) <> 19) "
            "+ (SELECT COUNT(*) FROM google_trends_news WHERE length(fetched_at) <> 19)"
        ).fetchone()[0]
        if non_canonical:
            failures.append(f"{non_canonical} timestamp(s) not canonical; run p0_2_timezone first")
        return failures

    def build_plan(self, connection: sqlite3.Connection) -> Plan:
        trends = connection.execute(
            "SELECT trend_id, keyword, published_at, fetched_at, approx_traffic FROM google_trends ORDER BY trend_id"
        ).fetchall()
        trend_groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
        for row in trends:
            _, keyword, published_at, _, _ = row
            if _has_value(keyword) and _has_value(published_at):
                trend_groups[(keyword, published_at)].append(row)

        trend_map: dict[int, int] = {}
        traffic_updates: list[list[object]] = []
        invariant_errors = []
        for (keyword, published_at), rows in trend_groups.items():
            survivor, latest = rows[0], rows[-1]
            if min(row[3] for row in rows) != survivor[3]:
                invariant_errors.append(f"{keyword!r}@{published_at}: MIN(trend_id) is not the first fetch")
            for row in rows[1:]:
                trend_map[row[0]] = survivor[0]
            if len(rows) > 1 and latest[4] != survivor[4]:
                traffic_updates.append([survivor[0], latest[4]])
        if invariant_errors:
            raise MigrationError(f"Survivor invariant violated ({len(invariant_errors)}): {invariant_errors[:5]}")

        news = connection.execute(
            "SELECT news_id, trend_id, news_url, news_sentiment, sentiment_score, event_type "
            "FROM google_trends_news ORDER BY news_id"
        ).fetchall()
        news_groups: dict[tuple[int, str], list[tuple]] = defaultdict(list)
        kept_news: list[tuple] = []
        for row in news:
            news_id, trend_id, news_url = row[0], row[1], row[2]
            target_trend = trend_map.get(trend_id, trend_id)
            remapped = (news_id, target_trend, *row[2:])
            if _has_value(news_url):
                news_groups[(target_trend, news_url)].append(remapped)
            else:
                kept_news.append(remapped)

        delete_news: list[int] = []
        sentiment_merges: list[list[object]] = []
        conflicts = []
        for (trend_id, news_url), rows in news_groups.items():
            survivor = rows[0]
            kept_news.append(survivor)
            delete_news.extend(row[0] for row in rows[1:])
            sentiments = {row[3] for row in rows if _has_value(row[3])}
            if len(sentiments) > 1:
                conflicts.append(f"trend_id={trend_id} url={news_url}: {sorted(sentiments)}")
                continue
            if not _has_value(survivor[3]):
                donor = next((row for row in rows if _has_value(row[3])), None)
                if donor is not None:
                    sentiment_merges.append([survivor[0], donor[3], donor[4], donor[5], donor[0]])
        if conflicts:
            raise MigrationError(f"Conflicting sentiments ({len(conflicts)}): {conflicts[:5]}")

        original_trend = {row[0]: row[1] for row in news}
        news_trend_updates = sorted(
            [row[0], row[1]] for row in kept_news if original_trend[row[0]] != row[1]
        )
        delete_trends = sorted(trend_map)
        merged_ids = {merge[0] for merge in sentiment_merges}
        analyzed_after = sum(1 for row in kept_news if _has_value(row[3]) or row[0] in merged_ids)

        largest = sorted(trend_groups.items(), key=lambda item: len(item[1]), reverse=True)[:5]
        summary = {
            "google_trends_before": len(trends),
            "google_trends_after": len(trends) - len(delete_trends),
            "trend_groups_with_duplicates": sum(1 for rows in trend_groups.values() if len(rows) > 1),
            "traffic_updates": len(traffic_updates),
            "google_trends_news_before": len(news),
            "google_trends_news_after": len(news) - len(delete_news),
            "news_rows_repointed": len(news_trend_updates),
            "sentiment_merges": len(sentiment_merges),
            "sentiment_conflicts": 0,
            "analyzed_news_before": sum(1 for row in news if _has_value(row[3])),
            "analyzed_news_after": analyzed_after,
            "largest_trend_groups": [
                {"keyword": keyword, "published_at": published_at, "rows": len(rows)}
                for (keyword, published_at), rows in largest
            ],
        }
        return Plan(summary=summary, changes={
            "traffic_updates": sorted(traffic_updates),
            "news_trend_updates": news_trend_updates,
            "sentiment_merges": sorted(sentiment_merges),
            "delete_news": sorted(delete_news),
            "delete_trends": delete_trends,
        })

    def apply(self, connection: sqlite3.Connection, plan: Plan) -> None:
        changes = plan.changes
        connection.executemany(
            "UPDATE google_trends SET approx_traffic = ? WHERE trend_id = ?",
            [(traffic, trend_id) for trend_id, traffic in changes["traffic_updates"]],
        )
        connection.executemany(
            "UPDATE google_trends_news SET trend_id = ? WHERE news_id = ?",
            [(trend_id, news_id) for news_id, trend_id in changes["news_trend_updates"]],
        )
        connection.executemany(
            "UPDATE google_trends_news SET news_sentiment = ?, sentiment_score = ?, event_type = ? WHERE news_id = ?",
            [(sentiment, score, event_type, news_id) for news_id, sentiment, score, event_type, _ in changes["sentiment_merges"]],
        )
        connection.executemany(
            "DELETE FROM google_trends_news WHERE news_id = ?", [(news_id,) for news_id in changes["delete_news"]]
        )
        connection.executemany(
            "DELETE FROM google_trends WHERE trend_id = ?", [(trend_id,) for trend_id in changes["delete_trends"]]
        )

    def verify(self, connection: sqlite3.Connection, plan: Plan) -> list[str]:
        summary = plan.summary
        failures = []
        checks = {
            "google_trends rows": (row_count(connection, "google_trends"), summary["google_trends_after"]),
            "google_trends_news rows": (row_count(connection, "google_trends_news"), summary["google_trends_news_after"]),
            "analyzed news rows": (
                connection.execute("SELECT COUNT(*) FROM google_trends_news WHERE news_sentiment IS NOT NULL").fetchone()[0],
                summary["analyzed_news_after"],
            ),
            "duplicate trend groups": (connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM google_trends WHERE keyword IS NOT NULL AND published_at IS NOT NULL "
                "GROUP BY keyword, published_at HAVING COUNT(*) > 1)"
            ).fetchone()[0], 0),
            "duplicate news groups": (connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM google_trends_news WHERE news_url IS NOT NULL AND news_url <> '' "
                "GROUP BY trend_id, news_url HAVING COUNT(*) > 1)"
            ).fetchone()[0], 0),
            "orphan news": (connection.execute(
                "SELECT COUNT(*) FROM google_trends_news n LEFT JOIN google_trends t ON t.trend_id = n.trend_id "
                "WHERE t.trend_id IS NULL"
            ).fetchone()[0], 0),
        }
        for label, (actual, expected) in checks.items():
            if actual != expected:
                failures.append(f"{label}: expected {expected}, found {actual}")
        traffic = dict(connection.execute("SELECT trend_id, approx_traffic FROM google_trends").fetchall())
        stale = sum(1 for trend_id, value in plan.changes["traffic_updates"] if traffic.get(trend_id) != value)
        if stale:
            failures.append(f"{stale} traffic update(s) not applied")
        return failures


MIGRATION = DedupeRssMigration()


if __name__ == "__main__":
    raise SystemExit(main(MIGRATION))
