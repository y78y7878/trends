"""P1: split keyword-to-stock links into direct and theme links, and drop the fan-out.

Until now an industry-sounding keyword with no stock code of its own was linked to *every*
stock ever mapped to its theme (「Uber」 → 78 tech stocks, 「謝金河」 → 25 financials). This
migration sets ``theme_mapping.link_type`` on every active row that has a stock:

- ``theme``: the row is in theme_mapping.csv (the curated basket of that theme).
- ``direct``: keyword and stock match an ``entity_master`` company/ETF (the keyword is the company).
- anything else is fan-out and is deactivated (``active = 0``; nothing is deleted).

Keywords left without any stock link are put back in the classification queue (classification
reset to fallback/「其他」, and removed from ``data.gemini_progress.json``), so the next ETL
resolves companies to their own stock and links industry words to the curated basket only.
The progress file is edited inside ``apply``; if the transaction then rolls back, those
keywords are merely re-sent to Gemini once. Usage::

    python -m trends.migrations.p1_link_type --report
    python -m trends.migrations.p1_link_type --rehearse backups/rehearsal_p1.db
    python -m trends.migrations.p1_link_type --apply --expected-plan DIGEST --confirm
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from trends.database import LINK_DIRECT, LINK_THEME
from trends.gemini_control import GeminiProgress
from trends.migrations._common import Plan, main, table_exists
from trends.theme_study import load_theme_mapping


# Seed aliases removed from entity_seed_mapping.csv because they are everyday words.
RETIRED_ALIASES = ("創意",)
REQUEUED_TASKS = ("entity", "keyword", "auto_quality")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _progress(connection: sqlite3.Connection) -> GeminiProgress | None:
    database = connection.execute("PRAGMA database_list").fetchone()[2]
    if not database:
        return None
    path = Path(database).with_name(f"{Path(database).stem}.gemini_progress.json")
    return GeminiProgress(path) if path.exists() else None


class LinkTypeMigration:
    name = "p1_link_type"
    from_version = 2
    to_version = 3

    def check_preconditions(self, connection: sqlite3.Connection) -> list[str]:
        missing = [table for table in ("theme_mapping", "entity_master", "keyword_classification")
                   if not table_exists(connection, table)]
        return [f"missing table {table}" for table in missing]

    def build_plan(self, connection: sqlite3.Connection) -> Plan:
        curated = {
            (row.theme_name, row.keyword, row.stock_id)
            for row in load_theme_mapping().itertuples(index=False) if row.stock_id
        }
        companies = set(connection.execute(
            "SELECT entity_name, stock_id FROM entity_master WHERE coalesce(stock_id, '') <> ''"
        ).fetchall())
        rows = connection.execute(
            "SELECT id, theme_name, keyword, stock_id FROM theme_mapping "
            "WHERE active = 1 AND coalesce(stock_id, '') <> '' ORDER BY id"
        ).fetchall()
        theme_ids, direct_ids, deactivate_ids = [], [], []
        linked_before, linked_after = set(), set()
        for row_id, theme_name, keyword, stock_id in rows:
            linked_before.add(keyword)
            if (theme_name, keyword, stock_id) in curated:
                theme_ids.append(row_id)
            elif keyword not in RETIRED_ALIASES and (keyword, stock_id) in companies:
                direct_ids.append(row_id)
            else:
                deactivate_ids.append(row_id)
                continue
            linked_after.add(keyword)
        unlinked = sorted(linked_before - linked_after)
        requeue = sorted({
            keyword for (keyword,) in connection.execute(
                "SELECT keyword FROM keyword_classification WHERE keyword IN ({0}) OR canonical_keyword IN ({0})"
                .format(",".join("?" * len(unlinked))), unlinked + unlinked,
            )
        }) if unlinked else []
        summary = {
            "active_stock_rows": len(rows),
            "theme_rows": len(theme_ids),
            "direct_rows": len(direct_ids),
            "deactivated_rows": len(deactivate_ids),
            "keywords_linked_before": len(linked_before),
            "keywords_linked_after": len(linked_after),
            "keywords_unlinked": len(unlinked),
            "classifications_requeued": len(requeue),
            "unlinked_sample": unlinked[:20],
        }
        changes = {"theme": theme_ids, "direct": direct_ids, "deactivate": deactivate_ids, "requeue": requeue}
        return Plan(summary=summary, changes=changes)

    def apply(self, connection: sqlite3.Connection, plan: Plan) -> None:
        if "link_type" not in _columns(connection, "theme_mapping"):
            connection.execute("ALTER TABLE theme_mapping ADD COLUMN link_type VARCHAR(20)")
        for link_type, key in ((LINK_THEME, "theme"), (LINK_DIRECT, "direct")):
            connection.executemany(
                "UPDATE theme_mapping SET link_type = ? WHERE id = ?",
                [(link_type, row_id) for row_id in plan.changes[key]],
            )
        connection.executemany(
            "UPDATE theme_mapping SET active = 0 WHERE id = ?", [(row_id,) for row_id in plan.changes["deactivate"]]
        )
        connection.executemany(
            "UPDATE keyword_classification SET theme_name = '其他', sub_theme = '未分類', stock_related = 0, "
            "classification_source = 'fallback', confidence_score = 0 WHERE keyword = ?",
            [(keyword,) for keyword in plan.changes["requeue"]],
        )
        progress = _progress(connection)
        if progress is not None:
            for task in REQUEUED_TASKS:
                progress.forget(task, plan.changes["requeue"])

    def verify(self, connection: sqlite3.Connection, plan: Plan) -> list[str]:
        failures = []
        untyped = connection.execute(
            "SELECT count(*) FROM theme_mapping WHERE active = 1 AND coalesce(stock_id, '') <> '' "
            "AND link_type IS NULL"
        ).fetchone()[0]
        if untyped:
            failures.append(f"{untyped} active stock rows still have no link_type")
        typed = connection.execute(
            "SELECT count(*) FROM theme_mapping WHERE active = 1 AND link_type IN (?, ?)", (LINK_THEME, LINK_DIRECT)
        ).fetchone()[0]
        if typed != plan.summary["theme_rows"] + plan.summary["direct_rows"]:
            failures.append(f"expected {plan.summary['theme_rows'] + plan.summary['direct_rows']} typed rows, found {typed}")
        return failures


MIGRATION = LinkTypeMigration()


if __name__ == "__main__":
    raise SystemExit(main(MIGRATION))
