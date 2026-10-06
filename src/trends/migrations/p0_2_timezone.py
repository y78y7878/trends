"""P0-2: convert RSS timestamps to canonical naive UTC ``YYYY-MM-DD HH:MM:SS``.

Stored conventions before this migration (verified against the RSS feed before running):
- ``published_at``: RSS pubDate wall-clock time in America/Los_Angeles, offset dropped.
- ``fetched_at`` with 26 characters (``.000000`` suffix, imported MariaDB era): Asia/Taipei.
- ``fetched_at`` with 19 characters (SQLite ``CURRENT_TIMESTAMP`` era): already UTC.

The string length is the only era marker, so era detection, time-zone conversion and format
normalisation happen in this single step. Usage::

    python -m trends.migrations.p0_2_timezone --report
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

from trends.database import RSS_UNIQUE_INDEXES
from trends.migrations._common import (
    MigrationError,
    Plan,
    main,
    row_count,
    table_exists,
)
from trends.timeutil import PACIFIC, TAIPEI, format_db_datetime, local_to_utc, parse_db_datetime


IMPORTED_LENGTH = 26
SQLITE_LENGTH = 19
MIN_LAG_HOURS = 0.0
DERIVED_TABLES = ("event_analysis", "news_theme_classification")
SAMPLE_SIZE = 3


def _era(value: str) -> str:
    return "imported" if len(value) == IMPORTED_LENGTH else "sqlite"


def _lag_hours(fetched: datetime, published: datetime) -> float:
    return (fetched - published).total_seconds() / 3600


def _lag_stats(lags: list[float]) -> dict[str, float | None]:
    if not lags:
        return {"min": None, "median": None, "avg": None, "max": None}
    ordered = sorted(lags)
    return {
        "min": round(ordered[0], 3),
        "median": round(ordered[len(ordered) // 2], 3),
        "avg": round(sum(ordered) / len(ordered), 3),
        "max": round(ordered[-1], 3),
    }


class TimezoneMigration:
    name = "p0_2_timezone"
    from_version = 0
    to_version = 1

    def check_preconditions(self, connection: sqlite3.Connection) -> list[str]:
        failures = []
        for table in ("google_trends", "google_trends_news"):
            if not table_exists(connection, table):
                failures.append(f"missing table {table}")
        if failures:
            return failures
        existing_indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        for index in sorted(existing_indexes & set(RSS_UNIQUE_INDEXES)):
            failures.append(f"unique index {index} exists; shifting timestamps under it can collide transiently")
        for table in DERIVED_TABLES:
            if row_count(connection, table):
                failures.append(f"{table} must be empty (has {row_count(connection, table)} rows)")
        checks = {
            "NULL timestamps in google_trends":
                "SELECT COUNT(*) FROM google_trends WHERE published_at IS NULL OR fetched_at IS NULL",
            "unknown timestamp layout in google_trends":
                "SELECT COUNT(*) FROM google_trends WHERE length(published_at) NOT IN (19, 26) "
                "OR length(fetched_at) NOT IN (19, 26)",
            "published_at/fetched_at layouts differ within a row":
                "SELECT COUNT(*) FROM google_trends WHERE length(published_at) <> length(fetched_at)",
            "NULL or unknown fetched_at layout in google_trends_news":
                "SELECT COUNT(*) FROM google_trends_news WHERE fetched_at IS NULL OR length(fetched_at) NOT IN (19, 26)",
            "news era differs from its trend era":
                "SELECT COUNT(*) FROM google_trends_news n JOIN google_trends t ON t.trend_id = n.trend_id "
                "WHERE length(n.fetched_at) <> length(t.fetched_at)",
            "non-zero fractional seconds":
                "SELECT (SELECT COUNT(*) FROM google_trends WHERE (length(published_at) = 26 AND substr(published_at, 20) <> '.000000') "
                "OR (length(fetched_at) = 26 AND substr(fetched_at, 20) <> '.000000')) "
                "+ (SELECT COUNT(*) FROM google_trends_news WHERE length(fetched_at) = 26 AND substr(fetched_at, 20) <> '.000000')",
        }
        for label, sql in checks.items():
            count = int(connection.execute(sql).fetchone()[0])
            if count:
                failures.append(f"{label}: {count} row(s)")
        return failures

    def build_plan(self, connection: sqlite3.Connection) -> Plan:
        trend_changes: list[list[object]] = []
        news_changes: list[list[object]] = []
        eras: Counter[str] = Counter()
        news_eras: Counter[str] = Counter()
        offsets: Counter[str] = Counter()
        lags_before: dict[str, list[float]] = {"imported": [], "sqlite": []}
        lags_after: dict[str, list[float]] = {"imported": [], "sqlite": []}
        samples: dict[str, list[dict[str, object]]] = {"imported": [], "sqlite": []}
        errors = []

        rows = connection.execute(
            "SELECT trend_id, published_at, fetched_at FROM google_trends ORDER BY trend_id"
        ).fetchall()
        for trend_id, published_raw, fetched_raw in rows:
            era = _era(fetched_raw)
            eras[era] += 1
            published_local = parse_db_datetime(published_raw)
            fetched_stored = parse_db_datetime(fetched_raw)
            try:
                published_utc = local_to_utc(published_local, PACIFIC)
                fetched_utc = local_to_utc(fetched_stored, TAIPEI) if era == "imported" else fetched_stored
            except ValueError as error:
                errors.append(f"trend_id={trend_id}: {error}")
                continue
            offsets[f"{(published_utc - published_local).total_seconds() / 3600:+.0f}h"] += 1
            lags_before[era].append(_lag_hours(fetched_stored, published_local))
            lags_after[era].append(_lag_hours(fetched_utc, published_utc))
            new_published, new_fetched = format_db_datetime(published_utc), format_db_datetime(fetched_utc)
            if (new_published, new_fetched) != (published_raw, fetched_raw):
                trend_changes.append([trend_id, new_published, new_fetched])
            if len(samples[era]) < SAMPLE_SIZE:
                samples[era].append({
                    "trend_id": trend_id,
                    "published_at": [published_raw, new_published],
                    "fetched_at": [fetched_raw, new_fetched],
                })

        for news_id, fetched_raw in connection.execute(
            "SELECT news_id, fetched_at FROM google_trends_news ORDER BY news_id"
        ):
            era = _era(fetched_raw)
            news_eras[era] += 1
            fetched_stored = parse_db_datetime(fetched_raw)
            fetched_utc = local_to_utc(fetched_stored, TAIPEI) if era == "imported" else fetched_stored
            new_fetched = format_db_datetime(fetched_utc)
            if new_fetched != fetched_raw:
                news_changes.append([news_id, new_fetched])

        if errors:
            raise MigrationError(f"Unconvertible timestamps ({len(errors)}): {errors[:5]}")

        summary = {
            "google_trends_rows": len(rows),
            "google_trends_rows_by_era": dict(eras),
            "google_trends_rows_to_update": len(trend_changes),
            "google_trends_news_rows_by_era": dict(news_eras),
            "google_trends_news_rows_to_update": len(news_changes),
            "published_at_utc_offsets_applied": dict(offsets),
            "lag_hours_before": {era: _lag_stats(values) for era, values in lags_before.items()},
            "lag_hours_after": {era: _lag_stats(values) for era, values in lags_after.items()},
            "samples": samples,
        }
        return Plan(summary=summary, changes={"trends": trend_changes, "news": news_changes})

    def apply(self, connection: sqlite3.Connection, plan: Plan) -> None:
        connection.executemany(
            "UPDATE google_trends SET published_at = ?, fetched_at = ? WHERE trend_id = ?",
            [(published, fetched, trend_id) for trend_id, published, fetched in plan.changes["trends"]],
        )
        connection.executemany(
            "UPDATE google_trends_news SET fetched_at = ? WHERE news_id = ?",
            [(fetched, news_id) for news_id, fetched in plan.changes["news"]],
        )

    def verify(self, connection: sqlite3.Connection, plan: Plan) -> list[str]:
        failures = []
        if row_count(connection, "google_trends") != plan.summary["google_trends_rows"]:
            failures.append("google_trends row count changed")
        layout_violations = connection.execute(
            "SELECT (SELECT COUNT(*) FROM google_trends WHERE length(published_at) <> 19 OR length(fetched_at) <> 19) "
            "+ (SELECT COUNT(*) FROM google_trends_news WHERE length(fetched_at) <> 19)"
        ).fetchone()[0]
        if layout_violations:
            failures.append(f"{layout_violations} timestamp(s) not in canonical layout")
        min_lag = connection.execute(
            "SELECT MIN((julianday(fetched_at) - julianday(published_at)) * 24) FROM google_trends"
        ).fetchone()[0]
        if min_lag is not None and min_lag < MIN_LAG_HOURS - 1e-6:
            failures.append(
                f"fetched_at earlier than published_at (min lag {min_lag:.3f}h): time-zone assumption is wrong"
            )
        expected = {trend_id: (published, fetched) for trend_id, published, fetched in plan.changes["trends"]}
        mismatched = sum(
            1 for trend_id, published, fetched in connection.execute(
                "SELECT trend_id, published_at, fetched_at FROM google_trends"
            )
            if trend_id in expected and expected[trend_id] != (published, fetched)
        )
        if mismatched:
            failures.append(f"{mismatched} trend row(s) differ from the plan")
        return failures


MIGRATION = TimezoneMigration()


if __name__ == "__main__":
    raise SystemExit(main(MIGRATION))
