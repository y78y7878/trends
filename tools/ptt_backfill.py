"""One-off backfill of the PTT Stock board for the pilot study (not scheduled).

Writes to ptt_stock.db next to data.db, so the production database is untouched:
- ptt_posts:    every post on the board's index pages since --since (title, category, push score;
                the post time comes from the article id ``M.<unix time>``).
- ptt_comments: every comment (推/噓/→, user, text, minute timestamp) of the daily
                「盤中閒聊」 and 「盤後閒聊」 threads, where most of the board's discussion happens.

Polite crawling: one request per --delay seconds, retries with back-off, safe to re-run (upserts).
Usage::

    .venv\\Scripts\\python.exe tools\\ptt_backfill.py --since 2026-08-21
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BOARD_URL = "https://www.ptt.cc/bbs/Stock"
DB = Path(__file__).resolve().parents[1] / "ptt_stock.db"
TAIPEI = timezone(timedelta(hours=8))
CHAT = re.compile(r"\[閒聊\].*(盤中|盤後)閒聊")
SCHEMA = """
CREATE TABLE IF NOT EXISTS ptt_posts (
    post_id TEXT PRIMARY KEY, posted_at TEXT NOT NULL, category TEXT, title TEXT NOT NULL,
    author TEXT, push_label TEXT, push_score INTEGER, url TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ptt_comments (
    post_id TEXT NOT NULL, seq INTEGER NOT NULL, commented_at TEXT, tag TEXT, user TEXT, text TEXT,
    PRIMARY KEY (post_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_ptt_posts_time ON ptt_posts (posted_at);
CREATE INDEX IF NOT EXISTS ix_ptt_comments_time ON ptt_comments (commented_at);
"""


class Crawler:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.session = requests.Session()
        self.session.cookies.set("over18", "1")
        self.session.headers["User-Agent"] = "Mozilla/5.0 (personal research; trends project)"

    def get(self, url: str) -> BeautifulSoup:
        for attempt in range(4):
            time.sleep(self.delay)
            try:
                response = self.session.get(url, timeout=20)
                if response.status_code == 404:
                    raise FileNotFoundError(url)
                response.raise_for_status()
                response.encoding = "utf-8"
                return BeautifulSoup(response.text, "html.parser")
            except requests.RequestException as error:
                wait = 5 * 2 ** attempt
                print(f"  retry in {wait}s ({error})", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"giving up on {url}")


def push_score(label: str) -> int:
    if label == "爆":
        return 100
    if label.startswith("XX"):
        return -100
    if label.startswith("X") and label[1:].isdigit():
        return -10 * int(label[1:])
    return int(label) if label.isdigit() else 0


def parse_index(soup: BeautifulSoup) -> list[dict]:
    posts = []
    for div in soup.select("div.r-list-container > div"):
        classes = div.get("class", [])
        if "r-list-sep" in classes:          # pinned announcements follow the separator
            break
        if "r-ent" not in classes:
            continue
        link = div.select_one("div.title a")
        if link is None:                     # deleted post
            continue
        match = re.search(r"(M\.(\d+)\.A\.\w+)\.html", link["href"])
        if not match:
            continue
        title = link.text.strip()
        category = re.match(r"^(?:Re:\s*|Fw:\s*)?\[([^\]]+)\]", title)
        label = div.select_one("div.nrec").text.strip()
        posts.append({
            "post_id": match.group(1),
            "posted_at": datetime.fromtimestamp(int(match.group(2)), timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "category": category.group(1) if category else None,
            "title": title,
            "author": div.select_one("div.author").text.strip(),
            "push_label": label,
            "push_score": push_score(label),
            "url": "https://www.ptt.cc" + link["href"],
        })
    return posts


def parse_comments(soup: BeautifulSoup, post_id: str, posted_at: str) -> list[dict]:
    post_time = datetime.strptime(posted_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TAIPEI)
    comments = []
    for seq, div in enumerate(soup.select("div.push")):
        stamp = re.search(r"(\d{2})/(\d{2})\s+(\d{2}):(\d{2})", div.select_one(".push-ipdatetime").text if div.select_one(".push-ipdatetime") else "")
        when = None
        if stamp:
            month, day, hour, minute = map(int, stamp.groups())
            year = post_time.year + (1 if month < post_time.month - 6 else 0)
            local = datetime(year, month, day, hour, minute, tzinfo=TAIPEI)
            when = local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        comments.append({
            "post_id": post_id, "seq": seq, "commented_at": when,
            "tag": (div.select_one(".push-tag").text.strip() if div.select_one(".push-tag") else None),
            "user": (div.select_one(".push-userid").text.strip() if div.select_one(".push-userid") else None),
            "text": (div.select_one(".push-content").text.lstrip(": ").strip() if div.select_one(".push-content") else ""),
        })
    return comments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="2026-08-21", help="Asia/Taipei date to backfill from")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = parser.parse_args()
    since_utc = datetime.fromisoformat(args.since).replace(tzinfo=TAIPEI).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    crawler = Crawler(args.delay)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    first = crawler.get(f"{BOARD_URL}/index.html")
    previous = [a["href"] for a in first.select("div.btn-group-paging a") if "上頁" in a.text][0]
    page = int(re.search(r"index(\d+)", previous).group(1)) + 1
    total = 0
    while page > 0:
        posts = parse_index(crawler.get(f"{BOARD_URL}/index{page}.html"))
        db.executemany(
            "INSERT INTO ptt_posts VALUES (:post_id, :posted_at, :category, :title, :author, :push_label, :push_score, :url, :fetched_at) "
            "ON CONFLICT (post_id) DO UPDATE SET push_label = excluded.push_label, push_score = excluded.push_score, fetched_at = excluded.fetched_at",
            [{**post, "fetched_at": now} for post in posts],
        )
        db.commit()
        total += len(posts)
        if page % 20 == 0:
            print(f"index page {page}: {total} posts so far, oldest {posts[0]['posted_at'] if posts else '-'} UTC", flush=True)
        if posts and all(post["posted_at"] < since_utc for post in posts):
            break
        page -= 1

    chats = db.execute(
        "SELECT post_id, posted_at, url, title FROM ptt_posts WHERE posted_at >= ? ORDER BY posted_at", (since_utc,)
    ).fetchall()
    chats = [row for row in chats if CHAT.search(row[3]) and not row[3].startswith(("Re:", "Fw:"))]
    done = {row[0] for row in db.execute("SELECT DISTINCT post_id FROM ptt_comments")}
    print(f"{total} posts read; {len(chats)} daily chat threads, {len(done)} already have comments", flush=True)
    for index, (post_id, posted_at, url, title) in enumerate(chats, 1):
        if post_id in done:
            continue
        try:
            comments = parse_comments(crawler.get(url), post_id, posted_at)
        except FileNotFoundError:
            print(f"  gone: {title}")
            continue
        db.executemany("INSERT OR REPLACE INTO ptt_comments VALUES (:post_id, :seq, :commented_at, :tag, :user, :text)", comments)
        db.commit()
        print(f"[{index}/{len(chats)}] {title[:40]} — {len(comments)} comments", flush=True)
    counts = db.execute("SELECT (SELECT COUNT(*) FROM ptt_posts), (SELECT COUNT(*) FROM ptt_comments)").fetchone()
    print(f"done: ptt_posts {counts[0]}, ptt_comments {counts[1]} -> {DB}")


if __name__ == "__main__":
    main()
