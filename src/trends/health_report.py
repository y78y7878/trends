"""Daily project health report: one read-only look at RSS, stocks, Google Trends, Gemini and the ETL.

Reads ``data.db`` in SQLite read-only mode and the collector logs under ``logs/``; it never writes
to the database. Coverage definitions match ``trends.etl.get_system_status`` (the Dashboard
sidebar), so both report the same numbers.

    python -m trends.health_report            # print and write logs/health_report.md
    python -m trends.health_report --no-write # print only

Exit code: 0 when no component is FAIL, 1 otherwise, so Task Scheduler flags a bad day.
"""
from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from trends.database import DB_PATH
from trends.timeutil import TAIPEI, parse_db_datetime, taipei_day_start_utc, utc_to_taipei


LOG_DIR = DB_PATH.parent / "logs"
REPORT_NAME = "health_report.md"

OK, WARN, FAIL, UNKNOWN = "OK", "WARN", "FAIL", "UNKNOWN"
STATUS_ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌", UNKNOWN: "❔"}
STATUS_POINTS = {OK: 20, WARN: 10, FAIL: 0, UNKNOWN: 10}

STOCK_READY_AT = time(18, 30)  # Stock Collector runs on trading days at 18:30 Taipei
RUN_MARKER = re.compile(
    r"^\[(?P<date>\d{4}/\d{1,2}/\d{1,2})[^\]]*?(?P<time>\d{1,2}:\d{2}:\d{2})[^\]]*\]\s+"
    r"(?P<job>\w+)\s+(?:start|exit\s+(?P<code>-?\d+))\s*$"
)
RSS_LINE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+(?P<level>[A-Z]+)\s+(?P<message>.*)$")
KEY_VALUE = re.compile(r"^(?P<key>[A-Za-z0-9 ]+):\s*(?P<value>.*)$")
ERROR_PATTERN = re.compile(
    r"\[ERROR\]|Traceback|batch failed|skipped:|stopped for this run|rate limited|Error:|\bERROR\b"
)


@dataclass
class LogRun:
    started_at: datetime
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)


@dataclass
class Component:
    name: str
    status: str
    last_success: str
    details: list[str]


@dataclass
class HealthReport:
    generated_at: datetime
    components: list[Component]
    today: dict[str, int]
    gemini_today: dict[str, float]
    coverage: dict[str, tuple[int, int]]
    recent_errors: list[str]

    @property
    def score(self) -> int:
        return sum(STATUS_POINTS[component.status] for component in self.components)

    @property
    def grade(self) -> str:
        if self.score >= 90:
            return "🟢 健康"
        if self.score >= 60:
            return "🟡 需要注意"
        return "🔴 異常"

    @property
    def has_failure(self) -> bool:
        return any(component.status == FAIL for component in self.components)


# ---------------------------------------------------------------- log parsing

def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    # Older run.bat-style headers wrote the locale weekday in cp950; never fail on it.
    return path.read_bytes().decode("utf-8", errors="replace").splitlines()


def parse_runs(lines: list[str], job: str) -> list[LogRun]:
    """Split a batch-file log into runs using its ``[date time] job start|exit N`` markers."""
    runs: list[LogRun] = []
    for line in lines:
        match = RUN_MARKER.match(line.strip())
        if match and match.group("job") == job:
            stamp = datetime.strptime(f"{match.group('date')} {match.group('time')}", "%Y/%m/%d %H:%M:%S")
            if match.group("code") is None:
                runs.append(LogRun(started_at=stamp))
            elif runs:
                runs[-1].exit_code = int(match.group("code"))
            continue
        if runs:
            runs[-1].lines.append(line)
    return runs


def parse_blocks(lines: list[str]) -> dict[str, dict[str, str]]:
    """Read the ``Key: value`` summary blocks the ETL prints (Gemini Stats, trend history)."""
    blocks: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in lines:
        line = raw.strip()
        if line == "Gemini Stats":
            current = blocks.setdefault("gemini", {})
            continue
        if line == "Completed" or line.startswith("Stopped: Google Trends"):
            current = blocks.setdefault("history", {})
            current["status"] = line
            continue
        if current is not None:
            match = KEY_VALUE.match(line)
            if match and not line.startswith("["):
                current[match.group("key").strip()] = match.group("value").strip()
                continue
            current = None
    return blocks


