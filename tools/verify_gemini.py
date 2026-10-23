"""Read-only verification report for the 2026-10-23 Gemini changes (batch 150, one 503 retry, model fallback).

Usage (project root):
    .venv\\Scripts\\python.exe tools\\verify_gemini.py
    .venv\\Scripts\\python.exe tools\\verify_gemini.py --cutover "2026-10-23 14:00" --reset-hour 15

Reads logs\\etl.log and data.db (opened read-only); never imports project code or writes to the database.
Writes the report to logs\\gemini_verification.md and appends one row to logs\\gemini_verification_snapshots.csv
so coverage can be compared across days.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "etl.log"
DB = ROOT / "data.db"
REPORT = ROOT / "logs" / "gemini_verification.md"
SNAPSHOTS = ROOT / "logs" / "gemini_verification_snapshots.csv"

# Baseline measured in the 7-day observation (2026-10-16 01:48 ~ 10-23 01:48), see docs/HANDOVER_2026-10-23.md.
BASELINE_START = datetime(2026, 10, 16, 1, 48)
BASELINE_COVERAGE = 1.0  # % news with sentiment, health report 2026-10-23 07:30 (109/10,796)

RUN = re.compile(r"\[(\d{4}/\d\d/\d\d)\s+(\d+:\d\d:\d\d)\] etl (start|exit)\s*(-?\d*)")
UNAVAILABLE = re.compile(r"^\[GEMINI\] (\S+) unavailable for this run: (.*)$")
BATCH_FAILED = re.compile(r"^(Entity AI|Keyword AI|Auto keyword classification|News AI) batch failed \((\d+) \w+\): (.*)$")
JSON_ERROR = re.compile(r"JSONDecodeError|Expecting value|Unterminated string|Expecting ',' delimiter")


@dataclass
class Run:
    started: datetime
    exit_code: str = "?"
    lines: list[str] = field(default_factory=list)
    requests: int = 0
    models: Counter = field(default_factory=Counter)
    processed: int = 0
    processed_by: Counter = field(default_factory=Counter)
    pending_keywords: int | None = None
    pending_news: int | None = None
    s429: int = 0
    s503: int = 0
    stopped: str = "no"
    unavailable: list[tuple[str, str]] = field(default_factory=list)
    batch_failures: list[tuple[str, int, str]] = field(default_factory=list)
    json_errors: int = 0
    has_stats: bool = False


def parse_runs() -> list[Run]:
    runs, current = [], None
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        match = RUN.match(line)
        if match:
            when = datetime.strptime(f"{match[1]} {match[2]}", "%Y/%m/%d %H:%M:%S")
            if match[3] == "start":
                current = Run(when)
                runs.append(current)
            elif current is not None:
                current.exit_code = match[4] or "?"
                current = None
            continue
        if current is not None:
            current.lines.append(line)
    for run in runs:
        for line in run.lines:
            if line.startswith("Requests: "):
                run.requests, run.has_stats = int(line.split()[1]), True
            elif line.startswith("Models: "):
                for name, count in re.findall(r"([\w.-]+) (\d+)", line[8:]):
                    run.models[name] += int(count)
            elif line.startswith("Processed: "):
                run.processed = int(line.split()[1])
                for task, count in re.findall(r"(\w+) (\d+)", line[line.find("("):] if "(" in line else ""):
                    run.processed_by[task] += int(count)
            elif line.startswith("Skipped: ") and "keywords" in line:
                found = re.search(r"keywords (\d+), news (\d+)", line)
                if found:
                    run.pending_keywords, run.pending_news = int(found[1]), int(found[2])
            elif line.startswith("429 Count: "):
                run.s429 = int(line.split()[2])
            elif line.startswith("503 Count: "):
                run.s503 = int(line.split()[2])
            elif line.startswith("Stopped: ") and not line.startswith("Stopped: Google"):
                run.stopped = line[9:].strip()
            if (found := UNAVAILABLE.match(line)):
                run.unavailable.append((found[1], found[2]))
            if (found := BATCH_FAILED.match(line)):
                run.batch_failures.append((found[1], int(found[2]), found[3][:120]))
            if JSON_ERROR.search(line):
                run.json_errors += 1
    return [run for run in runs if run.has_stats]


def quota_day(when: datetime, reset_hour: int) -> str:
    return (when - timedelta(hours=reset_hour)).date().isoformat()


def summarize(runs: list[Run], reset_hour: int) -> dict[str, float]:
    if not runs:
        return {}
    requests = sum(r.requests for r in runs)
    processed = sum(r.processed for r in runs)
    days = sorted({quota_day(r.started, reset_hour) for r in runs})
    span_days = max((runs[-1].started - runs[0].started).total_seconds() / 86400, 1 / 4)
    first, last = runs[0], runs[-1]
    by_task = Counter()
    for run in runs:
        by_task.update(run.processed_by)
    return {
        "runs": len(runs),
        "quota_days": len(days),
        "requests": requests,
        "processed": processed,
        "items_per_request": processed / requests if requests else 0.0,
        "requests_per_quota_day": requests / len(days),
        "processed_per_quota_day": processed / len(days),
        "s503": sum(r.s503 for r in runs),
        "s503_share": 100 * sum(r.s503 for r in runs) / requests if requests else 0.0,
        "s429": sum(r.s429 for r in runs),
        "zero_runs_share": 100 * sum(r.processed == 0 for r in runs) / len(runs),
        "all_models_out": sum(r.stopped.startswith("all models unavailable") for r in runs),
        "stopped_runs": sum(r.stopped != "no" for r in runs),
        "batch_failures": sum(len(r.batch_failures) for r in runs),
        "json_errors": sum(r.json_errors for r in runs),
        "news_backlog_first": first.pending_news,
        "news_backlog_last": last.pending_news,
        "kw_backlog_first": first.pending_keywords,
        "kw_backlog_last": last.pending_keywords,
        "news_backlog_per_day": ((last.pending_news or 0) - (first.pending_news or 0)) / span_days,
        "kw_backlog_per_day": ((last.pending_keywords or 0) - (first.pending_keywords or 0)) / span_days,
        **{f"task_{task}": count for task, count in by_task.items()},
    }


def db_snapshot() -> dict[str, object]:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    q = lambda sql: connection.execute(sql).fetchall()
    total, analyzed = q("select count(*), sum(news_sentiment is not null) from google_trends_news")[0]
    since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    new_total, new_analyzed = q(
        f"select count(*), sum(news_sentiment is not null) from google_trends_news where fetched_at >= '{since}'"
    )[0]
    snapshot = {
        "news_total": total,
        "news_analyzed": analyzed or 0,
        "coverage": 100 * (analyzed or 0) / total if total else 0.0,
        "news_24h": new_total,
        "news_24h_analyzed": new_analyzed or 0,
        "sentiment": dict(q("select news_sentiment, count(*) from google_trends_news where news_sentiment is not null group by 1")),
        "kw_sources": dict(q("select classification_source, count(*) from keyword_classification group by 1")),
    }
    connection.close()
    return snapshot


def append_snapshot(now: datetime, snap: dict[str, object], after: dict[str, float]) -> list[dict[str, str]]:
    fields = ["taken_at", "news_total", "news_analyzed", "coverage_pct", "kw_gemini", "kw_fallback",
              "news_backlog_log", "kw_backlog_log"]
    row = {
        "taken_at": now.strftime("%Y-%m-%d %H:%M"),
        "news_total": snap["news_total"],
        "news_analyzed": snap["news_analyzed"],
        "coverage_pct": f"{snap['coverage']:.1f}",
        "kw_gemini": snap["kw_sources"].get("gemini", 0),
        "kw_fallback": snap["kw_sources"].get("fallback", 0),
        "news_backlog_log": after.get("news_backlog_last", ""),
        "kw_backlog_log": after.get("kw_backlog_last", ""),
    }
    new_file = not SNAPSHOTS.exists()
    with SNAPSHOTS.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    with SNAPSHOTS.open(encoding="utf-8") as file:
        return list(csv.DictReader(file))


def verdict(ok: bool, bad: bool, enough: bool) -> str:
    if not enough:
        return "⏳ 觀察中"
    return "✅ 成功" if ok else ("❌ 失敗" if bad else "🟡 未達標")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutover", default="2026-10-23 14:00", help="第一次使用新程式的 ETL 時間（台北）")
    parser.add_argument("--reset-hour", type=int, default=15, help="Gemini 每日額度重置的台北整點；11/1 後改 16")
    args = parser.parse_args()
    cutover = datetime.strptime(args.cutover, "%Y-%m-%d %H:%M")
    now = datetime.now()

    runs = parse_runs()
    before_runs = [r for r in runs if BASELINE_START <= r.started < cutover]
    after_runs = [r for r in runs if r.started >= cutover]
    before, after = summarize(before_runs, args.reset_hour), summarize(after_runs, args.reset_hour)
    snap = db_snapshot()
    history = append_snapshot(now, snap, after)

    hours_after = (now - cutover).total_seconds() / 3600
    enough_runs = len(after_runs) >= 4
    enough_3d, enough_7d = hours_after >= 72, hours_after >= 168
    cut_news = before.get("news_backlog_last") or 0
    g = lambda d, k, fmt="{:.0f}": "—" if k not in d or d[k] is None else fmt.format(d[k])

    out = [f"# Gemini 優化驗證報告 — {now:%Y-%m-%d %H:%M}", "",
           f"- 切換時間：{cutover:%Y-%m-%d %H:%M}（之前 = 修改前，之後 = 修改後），已過 {hours_after:.1f} 小時",
           f"- 修改前：{len(before_runs)} 次 ETL（{BASELINE_START:%m/%d %H:%M} 起）；修改後：{len(after_runs)} 次 ETL",
           f"- 額度日以台北 {args.reset_hour}:00 為界", "",
           "## 1. 修改前後比較", "",
           "| 指標 | 修改前 | 修改後 |", "|---|---:|---:|"]
    rows = [
        ("ETL 次數", "runs", "{:.0f}"),
        ("Gemini 請求數", "requests", "{:.0f}"),
        ("每額度日請求數", "requests_per_quota_day", "{:.1f}"),
        ("處理筆數", "processed", "{:.0f}"),
        ("**每額度日處理筆數**", "processed_per_quota_day", "{:.0f}"),
        ("**每個請求處理筆數**", "items_per_request", "{:.1f}"),
        ("503 次數", "s503", "{:.0f}"),
        ("**503 占請求比例 %**", "s503_share", "{:.1f}"),
        ("429 次數", "s429", "{:.0f}"),
        ("**處理 0 筆的 ETL 比例 %**", "zero_runs_share", "{:.1f}"),
        ("Gemini 中途停止的 ETL 次數", "stopped_runs", "{:.0f}"),
        ("所有模型都用完的次數", "all_models_out", "{:.0f}"),
        ("批次失敗次數", "batch_failures", "{:.0f}"),
        ("JSON 解析錯誤（截斷跡象）", "json_errors", "{:.0f}"),
        ("新聞待處理（期間第一筆 → 最後一筆）", None, None),
        ("**新聞待處理每天淨變化**", "news_backlog_per_day", "{:+.0f}"),
        ("關鍵字待處理（期間第一筆 → 最後一筆）", None, None),
        ("關鍵字待處理每天淨變化", "kw_backlog_per_day", "{:+.0f}"),
    ]
    for label, key, fmt in rows:
        if key is None:
            prefix = "news" if "新聞" in label else "kw"
            cell = lambda d: f"{g(d, prefix + '_backlog_first')} → {g(d, prefix + '_backlog_last')}"
            out.append(f"| {label} | {cell(before)} | {cell(after)} |")
        else:
            out.append(f"| {label} | {g(before, key, fmt)} | {g(after, key, fmt)} |")
    for task in ("entity", "keyword", "auto_quality", "news"):
        out.append(f"| 處理筆數：{task} | {before.get('task_' + task, 0)} | {after.get('task_' + task, 0)} |")

    coverage = snap["coverage"]
    out += ["", "## 2. 資料庫現況（唯讀）", "",
            f"- 新聞情緒覆蓋率：**{coverage:.1f}%**（{snap['news_analyzed']:,} / {snap['news_total']:,}），基準 {BASELINE_COVERAGE}%",
            f"- 近 24 小時新抓的新聞：{snap['news_24h']:,} 篇，已分析 {snap['news_24h_analyzed']:,} 篇",
            f"- 情緒分布：{snap['sentiment']}",
            f"- 關鍵字分類來源：{snap['kw_sources']}", "",
            "覆蓋率歷次快照（每次執行本指令會新增一列）：", "",
            "| 時間 | 覆蓋率 % | 已分析 | 新聞總數 | gemini 關鍵字 | fallback 關鍵字 |", "|---|---:|---:|---:|---:|---:|"]
    for row in history[-10:]:
        out.append(f"| {row['taken_at']} | {row['coverage_pct']} | {row['news_analyzed']} | {row['news_total']} | {row['kw_gemini']} | {row['kw_fallback']} |")

    models = Counter()
    unavailable = Counter()
    for run in after_runs:
        models.update(run.models)
        unavailable.update(f"{model}: {reason}" for model, reason in run.unavailable)
    out += ["", "## 3. 修改後的模型使用", "",
            f"- 各模型請求數：{dict(models) or '—'}",
            f"- 模型被放棄的原因（次數）：{dict(unavailable) or '—'}"]

    news_drop_per_day = -(after.get("news_backlog_per_day") or 0)
    news_last = after.get("news_backlog_last")
    largest = max((share for share in snap["sentiment"].values()), default=0)
    sentiment_total = sum(snap["sentiment"].values()) or 1
    checks = [
        ("1. 待處理量：新聞每天淨減少 ≥ 1,000 篇，3 天內 ≤ 5,000",
         news_drop_per_day >= 1000 and (not enough_3d or (news_last or 1e9) <= 5000),
         after.get("news_backlog_per_day", 0) >= 0 or (enough_3d and (news_last or 0) >= cut_news),
         enough_runs, f"每天 {after.get('news_backlog_per_day', 0):+.0f}，目前 {g(after, 'news_backlog_last')}（切換前 {cut_news:,}）"),
        ("1. 待處理量：關鍵字待處理下降",
         (after.get("kw_backlog_per_day") or 0) < 0, (after.get("kw_backlog_per_day") or 0) > 0,
         enough_runs, f"每天 {after.get('kw_backlog_per_day', 0):+.0f}"),
        ("2. 503：占請求比例 ≤ 30%（基準 67%）",
         after.get("s503_share", 100) <= 30, after.get("s503_share", 0) >= 50,
         enough_runs, f"{after.get('s503_share', 0):.1f}%"),
        ("2. 503：處理 0 筆的 ETL ≤ 10%（基準 50%）",
         after.get("zero_runs_share", 100) <= 10, after.get("zero_runs_share", 0) >= 30,
         enough_runs, f"{after.get('zero_runs_share', 0):.1f}%"),
        ("3. 額度效率：每個請求 ≥ 100 筆（基準 12.6）",
         after.get("items_per_request", 0) >= 100, after.get("items_per_request", 0) < 50,
         enough_runs, f"{after.get('items_per_request', 0):.1f}"),
        ("3. 額度效率：每額度日處理 ≥ 3,000 筆（基準約 280）",
         after.get("processed_per_quota_day", 0) >= 3000, after.get("processed_per_quota_day", 0) < 1000,
         enough_runs and after.get("quota_days", 0) >= 2, f"{after.get('processed_per_quota_day', 0):.0f}"),
        ("3. 額度效率：所有模型用完 ≤ 每額度日 1 次",
         after.get("all_models_out", 0) <= after.get("quota_days", 0),
         after.get("all_models_out", 0) > 2 * max(after.get("quota_days", 0), 1),
         enough_runs, f"{after.get('all_models_out', 0):.0f} 次 / {after.get('quota_days', 0):.0f} 額度日"),
        ("4. 覆蓋率：3 天內 ≥ 30%（7 天內 ≥ 60%）",
         coverage >= (60 if enough_7d else 30), enough_3d and coverage < 10,
         enough_3d, f"{coverage:.1f}%"),
        ("品質：沒有 JSON 截斷或批次失敗",
         after.get("json_errors", 0) == 0 and after.get("batch_failures", 0) == 0,
         after.get("json_errors", 0) > 0,
         len(after_runs) >= 1, f"JSON {after.get('json_errors', 0):.0f}、批次失敗 {after.get('batch_failures', 0):.0f}"),
        ("品質：情緒分布沒有單一類別 > 80%",
         largest / sentiment_total <= 0.8, largest / sentiment_total > 0.8,
         sentiment_total >= 500, f"最大類別 {100 * largest / sentiment_total:.0f}%"),
    ]
    out += ["", "## 4. 判定", "", "| 項目 | 結果 | 實際值 |", "|---|---|---|"]
    for label, ok, bad, enough, value in checks:
        out.append(f"| {label} | {verdict(ok, bad, enough)} | {value} |")

    failures = [(r.started, kind, size, msg) for r in after_runs for kind, size, msg in r.batch_failures]
    if failures:
        out += ["", "## 5. 修改後的批次失敗（最近 10 筆）", ""]
        out += [f"- {when:%m/%d %H:%M} {kind} ({size}): {msg}" for when, kind, size, msg in failures[-10:]]
    out += ["", "## 6. 修改後每次 ETL", "", "| 時間 | exit | 請求 | 處理 | 503 | 429 | 模型 | 新聞待處理 | 停止原因 |",
            "|---|---|---:|---:|---:|---:|---|---:|---|"]
    for run in after_runs:
        out.append(f"| {run.started:%m/%d %H:%M} | {run.exit_code} | {run.requests} | {run.processed} | {run.s503} | {run.s429} | "
                   f"{', '.join(f'{m} {c}' for m, c in run.models.items()) or '—'} | {run.pending_news if run.pending_news is not None else '—'} | {run.stopped[:80]} |")
    if not after_runs:
        out.append("| — | | | | | | 尚無修改後的 ETL | | |")

    report = "\n".join(out) + "\n"
    REPORT.write_text(report, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(report)
    print(f"（已寫入 {REPORT.relative_to(ROOT)}；快照累積於 {SNAPSHOTS.relative_to(ROOT)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
