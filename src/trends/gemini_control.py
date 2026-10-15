"""Shared Gemini request control: one rate limiter, retry policy, run stats and resume state.

Every Gemini call in the project goes through ``GeminiLimiter.call`` (via
``keyword_classification._generate_json``), so entity resolution, keyword classification,
automatic classification and news sentiment all draw from the same per-minute budget.

Policy (free tier: 5 requests per minute per model):
- at most ``max_requests`` calls in any rolling ``window_seconds``;
- HTTP 429: wait the server's ``retryDelay`` (at least 60 s when absent) and retry, up to
  ``max_rate_limit_retries`` times; a per-day quota stops immediately;
- HTTP 5xx: exponential backoff (5 s, 10 s, 20 s), up to ``max_server_retries`` times;
- once the quota is exhausted, retries run out or the run's wait budget is spent, the limiter
  is *stopped*: every further call raises ``GeminiStopped`` without touching the API, so the
  remaining Gemini steps end quickly and the rest of the ETL still runs.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from sqlalchemy.engine import Engine


T = TypeVar("T")

MAX_REQUESTS_PER_MINUTE = 5
DEFAULT_RATE_LIMIT_WAIT = 60.0
SERVER_ERROR_BACKOFF = (5.0, 10.0, 20.0)
DEFAULT_WAIT_BUDGET = 30 * 60.0
_RETRY_DELAY_PATTERN = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")


class GeminiStopped(RuntimeError):
    """Raised when Gemini calls are paused for the rest of this run; pending items resume next run."""


def _status_code(error: Exception) -> int | None:
    # google-genai APIError exposes ``code``; other clients use ``status_code``.
    for attribute in ("code", "status_code"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value
    return None


def is_daily_quota_error(error: Exception) -> bool:
    message = str(error).casefold()
    return "quota exceeded" in message and ("perday" in message or "per day" in message)


def retry_delay_seconds(error: Exception) -> float | None:
    """Read google.rpc.RetryInfo.retryDelay (e.g. '29s') from a 429 error, if present."""
    details = getattr(error, "details", None)
    stack = [details]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            delay = item.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                try:
                    return float(delay[:-1])
                except ValueError:
                    pass
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    match = _RETRY_DELAY_PATTERN.search(str(error))
    return float(match.group(1)) if match else None


@dataclass
class GeminiStats:
    requests: int = 0
    processed: dict[str, int] = field(default_factory=dict)
    rate_limited: int = 0
    server_errors: int = 0
    wait_seconds: float = 0.0
    stopped_reason: str | None = None

    def record_processed(self, task: str, items: int) -> None:
        self.processed[task] = self.processed.get(task, 0) + max(items, 0)

    def report(self, skipped: dict[str, int] | None = None) -> str:
        processed_total = sum(self.processed.values())
        processed_detail = ", ".join(f"{task} {count}" for task, count in sorted(self.processed.items()))
        skipped = skipped or {}
        skipped_detail = ", ".join(f"{name} {count}" for name, count in skipped.items())
        lines = [
            "Gemini Stats",
            f"Requests: {self.requests}",
            f"Processed: {processed_total}" + (f" ({processed_detail})" if processed_detail else ""),
            f"Skipped: {sum(skipped.values())}" + (f" ({skipped_detail})" if skipped_detail else ""),
            f"429 Count: {self.rate_limited}",
            f"503 Count: {self.server_errors}",
            f"Wait Time: {round(self.wait_seconds)} seconds",
            f"Stopped: {self.stopped_reason or 'no'}",
        ]
        return "\n".join(lines)


class GeminiLimiter:
    def __init__(
        self,
        max_requests: int = MAX_REQUESTS_PER_MINUTE,
        window_seconds: float = 60.0,
        max_rate_limit_retries: int = 3,
        server_backoff: tuple[float, ...] = SERVER_ERROR_BACKOFF,
        wait_budget_seconds: float = DEFAULT_WAIT_BUDGET,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_rate_limit_retries = max_rate_limit_retries
        self.server_backoff = server_backoff
        self.wait_budget_seconds = wait_budget_seconds
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self.stats = GeminiStats()

    @property
    def stopped(self) -> bool:
        return self.stats.stopped_reason is not None

    def _stop(self, reason: str) -> GeminiStopped:
        if self.stats.stopped_reason is None:
            self.stats.stopped_reason = reason
            print(f"[GEMINI] stopped for this run: {reason}", flush=True)
        return GeminiStopped(reason)

    def _wait(self, seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        if self.stats.wait_seconds + seconds > self.wait_budget_seconds:
            raise self._stop(f"wait budget of {round(self.wait_budget_seconds)}s exhausted")
        print(f"[GEMINI] waiting {seconds:.0f}s ({reason})", flush=True)
        self._sleep(seconds)
        self.stats.wait_seconds += seconds

    def _acquire(self) -> None:
        now = self._clock()
        while self._sent and now - self._sent[0] >= self.window_seconds:
            self._sent.popleft()
        if len(self._sent) >= self.max_requests:
            self._wait(self.window_seconds - (now - self._sent[0]), f"{self.max_requests} requests per minute")
            now = self._clock()
            while self._sent and now - self._sent[0] >= self.window_seconds:
                self._sent.popleft()
        self._sent.append(self._clock())
        self.stats.requests += 1

    def call(self, request: Callable[[], T]) -> T:
        if self.stopped:
            raise GeminiStopped(self.stats.stopped_reason)
        rate_limit_retries = 0
        server_retries = 0
        while True:
            self._acquire()
            try:
                return request()
            except Exception as error:
                status = _status_code(error)
                if status == 429:
                    self.stats.rate_limited += 1
                    if is_daily_quota_error(error):
                        raise self._stop("daily quota exhausted") from error
                    if rate_limit_retries >= self.max_rate_limit_retries:
                        raise self._stop("rate limited after retries") from error
                    rate_limit_retries += 1
                    delay = retry_delay_seconds(error)
                    self._wait(delay if delay is not None else DEFAULT_RATE_LIMIT_WAIT, "HTTP 429")
                elif status is not None and 500 <= status < 600:
                    self.stats.server_errors += 1
                    if server_retries >= len(self.server_backoff):
                        raise self._stop(f"HTTP {status} after retries") from error
                    self._wait(self.server_backoff[server_retries], f"HTTP {status}")
                    server_retries += 1
                else:
                    raise


_limiter = GeminiLimiter()


def get_gemini_limiter() -> GeminiLimiter:
    return _limiter


def reset_gemini_limiter(**kwargs) -> GeminiLimiter:
    """Start a fresh run: new stats, empty request window. Keyword arguments configure the limiter."""
    global _limiter
    _limiter = GeminiLimiter(**kwargs)
    return _limiter


def should_stop_batches(error: Exception) -> bool:
    """True when a batch loop should end now and leave the remaining items for the next run."""
    return isinstance(error, GeminiStopped) or is_daily_quota_error(error)


class GeminiProgress:
    """Per-database record of items Gemini already answered, so a step never re-sends them.

    Stored next to the SQLite file (``data.db`` -> ``data.gemini_progress.json``) because some
    steps keep an answered item in their pending set (e.g. a keyword Gemini classifies as
    「其他」), which would otherwise be re-sent on every run. In-memory databases keep no state.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._tasks: dict[str, dict[str, str]] = {}
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._tasks = {
                        str(task): {str(key): str(value) for key, value in items.items()}
                        for task, items in loaded.items() if isinstance(items, dict)
                    }
            except (OSError, ValueError):
                print(f"[GEMINI] ignoring unreadable progress file {path}", flush=True)

    @classmethod
    def for_engine(cls, engine: Engine) -> GeminiProgress:
        database = engine.url.database
        if not database or database == ":memory:":
            return cls(None)
        db_path = Path(database)
        return cls(db_path.with_name(f"{db_path.stem}.gemini_progress.json"))

    def done(self, task: str) -> set[str]:
        return set(self._tasks.get(task, {}))

    def mark(self, task: str, items: Iterable[object]) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        bucket = self._tasks.setdefault(task, {})
        for item in items:
            bucket[str(item)] = stamp
        if self.path is None:
            return
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._tasks, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)