def parse_etl_result(lines: list[str]) -> dict[str, object]:
    for line in reversed(lines):
        if line.startswith("Theme ETL completed: "):
            try:
                value = ast.literal_eval(line.removeprefix("Theme ETL completed: "))
            except (ValueError, SyntaxError):
                return {}
            return value if isinstance(value, dict) else {}
    return {}


def _leading_number(value: str | None) -> float:
    match = re.match(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group()) if match else 0.0


def error_lines(lines: list[str], limit: int = 5) -> list[str]:
    found: list[str] = []
    for line in lines:
        text = line.strip()
        if text and ERROR_PATTERN.search(text) and text not in found:
            found.append(text)
    return [text[:200] for text in found[-limit:]]


# ---------------------------------------------------------------- database

def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def _scalar(connection: sqlite3.Connection, sql: str, *params: object) -> object:
    row = connection.execute(sql, params).fetchone()
    return row[0] if row else None


def _fmt_utc(value: object) -> str:
    parsed = parse_db_datetime(value)
    return "—" if parsed is None else utc_to_taipei(parsed).strftime("%Y-%m-%d %H:%M")


def _pct(part: int, total: int) -> str:
    return "—" if total == 0 else f"{part / total:.1%} ({part:,}/{total:,})"


def expected_trading_day(now: datetime) -> date:
    """Latest weekday whose close should be stored by now (holidays are not known here)."""
    day = now.date() if now.time() >= STOCK_READY_AT else now.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _previous_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# ---------------------------------------------------------------- report

