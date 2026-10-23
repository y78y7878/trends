"""Shared Gemini request control: one rate limiter, retry policy, run stats and resume state.

Every Gemini call in the project goes through ``GeminiLimiter.call`` (via
``keyword_classification._generate_json``), so entity resolution, keyword classification,
automatic classification and news sentiment all draw from the same per-minute budget.

Policy (free tier: 5 requests per minute and a small daily quota, both per model):
- each request tries the models in order (``gemini_models()``); a model that runs out of daily
  quota, keeps failing with 5xx, stays rate limited or does not exist is skipped for the rest of
  the run and the same request moves on to the next model;
- at most ``max_requests`` calls per model in any rolling ``window_seconds``;
- HTTP 429: wait the server's ``retryDelay`` (at least 60 s when absent) and retry, up to
  ``max_rate_limit_retries`` times; a per-day quota gives up on the model immediately;
- HTTP 5xx: one retry after 10 s, then the next model. Failed requests still count against the
  daily quota, so retrying an overloaded model mostly burns quota;
- once every model is out, or the run's wait budget is spent, the limiter is *stopped*: every
  further call raises ``GeminiStopped`` without touching the API, so the remaining Gemini steps
  end quickly and the rest of the ETL still runs.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence, TypeVar

from sqlalchemy.engine import Engine


T = TypeVar("T")

# Primary model first; each fallback has its own free-tier quota (20 requests per day each, checked
# 2026-10-23). Not every name is a separate model: gemini-3.5-flash draws on gemini-3.6-flash's quota
# and gemini-3.7-flash is served by gemini-3.8-flash, so neither is listed. Override with
# GEMINI_MODELS in .env (comma separated).
DEFAULT_MODELS = ("gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")
MAX_REQUESTS_PER_MINUTE = 5
DEFAULT_RATE_LIMIT_WAIT = 60.0
SERVER_ERROR_BACKOFF = (10.0,)
DEFAULT_WAIT_BUDGET = 30 * 60.0
_RETRY_DELAY_PATTERN = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")


class GeminiStopped(RuntimeError):
    """Raised when Gemini calls are paused for the rest of this run; pending items resume next run."""


class _ModelUnavailable(Exception):
    """One model is done for this run; the request moves on to the next model."""


def gemini_models() -> tuple[str, ...]:
    configured = tuple(name.strip() for name in os.getenv("GEMINI_MODELS", "").split(",") if name.strip())
    return configured or DEFAULT_MODELS


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
    model_requests: Counter = field(default_factory=Counter)
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
            *(["Models: " + ", ".join(f"{name} {count}" for name, count in self.model_requests.items())]
              if self.model_requests else []),
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
        self._sent: dict[str, deque[float]] = {}
        self.unavailable: dict[str, str] = {}
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

    def _acquire(self, model: str) -> None:
        sent = self._sent.setdefault(model, deque())
        now = self._clock()
        while sent and now - sent[0] >= self.window_seconds:
            sent.popleft()
        if len(sent) >= self.max_requests:
            self._wait(self.window_seconds - (now - sent[0]), f"{self.max_requests} requests per minute")
            now = self._clock()
            while sent and now - sent[0] >= self.window_seconds:
                sent.popleft()
        sent.append(self._clock())
        self.stats.requests += 1
        self.stats.model_requests[model] += 1

    def call(self, request: Callable[[str], T], models: Sequence[str] | None = None) -> T:
        """Send ``request(model)``, falling back through ``models`` (default ``gemini_models()``)."""
        if self.stopped:
            raise GeminiStopped(self.stats.stopped_reason)
        for model in models or gemini_models():
            if model in self.unavailable:
                continue
            try:
                return self._call_model(request, model)
            except _ModelUnavailable as unavailable:
                self.unavailable[model] = str(unavailable)
                print(f"[GEMINI] {model} unavailable for this run: {unavailable}", flush=True)
        reasons = "; ".join(f"{model} {reason}" for model, reason in self.unavailable.items())
        raise self._stop(f"all models unavailable ({reasons})")

    def _call_model(self, request: Callable[[str], T], model: str) -> T:
        rate_limit_retries = 0
        server_retries = 0
        while True:
            self._acquire(model)
            try:
                return request(model)
            except Exception as error:
                status = _status_code(error)
                if status == 429:
                    self.stats.rate_limited += 1
                    if is_daily_quota_error(error):
                        raise _ModelUnavailable("daily quota exhausted") from error
                    if rate_limit_retries >= self.max_rate_limit_retries:
                        raise _ModelUnavailable("rate limited after retries") from error
                    rate_limit_retries += 1
                    delay = retry_delay_seconds(error)
                    self._wait(delay if delay is not None else DEFAULT_RATE_LIMIT_WAIT, "HTTP 429")
                elif status is not None and 500 <= status < 600:
                    self.stats.server_errors += 1
                    if server_retries >= len(self.server_backoff):
                        raise _ModelUnavailable(f"HTTP {status} after retries") from error
                    self._wait(self.server_backoff[server_retries], f"HTTP {status}")
                    server_retries += 1
                elif status == 404:
                    raise _ModelUnavailable("model not found (HTTP 404)") from error
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