def build_report(db_path: Path = DB_PATH, log_dir: Path = LOG_DIR, now: datetime | None = None) -> HealthReport:
    now = (now or datetime.now(TAIPEI)).replace(tzinfo=None)  # naive Asia/Taipei wall clock
    today = now.date()
    day_start = taipei_day_start_utc(today).strftime("%Y-%m-%d %H:%M:%S")
    day_end = taipei_day_start_utc(today + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    rss_lines = _read_lines(log_dir / "rss_collector.log")
    stock_runs = parse_runs(_read_lines(log_dir / "stock_collector.log"), "stock_collector")
    etl_runs = parse_runs(_read_lines(log_dir / "etl.log"), "etl")

    with closing(_connect(db_path)) as connection:
        q = lambda sql, *params: _scalar(connection, sql, *params)
        latest_fetch = q("select max(coalesce(fetched_at, published_at)) from google_trends")
        today_counts = {
            "RSS 熱搜": q("select count(*) from google_trends where published_at >= ? and published_at < ?", day_start, day_end),
            "RSS 新聞": q("select count(*) from google_trends_news where fetched_at >= ? and fetched_at < ?", day_start, day_end),
            "股價（今日交易日）": q("select count(*) from stocks where date = ?", today.isoformat()),
            "歷史熱度": q("select count(*) from google_trends_history where created_at >= ? and created_at < ?", day_start, day_end),
            "關鍵字分類": q("select count(*) from keyword_classification where created_at >= ? and created_at < ?", day_start, day_end),
            "實體": q("select count(*) from entity_master where created_at >= ? and created_at < ?", day_start, day_end),
        }
        latest_stock = q("select max(date) from stocks")
        stock_count = q("select count(distinct stock_id) from stocks where date = ?", latest_stock) if latest_stock else 0
        latest_history = q("select max(trend_date) from google_trends_history")
        active_pairs = q(
            "select count(*) from (select distinct theme_name, keyword from theme_mapping where active = 1)"
        ) or 0
        fresh_pairs = q(
            """select count(*) from (select distinct m.theme_name, m.keyword from theme_mapping m
               join google_trends_history h on h.theme_name = m.theme_name and h.keyword = m.keyword
               where m.active = 1 and h.trend_date >= ?)""",
            (today - timedelta(days=3)).isoformat(),
        ) or 0
        stock_pairs = q(
            """select count(*) from (select distinct theme_name, keyword from theme_mapping
               where active = 1 and coalesce(stock_id, '') <> '')"""
        ) or 0
        fresh_stock_pairs = q(
            """select count(*) from (select distinct m.theme_name, m.keyword from theme_mapping m
               join google_trends_history h on h.theme_name = m.theme_name and h.keyword = m.keyword
               where m.active = 1 and coalesce(m.stock_id, '') <> '' and h.trend_date >= ?)""",
            (today - timedelta(days=3)).isoformat(),
        ) or 0
        total_keywords = q("select count(distinct keyword) from google_trends where keyword is not null and trim(keyword) <> ''") or 0
        unclassified = q(
            """select count(distinct t.keyword) from google_trends t
               left join keyword_classification k on k.keyword = t.keyword
               where t.keyword is not null and trim(t.keyword) <> ''
                 and (k.keyword is null or (k.classification_source = 'fallback' and k.theme_name = '其他'))"""
        ) or 0
        total_news = q("select count(*) from google_trends_news") or 0
        analyzed_news = q(
            """select count(*) from google_trends_news where news_sentiment is not null
               and sentiment_score is not null and event_type is not null"""
        ) or 0
        latest_stats = q("select max(stat_date) from theme_daily_stats")

    coverage = {
        "分類覆蓋率": (total_keywords - unclassified, total_keywords),
        "情緒覆蓋率": (analyzed_news, total_news),
        "熱度覆蓋率（近 3 天有熱度的主題關鍵字）": (fresh_pairs, active_pairs),
        "熱度覆蓋率（其中有對應股票，事件研究使用）": (fresh_stock_pairs, stock_pairs),
    }
    errors: list[str] = []
    components = [
        _rss_component(now, latest_fetch, rss_lines, errors),
        _stock_component(now, latest_stock, stock_count, stock_runs, errors),
    ]
    last_etl = etl_runs[-1] if etl_runs else None
    blocks = parse_blocks(last_etl.lines) if last_etl else {}
    components.append(_trends_component(today, latest_history, fresh_pairs, active_pairs, etl_runs, blocks))
    components.append(_gemini_component(etl_runs, blocks, analyzed_news, total_news))
    components.append(_etl_component(now, etl_runs, latest_stats, errors))

    gemini_today = {"runs": 0, "requests": 0, "processed": 0, "429": 0, "503": 0, "wait": 0}
    for run in etl_runs:
        if run.started_at.date() != today:
            continue
        stats = parse_blocks(run.lines).get("gemini")
        if not stats:
            continue
        gemini_today["runs"] += 1
        gemini_today["requests"] += _leading_number(stats.get("Requests"))
        gemini_today["processed"] += _leading_number(stats.get("Processed"))
        gemini_today["429"] += _leading_number(stats.get("429 Count"))
        gemini_today["503"] += _leading_number(stats.get("503 Count"))
        gemini_today["wait"] += _leading_number(stats.get("Wait Time"))

    return HealthReport(
        generated_at=now,
        components=components,
        today={name: int(value or 0) for name, value in today_counts.items()},
        gemini_today=gemini_today,
        coverage=coverage,
        recent_errors=errors,
    )


def _rss_component(now: datetime, latest_fetch: object, lines: list[str], errors: list[str]) -> Component:
    fetched = parse_db_datetime(latest_fetch)
    age = None if fetched is None else now - utc_to_taipei(fetched).replace(tzinfo=None)
    since = now - timedelta(hours=24)
    runs = failed_items = 0
    recent: list[str] = []
    for line in lines:
        match = RSS_LINE.match(line)
        if not match or datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S") < since:
            continue
        message = match.group("message")
        if message.startswith("RSS collected:"):
            runs += 1
            failed_items += int(re.search(r"failed=(\d+)", message).group(1)) if "failed=" in message else 0
        if match.group("level") in {"ERROR", "WARNING"}:
            recent.append(f"rss_collector: {line.strip()}")
    errors.extend(recent[-5:])
    if age is None:
        status = FAIL
    elif age <= timedelta(minutes=30):
        status = WARN if recent else OK
    elif age <= timedelta(hours=2):
        status = WARN
    else:
        status = FAIL
    age_text = "—" if age is None else f"{int(age.total_seconds() // 60)} 分鐘前"
    return Component("RSS", status, _fmt_utc(latest_fetch), [
        f"最新資料：{age_text}",
        f"近 24 小時執行 {runs} 次（預期約 144 次），失敗項目 {failed_items}，錯誤或警告 {len(recent)} 筆",
    ])


def _stock_component(now: datetime, latest: object, count: int, runs: list[LogRun], errors: list[str]) -> Component:
    expected = expected_trading_day(now)
    latest_day = date.fromisoformat(str(latest)[:10]) if latest else None
    if latest_day is None:
        status = FAIL
    elif latest_day >= expected:
        status = OK
    elif latest_day >= _previous_weekday(expected):
        status = WARN  # one weekday behind: a holiday, or tonight's run has not happened yet
    else:
        status = FAIL
    last = runs[-1] if runs else None
    succeeded = [run for run in runs if _completed(run, "筆行情")]
    details = [f"最新交易日 {latest or '—'}（{count} 檔），預期至少 {expected}（假日會顯示為落後一天）"]
    if last is not None:
        written = next((line.strip() for line in reversed(last.lines) if "筆行情" in line), "沒有寫入紀錄")
        missing = sum("possibly delisted" in line for line in last.lines)
        details.append(f"最後執行 {last.started_at:%Y-%m-%d %H:%M}，exit {_exit_text(last)}，{written}")
        if missing:
            details.append(f"{missing} 個代號查無行情（yfinance possibly delisted）")
        if last.exit_code not in (0, None):
            status = FAIL
            errors.extend(f"stock_collector: {text}" for text in error_lines(last.lines))
    else:
        details.append("logs/stock_collector.log 沒有執行紀錄")
    last_success = f"{succeeded[-1].started_at:%Y-%m-%d %H:%M}" if succeeded else "—"
    return Component("Stocks", status, last_success, details)


def _trends_component(
    today: date, latest: object, fresh: int, active: int, runs: list[LogRun], blocks: dict[str, dict[str, str]]
) -> Component:
    latest_day = date.fromisoformat(str(latest)[:10]) if latest else None
    ratio = fresh / active if active else 0.0
    if latest_day is None or latest_day < today - timedelta(days=3) or ratio < 0.2:
        status = FAIL
    elif latest_day >= today - timedelta(days=1) and ratio >= 0.8:
        status = OK
    else:
        status = WARN
    history = blocks.get("history")
    details = [f"最新熱度日 {latest or '—'}（完整日資料最新應為 {today - timedelta(days=1)}），近 3 天有熱度 {_pct(fresh, active)}"]
    if history:
        details.append(
            f"最後一次 ETL：{history.get('status')}；Success {history.get('Success', '—')}、Failed {history.get('Failed', '—')}、"
            f"Skipped {history.get('Skipped', '—')}、Not attempted {history.get('Not attempted', '—')}"
        )
        if history.get("status", "").startswith("Stopped") and status == OK:
            status = WARN
    successes = [run for run in runs if _leading_number(parse_blocks(run.lines).get("history", {}).get("Success")) > 0]
    last_success = f"{successes[-1].started_at:%Y-%m-%d %H:%M}" if successes else "—"
    return Component("Google Trends", status, last_success, details)


def _gemini_component(
    runs: list[LogRun], blocks: dict[str, dict[str, str]], analyzed: int, total: int
) -> Component:
    stats = blocks.get("gemini")
    successes = [run for run in runs if _leading_number(parse_blocks(run.lines).get("gemini", {}).get("Processed")) > 0]
    last_success = f"{successes[-1].started_at:%Y-%m-%d %H:%M}" if successes else "—"
    if not stats:
        return Component("Gemini", UNKNOWN, last_success, ["最後一次 ETL 沒有 Gemini Stats（舊版 ETL 或尚未執行）"])
    processed = _leading_number(stats.get("Processed"))
    stopped = stats.get("Stopped", "no")
    if stopped == "no":
        status = OK
    elif processed > 0:
        status = WARN
    else:
        status = FAIL
    if status == OK and total and analyzed / total < 0.5:
        status = WARN  # working, but most news still lacks sentiment
    return Component("Gemini", status, last_success, [
        f"最後一次 ETL：Requests {stats.get('Requests', '—')}、Processed {stats.get('Processed', '—')}、"
        f"429 {stats.get('429 Count', '—')}、503 {stats.get('503 Count', '—')}、Wait {stats.get('Wait Time', '—')}",
        f"待處理：{stats.get('Skipped', '—')}；停止原因：{stopped}",
    ])


def _etl_component(now: datetime, runs: list[LogRun], latest_stats: object, errors: list[str]) -> Component:
    last = runs[-1] if runs else None
    finished = [run for run in runs if _completed(run, "Theme ETL completed", (0, 3))]
    last_success = f"{finished[-1].started_at:%Y-%m-%d %H:%M}" if finished else "—"
    if last is None:
        return Component("ETL", FAIL, last_success, ["logs/etl.log 沒有執行紀錄"])
    result = parse_etl_result(last.lines)
    if now - last.started_at > timedelta(hours=26):
        status = FAIL
    elif _completed(last, "Theme ETL completed", (0, 3)):
        status = OK  # 3 = Google Trends rate limited; reported under Google Trends
    elif last.exit_code is None:
        status = WARN  # still running or killed before finishing
    else:
        status = FAIL
    stats_day = date.fromisoformat(str(latest_stats)[:10]) if latest_stats else None
    if status == OK and (stats_day is None or stats_day < now.date() - timedelta(days=1)):
        status = WARN
    errors.extend(f"etl: {text}" for text in error_lines(last.lines))
    details = [
        f"最後執行 {last.started_at:%Y-%m-%d %H:%M}，exit {_exit_text(last)}，主題統計最新日 {latest_stats or '—'}",
    ]
    if result:
        details.append(
            "結果：" + "、".join(
                f"{key} {result[key]}" for key in (
                    "resolved_entities", "classified_keywords", "quality_classified_keywords",
                    "history_rows", "classified_news", "daily_stat_themes",
                ) if key in result
            )
        )
    return Component("ETL", status, last_success, details)


def _completed(run: LogRun, marker: str, ok_codes: tuple[int, ...] = (0,)) -> bool:
    """A run succeeded if it exited cleanly, or (older batch files without an exit line) logged ``marker``."""
    if run.exit_code is not None:
        return run.exit_code in ok_codes
    return any(marker in line for line in run.lines) and not any("Traceback" in line for line in run.lines)


def _exit_text(run: LogRun) -> str:
    return "未記錄" if run.exit_code is None else str(run.exit_code)


def render_markdown(report: HealthReport) -> str:
    lines = [
        f"# Project Health Report — {report.generated_at:%Y-%m-%d %H:%M}（台北）",
        "",
        f"**系統健康度：{report.score} / 100　{report.grade}**",
        "",
        "| 元件 | 狀態 | 最後成功 | 摘要 |",
        "|---|---|---|---|",
    ]
    for component in report.components:
        lines.append(
            f"| {component.name} | {STATUS_ICON[component.status]} {component.status} | "
            f"{component.last_success} | {component.details[0]} |"
        )
    lines += ["", "## 細節", ""]
    for component in report.components:
        lines.append(f"**{component.name}**")
        lines += [f"- {detail}" for detail in component.details]
        lines.append("")
    lines += ["## 今日新增資料量（台北日）", "", "| 資料 | 筆數 |", "|---|---:|"]
    lines += [f"| {name} | {count:,} |" for name, count in report.today.items()]
    g = report.gemini_today
    lines += [
        "",
        "## Gemini 今日處理量",
        "",
        f"ETL 執行 {int(g['runs'])} 次；Requests {int(g['requests'])}、Processed {int(g['processed'])}、"
        f"429 {int(g['429'])} 次、503 {int(g['503'])} 次、等待 {int(g['wait'])} 秒",
        "",
        "## 覆蓋率",
        "",
        "| 指標 | 數值 |",
        "|---|---|",
    ]
    lines += [f"| {name} | {_pct(part, total)} |" for name, (part, total) in report.coverage.items()]
    lines += ["", "## 最近錯誤", ""]
    lines += [f"- `{text}`" for text in report.recent_errors] or ["- 無"]
    lines += [
        "",
        "---",
        "分數：每個元件 OK 20、WARN 10、FAIL 0、UNKNOWN 10 分；90 以上健康，60–89 需要注意，未滿 60 異常。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="產生專案健康度報告（唯讀）")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--logs", type=Path, default=LOG_DIR)
    parser.add_argument("--no-write", action="store_true", help=f"只輸出，不寫入 logs/{REPORT_NAME}")
    args = parser.parse_args(argv)
    report = build_report(args.db, args.logs)
    text = render_markdown(report)
    print(text)
    if not args.no_write:
        args.logs.mkdir(parents=True, exist_ok=True)
        (args.logs / REPORT_NAME).write_text(text, encoding="utf-8")
    return 1 if report.has_failure else 0


if __name__ == "__main__":
    sys.exit(main())
